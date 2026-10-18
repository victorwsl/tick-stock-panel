from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import personal_factor_history as history


def result():
    return {"run_id": "batch", "config": {"factor_names": ["momentum_5d"],
            "start": "2025-01-01", "end": "2025-06-30", "asset_type": "stock", "prediction_horizon": 60},
            "results": [{"factor_name": "momentum_5d", "ic_mean": -0.03}], "error": None}


def client():
    app = FastAPI()
    app.include_router(history.router)
    return TestClient(app)


def test_saved_batch_survives_new_app_and_keeps_config(tmp_path, monkeypatch):
    monkeypatch.setattr(history.settings, "data_dir", tmp_path)
    monkeypatch.setattr(history, "factor_batch", lambda req, request: result())
    response = client().post('/api/custom/factor-history/run', json={"factor_names": ["momentum_5d"]})
    assert response.status_code == 200
    saved = response.json()
    assert saved['history_id']
    fresh = client()
    listing = fresh.get('/api/custom/factor-history').json()
    assert listing['items'][0]['id'] == saved['history_id']
    loaded = fresh.get('/api/custom/factor-history/' + saved['history_id']).json()
    assert loaded == saved
    assert loaded['config'] == result()['config']
    assert loaded['results'][0]['ic_mean'] == -0.03
    assert fresh.get('/api/custom/factor-history/invalid').status_code == 404


def test_failed_calculation_is_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(history.settings, "data_dir", tmp_path)
    monkeypatch.setattr(history, "factor_batch", lambda req, request: {"error": "无数据"})
    assert client().post('/api/custom/factor-history/run', json={"factor_names": ["momentum_5d"]}).json()['error']
    assert client().get('/api/custom/factor-history').json()['items'] == []


def test_write_failure_keeps_metrics_and_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(history.settings, "data_dir", tmp_path)
    (tmp_path / 'factor_research').write_text('blocked')
    monkeypatch.setattr(history, "factor_batch", lambda req, request: result())
    saved = client().post('/api/custom/factor-history/run', json={"factor_names": ["momentum_5d"]}).json()
    assert saved['results'] == result()['results']
    assert saved['history_error']
    assert 'history_id' not in saved
