from datetime import date, datetime

import polars as pl
import pytest

from app.config import settings
from app.plugins.local_parquet.provider import LocalParquetProvider, availability


@pytest.fixture
def local_source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "trial")
    raw = tmp_path / "raw"
    raw.mkdir()
    pl.DataFrame({
        "ts_code": ["000300.SH", "000300.SH"],
        "trade_date": ["20250102", "20250103"],
        "open": [10., 11.], "high": [11., 12.], "low": [9., 10.],
        "close": [10., 11.], "vol": [100., 200.], "amount": [100., 220.],
    }).write_parquet(raw / "index_daily.parquet")
    monkeypatch.setenv("SLQUANT_DATA_ROOT", str(tmp_path))
    return raw


def test_index_units_dates_and_window(local_source):
    frame = LocalParquetProvider().get_daily(
        ["000300.SH"], datetime(2025, 1, 3), datetime(2025, 1, 3), "index")
    assert frame.height == 1
    assert frame["date"][0] == date(2025, 1, 3)
    assert frame["close"][0] == 11
    assert frame["volume"][0] == 200
    assert frame["amount"][0] == 220000


def test_never_import_qfq_as_raw(local_source):
    with pytest.raises(ValueError, match="daily_qfq"):
        LocalParquetProvider().get_daily(["000001.SZ"], asset_type="stock")


def test_missing_symbol_and_source_fail_closed(local_source):
    provider = LocalParquetProvider()
    with pytest.raises(ValueError, match="没有这些指数"):
        provider.get_daily(["999999.SH"], asset_type="index")
    (local_source / "index_daily.parquet").unlink()
    assert availability()[0] is False
    with pytest.raises(FileNotFoundError):
        provider.get_daily(["000300.SH"], asset_type="index")


def test_duplicate_key_rejected(local_source):
    path = local_source / "index_daily.parquet"
    frame = pl.read_parquet(path)
    pl.concat([frame, frame]).write_parquet(path)
    with pytest.raises(ValueError, match="键重复"):
        LocalParquetProvider().get_daily(["000300.SH"], asset_type="index")


def test_local_mode_blocks_sdk_before_client_creation(monkeypatch):
    from app.tickflow import client
    monkeypatch.setattr(settings, "personal_local_only", True, raising=False)
    for factory in (client.get_client, client.get_async_client, client.get_paid_realtime_client):
        with pytest.raises(RuntimeError, match="本地研究"):
            factory()


def test_local_mode_does_not_probe_tickflow(monkeypatch):
    from app.tickflow import policy
    from app.tickflow.capabilities import CapabilitySet
    monkeypatch.setattr(settings, "personal_local_only", True, raising=False)
    def forbidden(*args):
        raise AssertionError("不得探测在线能力")
    monkeypatch.setattr(policy, "_detect_tickflow_caps", forbidden)
    monkeypatch.setattr(policy, "_augment_custom_sources", lambda caps: None)
    assert isinstance(policy.detect_capabilities(force=True), CapabilitySet)
