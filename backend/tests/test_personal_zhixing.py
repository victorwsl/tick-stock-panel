from datetime import date, timedelta

import numpy as np
import pandas as pd
import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.indicators.zhixing import add_zhixing_lines
from app.strategy.personal.zhixing_pullback import MATRIX_STRATEGY, compute_features


def bars(n=260):
    rows = []
    for symbol, offset in (("600000.SH", 0), ("000001.SZ", 10)):
        for i in range(n):
            close = 10 + offset + i*.015 + np.sin(i/5)*.7
            rows.append(dict(symbol=symbol, date=date(2025, 1, 1)+timedelta(days=i),
                             open=close-.1, high=close+.2, low=close-.2,
                             close=close, volume=10000.+(i % 17)*300))
    return pl.DataFrame(rows).sort(["symbol", "date"])


def test_lines_match_independent_pandas_and_have_full_warmup():
    frame = bars()
    actual = add_zhixing_lines(frame)
    for code in frame["symbol"].unique():
        part = actual.filter(pl.col("symbol") == code)
        c = pd.Series(part["close"].to_numpy())
        np.testing.assert_allclose(part["zx_white"], c.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean())
        expected = sum(c.rolling(n).mean() for n in (14, 28, 57, 114))/4
        np.testing.assert_allclose(part["zx_yellow"].to_numpy(), expected, equal_nan=True)
        assert part["zx_yellow"][:113].null_count() == 113
        np.testing.assert_allclose(part["zx_long"].to_numpy(), c.rolling(114).mean(), equal_nan=True)


def test_signal_chart_formula_parity_and_prefix_isolation():
    frame = bars()
    market = build_market_data_matrix(frame)
    full = compute_features(market, {})
    chart = add_zhixing_lines(frame)
    for col, code in enumerate(market.symbols):
        part = chart.filter(pl.col("symbol") == code)
        np.testing.assert_allclose(full["white"][:, col], part["zx_white"], rtol=2e-6)
        np.testing.assert_allclose(full["yellow"][:, col], part["zx_yellow"].to_numpy(), rtol=2e-6, equal_nan=True)
    cutoff = date(2025, 8, 20)
    short = build_market_data_matrix(frame.filter(pl.col("date") <= cutoff))
    prefix = compute_features(short, {})
    for key in full:
        np.testing.assert_allclose(prefix[key], full[key][:short.shape[0]], equal_nan=True, err_msg=key)
    signals = MATRIX_STRATEGY.compute_signals(market, {})
    assert not signals.entry[:113].any()
    assert np.isfinite(signals.score).all()


def test_reference_volume_excludes_current_five_days():
    frame = bars(240).filter(pl.col("symbol") == "600000.SH")
    frame = frame.with_columns(pl.when(pl.int_range(pl.len()) >= 235).then(1000000.).otherwise(10000.).alias("volume"))
    features = compute_features(build_market_data_matrix(frame), {})
    assert abs(features["volume_ratio"][-1, 0]-100) < 1e-5


def test_strategy_install_is_idempotent_and_refuses_overwrite(tmp_path):
    import pytest

    from app.services.personal_zhixing import install_strategy
    from app.strategy.engine import StrategyEngine
    first = install_strategy(tmp_path)
    assert install_strategy(tmp_path) == first
    engine = StrategyEngine([tmp_path / 'strategies' / 'custom'])
    assert not engine.load_errors()
    assert engine.get(first['strategy_id']).execution_backend == 'matrix_native'
    path = tmp_path / 'strategies' / 'custom' / (first['strategy_id'] + '.py')
    path.write_text('# manual edit')
    with pytest.raises(ValueError, match='未覆盖'):
        install_strategy(tmp_path)


def test_positive_pullback_then_restrength_and_rr_rejection():
    close = np.r_[np.linspace(12,13,140), np.linspace(13,18,25), np.linspace(18,15,10), np.full(25,15.), 14.9,14.8,14.7,15.1,15.2]
    volume = np.r_[np.full(140,1e4),np.full(25,1e5),np.full(10,1e4),np.full(25,2e3),np.full(3,1e3),np.full(2,1e6)]
    panel = pl.DataFrame([dict(symbol='600000.SH', date=date(2025,1,1)+timedelta(days=i),
        open=float(c-.01), high=float(c+.05), low=float(c-.05), close=float(c), volume=float(v))
        for i,(c,v) in enumerate(zip(close,volume,strict=True))])
    market = build_market_data_matrix(panel)
    f = compute_features(market, {})
    assert f['entry'][-2,0]
    assert f['planned_rr'][-2,0] > 2
    assert not compute_features(market, {'min_rr': 100})['entry'][-2,0]
    assert not compute_features(market, {'max_bias_pct': .1})['entry'][-2,0]
