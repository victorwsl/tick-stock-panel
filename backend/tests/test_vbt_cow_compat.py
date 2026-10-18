"""两笔手算交易：+10与-5，盈亏总额比2，平均每笔盈利2.5元。"""
import pandas as pd
import pytest

from app.services.backtest import _vbt_metric_settings


def test_readonly_pandas_arrays_keep_trade_statistics():
    vbt = pytest.importorskip('vectorbt')
    close = pd.Series([100., 110., 100., 95.], index=pd.date_range('2025-01-01', periods=4))
    portfolio = vbt.Portfolio.from_signals(close, entries=[True, False, True, False],
        exits=[False, True, False, True], size=1, init_cash=1000, fees=0, freq='1D')
    stats = portfolio.stats(metrics=['profit_factor', 'expectancy'], metric_settings=_vbt_metric_settings())
    assert stats['Profit Factor'] == pytest.approx(2.)
    assert stats['Expectancy'] == pytest.approx(2.5)
