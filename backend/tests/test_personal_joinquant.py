from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from app.backtest.graham_monthly import simulate
from app.services.personal_joinquant import candidates, schedule


def sample():
    days = pd.bdate_range('2025-01-02',periods=17).strftime('%Y%m%d').tolist()
    codes=['600001.SH','600002.SH','600003.SH','600004.SH']
    prices=[]
    limits=[]
    adj=[]
    basic=[]
    for d in days:
        for i, code in enumerate(codes):
            prices.append({'ts_code':code,'trade_date':d,'open':10.,'close':10.,'high':11.,'low':9.,'vol':100.,'amount20':1e8})
            limits.append({'ts_code':code,'trade_date':d,'up_limit':11.,'down_limit':9.})
            adj.append({'ts_code':code,'trade_date':d,'adj_factor':1.})
            basic.append({'ts_code':code,'trade_date':d,'total_mv':200000.+i*10000})
    raw={'dates':days,'daily':pd.DataFrame(prices),'limits':pd.DataFrame(limits),'adj':pd.DataFrame(adj),
         'basic':pd.DataFrame(basic),'stocks':pd.DataFrame([{'ts_code':c,'list_date':'20200101','delist_date':pd.NA} for c in codes]),
         'names':pd.DataFrame([{'ts_code':c,'name':'ST样本' if i==0 else c,'start_date':'20200101','end_date':pd.NA,'ann_date':'20191231'} for i,c in enumerate(codes)]),
         'dividends':pd.DataFrame()}
    cfg=SimpleNamespace(start=date.fromisoformat(days[1]),end=date.fromisoformat(days[11]),initial_capital=1e6,
        commission_pct=.0003,fees_pct=.0003,stamp_tax_pct=.001,slippage_bps=10.,
        max_exposure_pct=1.,strategy_id='custom_joinquant_smallcap')
    return raw,days,codes,cfg


def plans(raw,days,cfg):
    return schedule(raw,str(cfg.start).replace('-',''),str(cfg.end).replace('-',''),days[1])


def test_exact_market_cap_band_no_extra_st_or_financial_filter():
    raw,days,codes,_=sample()
    raw['basic'].loc[raw['basic'].ts_code.eq(codes[3]),'total_mv']=300000
    rows=candidates(raw,days[0])
    assert rows.index.tolist()==codes
    assert rows.iloc[0]['name']=='ST样本'
    raw['basic'].loc[raw['basic'].ts_code.eq(codes[3]),'total_mv']=300000.1
    assert candidates(raw,days[0]).index.tolist()==codes[:3]


def test_every_five_sessions_uses_previous_day_and_stable_anchor():
    raw,days,_,cfg=sample()
    target=plans(raw,days,cfg)
    assert list(target)==[days[0],days[5],days[10]]
    cfg.start=date.fromisoformat(days[4])
    later=plans(raw,days,cfg)
    assert list(later)==[days[5],days[10]]
    pd.testing.assert_frame_equal(target[days[5]],later[days[5]])


def test_future_values_and_current_close_cannot_change_prior_candidates():
    raw,days,_,_=sample()
    before=candidates(raw,days[0])
    raw['basic'].loc[raw['basic'].trade_date.gt(days[0]),'total_mv']=9999999
    raw['daily'].loc[raw['daily'].trade_date.gt(days[0]),['high','low','close','vol']]=[999.,.1,500.,999999.]
    pd.testing.assert_frame_equal(before,candidates(raw,days[0]))


def test_cycle_sells_and_rebuys_unchanged_names_and_cash_is_conserved():
    raw,days,codes,cfg=sample()
    positions=[]
    stats,curve,_,trades,fills,journal=simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True,positions_log=positions)
    assert set(f['symbol'] for f in fills)==set(codes[:3])
    sells=[f for f in fills if f['side']=='sell']
    assert len(sells)==6 and {f['date'].replace('-','') for f in sells}=={days[6],days[11]}
    assert len([f for f in fills if f['side']=='buy'])==9
    assert all(t['record'].duration==5 for t in trades if t['record'].exit_reason!='open_position')
    for row in curve:
        holdings=[p for p in positions if p['date']==row['date']]
        value=sum((p['quantity']+p['pending_quantity'])*p['mark'] for p in holdings)
        assert row['value']==pytest.approx(row['cash']+row['receivable']+value)
        assert row['cash']>=0 and row['positions']<=3
    assert curve[-1]['cash']==pytest.approx(1e6+sum(j.get('cash_delta',0) for j in journal))
    assert stats['n_trades']==6


def test_open_pause_proxy_backfills_but_limit_up_does_not():
    raw,days,codes,cfg=sample()
    cfg.end=date.fromisoformat(days[1])
    raw['daily']=raw['daily'][~(raw['daily'].trade_date.eq(days[1]) & raw['daily'].ts_code.eq(codes[0]))]
    fills=simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True)[4]
    assert [f['symbol'] for f in fills]==codes[1:]
    raw,days,codes,cfg=sample()
    cfg.end=date.fromisoformat(days[1])
    raw['limits'].loc[raw['limits'].trade_date.eq(days[1]) & raw['limits'].ts_code.eq(codes[0]),'up_limit']=10.
    fills=simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True)[4]
    assert [f['symbol'] for f in fills]==codes[1:3]


def test_blocked_sale_keeps_slot_and_does_not_retry_next_day():
    raw,days,_codes,cfg=sample()
    cfg.end=date.fromisoformat(days[8])
    raw['limits'].loc[raw['limits'].trade_date.eq(days[6]),'down_limit']=10.
    rejections=[]
    stats,_,_,_,fills,_=simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True,order_log=rejections)
    assert len(fills)==3 and all(f['side']=='buy' for f in fills)
    assert stats['execution']['sell_limit_down']==3
    assert sum(r['reason']=='卖出后没有空名额' for r in rejections)==3


def test_full_history_and_truncated_history_have_same_past_fills():
    raw,days,_,cfg=sample()
    full=simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True)[4]
    cfg.end=date.fromisoformat(days[7])
    for key in ('daily','basic','adj','limits'):
        raw[key]=raw[key][raw[key].trade_date.le(days[7])]
    raw['dates']=raw['dates'][:8]
    past=simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True)[4]
    assert past==[f for f in full if f['date'].replace('-','')<=days[7]]


def test_unknown_prior_capacity_cannot_be_used_to_fill_buy():
    raw,days,_,cfg=sample()
    cfg.end=date.fromisoformat(days[1])
    raw['daily'].loc[raw['daily'].trade_date.eq(days[0]),'amount20']=float('nan')
    assert simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True)[4]==[]


def test_daily_high_low_close_volume_do_not_approve_or_size_roundtrip_orders():
    raw,days,_,cfg=sample()
    before=simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True)[4]
    raw['daily'].loc[raw['daily'].trade_date.ge(days[1]),['high','low','close','vol']]=[999.,.1,500.,999999.]
    after=simulate(None,raw,plans(raw,days,cfg),cfg,roundtrip=True)[4]
    assert before==after


def test_verified_adjustment_correction_requires_matching_original_value():
    from app.services.personal_joinquant import apply_adjustment_corrections
    raw={'adj':pd.DataFrame([{'ts_code':'X','trade_date':'20240625','adj_factor':1.0322}])}
    correction=pd.DataFrame([{'ts_code':'X','trade_date':'20240625','old_factor':1.0322,'new_factor':1.032}])
    assert apply_adjustment_corrections(raw,correction)['adj'].adj_factor.iloc[0]==1.032
    with pytest.raises(ValueError,match='原始值不匹配'):
        apply_adjustment_corrections(raw,correction)
