import json
from pathlib import Path

import httpx
import pytest

from app.services.etf_research.fetch import (
    APIS,
    FetchError,
    TushareClient,
    make_plan,
    run_fetch,
    spec,
)

S = "510050.SH"


def payload(request, dates=("20250102", "20250103")):
    fields = APIS[request["api"]]["fields"].split(",")
    rows = []
    for d in dates:
        row = {k: 1.0 for k in fields}
        row.update(ts_code=request["symbol"], trade_date=d, cal_date=d, exchange=request["symbol"] if request["api"] == "trade_cal" else "SH", is_open=1,
                   list_date="20050223", list_status="L", etf_type="境内")
        rows.append([row[k] for k in fields])
    return {"code": 0, "msg": None, "data": {"fields": fields, "items": rows}}


def plan(*requests):
    return {"schema_version": 1, "asset_type": "etf", "phase": "probe", "audit_sha256": "a" * 64,
            "symbols": [S], "requests": list(requests)}


class Fake:
    def __init__(self, values):
        self.values, self.calls = iter(values), []

    def request(self, request):
        self.calls.append(request)
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


def fetch(tmp_path, p, client, **kwargs):
    return run_fetch(tmp_path, "sample", p, client, sleep=lambda _: None, **kwargs)


def test_success_read_only_resume_and_hash_tampering(tmp_path):
    request = spec("fund_daily", S, "2025-01-02", "2025-01-03")
    p = plan(request)
    client = Fake([payload(request)])
    result = fetch(tmp_path, p, client)
    assert result["status"] == "staged_complete" and not result["ready_for_backtest"]
    assert len(client.calls) == 1
    assert fetch(tmp_path, p, Fake([]))["status"] == "staged_complete"
    stage = tmp_path / "etf_research/staging/sample"
    raw = next((stage / "raw").glob("*.json"))
    raw.write_text("tampered")
    with pytest.raises(ValueError, match="哈希"):
        fetch(tmp_path, p, Fake([]))
    assert not (tmp_path / "source_cache").exists()


def test_permission_not_retried_other_api_continues(tmp_path):
    a = spec("fund_daily", S, "2025-01-02", "2025-01-03")
    b = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    client = Fake([FetchError("permission"), payload(b)])
    r = fetch(tmp_path, plan(a, b), client)
    assert r["status"] == "blocked" and len(client.calls) == 2
    assert {x["status"] for x in r["records"].values()} == {"blocked", "fetched"}


def test_timeout_rate_limit_retry_is_bounded(tmp_path):
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    client = Fake([FetchError("transient"), FetchError("rate_limit"), payload(a)])
    assert fetch(tmp_path, plan(a), client)["status"] == "staged_complete"
    assert len(client.calls) == 3


def test_empty_is_unknown_not_success(tmp_path):
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    r = fetch(tmp_path, plan(a), Fake([payload(a, ())]))
    assert r["status"] == "needs_review"
    assert next(iter(r["records"].values()))["status"] == "empty_unverified"


def test_full_page_splits_and_resumes_without_parent_refetch(tmp_path, monkeypatch):
    monkeypatch.setitem(APIS["fund_adj"], "cap", 2)
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    r = fetch(tmp_path, plan(a), Fake([payload(a)]), max_calls=1)
    assert r["status"] == "partial"
    client = Fake([payload(a, ("20250102",)), payload(a, ("20250103",))])
    r = fetch(tmp_path, plan(a), client)
    assert r["status"] == "staged_complete" and len(client.calls) == 2
    assert all(c["start"] == c["end"] for c in client.calls)


def test_repeated_or_out_of_window_rows_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setitem(APIS["fund_adj"], "cap", 2)
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    client = Fake([payload(a), payload(a), payload(a)])
    r = fetch(tmp_path, plan(a), client)
    assert r["status"] == "failed" and len(client.calls) == 3
    assert all(x["status"] != "fetched" for x in r["records"].values())


def test_keyboard_interrupt_releases_lock_and_keeps_valid_chunk(tmp_path):
    a = spec("fund_daily", S, "2025-01-02", "2025-01-03")
    b = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    p = plan(a, b)
    with pytest.raises(KeyboardInterrupt):
        fetch(tmp_path, p, Fake([payload(a), KeyboardInterrupt()]))
    client = Fake([payload(b)])
    assert fetch(tmp_path, p, client)["status"] == "staged_complete"
    assert len(client.calls) == 1


def test_official_endpoint_and_token_redaction():
    for url in ("http://api.tushare.pro", "https://example.com", "https://api.tushare.pro@evil.example", "https://api.tushare.pro/path", "http://fast.xiaodefa.cn", "https://fast.xiaodefa.cn.evil.example", "https://fast.xiaodefa.cn?token=secret"):
        with pytest.raises(ValueError):
            TushareClient("secret", endpoint=url)
    messages = [
        {"code": 2002, "msg": "权限不足 secret", "data": None},
        {"code": 2002, "msg": "每分钟访问次数超限 secret", "data": None},
    ]
    for value, kind in zip(messages, ("permission", "rate_limit"), strict=True):
        def respond(request, value=value):
            assert json.loads(request.content)["token"] == "secret"
            return httpx.Response(200, json=value)
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            client = TushareClient("secret", http=http)
            with pytest.raises(FetchError) as error:
                client.request(spec("fund_adj", S, "2025-01-02", "2025-01-03"))
            assert error.value.kind == kind and "secret" not in str(error.value)
            assert "secret" not in repr(client)


def test_paths_plan_mutation_and_wrong_asset_rejected(tmp_path):
    a = spec("fund_daily", S, "2025-01-02", "2025-01-03")
    fetch(tmp_path, plan(a), Fake([payload(a)]))
    with pytest.raises(ValueError, match="计划"):
        fetch(tmp_path, plan(spec("fund_adj", S, a["start"], a["end"])), Fake([]))
    for ident in ("../old", "/old", "", "a/b"):
        with pytest.raises(ValueError):
            run_fetch(tmp_path, ident, plan(a), Fake([]))
    p = plan(a)
    p["asset_type"] = "stock"
    with pytest.raises(ValueError):
        fetch(tmp_path, p, Fake([]))


def test_make_plan_preserves_u0_and_requires_probe_before_bulk():
    from app.services.etf_research.overview import U0
    report = {"schema_version": 1, "asset_type": "etf", "module": "etf_research", "start": "2005-01-04", "end": "2026-10-16",
              "universes": {"U0": {"members": [{"symbol": s, "asset_type": "etf"} for s in U0]}},
              "calendar": {"unknown_ranges": [{"start": "2005-01-04", "end": "2023-01-02", "days": 6573}]}, "gaps": []}
    p = make_plan(report, "a" * 64, "probe")
    assert p["symbols"] == list(U0) and {r["api"] for r in p["requests"]} >= {"fund_daily", "fund_adj", "trade_cal", "etf_basic"}
    q = make_plan(report, "a" * 64, "gaps")
    assert all((__import__("datetime").date.fromisoformat(r["end"]) - __import__("datetime").date.fromisoformat(r["start"])).days < 366 for r in q["requests"] if r["api"] == "trade_cal")
    assert sum(r["api"] == "etf_basic" for r in q["requests"]) == 6


def test_bulk_refuses_missing_probe_before_writing(tmp_path):
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    p = plan(a)
    p["phase"] = "gaps"
    with pytest.raises(ValueError, match="先完成"):
        fetch(tmp_path, p, Fake([]))
    assert not (tmp_path / "etf_research").exists()


@pytest.mark.parametrize("http_status,kind", [(403, "permission"), (429, "rate_limit"), (503, "transient"), (302, "protocol")])
def test_http_failure_never_leaks_body_or_follows_redirect(http_status, kind):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(http_status, text="secret"))) as http:
        client = TushareClient("secret", http=http)
        with pytest.raises(FetchError) as error:
            client.request(spec("fund_adj", S, "2025-01-02", "2025-01-03"))
        assert error.value.kind == kind and "secret" not in str(error.value)


def test_secret_echo_and_bad_json_refused():
    for data in ({"code": 0, "data": {"fields": ["token"], "items": [["secret"]]}}, []):
        with httpx.Client(transport=httpx.MockTransport(lambda _, data=data: httpx.Response(200, json=data))) as http, pytest.raises(FetchError):
            TushareClient("secret", http=http).request(spec("fund_adj", S, "2025-01-02", "2025-01-03"))


def test_lock_is_scoped_to_staging_and_rejects_second_writer(tmp_path):
    from app.services.mining_process_lock import MiningProcessLock, MiningProcessLockError
    stage = tmp_path / "etf_research/staging/sample"
    lock = MiningProcessLock(stage)
    lock.acquire()
    try:
        with pytest.raises(MiningProcessLockError):
            fetch(tmp_path, plan(spec("fund_adj", S, "2025-01-02", "2025-01-03")), Fake([]))
    finally:
        lock.release()


def test_symlink_staging_and_duplicate_rows_refused(tmp_path):
    stage = tmp_path / "etf_research/staging/sample"
    stage.mkdir(parents=True)
    (stage / "raw").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        fetch(tmp_path, plan(spec("fund_adj", S, "2025-01-02", "2025-01-03")), Fake([]))
    (stage / "raw").unlink()
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    r = fetch(tmp_path, plan(a), Fake([payload(a, ("20250102", "20250102"))]))
    assert r["status"] == "failed"
    assert next(iter(r["records"].values()))["rows"] == 2


def test_missing_fields_saved_for_diagnosis_not_success(tmp_path):
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    value = {"code": 0, "data": {"fields": ["ts_code"], "items": [[S]]}}
    r = fetch(tmp_path, plan(a), Fake([value]))
    assert r["status"] == "failed"
    assert next(iter(r["records"].values()))["sha256"]


def test_permission_stops_same_api_without_retry_loop(tmp_path):
    a = spec("fund_daily", S, "2025-01-02", "2025-01-02")
    b = spec("fund_daily", S, "2025-01-03", "2025-01-03")
    client = Fake([FetchError("permission")])
    assert fetch(tmp_path, plan(a, b), client)["status"] == "blocked"
    assert len(client.calls) == 1


def audit_report():
    from app.services.etf_research.overview import U0
    return {"schema_version": 1, "asset_type": "etf", "module": "etf_research", "start": "2025-01-02", "end": "2025-01-03",
            "universes": {"U0": {"members": [{"symbol": s, "asset_type": "etf"} for s in U0]}},
            "calendar": {"unknown_ranges": []}, "gaps": []}


def successful_probe(root, report=None):
    p = make_plan(report or audit_report(), "a" * 64, "probe")
    values = [payload(r, ("20250102",) if r["api"] in ("etf_basic", "fund_div") else ("20250102", "20250103")) for r in p["requests"]]
    assert run_fetch(root, "probe", p, Fake(values), sleep=lambda _: None)["status"] == "staged_complete"
    return root / "etf_research/staging/probe"


def test_verified_probe_allows_bulk_but_tampered_probe_does_not(tmp_path):
    receipt = successful_probe(tmp_path)
    q = make_plan(audit_report(), "a" * 64, "gaps")
    first = q["requests"][0]
    result = run_fetch(tmp_path, "bulk", q, Fake([payload(first, ("20250102",))]), max_calls=1, sleep=lambda _: None, probe_dir=receipt)
    assert result["status"] == "partial" and result["calls_this_run"] == 1
    path = next((receipt / "raw").glob("*.json"))
    path.write_text("tampered")
    with pytest.raises(ValueError):
        run_fetch(tmp_path, "bulk2", q, Fake([]), probe_dir=receipt)
    assert not (tmp_path / "etf_research/staging/bulk2").exists()


def test_calendar_incomplete_scope_and_single_day_saturation(tmp_path, monkeypatch):
    a = spec("trade_cal", "SSE", "2025-01-02", "2025-01-03")
    assert fetch(tmp_path, plan(a), Fake([payload(a, ("20250102",))]))["status"] == "failed"
    monkeypatch.setitem(APIS["fund_adj"], "cap", 1)
    a = spec("fund_adj", S, "2025-01-02", "2025-01-02")
    r = run_fetch(tmp_path, "saturated", plan(a), Fake([payload(a, ("20250102",))]), sleep=lambda _: None)
    assert r["status"] == "failed"
    assert next(iter(r["records"].values()))["error"] == "saturated_unsplittable"


def test_cli_reads_sealed_audit_and_never_persists_token(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys

    import app.services.etf_research.fetch as service
    from app.services.etf_research.audit import digest

    script = Path(__file__).resolve().parents[2] / "scripts/fetch_etf_data.py"
    module_spec = importlib.util.spec_from_file_location("etf_fetch_cli", script)
    cli = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cli)
    root = tmp_path / "audit"
    root.mkdir()
    (root / "audit.json").write_text(json.dumps(audit_report()))
    (root / "manifest.json").write_text(json.dumps({"status": "audit_only", "files": {"audit.json": digest(root / "audit.json")}}))
    p = make_plan(audit_report(), digest(root / "audit.json"), "probe")
    client = Fake([payload(r, ("20250102",) if r["api"] in ("etf_basic", "fund_div") else ("20250102", "20250103")) for r in p["requests"]])
    monkeypatch.setenv("TUSHARE_TOKEN", "do-not-persist-secret")
    monkeypatch.setattr(cli, "TushareClient", lambda *a, **kw: client)
    monkeypatch.setattr(cli, "run_fetch", lambda *a, **kw: service.run_fetch(*a, **kw, sleep=lambda _: None))
    target = tmp_path / "private"
    monkeypatch.setattr(sys, "argv", [str(script), "--audit", str(root / "audit.json"), "--data-dir", str(target), "--fetch-id", "probe"])
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert "staged_complete" in captured.out and "do-not-persist-secret" not in captured.out + captured.err
    assert all("do-not-persist-secret" not in p.read_text() for p in target.rglob("*") if p.is_file())
    (root / "audit.json").write_text("tampered")
    assert cli.main() == 1 and "ETF采集未完成" in capsys.readouterr().err


def test_unknown_candidate_needs_etf_specific_identity(tmp_path):
    # A declared symbol alone must never turn a stock into a fetchable ETF.
    a = spec("fund_daily", "600000.SH", "2025-01-02", "2025-01-03")
    p = plan(a)
    p["phase"] = "gaps"
    p["symbols"] = [S, "600000.SH"]
    # The mandatory probe gate runs before any connection or staging write.
    with pytest.raises(ValueError):
        run_fetch(tmp_path, "bad", p, Fake([]))
    assert not (tmp_path / "etf_research").exists()


def test_unconfirmed_stock_claim_cannot_fetch_prices_after_probe(tmp_path):
    report = audit_report()
    report["universes"]["U2"] = {"members": [{"symbol": "600000.SH", "asset_type": "etf"}]}
    receipt = successful_probe(tmp_path, report)
    p = make_plan(report, "a" * 64, "gaps")
    p["requests"] = [spec("fund_daily", "600000.SH", "2025-01-02", "2025-01-03")]
    client = Fake([])
    r = run_fetch(tmp_path, "unknown", p, client, probe_dir=receipt, sleep=lambda _: None)
    assert r["status"] == "partial" and client.calls == []


def test_retry_budget_keeps_attempt_history_across_resume(tmp_path):
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    p = plan(a)
    first = fetch(tmp_path, p, Fake([FetchError("transient")]), max_calls=1)
    assert first["status"] == "partial"
    second = fetch(tmp_path, p, Fake([payload(a)]))
    assert second["status"] == "staged_complete"
    attempts = next(iter(second["records"].values()))["attempts"]
    assert [x["status"] for x in attempts] == ["transient", "received"]


def test_corrupt_split_journal_cannot_drop_half_of_date_range(tmp_path, monkeypatch):
    monkeypatch.setitem(APIS["fund_adj"], "cap", 2)
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    p = plan(a)
    fetch(tmp_path, p, Fake([payload(a)]), max_calls=1)
    path = tmp_path / "etf_research/staging/sample/requests.json"
    state = json.loads(path.read_text())
    record = next(iter(state["records"].values()))
    record["children"][1] = record["children"][0]
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="拆分"):
        fetch(tmp_path, p, Fake([payload(a, ("20250102",))]))


@pytest.mark.parametrize("endpoint", ["https://fast.xiaodefa.cn", "https://tt.xiaodefa.cn"])
def test_explicit_tradingsimple_endpoint_and_receipt(tmp_path, endpoint):
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    def respond(request):
        assert str(request.url).rstrip("/") == endpoint
        assert json.loads(request.content)["token"] == "private-token"
        return httpx.Response(200, json=payload(a))
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = TushareClient("private-token", endpoint=endpoint, http=http)
        r = fetch(tmp_path, plan(a), client)
        assert r["source"] == endpoint
        assert "private-token" not in (tmp_path / "etf_research/staging/sample/requests.json").read_text()
        assert fetch(tmp_path, plan(a), client)["calls_this_run"] == 0
    original = (tmp_path / "etf_research/staging/sample/requests.json").read_bytes()
    with pytest.raises(ValueError, match="数据源"):
        fetch(tmp_path, plan(a), Fake([]))
    assert (tmp_path / "etf_research/staging/sample/requests.json").read_bytes() == original


def test_bulk_cannot_use_probe_from_another_endpoint(tmp_path):
    receipt = successful_probe(tmp_path)
    q = make_plan(audit_report(), "a" * 64, "gaps")
    client = Fake([])
    client.endpoint = "https://fast.xiaodefa.cn"
    with pytest.raises(ValueError, match="小请求"):
        run_fetch(tmp_path, "mixed", q, client, probe_dir=receipt)
    assert not (tmp_path / "etf_research/staging/mixed").exists()


def test_interrupted_configured_fetch_retains_source_binding(tmp_path):
    a = spec("fund_adj", S, "2025-01-02", "2025-01-03")
    client = Fake([KeyboardInterrupt()])
    client.endpoint = "https://fast.xiaodefa.cn"
    with pytest.raises(KeyboardInterrupt):
        fetch(tmp_path, plan(a), client)
    journal = tmp_path / "etf_research/staging/sample/requests.json"
    assert json.loads(journal.read_text())["source"] == client.endpoint
    with pytest.raises(ValueError, match="数据源"):
        fetch(tmp_path, plan(a), Fake([]))
    client = Fake([payload(a)])
    client.endpoint = "https://fast.xiaodefa.cn"
    assert fetch(tmp_path, plan(a), client)["status"] == "staged_complete"
