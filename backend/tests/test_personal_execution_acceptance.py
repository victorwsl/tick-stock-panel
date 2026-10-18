"""仅验收最小next-open资金例；不等于公司行动与完整策略通过。"""
from datetime import date

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig


def test_next_open_hand_cash_and_fee_example():
    dates = [date(2024,1,2), date(2024,1,3), date(2024,1,4), date(2024,1,5)]
    panel=pl.DataFrame(dict(symbol=['600000.SH']*4, date=dates,
        open=[10.,10.,13.,11.], high=[10.,12.,13.,11.], low=[10.,10.,13.,9.],
        close=[10.,12.,13.,9.], volume=[100000.]*4))
    cfg=MatcherConfig(matching='open_t+1', commission_pct=.001, stamp_tax_pct=.001,
                      slippage_bps=0, max_positions=1, initial_capital=10000.)
    result=BacktestEngine(repo=None).simulate_portfolio(panel,
        pl.Series([True,False,False,False]),pl.Series([False,False,True,False]),cfg)
    # 买900股：9000+佣金9，现金991；次日信号、再下一开盘11元卖。
    # 卖9900，佣金9.90、印花税9.90：现金最终10871.20。
    assert [x['value'] for x in result.equity_curve] == pytest.approx([10000,11791,12691,10871.2])
    assert result.trades[0].shares == 900
    assert result.trades[0].entry_date == '2024-01-03'
    assert result.trades[0].exit_date == '2024-01-05'
    assert result.trades[0].pnl_amount == pytest.approx(871.2)
    changed=panel.with_columns(pl.when(pl.col('date')>=dates[2]).then(pl.col('close')*3).otherwise(pl.col('close')).alias('close'))
    replay=BacktestEngine(repo=None).simulate_portfolio(changed,
        pl.Series([True,False,False,False]),pl.Series([False,False,True,False]),cfg)
    assert replay.equity_curve[:2] == result.equity_curve[:2]
    assert replay.trades[0].shares == 900


def test_two_assets_cannot_each_spend_full_shared_cash():
    panel=pl.DataFrame([dict(symbol=code,date=date(2024,1,day),open=10.,high=10.,low=10.,close=10.,volume=100000.)
        for code in ['600000.SH','000001.SZ'] for day in [2,3,4]])
    result=BacktestEngine(repo=None).simulate_portfolio(panel,
        pl.Series([True,False,False,True,False,False]),pl.Series([False]*6),
        MatcherConfig(matching='open_t+1',fees_pct=0,slippage_bps=0,max_positions=2,initial_capital=10000))
    assert len(result.trades)==2
    assert sum(x.entry_value for x in result.trades)==pytest.approx(10000)
    assert min(x['cash'] for x in result.equity_curve)>=0
