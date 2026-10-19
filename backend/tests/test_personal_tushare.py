import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app.services import personal_tushare as ts


def response(rows=None):
    return {"code": 0, "data": {"fields": ["ts_code", "trade_date", "close"],
                                "items": [["510050.SH", "20261016", 3.0]] if rows is None else rows}}


def test_token_env_and_file_without_sdk_writes(tmp_path, monkeypatch):
    for key in ts.ENV_NAMES:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValueError, match="凭证"):
        ts.get_token()
    path = tmp_path / "token.txt"
    path.write_text('# local\ntushare_token="file-secret"\n')
    monkeypatch.setenv("TUSHARE_TOKEN_FILE", str(path))
    assert ts.get_token() == "file-secret"
    monkeypatch.setenv("tushare_token", "shell-secret")
    assert ts.get_token() == "shell-secret"
    assert not (tmp_path / "tk.csv").exists()


def test_reference_endpoint_and_rejected_destinations(monkeypatch):
    monkeypatch.delenv("TUSHARE_API_URL", raising=False)
    monkeypatch.delenv("tushare_api_url", raising=False)
    assert ts.get_endpoint() == "https://fast.xiaodefa.cn"
    for value in ("http://fast.xiaodefa.cn", "https://fast.xiaodefa.cn@evil.example", "https://evil.example"):
        monkeypatch.setenv("TUSHARE_API_URL", value)
        with pytest.raises(ValueError):
            ts.get_endpoint()


def test_limit_spacing_and_invalid_budget():
    now, waits = [0.0], []
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    limiter = ts.RateLimiter(30, sleep=sleep, clock=lambda: now[0])
    for _ in range(4):
        limiter.wait()
    assert waits == [2.0, 2.0, 2.0]
    with pytest.raises(ValueError):
        ts.RateLimiter(121)


def client_for(handler):
    client = ts.TushareClient("private-token", http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.limiter = ts.RateLimiter(120, sleep=lambda _: None)
    return client


def test_permissions_never_retry_or_leak_response(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 2002, "msg": "权限 private-token", "data": None})
    client = client_for(handler)
    with pytest.raises(ts.FetchError) as error:
        client.query("fund_daily", {}, "ts_code")
    assert len(calls) == 1 and error.value.kind == "permission"
    assert "private-token" not in str(error.value)


def test_transient_retry_bounded_and_redirect_rejected(monkeypatch):
    calls = []
    monkeypatch.setattr(ts.time, "sleep", lambda _: None)
    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="private-token")
    with pytest.raises(ts.FetchError, match="transient"):
        client_for(handler).query("fund_daily", {}, "ts_code")
    assert len(calls) == 3
    with pytest.raises(ts.FetchError, match="protocol"):
        client_for(lambda _: httpx.Response(302, headers={"Location": "https://evil.example"})).query("fund_daily", {}, "ts_code")


def test_persist_missing_request_then_offline_cache_hit(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        assert json.loads(request.content)["token"] == "private-token"
        return httpx.Response(200, json=response())
    client = client_for(handler)
    args = (tmp_path, "etf", "fund_daily", {"ts_code": "510050.SH", "trade_date": "20261016"}, "ts_code,trade_date,close")
    path, receipt, hit = ts.persist_request(*args, client=client)
    assert not hit and receipt["rows"] == 1 and not receipt["ready_for_backtest"]
    monkeypatch.setattr(ts, "get_token", lambda: pytest.fail("cached read must not require a token"))
    p2, r2, hit = ts.persist_request(*args)
    assert hit and p2 == path and r2 == receipt and len(calls) == 1
    assert "private-token" not in path.with_name("receipt.json").read_text()
    path.write_bytes(b"broken")
    with pytest.raises(ValueError, match="缓存损坏"):
        ts.persist_request(*args, client=client)
    assert len(calls) == 1


@pytest.mark.parametrize("rows", [[["000001.SZ", "20261016", 3.0]],
                                   [["510050.SH", "20261015", 3.0]],
                                   [["510050.SH", "20261016", 3.0]] * 2,
                                   [["510050.SH", "20261016", 3.0], ["510050.SH", "20261016", 4.0]]])
def test_wrong_scope_and_duplicate_rows_not_cached(tmp_path, rows):
    client = client_for(lambda _: httpx.Response(200, json=response(rows)))
    with pytest.raises(ValueError):
        ts.persist_request(tmp_path, "etf", "fund_daily", {"ts_code": "510050.SH", "trade_date": "20261016"}, "ts_code,trade_date,close", client=client)
    assert not list(tmp_path.rglob("receipt.json"))


def test_empty_and_saturated_responses_are_not_success(tmp_path, monkeypatch):
    client = client_for(lambda _: httpx.Response(200, json=response([])))
    _, receipt, _ = ts.persist_request(tmp_path, "etf", "fund_daily", {}, "ts_code,trade_date,close", client=client)
    assert receipt["status"] == "empty_unverified"
    monkeypatch.setitem(ts.DATASETS, "fund_daily", ("etf", 1))
    client = client_for(lambda _: httpx.Response(200, json=response()))
    with pytest.raises(ValueError, match="截断"):
        ts.persist_request(tmp_path / "full", "etf", "fund_daily", {}, "ts_code,trade_date,close", client=client)
    assert not list((tmp_path / "full").rglob("receipt.json"))


def test_asset_secret_params_and_symlinks_rejected(tmp_path):
    client = client_for(lambda _: pytest.fail("must not access network"))
    for asset, params in (("stock", {}), ("etf", {"token": "secret"})):
        with pytest.raises(ValueError):
            ts.persist_request(tmp_path, asset, "fund_daily", params, "ts_code", client=client)
    link = tmp_path / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="软链接"):
        ts.persist_request(link, "etf", "fund_daily", {}, "ts_code", client=client)


def test_local_financial_pagination_and_repeat_guard():
    calls = []
    class Client:
        def query(self, api, params, fields):
            calls.append(params)
            return {"code": 0, "data": {"fields": ["ts_code", "end_date"], "items": [["A", "20260331"], ["B", "20260331"]] if params["offset"] == 0 else []}}
    frame = ts.fetch_period_pages(Client(), "20260331", ("ts_code", "end_date"), page_size=2)
    assert len(frame) == 2 and [p["offset"] for p in calls] == [0, 2]
    class Repeated:
        def query(self, api, params, fields):
            return {"code": 0, "data": {"fields": ["ts_code", "end_date"], "items": [["A", "20260331"]]}}
    with pytest.raises(ValueError, match="分页重复"):
        ts.fetch_period_pages(Repeated(), "20260331", ("ts_code", "end_date"), page_size=1)
    assert ts.quarter_end_periods("20251231", "20260630") == ["20251231", "20260331", "20260630"]
    with pytest.raises(ValueError):
        ts.quarter_end_periods("20260101", "20260630")


def test_cli_real_persistence_contract(tmp_path, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts/fetch_tushare_data.py"
    spec = importlib.util.spec_from_file_location("fetch_tushare_data", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    client = client_for(lambda _: httpx.Response(200, json=response()))
    original = ts.persist_request
    monkeypatch.setattr(cli, "persist_request", lambda *a: original(*a, client=client))
    monkeypatch.setattr("sys.argv", [str(path), "--cache-dir", str(tmp_path), "--asset", "etf", "--api", "fund_daily", "--params", '{"ts_code":"510050.SH","trade_date":"20261016"}', "--fields", "ts_code,trade_date,close"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["cache_hit"] is False
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["cache_hit"] is True


def test_financial_script_no_longer_needs_collector_checkout(tmp_path, monkeypatch):
    import pandas as pd
    path = Path(__file__).resolve().parents[2] / "scripts/fetch_size_financials.py"
    spec = importlib.util.spec_from_file_location("standalone_financial_fetch", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    class Client:
        endpoint = ts.DEFAULT_ENDPOINT
        def __init__(self, **kwargs):
            pass
    monkeypatch.setattr(ts, "TushareClient", Client)
    monkeypatch.setattr(ts, "fetch_period_pages", lambda *a, **kw: pd.DataFrame(
        [("000001.SZ", "20260420", "20260331", 12., "0")], columns=script.FIELDS))
    monkeypatch.setattr("sys.argv", [str(path), "--output", str(tmp_path), "--start", "20260331", "--end", "20260331"])
    script.main()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["rows"] == 1 and manifest["source"] == ts.DEFAULT_ENDPOINT
    assert (tmp_path / "periods/20260331.parquet").is_file()
    assert pd.read_parquet(tmp_path / "fina_indicator.parquet").profit_dedt.tolist() == [12.]


def test_etf_legacy_sync_uses_shared_client_and_persists_pair(tmp_path, monkeypatch):
    import polars as pl

    from app.services import personal_local_assets as assets
    class Provider:
        def get_instruments(self, asset):
            assert asset == "etf"
            return [{"symbol": "510050.SH"}]
    monkeypatch.setattr(assets, "LocalParquetProvider", Provider)
    monkeypatch.setattr(assets, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(assets, "source_path", lambda: tmp_path / "daily.parquet")
    pl.DataFrame({"trade_date": ["20261016"]}).write_parquet(tmp_path / "daily_qfq.parquet")
    for api, values in (("fund_daily", {"open": [3.], "high": [3.], "low": [3.], "close": [3.]}), ("fund_adj", {"adj_factor": [1.]})):
        folder = tmp_path / "cache" / api
        folder.mkdir(parents=True)
        pl.DataFrame({"ts_code": ["510050.SH"], "trade_date": ["20261015"], **values}).write_parquet(folder / "510050.SH.parquet")
    calls = []
    class Client:
        def query(self, api, params, fields):
            calls.append(api)
            columns = fields.split(",")
            values = {c: 3. for c in columns}
            values.update(ts_code="510050.SH", trade_date="20261016", adj_factor=1.)
            return {"code": 0, "data": {"fields": columns, "items": [[values[c] for c in columns]]}}
    monkeypatch.setattr(ts, "TushareClient", Client)
    assert assets.sync_etfs()["updated_symbols"] == 1
    for api in ("fund_daily", "fund_adj"):
        assert pl.read_parquet(tmp_path / "cache" / api / "510050.SH.parquet")["trade_date"].to_list() == ["20261015", "20261016"]
    assert assets.sync_etfs()["updated_symbols"] == 0
    assert calls == ["fund_daily", "fund_adj"]


def test_value_momentum_collector_runs_without_external_checkout(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "scripts/fetch_value_momentum.py"
    spec = importlib.util.spec_from_file_location("standalone_value_fetch", path)
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    class Client:
        endpoint = ts.DEFAULT_ENDPOINT
        def __init__(self, **kwargs):
            pass
        def query(self, api, params, fields):
            assert api in ("fina_indicator_vip", "index_daily")
            row = {"ts_code": "000001.SZ", "ann_date": "20260420", "end_date": "20260331", "roe": 10., "netprofit_margin": 20.,
                   "trade_date": params.get("start_date"), "open": 3., "close": 4.}
            columns = fields.split(",")
            return {"code": 0, "data": {"fields": columns, "items": [[row[c] for c in columns]]}}
    monkeypatch.setattr(ts, "TushareClient", Client)
    monkeypatch.setattr(ts, "quarter_end_periods", lambda *a: ["20260331"])
    monkeypatch.setattr("sys.argv", [str(path), "--output", str(tmp_path)])
    script.main()
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["financial_rows"] == 1 and manifest["index_rows"] == 5
    assert (tmp_path / "metrics.parquet").is_file() and (tmp_path / "index.parquet").is_file()
