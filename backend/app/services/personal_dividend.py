"""本地红利增强：选股与回测共用纯函数；严格原版与日频研究版分开。

日频版的本地因子定义写入SPEC，不能声称与聚宽平台因子等价。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from itertools import pairwise
from pathlib import Path

import numpy as np
import pandas as pd

from app.services.personal_graham import _frame

STRICT_ERROR = ('严格原版暂不能回测：缺少11:00涨停打开及511880成交数据、'
                '经对拍的聚宽beta和单季度财务口径；不得用日线高低或普通ROE冒充。'
                '如研究核心月调，请明确选择另一个“日频本地口径”策略。')
SPEC = {
    'version': 'dividend_daily_local_v1',
    'signal': '完整交易日历的月末收盘，下一交易日开盘执行；不在区间首日补造月调',
    'universe': '历史SH/SZ普通A股；排除68开头；信号日上市满250自然日；历史名称排除ST/*/退；当日有有效收盘与成交',
    'yield': '过去365自然日内已除息、已公告实施的每股税前现金红利×登记日历史总股本，再除以信号日总市值；单位同为万元',
    'low_beta': '股息率降序前floor(N*0.1)，再要求>3%；252市场交易日对沪深300的复权日收益OLS beta升序前floor(N*0.5)，再取1或4只',
    'value': '本地PE_TTM在[8,50]，已公告累计扣非ROE在[5,100]，累计总营收同比在[5,100]、累计净利润同比在[10,100]；其后按股息率同样选前10%且>3%，取4或1只',
    'regime': '沪深300已完成月线TA-Lib MACD(12,26,9)，hist>0时低beta1+价值4，否则4+1；不足34个月报错',
    'allocation': '两路去重、以路线次序再代码确定订单顺序；仅新票按调仓前净资产/去重目标数分配，保留旧仓不重新等权；不足现金减量、不融资',
    'missing_policy': '同一已知分红事件存在不同金额时，整只股票在该信号日记缺失排除，输出data_quality.json；不挑版本、不取最大、不双算。登记日股本缺失时同样整只股票记缺失排除，输出明细；整表或必要字段缺失则中止。',
    'daily_variant': '关闭原版昨日涨停保护、11:00开板交易和511880防御ETF分支，未投资金留现金；原版默认关闭的止损/动量/三年分红/熊市降仓保持关闭',
    'financial_time': '公告日期严格早于信号日；每期先取当时可见版本，再取最新报告期；不以最新值回填',
    'execution': '复用Panel月调共用现金与权益账本；原始价成交；开盘在涨跌停价不假定成交；仅月调下单，买单不追买、旧失败卖单不跨日追卖；期末只估值',
    'costs': 'Panel显式佣金/滑点；最低佣金5元、过户费和卖出印花税按现有月调路径；红利永久预留20%税为保守研究情景',
    'original_equivalent': False,
}
WARNINGS = [
    '日频本地口径研究版，非聚宽原版等价复现；11点开板和防御ETF分支已明确关闭。',
    'Beta固定为252日对沪深300OLS；财务用本地累计扣非ROE和同比；未证明与平台beta/单季度指标等价，禁止跨口径对比收益。',
    '历史财报原始版本完整性、历史ST与退市整理覆盖未完全验证；研究窗口不能称为真正样本外。',
    '股息率按已除息事件回溯365日（原版按登记日），以登记日历史股本估算派息总额，尚未核对多股类分红基数；现金分红永久预留20%税，与券商最终税账不同。',
    '日线不能还原开盘排队与市场冲击；已知退市持仓或无法解释的除权会中止，不能自动消失。',
]


def latest_metrics(frame, day):
    required = {'ts_code','ann_date','end_date','roe_dt','tr_yoy','netprofit_yoy'}
    if not required <= set(frame.columns):
        raise ValueError('本地财务缺少扣非ROE/总营收同比/净利润同比，不用普通ROE或营业收入替代')
    f = frame[frame.ann_date.lt(day) & frame.ann_date.ge(frame.end_date)].drop_duplicates().copy()
    if f.duplicated(['ts_code','end_date','ann_date']).any():
        raise ValueError('财务同次公告存在冲突版本')
    f = f.sort_values(['ann_date','end_date']).drop_duplicates(['ts_code','end_date'],keep='last')
    return f.sort_values(['end_date','ann_date']).drop_duplicates('ts_code',keep='last').set_index('ts_code')


def dividend_yield(events, basic, day, diagnostics=None):
    lower = (pd.Timestamp(day)-pd.Timedelta(days=365)).strftime('%Y%m%d')
    e = events[events.div_proc.eq('实施') & events.ex_date.between(lower,day) & events.record_date.le(day)].copy()
    # 实施公告比预案更重要；只有日期时从下一交易日可用。
    known = pd.concat([e.imp_ann_date,e.ann_date],axis=1).max(axis=1)
    e = e[known.lt(day) & e.cash_div_tax.gt(0)]
    fields = ['ts_code','record_date','ex_date','cash_div_tax']
    e = e[fields].drop_duplicates()
    conflict = e.duplicated(['ts_code','record_date','ex_date'],keep=False)
    excluded = set(e.loc[conflict,'ts_code'])
    if excluded and diagnostics is None:
        raise ValueError('同一分红登记事件金额冲突')
    if diagnostics is not None:
        diagnostics.extend({'date':day,'symbol':code,'reason':'同一登记/除息日存在不同派息金额，不能判断合并/重复/修订，整只股票本次缺失排除'} for code in sorted(excluded))
    e = e[~e.ts_code.isin(excluded)]
    shares = basic.loc[basic.trade_date.isin(e.record_date), ['ts_code','trade_date','total_share']].rename(
        columns={'trade_date':'record_date'})
    e = e.merge(shares,on=['ts_code','record_date'],how='left',validate='many_to_one')
    missing = e.total_share.isna() | e.total_share.le(0)
    bad_shares = set(e.loc[missing,'ts_code'])
    if bad_shares and diagnostics is None:
        raise ValueError('分红登记日股本缺失，未用最新股本回填')
    if diagnostics is not None:
        diagnostics.extend({'date':day,'symbol':code,'reason':'分红登记日历史股本缺失，整只股票本次缺失排除；不用最新股本回填'} for code in sorted(bad_shares))
    e = e[~e.ts_code.isin(bad_shares)]
    e['cash_wan'] = e.cash_div_tax * e.total_share
    cap = basic[basic.trade_date.eq(day)].set_index('ts_code').total_mv
    result = e.groupby('ts_code').cash_wan.sum()/cap
    return result.where(cap.gt(0)).dropna()


def monthly_histogram(index, calendar, day):
    # 由后继交易日确认月末，不能把文件尾或截断日期误当作完整月线。
    source = index[index.ts_code.eq('000300.SH')]
    if source.empty:
        raise ValueError('缺少沪深300历史指数')
    first = source.trade_date.min()
    month_first = min(d for d in calendar if d[:6] == first[:6])
    ends = {d for d,nxt in pairwise(calendar) if d[:6] != nxt[:6] and d <= day
            and (d >= first if first == month_first else d[:6] > first[:6])}
    f = source[source.trade_date.isin(ends)].sort_values('trade_date')
    if len(f) < 34 or set(f.trade_date) != ends:
        raise ValueError('沪深300完整月线不足34个月或月末行情缺失，不能默认切换防御模式')
    values = f.close.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('沪深300月线存在无效价格')
    # 与TA-Lib MACD默认SMA播种对齐：两条EMA从第26个样本对齐起算。
    fast, slow = values[14:26].mean(), values[:26].mean()
    dif = [fast-slow]
    for value in values[26:]:
        fast += (value-fast)*2/13
        slow += (value-slow)*2/27
        dif.append(fast-slow)
    dea = float(np.mean(dif[:9]))
    for value in dif[9:]:
        dea += (value-dea)*2/10
    hist = dif[-1]-dea
    if not np.isfinite(hist):
        raise ValueError('月线MACD未完成预热')
    return float(hist*2)


def local_beta(raw, day, codes):
    dates = [d for d in raw['dates'] if d <= day][-253:]
    if len(dates) != 253:
        raise ValueError('Beta需要253个市场交易日价格，不足时不得跳过筛选')
    index = raw['index']
    m = index[index.ts_code.eq('000300.SH')].set_index('trade_date').close.reindex(dates)
    x = m.pct_change(fill_method=None).iloc[1:].to_numpy()
    if not np.isfinite(x).all() or np.var(x) <= 0:
        raise ValueError('Beta基准缺失或方差无效')
    f = raw['daily'][raw['daily'].trade_date.isin(dates) & raw['daily'].ts_code.isin(codes)]
    f = f[['ts_code','trade_date','close']].merge(raw['adj'].loc[raw['adj'].trade_date.isin(dates) & raw['adj'].ts_code.isin(codes), ['ts_code','trade_date','adj_factor']],
        on=['ts_code','trade_date'],how='left',validate='one_to_one')
    f['adjusted'] = f.close*f.adj_factor
    # 信号日以前的累计因子只用于收益比率；不以未来复权锚点修改历史股数。
    prices = f.pivot(index='trade_date',columns='ts_code',values='adjusted').reindex(dates)
    returns = prices.pct_change(fill_method=None).iloc[1:]
    y = returns.to_numpy()
    valid = np.isfinite(prices.to_numpy()).all(axis=0) & (prices.to_numpy()>0).all(axis=0)
    result = ((x-x.mean())[:,None]*(y-y.mean(axis=0))).mean(axis=0)/np.var(x)
    return pd.Series(np.where(valid,result,np.nan),index=returns.columns)


def route_targets(frame, bull):
    frame = frame.rename_axis('ts_code')
    counts = (1,4) if bull else (4,1)
    def high_yield(f):
        f = f[np.isfinite(f.dividend_yield)].sort_values(['dividend_yield','symbol'],ascending=[False,True])
        f = f.iloc[:int(len(f)*.1)]
        return f[f.dividend_yield.gt(.03)]
    low = high_yield(frame)
    low = low[np.isfinite(low.beta)].sort_values(['beta','symbol']).iloc[:int(np.isfinite(low.beta).sum()*.5)]
    low = low.head(counts[0]).assign(route='low_beta')
    value = high_yield(frame[frame.value_eligible]).head(counts[1]).assign(route='value')
    rows = pd.concat([low,value]).copy()
    # 原版set的迭代顺序不稳定；固定低beta路线优先，再按各路线排名，避免现金短缺时随机收益。
    if rows.empty:
        return rows.assign(rank=pd.Series(dtype=int),score=pd.Series(dtype=float))
    routes = rows.groupby('symbol',sort=False).route.agg('|'.join)
    rows = rows.drop_duplicates('symbol',keep='first')
    rows['route'] = routes.reindex(rows.index)
    rows['rank'] = np.arange(1,len(rows)+1)
    rows['score'] = 100.-rows['rank']
    return rows


def candidates(raw, day, pool=None):
    if day not in raw['dates']:
        raise ValueError('目标日不在交易所日历内')
    daily = raw['daily'][raw['daily'].trade_date.eq(day)].set_index('ts_code')
    basic = raw['basic'][raw['basic'].trade_date.eq(day)].set_index('ts_code')
    frame = daily.join(basic[['total_mv','pe_ttm']],how='inner')
    stocks = raw['stocks'].drop_duplicates('ts_code').set_index('ts_code')
    frame = frame.join(stocks[['list_date','delist_date']],how='left')
    names = raw['names']
    names = names[names.start_date.le(day) & (names.end_date.isna() | names.end_date.ge(day))]
    names = names[names.ann_date.isna() | names.ann_date.le(day)]
    frame['name'] = names.sort_values('start_date').drop_duplicates('ts_code',keep='last').set_index('ts_code').name.reindex(frame.index)
    oldest = (pd.Timestamp(day)-pd.Timedelta(days=250)).strftime('%Y%m%d')
    valid = (frame.index.str.match(r'^(60\d{4}\.SH|[03]\d{5}\.SZ)$')
             & frame.list_date.le(oldest) & (frame.delist_date.isna() | frame.delist_date.gt(day))
             & frame.name.notna() & ~frame.name.str.contains(r'ST|\*|退',case=False,na=True)
             & frame.close.gt(0) & frame.vol.gt(0) & frame.total_mv.gt(0))
    frame = frame[valid].copy()
    if pool is not None:
        frame = frame[frame.index.isin(pool)]
    frame['symbol'] = frame.index
    frame['date'] = day
    quality = []
    frame['dividend_yield'] = dividend_yield(raw['dividends'][raw['dividends'].ts_code.isin(frame.index)],raw['basic'],day,quality).reindex(frame.index)
    metrics = latest_metrics(raw['metrics'],day).reindex(frame.index)
    for key in ('roe_dt','tr_yoy','netprofit_yoy','end_date','ann_date'):
        frame[key] = metrics[key]
    frame['value_eligible'] = (frame.pe_ttm.between(8,50) & frame.roe_dt.between(5,100)
        & frame.tr_yoy.between(5,100) & frame.netprofit_yoy.between(10,100))
    ranked = frame[frame.dividend_yield.notna()].sort_values(['dividend_yield','symbol'],ascending=[False,True])
    beta_codes = ranked.iloc[:int(len(ranked)*.1)].index.tolist()
    frame['beta'] = local_beta(raw,day,beta_codes).reindex(frame.index) if beta_codes else np.nan
    histogram = monthly_histogram(raw['index'],raw['dates'],day)
    selected = route_targets(frame,histogram>0)
    selected['macd_hist'] = histogram
    selected['market_cap'] = selected.total_mv*10000
    selected['report_period'] = selected.end_date
    selected['announce_date'] = selected.ann_date
    selected.attrs['data_quality'] = quality
    selected.attrs['coverage'] = {'date':day,'universe':len(frame),'known_dividend':int(frame.dividend_yield.notna().sum()),'valid_beta':int(frame.beta.notna().sum()),'value_eligible':int(frame.value_eligible.sum()),'selected':len(selected)}
    return selected


def schedule(raw,start,end):
    # 以执行日是否落在区间为准，支持从每月首交易日开始的回测。
    return {d:candidates(raw,d) for d,nxt in pairwise(raw['dates'])
            if d[:6] != nxt[:6] and start <= nxt <= end}


def snapshot(row):
    keys = ['date','symbol','name','rank','score','route','dividend_yield','beta','macd_hist',
            'pe_ttm','roe_dt','tr_yoy','netprofit_yoy','market_cap','report_period','announce_date']
    return {key:None if pd.isna(row.get(key)) else row.get(key) for key in keys}


def load_inputs(data_dir):
    # 复用已安装的只读快照指针，新增指数/指标仍由显式安装验证。
    from app.services.personal_graham import load_inputs as load_common
    config = Path(data_dir)/'dividend_inputs/sources.json'
    if not config.is_file():
        raise ValueError('红利输入未安装，请运行 scripts/install_dividend.py')
    spec = json.loads(config.read_text())
    common = load_common(data_dir)
    raw = dict(common)
    raw['signature'] = list(common.get('signature',()))
    for key,value in spec['paths'].items():
        path = Path(value)
        stat = path.stat()
        raw['signature'].append((key,str(path),stat.st_mtime_ns,stat.st_size))
        raw[key] = _frame(path)
        raw['input_hashes'] = {**raw['input_hashes'],key:hashlib.sha256(path.read_bytes()).hexdigest()}
    if 'index_supplement' in raw:
        supplement = raw.pop('index_supplement')
        merged = pd.concat([raw['index'],supplement],ignore_index=True).drop_duplicates()
        if merged.duplicated(['ts_code','trade_date']).any():
            raise ValueError('指数补录与原输入存在冲突，未自动挑选版本')
        raw['index'] = merged
    lower = spec['dividend_coverage_start']
    upper = spec['dividend_coverage_end']
    raw['dividend_coverage'] = (lower,upper)
    raw['source_audit'] = spec.get('source_audit', {})
    return raw


def validate_overrides(overrides,strategy):
    defaults = {'basic_filter':strategy.basic_filter,'scoring_replace':True,
                'entry_signals':strategy.entry_signals,'exit_signals':strategy.exit_signals}
    blocked = {k:v for k,v in (overrides or {}).items()
               if v is not None and v != [] and v != {} and k != 'params' and v != defaults.get(k)}
    if blocked or (overrides or {}).get('params'):
        raise ValueError('红利首轮不支持额外信号、叠加条件或风控覆盖，请恢复默认：'+','.join(blocked))


def scan(data_dir,strategy_id,variant,day,params,pool,overrides=None,strategy=None):
    from app.strategy.engine import StrategyResult
    if variant == 'strict':
        raise ValueError(STRICT_ERROR)
    if params or (overrides and strategy is None):
        raise ValueError('本轮红利规则已冻结，不接收额外参数或筛选覆盖')
    if strategy is not None:
        validate_overrides(overrides,strategy)
    raw = load_inputs(data_dir)
    text = str(day).replace('-','')
    validate_coverage(raw,text,text)
    rows = candidates(raw,text,pool)
    records = [snapshot(row) for _,row in rows.iterrows()]
    return StrategyResult(as_of=day,strategy_id=strategy_id,rows=records,total=len(records),
        scores={r['symbol']:float(r['score']) for r in records})


def validate_coverage(raw,start,end):
    lower,upper = raw['dividend_coverage']
    needed = (pd.Timestamp(start)-pd.Timedelta(days=365)).strftime('%Y%m%d')
    if needed < lower or end > upper:
        raise ValueError(f'股息率需要完整365日分红覆盖；当前除息日覆盖声明{lower}至{upper}，不得把缺历史当零分红')
    if end > raw['daily'].trade_date.max():
        raise ValueError('请求区间超出本地日线覆盖')


def run_backtest(service,config,strategy,progress_cb=None,cancel_event=None,result_policy=None):
    from app.backtest.graham_monthly import simulate
    from app.backtest.strategy import StrategyBacktestResult
    from app.services.fs_utils import atomic_write_text
    started = time.perf_counter()
    run_id = uuid.uuid4().hex[:10]
    base = service._config_to_dict(config)
    try:
        if strategy.meta['personal_dividend_variant'] == 'strict':
            raise ValueError(STRICT_ERROR)
        if config.asset_type != 'stock' or config.mode != 'position' or config.minute_fill:
            raise ValueError('红利日频版仅支持股票、共用资金、关闭分钟细化')
        if config.entry_fill != 'open_t+1' or config.exit_fill != 'open_t+1':
            raise ValueError('红利日频版仅支持次日开盘买卖')
        if config.symbols or config.regime_filter or config.params:
            raise ValueError('红利首轮使用完整冻结股票池和默认规则，清空代码范围、额外环境过滤及参数')
        validate_overrides(config.overrides,strategy)
        if config.max_positions != 5 or config.position_sizing != 'equal':
            raise ValueError('红利两路最多5只；请设置最大持仓5、等权分配（仅新票分配）')
        if not np.isfinite(config.initial_capital) or config.initial_capital <= 0 or not 0 < config.max_exposure_pct <= 1:
            raise ValueError('本金或仓位上限无效')
        if result_policy and result_policy.required_stats is not None:
            raise ValueError('红利首轮不进入自动寻优')
        raw = load_inputs(service.engine.repo.store.data_dir)
        start,end = str(config.start).replace('-',''),str(config.end).replace('-','')
        execution_pairs = [(d,n) for d,n in pairwise(raw['dates']) if d[:6]!=n[:6] and start<=n<=end]
        if not execution_pairs:
            raise ValueError('区间内没有月度调仓执行日，未生成空账户收益')
        validate_coverage(raw,execution_pairs[0][0],end)
        targets = schedule(raw,start,end)
        coverage = [f.attrs.get('coverage',{}) for f in targets.values()]
        quality = [row for f in targets.values() for row in f.attrs.get('data_quality',[])]
        orders,positions = [],[]
        code_paths = [Path(__file__),Path(__file__).with_name('personal_graham.py'),
            Path(__file__).parents[1]/'backtest/graham_monthly.py',
            Path(__file__).parents[1]/'backtest/graham_distributions.py',
            Path(__file__).parents[1]/'backtest/engine.py',
            Path(__file__).parents[1]/'strategy/personal/dividend_enhanced.py']
        code_hashes = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in code_paths}
        stats,curve,dd,trades,fills,journal = simulate(service.engine,raw,targets,config,progress_cb,cancel_event,
            dividend_core=True,attribution=snapshot,order_log=orders,positions_log=positions)
        for _,name,mtime,size in raw['signature']:
            stat = Path(name).stat()
            if (stat.st_mtime_ns,stat.st_size) != (mtime,size):
                raise ValueError('运行中输入被修改，结果未发布：'+name)
        if code_hashes != {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in code_paths}:
            raise ValueError('运行中代码被修改，结果未发布')
        reconciliation = reconcile(config.initial_capital,curve,journal,positions)
        records = [{**service._trade_to_dict(x['record']),**{k:v for k,v in x.items() if k!='record'}} for x in trades]
        stats.update(research_warnings=WARNINGS,execution_backend='panel_monthly_targets_raw',
            signal_version=SPEC['version'],input_sha256=raw['input_hashes'],original_equivalent=False)
        output = service.engine.repo.store.data_dir/'research/dividend'/run_id
        output.mkdir(parents=True,exist_ok=False)
        atomic_write_text(output/'resolved_spec.json',json.dumps(SPEC,ensure_ascii=False,indent=2))
        for filename,data in [('fills',fills),('cash_journal',journal),('equity_daily',curve),
                ('orders', orders+[{**f, 'status':'filled'} for f in fills]), ('positions_daily',positions),
                ('validation',reconciliation),('data_quality',quality),('coverage',coverage),
                ('ranked_targets',[snapshot(r) for f in targets.values() for _,r in f.iterrows()])]:
            atomic_write_text(output/(filename+'.json'),json.dumps(data,ensure_ascii=False,allow_nan=False,default=str))
        atomic_write_text(output/'manifest.json',json.dumps({'run_id':run_id,'config':base,
            'input_sha256':raw['input_hashes'],'warnings':WARNINGS,'spec':SPEC,'source_audit':raw['source_audit'],
            'code_sha256':code_hashes,
            'status':'research_only_not_original_equivalent'},ensure_ascii=False,indent=2))
        stats['artifact_dir'] = str(output)
        stats['account_reconciliation'] = reconciliation
        stats['data_quality_exclusions'] = len(quality)
        stats['signal_coverage'] = coverage
        benchmark = service._build_benchmark_curve(config.start,config.end)
        report(output/'report.html',base,stats,curve,dd,records,orders)
        pd.DataFrame(records).to_csv(output/'trades.csv',index=False)
        for name,rows in [('fills',fills),('cash_ledger',journal),('positions_daily',positions),
                          ('equity_daily',curve),('candidates',[snapshot(r) for f in targets.values() for _,r in f.iterrows()])]:
            if rows:
                pd.DataFrame(rows).to_parquet(output/(name+'.parquet'),index=False)
        info = {'id':config.strategy_id,'name':strategy.meta['name'],'description':strategy.meta['description'],
            'source':strategy.source,'entry_signals':strategy.entry_signals,'exit_signals':strategy.exit_signals,
            'execution_backend':'panel_monthly_targets_raw'}
        return StrategyBacktestResult(run_id=run_id,config=base,stats=stats,equity_curve=curve,drawdown_curve=dd,
            trades=records,per_symbol_stats=[],strategy_info=info,
            benchmark_curve=benchmark,
            elapsed_ms=(time.perf_counter()-started)*1000)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        return StrategyBacktestResult(run_id=run_id,config=base,error='红利研究未完成：'+str(exc),
            elapsed_ms=(time.perf_counter()-started)*1000)


def reconcile(initial,curve,journal,positions):
    flows = pd.DataFrame(journal)
    marks = pd.DataFrame(positions)
    maximum = 0.
    cash = float(initial)
    claims = 0.
    for row in curve:
        day = row['date'].replace('-','')
        if len(flows):
            events = flows[flows.trade_date.eq(day)]
            cash += events.cash_delta.fillna(0).sum()
            claims += events.get('receivable_delta',pd.Series(dtype=float)).fillna(0).sum()
        held = marks[marks.date.eq(row['date'])] if len(marks) else marks
        value = ((held.quantity+held.pending_quantity)*held.mark).sum() if len(held) else 0.
        error = max(abs(cash-row['cash']),abs(claims-row['receivable']),abs(cash+claims+value-row['value']))
        maximum = max(maximum,float(error))
        if error > max(1e-6,abs(row['value'])*1e-10):
            raise ValueError('资金账重建不一致：'+row['date'])
    return {'passed':True,'daily_rows':len(curve),'max_absolute_error':maximum}


def report(path,config,stats,curve,dd,trades,rejections):
    import html
    def table(rows):
        return pd.DataFrame(rows).to_html(index=False,escape=True) if rows else '<p>无记录</p>'
    def line(rows,color):
        values = [float(r['value']) for r in rows]
        low,high = min(values),max(values)
        points = ' '.join(f'{30+940*i/max(1,len(values)-1):.2f},{210-180*(v-low)/max(1e-12,high-low):.2f}' for i,v in enumerate(values))
        return f'<svg viewBox="0 0 1000 240" role="img"><title>范围{low:.4f}至{high:.4f}</title><text x="30" y="20">最高 {high:,.4f}</text><text x="30" y="235">最低 {low:,.4f}</text><polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/></svg>'
    closed = [t for t in trades if t['exit_reason']!='open_position']
    metrics = [{'总收益':stats.get('total_return'),'最大回撤':stats.get('max_drawdown'),
                '已平仓批次':len(closed),'期末未平仓':stats.get('unrealized_positions'),
                '费用':stats.get('fees_total')}]
    text = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>红利增强日频本地研究</title><style>body{{font:16px/1.7 system-ui;background:#f5f6f7;color:#222;margin:0}}main{{max-width:1080px;margin:auto;padding:24px}}section{{background:white;padding:20px;margin:18px 0}}.scroll{{overflow:auto}}table{{border-collapse:collapse;white-space:nowrap}}td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}svg{{width:100%;height:auto}}pre{{white-space:pre-wrap;word-break:break-word}}small{{color:#555}}</style><main>
    <h1>红利增强：日频本地口径研究</h1><p>{html.escape(str(config.get('start')))} 至 {html.escape(str(config.get('end')))}。独立版本，不能称为聚宽等价复现或养老金安全证明。</p>
    <section><h2>本次结果</h2><div class="scroll">{table(metrics)}</div><p>资金账逐日重建：{html.escape(str(stats['account_reconciliation']))}。短窗口只验证工程接线，不用于推算长期收益。</p></section>
    <section><h2>账户净值</h2>{line(curve,'#527aa3')}<small>{curve[0]['date']} 至 {curve[-1]['date']}；人民币，含费用及权益应收。</small><h2>回撤</h2>{line(dd,'#c98263')}</section>
    <section><h2>冻结规则及差异</h2>{''.join('<p><b>'+html.escape(k)+'</b>：'+html.escape(str(v))+'</p>' for k,v in SPEC.items())}</section>
    <section><h2>限制</h2>{''.join('<p>'+html.escape(w)+'</p>' for w in WARNINGS)}</section>
    <section><h2>逐笔及期末持仓</h2><div class="scroll">{table([{k:t.get(k) for k in ['symbol','name','entry_signal_date','entry_date','exit_signal_date','exit_date','shares','entry_price','exit_price','pnl_amount','exit_reason']} for t in trades])}</div></section>
    <section><h2>拒单</h2><div class="scroll">{table(rejections)}</div></section>
    <section><h2>原始配置</h2><pre>{html.escape(json.dumps(config,ensure_ascii=False,indent=2))}</pre><p>完整候选、成交、资金和持仓明细与输入哈希在同目录；所有结果为回顾性研究。</p></section></main></html>'''
    path.write_text(text,encoding='utf-8')


def install(data_dir,original_source=None,index_supplement=None):
    """显式安装独立策略与只读输入指针；先检查全部目标再写，不覆盖人工修改。"""
    from app.strategy.engine import StrategyEngine
    data_dir = Path(data_dir)
    common = data_dir/'graham_inputs/sources.json'
    if not common.is_file():
        raise ValueError('请先配置已有本地月调输入 graham_inputs/sources.json；不自动下载')
    paths = {k:Path(v) for k,v in json.loads(common.read_text())['paths'].items()}
    root = paths['basic'].parent
    index,metrics = root/'index_daily.parquet',root/'fina_indicator.parquet'
    dividend_manifest = paths['dividends'].with_name('dividend_ingestion_manifest.json')
    for path in (index,metrics,dividend_manifest):
        if not path.is_file():
            raise ValueError('缺少本地输入：'+str(path))
    manifest = json.loads(dividend_manifest.read_text())
    digest = hashlib.sha256(paths['dividends'].read_bytes()).hexdigest()
    if manifest.get('sha256') != digest or not manifest.get('start') or not manifest.get('end'):
        raise ValueError('分红输入与采集清单哈希或覆盖区间不匹配')
    source = Path(__file__).resolve().parents[1]/'strategy/personal'
    targets = {}
    for filename,strategy_id in [('dividend_enhanced.py','custom_dividend_enhanced_daily_local'),
                                 ('dividend_enhanced_strict.py','custom_dividend_enhanced_strict')]:
        path = source/filename
        StrategyEngine._load_file(path)
        targets[data_dir/'strategies/custom'/(strategy_id+'.py')] = path.read_text()
    audit = {'dividend_ingestion_manifest_sha256':hashlib.sha256(dividend_manifest.read_bytes()).hexdigest(),
             'dividend_query':manifest.get('query'),
             'coverage_limit':'采集按本地复权变动日期查询除息日；未验证供应商遗漏或多股类分红基数。'}
    if original_source:
        audit['original_source_sha256'] = hashlib.sha256(Path(original_source).read_bytes()).hexdigest()
    extra_paths = {'index':str(index.resolve()),'metrics':str(metrics.resolve())}
    if index_supplement:
        supplement = Path(index_supplement).resolve()
        if not supplement.is_file():
            raise ValueError('显式指定的本地指数补录不存在：'+str(supplement))
        extra_paths['index_supplement'] = str(supplement)
        audit['index_supplement_sha256'] = hashlib.sha256(supplement.read_bytes()).hexdigest()
        audit['index_supplement_policy'] = '显式本地采集文件，只补实际日期；同键冲突中止；不插值、不前填'
    spec = {'version':1,'paths':extra_paths,
            'dividend_coverage_start':manifest['start'],'dividend_coverage_end':manifest['end'],
            'source_audit':audit}
    targets[data_dir/'dividend_inputs/sources.json'] = json.dumps(spec,ensure_ascii=False,indent=2)+'\n'
    targets[data_dir/'dividend_inputs/resolved_spec.json'] = json.dumps(SPEC,ensure_ascii=False,indent=2)+'\n'
    for target,text in targets.items():
        if target.exists() and target.read_text()!=text:
            raise ValueError('已有不同内容，未覆盖：'+str(target))
    for target,text in targets.items():
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            with target.open('x') as stream:
                stream.write(text)
    return {'installed':['custom_dividend_enhanced_daily_local','custom_dividend_enhanced_strict'],
            'production_ready':False,'original_equivalent':False,'data_dir':str(data_dir)}
