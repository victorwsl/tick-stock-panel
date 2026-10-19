import json
import subprocess
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path

import polars as pl
import pytest

from app.services.etf_research.audit import audit, load_audit, write_report

D = date.fromisoformat
S = "510300.SH"


def run(frames=None, calendar=None, identities=None, **kwargs):
    return audit(
        frames or {},
        calendar if calendar is not None else [{"trade_date": "2025010" + str(i), "is_open": 1} for i in (2, 3, 6, 7, 8)],
        identities if identities is not None else [{"symbol": S, "asset_type": "etf", "listing_date": "2025-01-02"}],
        D("2025-01-02"), D("2025-01-08"), **kwargs,
    )


def item(report):
    return next(x for x in report["items"] if x["symbol"] == S)


def test_leading_internal_trailing_and_entire_gaps():
    r = run({"kline_etf_daily": [{"symbol": S, "date": "2025-01-03"}, {"symbol": S, "date": "2025-01-07"}]})
    gaps = [g for g in r["gaps"] if g["symbol"] == S and g["dataset"] == "kline_etf_daily"]
    assert [(g["start"], g["position"], g["sessions"]) for g in gaps] == [
        ("2025-01-02", "leading", 1), ("2025-01-06", "internal", 1), ("2025-01-08", "trailing", 1)]
    assert item(r)["datasets"]["fund_adj"]["missing_sessions"] == 5
    assert len(r["items"]) == 6 and not r["ready_for_backtest"]
    assert r["universes"]["U1"]["status"] == "not_registered"


def test_closed_unknown_listing_suspension_and_sparse_events():
    cal = [{"trade_date": "2025010" + str(i), "is_open": int(i != 4)} for i in (2, 3, 4, 6, 7, 8)]
    r = run({"adj_factor_etf": [{"symbol": S, "trade_date": "2025-01-06", "ex_factor": 1.02}]}, cal,
            [{"symbol": S, "type": "etf", "listing_date": "2025-01-03", "delist_date": "2025-01-08"}],
            suspensions=[{"symbol": S, "date": "2025-01-06", "asset_type": "etf", "status": "suspended", "evidence": "exchange-notice-1"}])
    assert r["calendar"]["unknown_ranges"] == [{"start": "2025-01-05", "end": "2025-01-05", "days": 1}]
    assert item(r)["excluded_sessions"] == {"not_listed": 1, "terminated": 1, "outside_membership": 0}
    assert item(r)["datasets"]["adj_factor_etf"]["semantics"] == "sparse_events_completeness_unknown"
    assert not any(g["dataset"] == "adj_factor_etf" for g in r["gaps"])
    assert any(g["reason"] == "confirmed_suspension" and g["evidence"] == "exchange-notice-1" for g in r["gaps"])
    assert all(g["reason"] == "unknown" for g in r["gaps"] if g["dataset"] == "fund_adj" and g["symbol"] == S)


def test_unknown_identity_and_lifecycle_not_inferred_from_prices():
    r = run({"kline_etf_daily": [{"symbol": S, "date": "2025-01-07"}]}, identities=[])
    assert item(r)["identity_status"] == "unknown"
    assert item(r)["listing_date"] is None
    assert item(r)["datasets"]["kline_etf_daily"]["missing_sessions"] == 4
    assert all(g["reason"] == "identity_unconfirmed" for g in r["gaps"] if g["symbol"] == S)


def test_duplicates_and_nontrading_rows_are_visible():
    r = run({"kline_etf_daily": [{"symbol": S, "date": "2025-01-03"}] * 2 + [{"symbol": S, "date": "2025-01-04"}]})
    stats = item(r)["datasets"]["kline_etf_daily"]
    assert stats["rows"] == 3 and stats["duplicate_rows"] == 1 and stats["calendar_unconfirmed_rows"] == 1


@pytest.mark.parametrize("kwargs", [
    {"identities": [{"symbol": S, "type": "stock"}]},
    {"registry": {"U2": [{"symbol": "600000.SH", "asset_type": "stock", "effective_from": "2025-01-02", "reason": "test"}]}},
    {"suspensions": [{"symbol": S, "date": "2025-01-03", "status": "suspended", "asset_type": "etf", "evidence": ""}]},
    {"frames": {"fund_adj": [{"ts_code": S, "trade_date": "20250103", "adj_factor": 0}]}},
    {"calendar": [{"trade_date": "20250102", "is_open": 1}, {"trade_date": "20250102", "is_open": 0}]},
    {"identities": [{"symbol": S, "type": "etf"}] * 2},
])
def test_invalid_inputs_fail_closed(kwargs):
    with pytest.raises(ValueError):
        run(**kwargs)


def test_extension_members_are_point_in_time_not_winners():
    extra = {"symbol": "518880.SH", "asset_type": "etf", "effective_from": "2025-01-06", "effective_to": "2025-01-07", "reason": "predeclared gold control"}
    r = run(registry={"U1": [extra]})
    x = next(x for x in r["items"] if x["symbol"] == extra["symbol"])
    assert x["excluded_sessions"]["outside_membership"] == 3
    assert x["datasets"]["fund_adj"]["missing_sessions"] == 2
    assert r == run(registry={"U1": [extra]})



def hashes(root):
    return {str(p.relative_to(root)): sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def local(tmp_path):
    root = tmp_path / "data"
    for table, frame in {
        "instruments_etf": pl.DataFrame({"symbol": [S], "asset_type": ["etf"], "listing_date": ["2025-01-02"]}),
        "kline_etf_daily": pl.DataFrame({"symbol": [S], "date": [D("2025-01-03")]}),
        "adj_factor_etf": pl.DataFrame({"symbol": [S], "trade_date": [D("2025-01-03")], "ex_factor": [1.01]}),
        "source_cache/fund_adj": pl.DataFrame({"ts_code": [S], "trade_date": ["20250103"], "adj_factor": [1.01]}),
    }.items():
        folder = root / table
        folder.mkdir(parents=True)
        frame.write_parquet(folder / "part.parquet")
    cal = tmp_path / "raw" / "trade_cal.parquet"
    cal.parent.mkdir()
    pl.DataFrame({"trade_date": ["20250102", "20250103"], "is_open": [1, 1]}).write_parquet(cal)
    return root, cal


def load(local, **kwargs):
    return load_audit(*local, D("2025-01-02"), D("2025-01-03"), **kwargs)


def test_real_parquet_is_read_only_repeatable_and_output_is_exclusive(local, tmp_path):
    before = hashes(tmp_path)
    report = load(local)
    assert report == load(local) and hashes(tmp_path) == before
    assert len(report["sources"]["files"]) == 5
    assert "kline_etf_enriched" in report["sources"]["missing_tables"]
    output = tmp_path / "out"
    write_report(report, output, data_dir=local[0], protected_files=[local[1]])
    assert json.loads((output / "manifest.json").read_text())["status"] == "audit_only"
    assert all(hashes(tmp_path)[k] == v for k, v in before.items())
    with pytest.raises(FileExistsError):
        write_report(report, output, data_dir=local[0])
    for path in (local[0], local[0] / "kline_etf_daily" / "bad", local[1].parent / "out"):
        with pytest.raises(ValueError):
            write_report(report, path, data_dir=local[0], protected_files=[local[1]])


def test_symlink_and_corrupt_inputs_fail(local, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(local[0], target_is_directory=True)
    with pytest.raises(ValueError):
        load((link, local[1]))
    (local[0] / "kline_etf_daily" / "part.parquet").write_bytes(b"corrupt")
    with pytest.raises(pl.exceptions.PolarsError):
        load(local)


def test_missing_calendar_no_weekday_fallback(local):
    local[1].unlink()
    with pytest.raises(ValueError):
        load(local)


def test_input_changes_during_audit_invalidate_report(local, monkeypatch):
    from app.services.etf_research import audit as module
    real = module.audit

    def changed(*args, **kwargs):
        result = real(*args, **kwargs)
        with local[1].open("ab") as f:
            f.write(b"changed")
        return result

    monkeypatch.setattr(module, "audit", changed)
    with pytest.raises(ValueError, match="输入内容发生变化"):
        load(local)


def test_failed_report_has_no_success_manifest(local, tmp_path, monkeypatch):
    report = load(local)
    original = Path.write_text

    def fail(self, *args, **kwargs):
        if self.name == "audit.json":
            raise OSError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    out = tmp_path / "failed"
    with pytest.raises(OSError):
        write_report(report, out, data_dir=local[0])
    assert not (out / "manifest.json").exists()


def test_cli_success_and_failure_do_not_write_input(local, tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "audit_etf_data.py"
    before = hashes(local[0])
    out = tmp_path / "cli"
    cmd = [sys.executable, str(script), "--data-dir", str(local[0]), "--calendar", str(local[1]),
           "--start", "2025-01-02", "--end", "2025-01-03", "--output", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["members"] == 6
    assert hashes(local[0]) == before
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 1 and "ETF审计未完成" in result.stderr
    assert hashes(local[0]) == before


@pytest.mark.parametrize("frame", [
    {"fund_adj": [{"ts_code": S, "trade_date": "20250103", "adj_factor": float("nan")}]},
    {"adj_factor_etf": [{"symbol": S, "trade_date": "2025-01-03", "ex_factor": True}]},
    {"kline_etf_daily": [{"symbol": S, "date": "not-a-date"}]},
])
def test_invalid_dates_and_factors_not_silently_dropped(frame):
    with pytest.raises(ValueError):
        run(frame)


def test_empty_or_out_of_range_calendar_stays_unknown():
    with pytest.raises(ValueError):
        run(calendar=[])
    report = run(calendar=[{"trade_date": "20240101", "is_open": 1}])
    assert report["calendar"]["open_sessions"] == 0
    assert report["calendar"]["unknown_ranges"] == [{"start": "2025-01-02", "end": "2025-01-08", "days": 7}]
    assert not report["ready_for_backtest"]


def test_added_partition_during_read_invalidates_empty_table(local, monkeypatch):
    from app.services.etf_research import audit as module
    real = module.audit

    def changed(*args, **kwargs):
        result = real(*args, **kwargs)
        folder = local[0] / "kline_etf_enriched"
        folder.mkdir()
        pl.DataFrame({"symbol": [S], "date": [D("2025-01-03")]}).write_parquet(folder / "new.parquet")
        return result

    monkeypatch.setattr(module, "audit", changed)
    with pytest.raises(ValueError, match="文件列表发生变化"):
        load(local)


def test_offline_audit_does_not_connect_network(local, monkeypatch):
    import socket

    def deny(*args, **kwargs):
        raise AssertionError("unexpected network")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    assert len(load(local)["items"]) == 6


def test_predeclared_stock_conflict_does_not_become_etf(local, tmp_path):
    pl.DataFrame({"symbol": ["600000.SH"], "type": ["stock"]}).write_parquet(local[0] / "instruments_etf" / "part.parquet")
    registry = tmp_path / "members.json"
    registry.write_text(json.dumps({"U2": [{"symbol": "600000.SH", "asset_type": "etf", "effective_from": "2025-01-02", "effective_to": None, "reason": "bad claim"}]}))
    with pytest.raises(ValueError, match="混入股票"):
        load(local, registry_file=registry)
