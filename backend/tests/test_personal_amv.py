from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.regime import router
from app.backtest.strategy import StrategyBacktestService
from app.plugins.local_parquet.provider import LocalParquetProvider
from app.services.personal_amv import (
    calculate_states,
    entry_mask,
    history_payload,
    source_fingerprint,
)


def bars(values):
    return pl.DataFrame([dict(trade_date=(date(2025, 1, 1)+timedelta(days=i)).strftime('%Y%m%d'),
                             open=float(v), close=float(v), high=float(v)+1, low=float(v)-1)
                         for i, v in enumerate(values)])


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv('SLQUANT_DATA_ROOT', str(tmp_path))
    raw = tmp_path/'raw'
    raw.mkdir()
    path = raw/'compass_0amv.parquet'
    bars([100, 100, 100, 105, 107, 108, 104, 105]).write_parquet(path)
    return path


def test_hand_calculation_hysteresis_and_no_future(source):
    full = LocalParquetProvider().get_active_mv()
    actual = calculate_states(full)
    assert actual['amv_state'].to_list() == ['risk_off']*3 + ['probe', 'confirmed', 'confirmed', 'risk_off', 'risk_off']
    assert actual['r3'][4] == pytest.approx(.07)
    for n in (3, 4, 5, 7):
        assert calculate_states(full.head(n))['amv_state'].to_list() == actual['amv_state'].head(n).to_list()
    # 一个窗口从已启动的中段开始，状态仍来自完整历史，不重置。
    payload = history_payload(date(2025, 1, 5), date(2025, 1, 6))
    assert [r['amv_state'] for r in payload['rows']] == ['confirmed', 'confirmed']


def test_gate_uses_signal_date_without_double_delay_and_gaps_fail_closed(source):
    labels = ('2025-01-03', '2025-01-04', '2025-01-05', '2025-01-09')
    np.testing.assert_array_equal(entry_mask(labels, 'active'), [False, True, True, False])
    np.testing.assert_array_equal(entry_mask(labels, 'confirmed'), [False, False, True, False])
    np.testing.assert_array_equal(StrategyBacktestService._build_regime_mask(labels, {'amv': 'active'}, None),
                                  [False, True, True, False])
    assert StrategyBacktestService._build_regime_mask(labels, None, None) is None
    with pytest.raises(ValueError, match='未知0AMV'):
        entry_mask(labels, 'typo')


def test_api_reads_fresh_source_and_does_not_modify_it(source):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    before = source_fingerprint()
    response = client.get('/api/regime/active-mv?limit=2')
    assert response.status_code == 200
    data = response.json()
    assert data['total'] == 8
    assert len(data['rows']) == 2
    assert data['rows'][-1]['date'] == '2025-01-08'
    assert data['source_sha256'] == before == source_fingerprint()
    assert data['rows'][0]['zx_yellow'] is None  # 不伪造预热数据
    bars([100]*10).write_parquet(source)
    updated = client.get('/api/regime/active-mv?limit=2').json()
    assert updated['total'] == 10
    assert updated['source_sha256'] != before
    assert client.get('/api/regime/active-mv?start=2026-01-01').json()['rows'] == []
    source.unlink()
    assert client.get('/api/regime/active-mv').status_code == 422


@pytest.mark.parametrize('bad', ['duplicate', 'ohlc', 'null', 'nan'])
def test_invalid_source_rejected(source, bad):
    frame = pl.read_parquet(source)
    if bad == 'duplicate':
        frame = pl.concat([frame, frame.head(1)])
    else:
        frame = frame.with_columns(pl.lit({'ohlc': 0., 'null': None, 'nan': float('nan')}[bad]).alias('close'))
    frame.write_parquet(source)
    with pytest.raises(ValueError):
        LocalParquetProvider().get_active_mv()


def test_source_update_invalidates_shared_matrix_signature(source):
    from app.backtest.strategy import StrategyBacktestConfig
    config = StrategyBacktestConfig(strategy_id='test', symbols=None,
        start=date(2025, 1, 1), end=date(2025, 1, 8), regime_filter={'amv': 'active'})
    before = StrategyBacktestService._matrix_prepare_signature(config)
    bars([100, 110, 120]).write_parquet(source)
    assert before != StrategyBacktestService._matrix_prepare_signature(config)


def test_market_gate_does_not_suppress_exit_signals(source):
    from app.backtest.matrix import apply_time_masks, make_signal_matrix
    signals = make_signal_matrix((4, 1), entry=np.ones((4, 1)), exit=np.ones((4, 1)))
    allowed = entry_mask(['2025-01-03', '2025-01-04', '2025-01-05', '2025-01-07'], 'active')
    gated = apply_time_masks(signals, allowed, np.ones(4, dtype=bool))
    assert gated.entry[:, 0].tolist() == [0, 1, 1, 0]
    assert gated.exit[:, 0].tolist() == [1, 1, 1, 1]
