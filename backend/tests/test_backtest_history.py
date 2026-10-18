import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest
from app.services.backtest_history import list_results, load_result, save_result


def sample():
    return {"run_id": "run-a", "error": None, "config": {"start": "2025-01-01", "end": "2025-12-31"},
            "strategy_info": {"name": "趋势"}, "trades": [{"symbol": "600000.SH", "pnl": 123}],
            "equity_curve": [{"date": "2025-01-02", "value": 1.1}],
            "drawdown_curve": [{"date": "2025-01-02", "value": -0.03}]}


def test_snapshot_reload_and_repeated_runs_are_immutable(tmp_path):
    result = sample()
    cfg = {"strategy_id": "test", "asset_type": "etf", "mode": "position"}
    first = save_result(tmp_path, result, cfg)
    second = save_result(tmp_path, result, cfg)
    assert first["history_id"] != second["history_id"]
    assert "history_id" not in result
    loaded = load_result(tmp_path, first["history_id"])
    assert loaded == first
    for key in ("trades", "equity_curve", "drawdown_curve"):
        assert loaded[key] == result[key]
    assert loaded["config"]["asset_type"] == "etf"
    listing = list_results(tmp_path)
    assert [i["id"] for i in listing["items"]] == [second["history_id"], first["history_id"]]
    assert listing["items"][0]["trade_count"] == 1


def test_failed_save_visible_and_no_partial_record(tmp_path):
    result = sample()
    result["stats"] = {"bad": float("nan")}
    saved = save_result(tmp_path, result, {})
    assert saved["history_error"]
    assert saved["trades"] == result["trades"]
    assert list_results(tmp_path) == {"items": [], "unavailable": 0}
    assert list((tmp_path / "backtest_history").iterdir()) == []


def test_error_not_saved_and_corruption_reported(tmp_path):
    assert save_result(tmp_path, {"error": "cancelled"}, {}) == {"error": "cancelled"}
    saved = save_result(tmp_path, sample(), {})
    (tmp_path / "backtest_history" / saved["history_id"] / "summary.json").write_text("invalid")
    assert list_results(tmp_path) == {"items": [], "unavailable": 1}
    assert load_result(tmp_path, saved["history_id"])["trades"]
    with pytest.raises(FileNotFoundError):
        load_result(tmp_path, "../../.env")


def test_api_survives_new_application(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    saved = save_result(tmp_path, sample(), {})
    app = FastAPI()
    app.include_router(backtest.router)
    with TestClient(app) as client:
        rows = client.get('/api/backtest/strategy/history').json()
        assert rows["items"][0]["id"] == saved["history_id"]
        response = client.get('/api/backtest/strategy/history/' + saved["history_id"])
        assert response.json() == saved
        assert client.get('/api/backtest/strategy/history/invalid').status_code == 404
        (tmp_path / 'backtest_history' / saved['history_id'] / 'result.json').write_text('{')
        assert client.get('/api/backtest/strategy/history/' + saved['history_id']).status_code == 500


@pytest.mark.parametrize("stream", [False, True])
def test_execution_endpoints_save_before_return(tmp_path, monkeypatch, stream):
    from types import SimpleNamespace

    from app.backtest import worker

    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    monkeypatch.setattr(backtest, "_guard_minute_strategy_backtest", lambda *a: None)
    monkeypatch.setattr(worker, "make_worker_task", lambda *a: {})
    monkeypatch.setattr(worker, "run_worker_task", lambda *a: sample())
    app = FastAPI()
    app.state.repo = SimpleNamespace()
    app.include_router(backtest.router)
    args = {"strategy_id": "test", "start": "2025-01-01", "end": "2025-03-01"}
    monkeypatch.setattr(backtest.settings, "backtest_range_guard", False)
    with TestClient(app) as client:
        if stream:
            with backtest._jobs_lock:
                backtest._running_jobs.clear()
            response = client.get('/api/backtest/strategy/stream', params=args)
            assert 'event: done' in response.text
            assert 'history_id' in response.text
        else:
            response = client.post('/api/backtest/strategy/run', json=args)
            assert response.status_code == 200
            assert response.json()["history_id"]
        rows = client.get('/api/backtest/strategy/history').json()["items"]
        assert len(rows) == 1
        assert client.get('/api/backtest/strategy/history/' + rows[0]["id"]).json()["trades"] == sample()["trades"]


def test_real_worker_result_can_be_saved_and_reloaded(tmp_path):
    from dataclasses import asdict
    from datetime import date, timedelta

    from fastapi.encoders import jsonable_encoder

    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import make_worker_task, run_worker_task
    from tests.backtest.test_worker_process import _write_market_data, _write_worker_strategy

    start = date(2024, 1, 1)
    _write_worker_strategy(tmp_path)
    _write_market_data(tmp_path, start)
    cfg = StrategyBacktestConfig(strategy_id="worker_always_entry", symbols=["600000.SH"],
                                start=start, end=start + timedelta(days=2),
                                overrides={"basic_filter": {"enabled": False}},
                                matching="close_t", fees_pct=0, slippage_bps=0, max_positions=1)
    result = run_worker_task(make_worker_task("backtest", tmp_path, cfg))
    assert result["error"] is None
    assert result["trades"]
    saved = save_result(tmp_path, result, jsonable_encoder(asdict(cfg)))
    assert "history_error" not in saved
    loaded = load_result(tmp_path, saved["history_id"])
    assert loaded["trades"] == result["trades"]
    assert loaded["equity_curve"] == result["equity_curve"]
    assert loaded["drawdown_curve"] == result["drawdown_curve"]


def test_summary_contains_stats_and_legacy_list_never_reads_result(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    result = {**sample(), "stats": {"total_return": .23, "max_drawdown": -.11, "win_rate": .5}}
    saved = save_result(tmp_path, result, {"strategy_id": "trend_v1"})
    folder = tmp_path / "backtest_history" / saved["history_id"]
    item = list_results(tmp_path)["items"][0]
    assert item["strategy_id"] == "trend_v1"
    assert item["stats"] == result["stats"]
    legacy = json.loads((folder / "summary.json").read_text())
    legacy.pop("strategy_id")
    legacy.pop("stats")
    (folder / "summary.json").write_text(json.dumps(legacy))
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        assert path.name != "result.json", "list must not read large immutable results"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    old = list_results(tmp_path)["items"][0]
    assert old["strategy_id"] is None
    assert old["stats"] == {"total_return": None, "max_drawdown": None, "win_rate": None}


def test_metadata_edits_are_independent_and_delete_only_run(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    first = save_result(tmp_path, sample(), {"strategy_id": "trend"})
    second = save_result(tmp_path, sample(), {})
    folder = tmp_path / "backtest_history" / first["history_id"]
    before = {name: (folder / name).read_bytes() for name in ("result.json", "summary.json")}
    app = FastAPI()
    app.include_router(backtest.router)
    url = "/api/backtest/strategy/history/" + first["history_id"]
    with TestClient(app) as client:
        response = client.patch(url, json={"favorite": True, "label": "基线", "note": "保留对照"})
        assert response.status_code == 200
        assert response.json()["strategy_id"] == "trend"
        assert response.json()["favorite"] is True
        response = client.patch(url, json={"label": "新标签"})
        assert response.json()["note"] == "保留对照"
        assert list_results(tmp_path)["items"][1]["label"] == "新标签"
        assert before == {name: (folder / name).read_bytes() for name in before}
        assert client.delete(url).json() == {"id": first["history_id"]}
        assert client.get(url).status_code == 404
        assert client.delete(url).status_code == 404
        assert load_result(tmp_path, second["history_id"]) == second


@pytest.mark.parametrize("patch", [{"unknown": True}, {"label": "x" * 121},
                                    {"note": "x" * 4001}, {"favorite": "true"}, {"label": None}])
def test_metadata_patch_validation(tmp_path, monkeypatch, patch):
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    saved = save_result(tmp_path, sample(), {})
    app = FastAPI()
    app.include_router(backtest.router)
    with TestClient(app) as client:
        assert client.patch("/api/backtest/strategy/history/" + saved["history_id"], json=patch).status_code == 422


def test_corrupt_metadata_visible_and_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)
    saved = save_result(tmp_path, sample(), {})
    folder = tmp_path / "backtest_history" / saved["history_id"]
    meta = folder / "metadata.json"
    meta.write_text("{")
    rows = list_results(tmp_path)
    assert rows["unavailable"] == 1
    assert rows["items"][0]["metadata_error"]
    assert load_result(tmp_path, saved["history_id"]) == saved
    app = FastAPI()
    app.include_router(backtest.router)
    with TestClient(app) as client:
        assert client.patch("/api/backtest/strategy/history/" + saved["history_id"], json={"note": "a"}).status_code == 500
    assert meta.read_text() == "{"
    (folder / "summary.json").write_text('{"id":"bad"}')
    assert list_results(tmp_path) == {"items": [], "unavailable": 1}


def test_symlink_and_traversal_boundaries(tmp_path):
    from app.services.backtest_history import delete_result, patch_result_metadata
    from app.services.research_metadata import HistoryMetadataPatch

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("preserve")
    root = tmp_path / "backtest_history"
    root.mkdir()
    linked_id = "a" * 32
    (root / linked_id).symlink_to(outside, target_is_directory=True)
    for history_id in (linked_id, "..", "../outside", "A" * 32, "a" * 31):
        with pytest.raises(FileNotFoundError):
            load_result(tmp_path, history_id)
        with pytest.raises(FileNotFoundError):
            delete_result(tmp_path, history_id)
        with pytest.raises(FileNotFoundError):
            patch_result_metadata(tmp_path, history_id, HistoryMetadataPatch(note="x"))
    assert list_results(tmp_path)["unavailable"] == 1
    assert sentinel.read_text() == "preserve"
    saved = save_result(tmp_path, sample(), {})
    folder = root / saved["history_id"]
    # Deleting a real run does not follow child links.
    (folder / "outside").symlink_to(outside, target_is_directory=True)
    delete_result(tmp_path, saved["history_id"])
    assert sentinel.read_text() == "preserve"


def test_summary_and_result_symlink_rejected(tmp_path):
    saved = save_result(tmp_path, sample(), {})
    folder = tmp_path / "backtest_history" / saved["history_id"]
    for name in ("summary.json", "result.json"):
        path = folder / name
        outside = tmp_path / name
        path.rename(outside)
        path.symlink_to(outside)
    assert list_results(tmp_path) == {"items": [], "unavailable": 1}
    with pytest.raises(FileNotFoundError):
        load_result(tmp_path, saved["history_id"])


def test_concurrent_metadata_patches_keep_both_fields(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from app.services.backtest_history import patch_result_metadata
    from app.services.research_metadata import HistoryMetadataPatch

    saved = save_result(tmp_path, sample(), {})
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(patch_result_metadata, tmp_path, saved["history_id"],
                               HistoryMetadataPatch(**patch))
                   for patch in [{"label": "baseline"}, {"note": "keep"}, {"favorite": True}] * 8]
        for future in futures:
            future.result()
    row = list_results(tmp_path)["items"][0]
    assert (row["label"], row["note"], row["favorite"]) == ("baseline", "keep", True)


def test_failed_atomic_metadata_write_preserves_old_annotation(tmp_path, monkeypatch):
    from app.services import research_metadata
    from app.services.backtest_history import patch_result_metadata
    from app.services.research_metadata import HistoryMetadataPatch

    saved = save_result(tmp_path, sample(), {})
    patch_result_metadata(tmp_path, saved["history_id"], HistoryMetadataPatch(note="old"))

    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(research_metadata.os, "replace", fail)
    with pytest.raises(OSError):
        patch_result_metadata(tmp_path, saved["history_id"], HistoryMetadataPatch(note="new"))
    assert list_results(tmp_path)["items"][0]["note"] == "old"
    assert not list((tmp_path / "backtest_history" / saved["history_id"]).glob(".metadata-*"))


def test_history_root_symlink_never_read_written_or_deleted(tmp_path):
    from app.services.backtest_history import delete_result

    outside_data = tmp_path / "outside"
    saved = save_result(outside_data, sample(), {})
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "backtest_history").symlink_to(outside_data / "backtest_history", target_is_directory=True)
    assert list_results(data_dir) == {"items": [], "unavailable": 1}
    assert save_result(data_dir, sample(), {})["history_error"]
    with pytest.raises(FileNotFoundError):
        delete_result(data_dir, saved["history_id"])
    assert load_result(outside_data, saved["history_id"]) == saved


def test_failed_delete_returns_visible_error_and_preserves_record(tmp_path, monkeypatch):
    from app.services import backtest_history

    saved = save_result(tmp_path, sample(), {})
    monkeypatch.setattr(backtest.settings, "data_dir", tmp_path)

    def fail(*args):
        raise PermissionError("not writable")

    monkeypatch.setattr(backtest_history.shutil, "rmtree", fail)
    app = FastAPI()
    app.include_router(backtest.router)
    with TestClient(app) as client:
        response = client.delete("/api/backtest/strategy/history/" + saved["history_id"])
        assert response.status_code == 500
    assert load_result(tmp_path, saved["history_id"]) == saved
