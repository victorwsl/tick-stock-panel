from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import (
    build_market_data_matrix,
    build_market_matrix_from_signals,
    make_signal_matrix,
)
from app.backtest.pullback_rules import PullbackTradeRules
from app.services.personal_zhixing import install_early_strategy
from app.strategy.personal.zhixing_early import MATRIX_STRATEGY, compute_features


def run_case(prices=None, patches=None, independent=False, stop=97.5, target=120.):
    closes = prices or [100., 100., 101., 103., 104., 104., 106., 106., 107., 107.]
    patches = patches or {}
    rows = [dict(symbol='600000.SH', date=date(2025, 1, 1)+timedelta(days=i),
                 open=c, high=c+1, low=c-1, close=c, volume=10000.) for i,c in enumerate(closes)]
    for i, changes in patches.items():
        rows[i].update(changes)
    market = build_market_data_matrix(pl.DataFrame(rows))
    entries = np.zeros(market.shape, dtype=np.uint8)
    entries[0, 0] = 1
    signals = make_signal_matrix(market.shape, entry=entries)
    matrix = build_market_matrix_from_signals(market, signals, entry_delay_bars=1, exit_delay_bars=1)
    rules = PullbackTradeRules(np.full(market.shape, stop), np.full(market.shape, target),
                              np.full(market.shape, 105.))
    config = MatcherConfig(matching='open_t+1', fees_pct=0, slippage_bps=0,
                           max_positions=1, initial_capital=100000., pullback_rules=rules)
    engine = BacktestEngine(None)
    if independent:
        return engine.simulate_independent_market_matrix(matrix, 1, config)
    return engine.simulate_market_matrix(matrix, config)


@pytest.mark.parametrize('independent', [False, True])
def test_three_day_clock_starts_at_fill_then_sells_next_open(independent):
    result = run_case(independent=independent)
    t = result.trades[0]
    assert str(t.entry_date) == '2025-01-02'
    assert str(t.exit_signal_date) == '2025-01-05'
    assert str(t.exit_date) == '2025-01-06'
    assert t.exit_reason == 'three_day_weak'
    assert t.planned_stop == 97.5
    assert t.planned_rr == 8.


@pytest.mark.parametrize('independent', [False, True])
def test_successful_three_days_hold_until_white_not_reclaimed(independent):
    result = run_case([100., 100., 102., 103., 106., 104., 104., 103., 104., 104.], independent=independent)
    t = result.trades[0]
    assert t.exit_reason == 'white_not_reclaimed'
    assert str(t.exit_signal_date) == '2025-01-07'
    assert str(t.exit_date) == '2025-01-08'
    assert t.exit_price == 103.


def test_exact_five_percent_passes_and_reclaim_cancels_white_exit():
    result = run_case([100., 100., 101., 102., 105., 104., 106., 106., 107., 108.])
    assert result.trades[0].exit_reason == 'end'


@pytest.mark.parametrize('independent', [False, True])
@pytest.mark.parametrize('patch,expected', [({'low': 96.}, 97.5), ({'open': 95., 'low': 94.}, 95.)])
def test_structure_stop_and_gap_not_idealized(independent, patch, expected):
    t = run_case(patches={2: patch}, independent=independent).trades[0]
    assert t.exit_reason == 'stop_loss'
    assert t.exit_price == expected


def test_entry_day_cannot_stop_out():
    t = run_case(patches={1: {'low': 95.}, 2: {'open': 96., 'low': 95.}}).trades[0]
    assert str(t.exit_date) == '2025-01-03'
    assert t.exit_price == 96.


def test_gap_rechecks_risk_and_reward_before_fill():
    result = run_case(patches={1: {'open': 102., 'high': 103.}})
    assert not result.trades
    assert result.stats['execution']['buy_planned_risk'] == 1
    result = run_case(target=104.)
    assert not result.trades
    assert result.stats['execution']['buy_planned_rr'] == 1


def test_costs_included_and_future_plan_not_used():
    rules = PullbackTradeRules(np.array([[97.5], [10.]]), np.array([[120.], [1000.]]), np.ones((2,1)))
    terms, _ = rules.entry_terms(0, 0, 100., .001, .002)
    assert terms['planned_risk_pct'] > .025
    assert terms['planned_rr'] < 8.
    assert terms['planned_stop'] == 97.5


def test_install_is_idempotent_and_no_manual_overwrite(tmp_path):
    from app.strategy.engine import StrategyEngine
    first = install_early_strategy(tmp_path)
    assert install_early_strategy(tmp_path) == first
    engine = StrategyEngine([tmp_path/'strategies'/'custom'])
    assert engine.get(first['strategy_id']).matrix_strategy.build_pullback_trade_rules
    target = tmp_path/'strategies'/'custom'/(first['strategy_id']+'.py')
    target.write_text('# manual')
    with pytest.raises(ValueError, match='未覆盖'):
        install_early_strategy(tmp_path)


def test_signal_prefix_isolation_and_early_entry():
    from tests.test_personal_zhixing import bars
    frame = bars(260)
    full = compute_features(build_market_data_matrix(frame), {})
    prefix = compute_features(build_market_data_matrix(frame.filter(pl.col('date') <= date(2025,8,20))), {})
    for key in prefix:
        np.testing.assert_allclose(prefix[key], full[key][:len(prefix[key])], equal_nan=True, err_msg=key)
    assert not MATRIX_STRATEGY.compute_signals(build_market_data_matrix(frame), {}).entry[:114].any()


def test_intraday_stop_proceeds_cannot_fund_todays_open():
    # A占满现金；D2盘中止损，B的D2开盘订单不能使用这笔未来卖款。
    rows = []
    for symbol in ['600000.SH', '000001.SZ']:
        for i in range(7):
            rows.append(dict(symbol=symbol, date=date(2025,1,1)+timedelta(days=i),
                             open=100., high=101., low=96. if symbol=='600000.SH' and i==2 else 99.,
                             close=100., volume=10000.))
    market = build_market_data_matrix(pl.DataFrame(rows))
    entry = np.zeros(market.shape, dtype=np.uint8)
    a, b = market.symbols.index('600000.SH'), market.symbols.index('000001.SZ')
    entry[0,a], entry[1,b] = 1, 1
    matrix = build_market_matrix_from_signals(market, make_signal_matrix(market.shape, entry=entry),
                                             entry_delay_bars=1, exit_delay_bars=1)
    rules = PullbackTradeRules(np.full(market.shape,97.5), np.full(market.shape,120.), np.full(market.shape,100.))
    result = BacktestEngine(None).simulate_market_matrix(matrix, MatcherConfig(matching='open_t+1',
        fees_pct=0, slippage_bps=0, max_positions=1, initial_capital=100000., pullback_rules=rules))
    assert [t.symbol for t in result.trades] == ['600000.SH']
    assert result.stats['execution']['buy_no_slot'] == 1


def early_panel():
    closes = np.r_[np.linspace(10,11,140),np.linspace(11,18,25),np.linspace(18,15,10),
                   np.full(25,15.),15.2,15.1,15.,14.9,14.8,14.7]
    volumes = np.r_[np.full(140,1e4),np.full(25,1e5),np.full(10,1e4),np.full(25,2e3),1e6,np.full(5,1e3)]
    return pl.DataFrame([dict(symbol='600000.SH',date=date(2025,1,1)+timedelta(days=i),
        open=float(c+.01),high=float(c+.05),low=float(c-.05),close=float(c),volume=float(v))
        for i,(c,v) in enumerate(zip(closes,volumes,strict=True))])


def test_buys_pullback_without_waiting_for_restrength():
    from app.strategy.personal.zhixing_pullback import compute_features as baseline
    market = build_market_data_matrix(early_panel())
    f = compute_features(market,{})
    assert f['entry'][-1,0]
    assert f['j'][-1,0] < 20
    assert market.close[-1,0] < f['white'][-1,0]
    assert not baseline(market,{})['entry'][-1,0]


def test_real_worker_uses_position_rules_and_records_plan(tmp_path):
    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import make_worker_task, run_worker_task
    install_early_strategy(tmp_path)
    panel = early_panel()
    rows = panel.to_dicts()
    for c in [14.7,14.8,14.9,15.,15.,15.1,15.2]:
        rows.append(dict(symbol='600000.SH', date=rows[-1]['date']+timedelta(days=1),
                         open=c, high=c+.05, low=c-.05, close=c, volume=1000.))
    for row in rows:
        row.update(raw_close=row['close'], raw_high=row['high'], raw_low=row['low'],
                   amount=row['close']*row['volume']*100, turnover_rate=1.,
                   consecutive_limit_ups=0, consecutive_limit_downs=0)
        folder = tmp_path/'kline_daily_enriched'/('date='+row['date'].isoformat())
        folder.mkdir(parents=True)
        pl.DataFrame([row]).write_parquet(folder/'part.parquet')
    folder = tmp_path/'instruments'
    folder.mkdir()
    pl.DataFrame({'symbol':['600000.SH'],'name':['浦发银行'],
                  'total_shares':[1000000000.],'float_shares':[1000000000.]}).write_parquet(folder/'part.parquet')
    cfg = StrategyBacktestConfig(strategy_id='personal_zhixing_early_v0',symbols=['600000.SH'],
        start=rows[205]['date'],end=rows[-1]['date'],matching='open_t+1',fees_pct=0,slippage_bps=0,
        max_positions=1,initial_capital=100000.)
    result = run_worker_task(make_worker_task('backtest', tmp_path,cfg))
    assert result['error'] is None
    assert result['trades']
    assert result['trades'][0]['planned_rr'] >= 3
    assert result['trades'][0]['exit_reason'] == 'three_day_weak'


def test_daily_check_waits_for_valid_close_during_suspension():
    rules = PullbackTradeRules(np.ones((6,1)),np.ones((6,1))*20,np.ones((6,1))*10)
    pos = {'entry_time':0,'entry_price':10.,'three_day_checked':False,'below_white':False}
    assert rules.close_reason(pos,3,0,10.,False) is None
    assert not pos['three_day_checked']
    assert rules.close_reason(pos,4,0,10.,True) == 'three_day_weak'
