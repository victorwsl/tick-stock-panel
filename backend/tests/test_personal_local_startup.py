from fastapi.testclient import TestClient

from app.config import settings


def test_local_startup_without_online_probe_or_scheduler(tmp_path, monkeypatch):
    from app.jobs import daily_pipeline
    from app.main import app
    from app.tickflow import policy

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "personal_local_only", True)
    monkeypatch.setattr(settings, "watchdog_enabled", False)
    monkeypatch.setattr(settings, "backtest_matrix_cache_prewarm", False)

    def forbidden(*args, **kwargs):
        raise AssertionError("本地启动不得调用在线探测或定时取数")

    monkeypatch.setattr(policy, "_detect_tickflow_caps", forbidden)
    monkeypatch.setattr(daily_pipeline, "start_scheduler", forbidden)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.scheduler is None
        assert not hasattr(app.state, "pull_scheduler")
        assert app.state.financial_scheduler._task is None  # 仅手动同步，无周期任务
