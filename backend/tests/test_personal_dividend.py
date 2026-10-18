from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.services.personal_dividend import (
    dividend_yield,
    latest_metrics,
    monthly_histogram,
    route_targets,
)


def test_route_order_is_beta_then_yield_not_volatility_or_equal_baskets():
    # Top 10% has ten names; low beta chooses within that set, then truncates.
    f = pd.DataFrame({'symbol': [f'{i:06d}.SH' for i in range(100)],
                      'dividend_yield': np.linspace(.2, .04, 100),
                      'beta': np.arange(100)[::-1], 'value_eligible': True}).set_index('symbol', drop=False)
    a = route_targets(f, True)
    assert a[a.route.str.contains('low_beta')].symbol.tolist() == ['000009.SH']
    assert a[a.route.str.contains('value')].symbol.tolist() == [f'{i:06d}.SH' for i in range(4)]
    assert len(a) == 5
    b = route_targets(f, False)
    assert b[b.route.str.contains('low_beta')].symbol.tolist() == [f'{i:06d}.SH' for i in (9, 8, 7, 6)]


def test_route_floor_cutoffs_and_missing_beta_do_not_relax_filter():
    f = pd.DataFrame({'symbol': ['A','B','C','D','E','F','G','H','I'],
                      'dividend_yield': .1, 'beta': 1., 'value_eligible': True}).set_index('symbol', drop=False)
    assert route_targets(f, True).empty  # int(.1*9) = 0, no minimum-one shortcut
    f = pd.concat([f, pd.DataFrame([{'symbol':'J','dividend_yield':.1,'beta':np.nan,'value_eligible':True}]).set_index('symbol', drop=False)])
    a = route_targets(f, False)
    assert a.route.tolist() == ['value']  # low-beta floor(1/2)=0


def test_dividend_amount_uses_event_shares_and_no_future_registration():
    events = pd.DataFrame([
        dict(ts_code='A', record_date='20240610', ex_date='20240611', pay_date='20240612',
             ann_date='20240501', imp_ann_date='20240601', div_proc='实施', cash_div_tax=1.),
        dict(ts_code='A', record_date='20250610', ex_date='20250611', pay_date='20250612',
             ann_date='20250501', imp_ann_date='20250601', div_proc='实施', cash_div_tax=50.),
    ])
    basic = pd.DataFrame([
        dict(ts_code='A', trade_date='20240610', total_share=100., total_mv=1000.),
        dict(ts_code='A', trade_date='20250530', total_share=200., total_mv=2000.),
    ])
    actual = dividend_yield(events, basic, '20250530')
    assert actual['A'] == .05  # 1元×100万股 / 2000万元, not latest 200万股
    shuffled = pd.concat([events.iloc[[0]], events.iloc[[0]], events.iloc[[1]]])
    assert dividend_yield(shuffled, basic, '20250530')['A'] == .05
    with pytest.raises(ValueError, match='登记日股本'):
        dividend_yield(events, basic.iloc[[1]], '20250530')


def test_metrics_strictly_after_announcement_latest_period_before_revision():
    f = pd.DataFrame([
        dict(ts_code='A', end_date='20231231', ann_date='20240301', roe_dt=5., tr_yoy=6., netprofit_yoy=10.),
        dict(ts_code='A', end_date='20240331', ann_date='20240420', roe_dt=7., tr_yoy=8., netprofit_yoy=12.),
        dict(ts_code='A', end_date='20231231', ann_date='20240501', roe_dt=99., tr_yoy=99., netprofit_yoy=99.),
    ])
    assert latest_metrics(f, '20240420').loc['A','roe_dt'] == 5.
    assert latest_metrics(f, '20240502').loc['A','roe_dt'] == 7.


def test_macd_uses_completed_months_and_refuses_short_warmup():
    days = pd.bdate_range('2020-01-01','2024-01-15')
    calendar = days.strftime('%Y%m%d').tolist()
    prices = pd.DataFrame({'ts_code':'000300.SH','trade_date':calendar,
                           'close': np.linspace(100,200,len(days))})
    with pytest.raises(ValueError, match='34'):
        monthly_histogram(prices, calendar, '20210129')
    a = monthly_histogram(prices, calendar, '20231229')
    prices.loc[prices.trade_date.gt('20231229'),'close'] = 1e9
    assert monthly_histogram(prices, calendar, '20231229') == a
    assert monthly_histogram(prices, calendar, '20240110') == a


def test_strict_original_refuses_unverifiable_platform_behavior():
    from types import SimpleNamespace

    from app.backtest.strategy import StrategyBacktestConfig
    from app.services.personal_dividend import run_backtest
    cfg = StrategyBacktestConfig('custom_dividend_enhanced_strict',None,date(2026,7,1),date(2026,10,13))
    service = SimpleNamespace(_config_to_dict=lambda c: {'strategy_id': c.strategy_id})
    strategy = SimpleNamespace(meta={'personal_dividend_variant':'strict'})
    result = run_backtest(service,cfg,strategy)
    assert '11:00' in result.error and result.equity_curve == []


def test_installable_files_use_real_strategy_loader():
    from pathlib import Path

    from app.strategy.engine import StrategyEngine
    root = Path(__file__).parents[1]/'app/strategy/personal'
    for name in ('dividend_enhanced.py','dividend_enhanced_strict.py'):
        strategy = StrategyEngine._load_file(root/name)
        assert strategy.meta['personal_dividend_variant'] in {'strict','daily_local'}
        assert strategy.stop_loss is None


def test_existing_holdings_are_not_reweighted_or_rebought():
    from types import SimpleNamespace

    from app.backtest.graham_monthly import simulate
    days = ['20250130','20250131','20250203','20250228','20250303','20250304']
    rows = [dict(ts_code=code,trade_date=day,open=price,close=price,vol=1000,amount20=1e9)
            for day in days for code,price in [('A',20. if day>='20250228' else 10.),('B',10.)]]
    daily = pd.DataFrame(rows)
    raw = {'daily':daily,'dates':days,
           'adj':daily[['ts_code','trade_date']].assign(adj_factor=1.),
           'limits':daily[['ts_code','trade_date']].assign(up_limit=100.,down_limit=1.),
           'stocks':pd.DataFrame({'ts_code':['A','B'],'delist_date':[pd.NA,pd.NA]}),
           'dividends':pd.DataFrame()}
    def target(day,codes):
        return pd.DataFrame([dict(symbol=c,name=c,date=day,score=100.,rank=i+1,amount20=1e9)
                             for i,c in enumerate(codes)]).set_index('symbol',drop=False)
    targets = {'20250131':target('20250131',['A']), '20250228':target('20250228',['A','B'])}
    cfg = SimpleNamespace(start=date(2025,1,30),end=date(2025,3,4),initial_capital=1e6,
        commission_pct=.0002,fees_pct=.0002,stamp_tax_pct=None,slippage_bps=0.,
        max_exposure_pct=1.,strategy_id='custom_dividend_enhanced_daily_local')
    a = simulate(None,raw,targets,cfg,dividend_core=True,attribution=lambda row:row.to_dict())
    assert [(f['date'],f['symbol'],f['side']) for f in a[4]] == [('2025-02-03','A','buy')]
    assert a[0]['n_trades'] == 0  # 不卖旧仓给新票凑等权
    assert all(r['cash']>=0 for r in a[1])
    raw['daily'].loc[raw['daily'].trade_date.ge('20250303'),'close'] = 1000.
    b = simulate(None,raw,targets,cfg,dividend_core=True,attribution=lambda row:row.to_dict())
    assert a[4] == b[4]  # 当日及未来收盘不反过来修改开盘订单


def test_ambiguous_dividend_is_missing_with_audited_exclusion_not_best_value():
    events = pd.DataFrame([dict(ts_code='A',record_date='20240610',ex_date='20240611',
        ann_date='20240501',imp_ann_date='20240601',div_proc='实施',cash_div_tax=v) for v in (1.,2.)])
    basic = pd.DataFrame([dict(ts_code='A',trade_date='20240610',total_share=100.,total_mv=1000.),
                          dict(ts_code='A',trade_date='20250530',total_share=100.,total_mv=1000.)])
    issues = []
    assert dividend_yield(events,basic,'20250530',issues).empty
    assert len(issues)==1 and issues[0]['symbol']=='A'
    with pytest.raises(ValueError,match='金额冲突'):
        dividend_yield(events,basic,'20250530')


def test_exdate_and_revision_announcement_gate_future_cash():
    events = pd.DataFrame([dict(ts_code='A',record_date='20250529',ex_date='20250602',
        ann_date='20250501',imp_ann_date='20250520',div_proc='实施',cash_div_tax=1.)])
    basic = pd.DataFrame([dict(ts_code='A',trade_date='20250529',total_share=100.,total_mv=1000.),
                          dict(ts_code='A',trade_date='20250530',total_share=100.,total_mv=1000.)])
    assert dividend_yield(events,basic,'20250530').empty
    events.loc[0,'ex_date'] = '20250530'
    events.loc[0,'ann_date'] = '20250603'
    assert dividend_yield(events,basic,'20250530').empty


def test_full_candidate_prefix_future_append_and_input_shuffle_are_identical():
    from app.services.personal_dividend import candidates
    calendar = pd.bdate_range('2020-01-01','2026-08-05').strftime('%Y%m%d').tolist()
    signal = '20260630'
    dates = [d for d in calendar if '20250501'<=d<=signal]
    symbols = [f'600{i:03d}.SH' for i in range(100)]
    daily = pd.DataFrame([dict(ts_code=c,trade_date=d,close=10.+i*.01+j*.01,
        open=10.+i*.01+j*.01,vol=1000.,amount20=1e8) for j,d in enumerate(dates) for i,c in enumerate(symbols)])
    raw = {'dates':calendar,'daily':daily,
           'adj':daily[['ts_code','trade_date']].assign(adj_factor=1.),
           'basic':daily[['ts_code','trade_date']].assign(total_mv=1000.,total_share=100.,pe_ttm=10.),
           'index':pd.DataFrame({'ts_code':'000300.SH','trade_date':calendar,
                                'close':100+np.arange(len(calendar))*.1+np.sin(np.arange(len(calendar))*.05)}),
           'stocks':pd.DataFrame({'ts_code':symbols,'list_date':'20100101','delist_date':pd.NA}),
           'names':pd.DataFrame({'ts_code':symbols,'start_date':'20100101','end_date':pd.NA,
                                'ann_date':'20100101','name':['企业'+c for c in symbols]}),
           'dividends':pd.DataFrame([dict(ts_code=c,record_date='20251201',ex_date='20251202',
               ann_date='20251001',imp_ann_date='20251115',div_proc='实施',cash_div_tax=2.-i*.01) for i,c in enumerate(symbols)]),
           'metrics':pd.DataFrame({'ts_code':symbols,'end_date':'20260331','ann_date':'20260420',
                                   'roe_dt':10.,'tr_yoy':20.,'netprofit_yoy':20.})}
    a = candidates(raw,signal)
    for key in ('daily','basic','adj','index','dividends','metrics','names'):
        raw[key] = raw[key].sample(frac=1,random_state=42)
    raw['index'].loc[raw['index'].trade_date.gt(signal),'close'] = 1e8
    revision = raw['metrics'].copy().assign(ann_date='20260715',roe_dt=-100.)
    raw['metrics'] = pd.concat([raw['metrics'],revision],ignore_index=True)
    future = raw['dividends'].copy().assign(ann_date='20260715',imp_ann_date='20260715',cash_div_tax=999.)
    raw['dividends'] = pd.concat([raw['dividends'],future],ignore_index=True)
    b = candidates(raw,signal)
    assert not a.empty
    pd.testing.assert_frame_equal(a,b)


def test_scan_cannot_silently_ignore_overrides():
    from app.services.personal_dividend import scan
    with pytest.raises(ValueError, match='覆盖'):
        scan(None,'custom_dividend_enhanced_daily_local','daily_local','20260630',None,None,{'stop_loss':-.05})


def test_explicit_index_supplement_rejects_conflicting_history(tmp_path, monkeypatch):
    import json

    from app.services import personal_dividend, personal_graham
    folder=tmp_path/'dividend_inputs'
    folder.mkdir()
    (folder/'sources.json').write_text(json.dumps({'paths':{'index':'index','index_supplement':'extra'},
        'dividend_coverage_start':'20240102','dividend_coverage_end':'20261016'}))
    frames={'index':pd.DataFrame([{'ts_code':'000300.SH','trade_date':'20260731','close':10.}]),
            'extra':pd.DataFrame([{'ts_code':'000300.SH','trade_date':'20260731','close':20.}])}
    for name in frames:
        (tmp_path/name).write_text(name)
    config=json.loads((folder/'sources.json').read_text())
    config['paths']={k:str(tmp_path/v) for k,v in config['paths'].items()}
    (folder/'sources.json').write_text(json.dumps(config))
    monkeypatch.setattr(personal_graham,'load_inputs',lambda _: {'input_hashes':{}})
    monkeypatch.setattr(personal_dividend,'_frame',lambda path:frames[path.name])
    with pytest.raises(ValueError, match=r'补录.*冲突'):
        personal_dividend.load_inputs(tmp_path)
    frames['extra'].loc[0,'close']=10.
    assert len(personal_dividend.load_inputs(tmp_path)['index'])==1



def test_default_ui_overrides_pass_and_changed_rules_fail():
    from types import SimpleNamespace

    from app.services.personal_dividend import validate_overrides
    strategy=SimpleNamespace(basic_filter={'enabled':False,'price_min':3},
        entry_signals=['dividend_selected'],exit_signals=['dividend_monthly_removed'])
    default={'basic_filter':strategy.basic_filter,'entry_signals':strategy.entry_signals,
             'exit_signals':strategy.exit_signals,'scoring_replace':True,'stop_loss':None,'scoring':{}}
    validate_overrides(default,strategy)
    with pytest.raises(ValueError, match='stop_loss'):
        validate_overrides({**default,'stop_loss':-.05},strategy)
    with pytest.raises(ValueError, match='basic_filter'):
        validate_overrides({**default,'basic_filter':{'enabled':True}},strategy)
