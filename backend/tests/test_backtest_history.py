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
