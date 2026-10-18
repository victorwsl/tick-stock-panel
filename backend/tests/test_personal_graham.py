from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.backtest.graham_monthly import simulate
from app.services.personal_graham import _asof, _latest, _ttm, percentile, schedule


def test_revision_does_not_replace_newer_report_or_leak():
    f = pd.DataFrame([
        ['X', '20231231', '20240301', 10.],
        ['X', '20240331', '20240420', 4.],
        ['X', '20231231', '20240501', 99.],
    ], columns=['ts_code', 'end_date', 'known', 'n'])
    assert _latest(f, '20240502').loc['X', 'n'] == 4.
    assert _asof(f, '20240501').query('end_date == "20231231"').n.iloc[0] == 10.


def test_ttm_uses_cumulative_components_known_at_date():
    f = pd.DataFrame([
        ['X', '20231231', '20240301', 100.],
        ['X', '20230630', '20230801', 40.],
        ['X', '20240630', '20240801', 70.],
        ['X', '20231231', '20240901', 999.],
    ], columns=['ts_code', 'end_date', 'known', 'n']).sort_values('known')
    assert np.isnan(_ttm(f, '20240801', 'n', {'X': '20240630'})['X'])
    assert _ttm(f, '20240802', 'n', {'X': '20240630'})['X'] == 130.


def test_percentile_keeps_missing_and_average_ties():
    result = percentile(pd.Series([10., 10., 20., np.nan]))
    assert result.iloc[:3].tolist() == [.25, .25, 1.]
    assert np.isnan(result.iloc[3])


def test_partial_month_is_not_a_month_end():
    raw = {'dates': ['20250130', '20250131', '20250203', '20250204']}
    assert schedule(raw, '20250203', '20250204', 's0') == {}


def inputs(high=11, close=10, blocked=False):
    dates = ['20250130', '20250131', '20250203', '20250204']
    records = [{'ts_code': '600001.SH', 'trade_date': d, 'open': 10., 'high': high, 'low': 9.,
                'close': close, 'amount20': 1e9, 'vol': 1000} for d in dates]
    limits = [{'ts_code': '600001.SH', 'trade_date': d, 'up_limit': 10. if blocked and d == '20250203' else 11., 'down_limit': 9.} for d in dates]
    adj = [{'ts_code': '600001.SH', 'trade_date': d, 'adj_factor': 1.} for d in dates]
    raw = {'dates': dates, 'daily': pd.DataFrame(records), 'limits': pd.DataFrame(limits),
           'adj': pd.DataFrame(adj), 'dividends': pd.DataFrame(),
           'stocks': pd.DataFrame([{'ts_code': '600001.SH', 'delist_date': pd.NA}])}
    target = pd.DataFrame([{'symbol': '600001.SH', 'name': '样本', 'date': '20250131',
        'rank': 1, 'score': 100., 'amount20': 1e9, 'market_cap': 1e9}]).set_index('symbol', drop=False)
    cfg = SimpleNamespace(start=date(2025,1,30), end=date(2025,2,4), initial_capital=1e6,
        commission_pct=.0002, fees_pct=.0002, stamp_tax_pct=None, slippage_bps=10.,
        max_exposure_pct=1., strategy_id='custom_graham_s0')
    return raw, {'20250131': target}, cfg


def test_next_open_and_missing_slots_keep_cash_and_end_is_not_sale():
    raw, targets, cfg = inputs()
    stats, curve, dd, trades, fills, journal = simulate(None, raw, targets, cfg)
    assert curve[0]['value'] == curve[1]['value'] == 1e6
    assert len(fills) == 1 and fills[0]['date'] == '2025-02-03'
    assert fills[0]['side'] == 'buy'
    assert 0 < curve[-1]['exposure'] < .051
    assert curve[-1]['cash'] > 949000
    assert stats['n_trades'] == 0
    assert trades[-1]['record'].exit_reason == 'open_position'
    assert curve[-1]['value'] == cfg.initial_capital + sum(j.get('cash_delta', 0) for j in journal) + fills[0]['quantity'] * 10
    assert min(x['value'] for x in dd) < 0  # 买入费即时计入回撤


def test_future_high_close_do_not_approve_or_size_open():
    raw, targets, cfg = inputs()
    a = simulate(None, raw, targets, cfg)[4]
    raw['daily'].loc[raw['daily'].trade_date.ge('20250203'), ['high', 'low', 'close', 'vol']] = [100., 1., 90., 9999999.]
    b = simulate(None, raw, targets, cfg)[4]
    assert a == b


def test_open_limit_blocks_buy_even_if_later_unlocked():
    raw, targets, cfg = inputs(high=12, blocked=True)
    stats, curve, _dd, _trades, fills, _journal = simulate(None, raw, targets, cfg)
    assert fills == []
    assert stats['execution']['buy_limit_up'] == 1
    assert all(r['cash'] == 1e6 for r in curve)


def test_star_board_minimum_and_one_share_increment():
    raw, targets, cfg = inputs()
    for key in ('daily', 'limits', 'adj', 'stocks'):
        raw[key]['ts_code'] = '688001.SH'
    target = targets['20250131'].copy()
    target.index = ['688001.SH']
    target['symbol'] = '688001.SH'
    fills = simulate(None, raw, {'20250131': target}, cfg)[4]
    assert fills[0]['quantity'] >= 200
    assert fills[0]['quantity'] % 100 != 0
    cfg.initial_capital = 30000  # 单槽1500元不足200股，不能假设可买100股
    assert simulate(None, raw, {'20250131': target}, cfg)[4] == []


def test_delisted_holding_fails_instead_of_freezing_mark():
    import pytest
    raw, targets, cfg = inputs()
    raw['stocks']['delist_date'] = '20250204'
    with pytest.raises(ValueError, match='缺清算'):
        simulate(None, raw, targets, cfg)


def test_market_wide_missing_session_fails():
    import pytest
    raw, targets, cfg = inputs()
    raw['daily'] = raw['daily'][raw['daily'].trade_date.ne('20250203')]
    with pytest.raises(ValueError, match='整日市场行情缺失'):
        simulate(None, raw, targets, cfg)


def test_delayed_exit_still_respects_original_signal_capacity():
    raw, targets, cfg = inputs()
    dates = ['20250130', '20250131', '20250203', '20250228', '20250303', '20250304']
    for key in ('daily', 'limits', 'adj'):
        base = raw[key].iloc[0].to_dict()
        raw[key] = pd.DataFrame([{**base, 'trade_date': d} for d in dates])
    raw['dates'] = dates
    cfg.end = date(2025, 3, 4)
    raw['daily'].loc[raw['daily'].trade_date.eq('20250228'), 'amount20'] = 100000.
    raw['limits'].loc[raw['limits'].trade_date.eq('20250303'), 'down_limit'] = 10.
    targets['20250228'] = targets['20250131'].iloc[:0]
    fills = simulate(None, raw, targets, cfg)[4]
    sells = [f for f in fills if f['side'] == 'sell']
    assert len(sells) == 1 and sells[0]['date'] == '2025-03-04'
    assert sells[0]['quantity'] == 100


def test_dividend_is_receivable_then_spendable_cash_without_double_count():
    raw, targets, cfg = inputs()
    raw['dates'].append('20250205')
    for key in ('daily', 'limits', 'adj'):
        raw[key] = pd.concat([raw[key], raw[key].iloc[[-1]].assign(trade_date='20250205')], ignore_index=True)
    raw['daily'].loc[raw['daily'].trade_date.eq('20250205'), ['open', 'close']] = 9.
    raw['adj'].loc[raw['adj'].trade_date.eq('20250205'), 'adj_factor'] = 10/9
    cfg.end = date(2025, 2, 5)
    raw['dividends'] = pd.DataFrame([{'ts_code':'600001.SH', 'div_proc':'实施',
        'record_date':'20250204', 'ex_date':'20250205', 'pay_date':'20250205',
        'div_listdate':None, 'cash_div_tax':1., 'stk_div':0., 'stk_bo_rate':0.}])
    _stats, curve, _, _, fills, _journal = simulate(None, raw, targets, cfg)
    qty = fills[0]['quantity']
    assert abs(curve[-1]['cash'] - curve[-2]['cash'] - qty*.8) < 1e-6
    assert curve[-1]['receivable'] == 0
    assert abs(curve[-1]['value'] - curve[-2]['value'] + qty*.2) < 1e-6


def test_symbol_summary_weights_partial_exits_and_excludes_open_win():
    from app.backtest.graham_monthly import summarize_trades

    rows = [
        dict(symbol='X', entry_value=100., pnl_amount=50., pnl_pct=.5, exit_reason='rebalance'),
        dict(symbol='X', entry_value=300., pnl_amount=-30., pnl_pct=-.1, exit_reason='removed'),
        dict(symbol='X', entry_value=100., pnl_amount=100., pnl_pct=1., exit_reason='open_position'),
    ]
    assert summarize_trades(rows) == [dict(symbol='X', n_trades=2, total_return=.24,
                                           win_rate=.5, best=1., worst=-.1)]
