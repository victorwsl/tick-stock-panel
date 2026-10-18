"""聚宽20—30亿小市值日频复现：只消费冻结本地输入，不联网。"""
from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

from app.services.personal_graham import load_inputs, snapshot

STRATEGY_ID = 'custom_joinquant_smallcap'
WARNINGS = [
    '原every_bar频率未知；此处固定日频，前收市值选股、下一交易日开盘成交。',
    '开盘正报价作为未停牌代理，缺开盘报价无法区分停牌与数据缺失；不使用全日成交量判断开盘可交易性。',
    '原order_volume_ratio=1依赖聚宽撮合；本地保守限制为此前最多20条日线均成交额的1%，不足20条用已有前缀，不保证原平台逐笔一致。',
    '未额外排除ST、亏损、新股；仅覆盖沪深普通股票，历史证券与多股类覆盖仍需核验。',
    '默认基线佣金万三、固定卖出印花税千一；本地另计历史过户费和10bp滑点，均在配置保存。',
    '分红按20%永久税费准备金；持仓退市缺回收账本时中止，不用旧价保住虚假净值。',
]


def candidates(raw, day):
    """信号日全体历史市值候选；停牌代理只在下一开盘执行阶段判断。"""
    if day not in raw['dates']:
        raise ValueError('信号日不在交易日历中')
    basic = raw['basic'][raw['basic'].trade_date.eq(day)].set_index('ts_code')
    a_share = (basic.index.str.startswith('6') & basic.index.str.endswith('.SH')) | (basic.index.str.startswith(('0','3')) & basic.index.str.endswith('.SZ'))
    frame = basic[a_share].copy()
    frame['market_cap'] = frame.total_mv * 10000
    meta = raw['stocks'].drop_duplicates('ts_code').set_index('ts_code')
    frame = frame.join(meta[['list_date', 'delist_date']], how='left')
    frame = frame[frame.market_cap.between(2e9, 3e9) & frame.list_date.le(day)
                  & (frame.delist_date.isna() | frame.delist_date.gt(day))].copy()
    prior = raw['daily'][raw['daily'].trade_date.eq(day)].set_index('ts_code')
    frame['amount20'] = prior.amount20.reindex(frame.index)
    names = raw['names']
    names = names[names.start_date.le(day) & (names.end_date.isna() | names.end_date.ge(day))]
    if 'ann_date' in names:
        names = names[names.ann_date.isna() | names.ann_date.le(day)]
    names = names.drop_duplicates('ts_code', keep='last').set_index('ts_code')
    frame['name'] = names.name.reindex(frame.index).fillna(frame.index.to_series())
    frame['symbol'] = frame.index
    frame = frame.sort_values(['market_cap', 'symbol'])
    frame['rank'] = np.arange(1, len(frame)+1)
    frame['score'] = 100./frame['rank']  # 显示用单调分数，不添加第二个因子。
    frame['date'] = day
    frame['strategy_kind'] = 'joinquant_smallcap'
    frame['size_contribution'] = frame.score/100
    return frame


def schedule(raw, start, end, anchor):
    dates = raw['dates']
    anchor = str(anchor).replace('-', '')
    if anchor not in dates or anchor < raw['daily'].trade_date.min():
        raise ValueError('调仓锚点必须是本地覆盖的交易日')
    anchor_i = dates.index(anchor)
    first = next((i for i, d in enumerate(dates) if d >= start), len(dates))
    targets = {}
    for i in range(max(1, first, anchor_i), len(dates)):
        day = dates[i]
        if day > end:
            break
        if (i-anchor_i) % 5:
            continue
        signal = dates[i-1]
        if signal < raw['daily'].trade_date.min():
            raise ValueError('首次开盘缺前一交易日市值，不能用当日收盘补齐')
        frame = candidates(raw, signal)
        frame['anchor_date'] = anchor
        frame['next_execution_date'] = day
        targets[signal] = frame
    return targets


def apply_adjustment_corrections(raw, corrections):
    keys = ['ts_code', 'trade_date']
    if corrections.duplicated(keys).any():
        raise ValueError('已核验复权更正键重复')
    known = raw['adj'].merge(corrections, on=keys, how='inner', validate='one_to_one')
    if len(known) != len(corrections) or not np.allclose(known.adj_factor, known.old_factor, rtol=0, atol=1e-12):
        raise ValueError('复权更正的原始值不匹配，禁止静默套用旧补丁')
    adj = raw['adj'].merge(corrections, on=keys, how='left', validate='one_to_one')
    matched = adj.new_factor.notna()
    adj.loc[matched, 'adj_factor'] = adj.loc[matched, 'new_factor']
    raw['adj'] = adj.drop(columns=['old_factor','new_factor'])
    return raw


def prepared_inputs(data_dir):
    raw = dict(load_inputs(data_dir))
    raw['input_hashes'] = dict(raw['input_hashes'])
    correction = Path(data_dir)/'joinquant_inputs/verified_adj_corrections.parquet'
    if correction.is_file():
        payload = correction.read_bytes()
        raw = apply_adjustment_corrections(raw, pd.read_parquet(io.BytesIO(payload)))
        raw['verified_adj_payload'] = payload
        raw['input_hashes']['verified_adj_corrections'] = hashlib.sha256(payload).hexdigest()
    # 不修改共享缓存。首20日允许用已有前缀估算容量，绝不向未来补数据。
    daily = raw['daily']
    amount = daily.groupby('ts_code', sort=False).amount_yuan.transform(lambda x: x.rolling(20, min_periods=1).mean())
    raw['daily'] = daily.assign(amount20=amount)
    return raw


def scan(data_dir, as_of, params, pool=None, overrides=None):
    from app.strategy.engine import StrategyResult
    if pool or any(v is not None and v != [] and v != {} for k, v in (overrides or {}).items() if k != 'params'):
        raise ValueError('聚宽复现固定全市场市值排序，不支持股票池或额外规则覆盖')
    raw = prepared_inputs(data_dir)
    day = str(as_of).replace('-', '')
    rows = candidates(raw, day).head(3)
    # 盘后观察候选；下一开盘停牌过滤前不是最终订单，不发伪造开仓事件。
    records = [snapshot(row) | {'name': row['name']} for _, row in rows.iterrows()]
    return StrategyResult(as_of=as_of, strategy_id=STRATEGY_ID, rows=records, total=len(records),
                          scores={r['symbol']:float(r['score']) for r in records})


def run_backtest(service, config, strategy, progress_cb=None, cancel_event=None, result_policy=None):
    from app.backtest.graham_monthly import simulate, summarize_trades
    from app.backtest.strategy import StrategyBacktestResult
    from app.services.fs_utils import atomic_write_text
    from app.services.joinquant_report import report
    started = time.perf_counter()
    run_id = uuid.uuid4().hex[:10]
    base = service._config_to_dict(config)
    try:
        if config.asset_type != 'stock' or config.mode != 'position':
            raise ValueError('仅支持股票、共用资金持仓模式')
        if config.entry_fill != 'open_t+1' or config.exit_fill != 'open_t+1' or config.minute_fill:
            raise ValueError('仅支持日线、次日开盘买卖，请关闭分钟细化')
        if config.symbols or config.regime_filter:
            raise ValueError('请清空代码范围和市场环境过滤，保留原始全市场规则')
        if any(v is not None and v != [] and v != {} for k, v in (config.overrides or {}).items() if k != 'params'):
            raise ValueError('不支持额外信号、评分或止盈止损覆盖')
        if config.max_positions != 3 or config.position_sizing != 'equal' or config.max_exposure_pct != 1:
            raise ValueError('原策略要求最大3只、等额分配、100%仓位上限；卖不出占名额')
        if not np.isfinite(config.initial_capital) or config.initial_capital <= 0:
            raise ValueError('本金必须为正数')
        if result_policy is not None and result_policy.required_stats is not None:
            raise ValueError('固定原始规则验证，不进入自动寻优')
        params = service._normalize_params(config.params or {}, strategy)
        raw = prepared_inputs(service.engine.repo.store.data_dir)
        targets = schedule(raw, str(config.start).replace('-', ''), str(config.end).replace('-', ''), params['anchor_date'])
        rejections, positions = [], []
        stats, curve, dd, trades, fills, journal = simulate(service.engine, raw, targets, config,
            progress_cb, cancel_event, roundtrip=True, order_log=rejections, positions_log=positions)
        records = [{**service._trade_to_dict(x['record']), **{k:v for k,v in x.items() if k != 'record'}} for x in trades]
        # 包含首日费用与开盘后浮盈亏；通用统计的相邻收盘收益会漏掉首日。
        equity = np.asarray([config.initial_capital]+[r['value'] for r in curve],dtype=float)
        returns = equity[1:]/equity[:-1]-1
        stats['total_return'] = float(equity[-1]/config.initial_capital-1)
        stats['sharpe'] = float(returns.mean()/returns.std()*np.sqrt(252)) if returns.std()>0 else 0.
        stats['calmar'] = stats['annual_return']/abs(stats['max_drawdown']) if stats['max_drawdown']<0 else None
        stats['sortino'] = service.engine._sortino_ratio(returns)

        # 原基准沪深300，不能沿用通用路径默认上证指数。
        b = service.engine.repo.get_index_daily('000300.SH', config.start, config.end, columns=['date','close'])
        benchmark = [{'date':str(r['date'])[:10], 'value':float(r['close']), 'close':float(r['close']),
                      'name':'沪深300', 'symbol':'000300.SH'} for r in b.sort('date').to_dicts()
                     if r['close'] is not None and np.isfinite(r['close']) and r['close']>0]
        warnings = list(WARNINGS)
        if 'verified_adj_corrections' in raw['input_hashes']:
            warnings.append('使用单独登记的供应商历史复权核验更正，原始快照未改；记录与证据在joinquant_inputs。属于修订后数据研究，不称严格历史原始版本。')
        if len(benchmark) != len(curve):
            warnings.append('沪深300未完整覆盖账户日期，不补零；基准比较仅限可见日期。')
        stats.update(research_warnings=warnings, execution_backend='panel_five_session_roundtrip_raw',
                     signal_version='joinquant_smallcap_daily_v1', input_sha256=raw['input_hashes'],
                     selection={'strategy_matches':sum(len(f) for f in targets.values()),
                                'entry_candidates':sum(min(3,len(f)) for f in targets.values())},
                     cycle_count=len(targets), rejected_orders=len(rejections),
                     holding_statistics='已平仓按实际卖出批次；期末未卖出仅估值，不计胜率。')
        output = service.engine.repo.store.data_dir/'research/joinquant_smallcap'/run_id
        output.mkdir(parents=True, exist_ok=False)
        if 'verified_adj_payload' in raw:
            (output/'verified_adj_corrections.parquet').write_bytes(raw['verified_adj_payload'])
        sources = [Path(__file__), Path(simulate.__code__.co_filename),
                   Path(__file__).with_name('personal_graham.py'),
                   Path(simulate.__code__.co_filename).with_name('graham_distributions.py'),
                   Path(strategy.file_path) if getattr(strategy, 'file_path', None) else Path(__file__).parents[1]/'strategy/personal/joinquant_smallcap.py']
        original = Path(__file__).parents[3]/'strategy/joinquant/小市值.py'
        sources.append(original)
        code_hashes = {str(p.relative_to(Path(__file__).parents[3])):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        spec = {'variant':'joinquant_smallcap_daily_v1','anchor_date':params['anchor_date'],
                'cycle_sessions':5, 'market_cap_yuan':[2e9,3e9], 'max_positions':3,
                'signal':'前一交易日收盘市值', 'execution':'下一交易日开盘',
                'rebalance':'先全部卖出，再按空名额等额买入；当日未成交取消，下一周期再尝试',
                'capacity':'此前最多20条日线均成交额1%；未知容量拒单',
                'config':base,'warnings':warnings}
        for name, data in [('manifest',{'run_id':run_id,'input_sha256':raw['input_hashes'],
            'code_sha256':code_hashes,'status':'retrospective_engineering_check'}),('resolved_spec',spec),
            ('fills',fills),('rejections',rejections),('cash_journal',journal),('positions_daily',positions),
            ('equity_daily',curve),('trades',records),
            ('ranked_candidates',[snapshot(r) for f in targets.values() for _,r in f.iterrows()])]:
            atomic_write_text(output/(name+'.json'),json.dumps(data,ensure_ascii=False,default=str,allow_nan=False))
        orders = [dict(status='filled',**f) for f in fills]+rejections
        pd.DataFrame(orders).to_parquet(output/'orders.parquet',index=False)
        for name, data in [('fills',fills),('cash_ledger',journal),('positions_daily',positions),
                           ('equity_daily',curve),('rejections',rejections)]:
            pd.DataFrame(data).to_parquet(output/(name+'.parquet'),index=False)
        ranked = [snapshot(r) for f in targets.values() for _,r in f.iterrows()]
        pd.DataFrame(ranked).to_parquet(output/'ranked_candidates.parquet',index=False)
        pd.DataFrame(records).to_csv(output/'trades.csv',index=False)
        pd.DataFrame(rejections).to_csv(output/'rejections.csv',index=False)
        stats['artifact_dir'] = str(output)
        info = {'id':config.strategy_id,'name':strategy.meta['name'],'description':strategy.meta['description'],
                'entry_signals':strategy.entry_signals,'exit_signals':strategy.exit_signals,'source':strategy.source,
                'stop_loss':None,'take_profit':None,'max_hold_days':None,'execution_backend':stats['execution_backend']}
        report(output/'report.html',spec,stats,curve,dd,benchmark,records,rejections)
        per_symbol = summarize_trades(records)
        return StrategyBacktestResult(run_id=run_id,config=base,stats=stats,equity_curve=curve,
            drawdown_curve=dd,trades=records,per_symbol_stats=per_symbol,strategy_info=info,benchmark_curve=benchmark,
            elapsed_ms=(time.perf_counter()-started)*1000)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return StrategyBacktestResult(run_id=run_id,config=base,error='聚宽小市值复现未完成：'+str(exc),
                                      elapsed_ms=(time.perf_counter()-started)*1000)
