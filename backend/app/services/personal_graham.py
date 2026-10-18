"""Graham研究输入：本地、按公告版本回看；扫描与回测共用候选生成。"""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import suppress
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

VARIANTS = {'s0', 's1', 's2'}
WARNINGS = [
    '研究版：供应商历史财报原始版本完整性未验证，不能声称严格样本外。',
    '非金融按当时合并报表公司类型识别；A/H、A/B和历史退市整理名单尚未完整还原。',
    'S1默认使用原始NCAV核心条件，另扣少数股东权益；优先权益字段尚缺，不能声称已验证清算安全边际。严格口径可在参数中选择，缺字段时不入选。',
    '分红按20%永久税费准备金研究情景；不等于券商最终持有期税负。',
]
_CACHE = {}
_LOCK = threading.RLock()


def _frame(path):
    frame = pl.read_parquet(path).to_pandas()
    for column in ('trade_date', 'ann_date', 'f_ann_date', 'end_date', 'list_date', 'delist_date',
                   'start_date', 'end_date', 'record_date', 'ex_date', 'pay_date', 'div_listdate'):
        if column in frame:
            frame[column] = frame[column].astype('string').str.replace('-', '', regex=False).str.slice(0, 8)
    return frame


def load_inputs(data_dir: Path):
    config_path = Path(data_dir) / 'graham_inputs/sources.json'
    if not config_path.is_file():
        raise ValueError('Graham本地输入未安装，请先运行 scripts/install_graham.py')
    spec = json.loads(config_path.read_text())
    paths = {key: Path(value) for key, value in spec['paths'].items()}
    signature = tuple((key, str(path), path.stat().st_mtime_ns, path.stat().st_size)
                      for key, path in sorted(paths.items()))
    with _LOCK:
        cached = _CACHE.get(str(config_path))
        if cached and cached[0] == signature:
            return cached[1]
        raw = {key: _frame(path) for key, path in paths.items()}
        for key in ('income', 'balance', 'cashflow'):
            f = raw[key]
            f = f[f.report_type.astype(str).eq('1')].copy()
            f['known'] = f.f_ann_date.fillna(f.ann_date) if 'f_ann_date' in f else f.ann_date
            f = f[f.known.ge(f.end_date) & f.known.str.fullmatch(r'\d{8}', na=False)].copy()
            f = f.drop_duplicates()
            if f.duplicated(['ts_code', 'end_date', 'known']).any():
                raise ValueError(f'{key}同次公告有冲突版本，未自动挑选')
            raw[key] = f.sort_values(['known', 'end_date'])
        if raw['daily'].duplicated(['ts_code', 'trade_date']).any():
            raise ValueError('Graham原始行情键重复')
        if raw['basic'].duplicated(['ts_code', 'trade_date']).any():
            raise ValueError('Graham历史市值键重复')
        calendar = raw['calendar']
        raw['dates'] = sorted(calendar.loc[calendar.is_open.astype(str).eq('1'), 'trade_date'].tolist())
        daily = raw['daily'].sort_values(['ts_code', 'trade_date']).copy()
        daily['amount_yuan'] = daily.amount * 1000  # Tushare daily为千元
        daily['amount20'] = daily.groupby('ts_code', sort=False).amount_yuan.transform(
            lambda x: x.rolling(20, min_periods=20).mean())
        raw['daily'] = daily
        raw['names'] = raw['names'].sort_values(['ts_code', 'start_date'])
        raw['input_hashes'] = {key: hashlib.sha256(path.read_bytes()).hexdigest()
                               for key, path in paths.items()}
        raw['signature'] = signature
        _CACHE[str(config_path)] = (signature, raw)
        return raw


def _asof(f, day):
    # 先回看每个报告期的版本，再选最新报告期，不能让旧报告的晚修订取代新季度。
    known = f[f.known.lt(day)]
    return known.drop_duplicates(['ts_code', 'end_date'], keep='last')


def _latest(f, day):
    return _asof(f, day).sort_values(['end_date', 'known']).drop_duplicates('ts_code', keep='last').set_index('ts_code')


def _ttm(f, day, column, periods):
    visible = _asof(f, day).set_index(['ts_code', 'end_date'])
    result = {}
    for code, period in periods.items():
        def value(p, code=code):
            try:
                v = float(visible.at[(code, p), column])
                return v if np.isfinite(v) else np.nan
            except (KeyError, TypeError, ValueError):
                return np.nan
        if period.endswith('1231'):
            result[code] = value(period)
        else:
            year = int(period[:4])
            result[code] = value(f'{year-1}1231') + value(period) - value(f'{year-1}{period[4:]}')
    return pd.Series(result, dtype=float)


def percentile(x):
    valid = x.notna() & np.isfinite(x)
    n = int(valid.sum())
    result = pd.Series(np.nan, index=x.index)
    if n == 1:
        result.loc[valid] = .5
    elif n > 1:
        result.loc[valid] = (x.loc[valid].rank(method='average') - 1) / (n - 1)
    return result


def candidates(raw, day, variant, params=None, pool=None):
    if variant not in VARIANTS:
        raise ValueError('未知Graham策略')
    params = params or {}
    dates = raw['dates']
    if day not in dates:
        raise ValueError('目标日不在本地交易日历中')
    today = raw['daily'][raw['daily'].trade_date.eq(day)].set_index('ts_code')
    basic = raw['basic'][raw['basic'].trade_date.eq(day)].set_index('ts_code')
    frame = today.join(basic[['total_mv']], how='inner')
    frame['market_cap'] = frame.total_mv * 10000  # 万元→元，绝不使用复权价乘最新股本
    meta = raw['stocks'].drop_duplicates('ts_code').set_index('ts_code')
    frame = frame.join(meta[['list_date', 'delist_date']], how='left')
    names = raw['names']
    visible = names[names.start_date.le(day) & (names.end_date.isna() | names.end_date.ge(day))]
    if 'ann_date' in visible:
        visible = visible[visible.ann_date.isna() | visible.ann_date.le(day)]
    visible = visible.drop_duplicates('ts_code', keep='last').set_index('ts_code')
    frame['name'] = visible.name.reindex(frame.index)
    oldest = dates[max(0, dates.index(day) - 252)]
    balance = _latest(raw['balance'], day)
    # 对照组和挑战组同样要求当时有非金融合并报表，避免拿当前行业回填历史。
    company = balance.comp_type.astype(str).eq('1')
    frame['company'] = company.reindex(frame.index).fillna(False)
    valid = (frame.index.str.endswith(('.SH', '.SZ')) & frame.list_date.le(oldest)
             & (frame.delist_date.isna() | frame.delist_date.gt(day))
             & frame.name.notna() & ~frame.name.str.contains('ST|退', case=False, na=True)
             & frame.company & frame.market_cap.gt(0) & frame.amount20.ge(20_000_000)
             & frame.vol.gt(0))
    frame = frame.loc[valid].copy()
    if pool:
        # 股票池限制先于全部横截面计算，扫描与回测必须传入相同pool。
        frame = frame[frame.index.isin(pool)]
    frame['symbol'] = frame.index
    frame['date'] = day
    frame['size_pct'] = percentile(frame.market_cap)
    frame['ncav'] = np.nan
    frame['ep'] = np.nan
    frame['roa'] = np.nan
    frame['ncav_ratio'] = np.nan
    frame['report_period'] = balance.end_date.reindex(frame.index)
    frame['announce_date'] = balance.known.reindex(frame.index)
    frame['size_contribution'] = 1 - frame.size_pct
    frame['value_contribution'] = 0.
    frame['quality_contribution'] = 0.
    frame['eligible'] = True
    if variant in ('s1', 's2'):
        period_age = (pd.Timestamp(day) - pd.to_datetime(frame.report_period, format='%Y%m%d')).dt.days
        frame['eligible'] &= period_age.le(180)
    if variant == 's0':
        frame['score'] = 100 * (1 - frame.size_pct)
    elif variant == 's1':
        bs = balance.reindex(frame.index)
        minority = bs.total_hldr_eqy_inc_min_int - bs.total_hldr_eqy_exc_min_int
        # 缺失保持NaN。保守扣减少数股东权益；不能用负少数权益提高可分配资产。
        ncav = bs.total_cur_assets - bs.total_liab - minority.clip(lower=0)
        if params.get('strict_preferred', False):
            preferred = bs.get('oth_eqt_tools_p_shr', pd.Series(np.nan, index=bs.index))
            ncav -= preferred
        frame['ncav'] = ncav
        frame['ncav_ratio'] = ncav / frame.market_cap
        frame['eligible'] &= ncav.gt(0) & frame.market_cap.le(ncav * (2/3))
        frame['score'] = frame.ncav_ratio
        frame['value_contribution'] = frame.ncav_ratio
        frame['size_contribution'] = 0.
    else:
        income = _latest(raw['income'], day)
        cash = _latest(raw['cashflow'], day)
        parent = _ttm(raw['income'], day, 'n_income_attr_p', income.end_date.to_dict()).reindex(frame.index)
        profit = _ttm(raw['income'], day, 'n_income', income.end_date.to_dict()).reindex(frame.index)
        ocf = _ttm(raw['cashflow'], day, 'n_cashflow_act', cash.end_date.to_dict()).reindex(frame.index)
        bs = balance.reindex(frame.index)
        historical = _asof(raw['balance'], day).set_index(['ts_code', 'end_date'])
        old_assets = pd.Series(np.nan, index=frame.index)
        for code, period in bs.end_date.items():
            if not isinstance(period, str):
                continue
            with suppress(KeyError):
                old_assets.loc[code] = historical.at[(code, f'{int(period[:4])-1}{period[4:]}'), 'total_assets']
        frame['ep'] = parent / frame.market_cap
        frame['roa'] = profit / ((bs.total_assets + old_assets) / 2)
        frame['debt_ratio'] = bs.total_liab / bs.total_assets
        # 利润、现金流、资产使用同一报告期，禁止拼成一份不存在的财报。
        same_period = income.end_date.reindex(frame.index).eq(bs.end_date) & cash.end_date.reindex(frame.index).eq(bs.end_date)
        frame['eligible'] &= (frame.size_pct.le(.20) & parent.gt(0) & ocf.gt(0)
                              & bs.total_assets.gt(0) & old_assets.gt(0)
                              & frame.debt_ratio.between(0, .60) & same_period)
        frame['size_contribution'] = .4 * (1 - frame.size_pct)
        frame['value_contribution'] = .4 * percentile(frame.ep)
        frame['quality_contribution'] = .2 * percentile(frame.roa)
        frame['score'] = 100 * (frame.size_contribution + frame.value_contribution + frame.quality_contribution)
        frame['announce_date'] = pd.concat([frame.announce_date, income.known.reindex(frame.index),
                                           cash.known.reindex(frame.index)], axis=1).max(axis=1)
    rows = frame.loc[frame.eligible & np.isfinite(frame.score)].sort_values(
        ['score', 'symbol'], ascending=[False, True]).copy()
    rows['rank'] = np.arange(1, len(rows)+1)
    return rows


def schedule(raw, start, end, variant, params=None, pool=None):
    dates = raw['dates']
    # 月末由完整交易日历确定，不能把样本末日当月末。
    ends = [d for i, d in enumerate(dates[:-1]) if dates[i+1][:6] != d[:6] and start <= d <= end]
    return {d: candidates(raw, d, variant, params, pool).head(20) for d in ends}


def snapshot(row):
    fields = ['strategy_kind', 'anchor_date', 'next_execution_date', 'date', 'symbol', 'rank', 'score', 'market_cap', 'ncav', 'ncav_ratio', 'ep', 'roa',
              'debt_ratio', 'report_period', 'announce_date', 'size_contribution', 'value_contribution', 'quality_contribution']
    return {key: (None if pd.isna(row.get(key)) else row.get(key)) for key in fields}


def scan(data_dir, strategy_id, variant, day, params, pool):
    from app.strategy.engine import StrategyResult
    if pool:
        raise ValueError('Graham研究只支持完整冻结股票池，请清空代码范围')
    raw = load_inputs(data_dir)
    text = str(day).replace('-', '')
    rows = candidates(raw, text, variant, params, pool).head(20)
    records = [snapshot(row) | {'name': row['name']} for _, row in rows.iterrows()]
    return StrategyResult(as_of=day, strategy_id=strategy_id, rows=records, total=len(records),
                          scores={r['symbol']: float(r['score']) for r in records})


def run_backtest(service, config, strategy, progress_cb=None, cancel_event=None, result_policy=None):
    import time
    import uuid

    from app.backtest.graham_monthly import simulate, summarize_trades
    from app.backtest.strategy import StrategyBacktestResult
    from app.services.fs_utils import atomic_write_text
    started = time.perf_counter()
    run_id = uuid.uuid4().hex[:10]
    base = service._config_to_dict(config)
    try:
        if config.asset_type != 'stock' or config.mode != 'position':
            raise ValueError('Graham三组仅支持股票、共用资金持仓模式；不能使用候选独立收益模式')
        if config.entry_fill != 'open_t+1' or config.exit_fill != 'open_t+1' or config.minute_fill:
            raise ValueError('Graham三组仅支持日线信号、次日开盘买卖；请关闭分钟细化')
        if config.symbols:
            raise ValueError('Graham对照要求完整冻结股票池，请清空代码范围；标的回放可以单独查看股票')
        if config.regime_filter:
            raise ValueError('Graham首轮固定不择时，请清空市场环境和0AMV过滤')
        blocked = {k: v for k, v in (config.overrides or {}).items()
                   if v is not None and v != [] and v != {} and k != 'params'}
        if blocked:
            raise ValueError('Graham首轮不支持额外信号、评分、止盈止损覆盖，请恢复默认参数：'+','.join(blocked))
        if config.max_positions != 20 or config.position_sizing != 'equal':
            raise ValueError('Graham冻结口径为20槽等权，请设最大持仓20、等权分配')
        if not 0 < config.max_exposure_pct <= 1 or not np.isfinite(config.initial_capital) or config.initial_capital <= 0:
            raise ValueError('请设置正本金和0至100%的有效仓位上限')
        if result_policy is not None and result_policy.required_stats is not None:
            raise ValueError('Graham首轮只支持人工固定参数回测，不进入自动寻优')
        raw = load_inputs(service.engine.repo.store.data_dir)
        variant = strategy.meta['personal_graham_variant']
        params = service._normalize_params(config.params or {}, strategy)
        targets = schedule(raw, str(config.start).replace('-', ''), str(config.end).replace('-', ''), variant, params)
        stats, curve, dd, trades, fills, journal = simulate(service.engine, raw, targets, config, progress_cb, cancel_event)
        records = [{**service._trade_to_dict(x['record']), **{k: v for k, v in x.items() if k != 'record'}} for x in trades]
        stats['research_warnings'] = WARNINGS
        stats['execution_backend'] = 'panel_monthly_targets_raw'
        stats['signal_version'] = 'graham_monthly_v1'
        stats['input_sha256'] = raw['input_hashes']
        stats['strict_preferred'] = bool(params.get('strict_preferred', False))
        stats['holding_statistics'] = '已平仓按实际减仓批次统计；期末未卖出不计胜率；同一持仓加仓按加权成本展示，真实成交见买入明细。'
        output = service.engine.repo.store.data_dir / 'research/graham' / run_id
        output.mkdir(parents=True, exist_ok=False)
        atomic_write_text(output/'manifest.json', json.dumps({'run_id': run_id, 'config': base,
            'input_sha256': raw['input_hashes'], 'warnings': WARNINGS, 'variant': variant,
            'strict_preferred': bool(params.get('strict_preferred', False)),
            'status': 'research_only_not_out_of_sample'}, ensure_ascii=False, indent=2))
        for filename, data in [('fills', fills), ('cash_journal', journal), ('equity_daily', curve),
                               ('ranked_targets', [snapshot(r) for f in targets.values() for _, r in f.iterrows()])]:
            atomic_write_text(output/(filename+'.json'), json.dumps(data, ensure_ascii=False, default=str))
        stats['artifact_dir'] = str(output)
        # 同一股票多次减仓不能把重叠收益相乘。标的汇总按净盈亏/累计分摊成本。
        per_symbol = summarize_trades(records)
        fields = ['market_cap', 'ncav_ratio', 'ep', 'roa']
        closed = [r for r in records if r['exit_reason'] != 'open_position']
        factors = []
        for key in fields:
            win = [r['entry_attribution'][key] for r in closed if r['pnl_pct'] > 0 and r['entry_attribution'].get(key) is not None]
            lose = [r['entry_attribution'][key] for r in closed if r['pnl_pct'] <= 0 and r['entry_attribution'].get(key) is not None]
            if win or lose:
                factors.append({'factor': key, 'win_mean': float(np.mean(win)) if win else None,
                    'lose_mean': float(np.mean(lose)) if lose else None, 'win_n': len(win), 'lose_n': len(lose)})
        info = {'id': config.strategy_id, 'name': strategy.meta['name'], 'description': strategy.meta['description'],
                'entry_signals': strategy.entry_signals, 'exit_signals': strategy.exit_signals,
                'source': strategy.source, 'stop_loss': None, 'take_profit': None, 'trailing_stop': None,
                'trailing_take_profit_activate': None, 'trailing_take_profit_drawdown': None,
                'max_hold_days': None, 'score_min': None, 'score_max': None, 'execution_backend': 'panel_monthly_targets_raw'}
        return StrategyBacktestResult(run_id=run_id, config=base, stats=stats, equity_curve=curve,
            drawdown_curve=dd, trades=records, per_symbol_stats=per_symbol, strategy_info=info,
            benchmark_curve=service._build_benchmark_curve(config.start, config.end),
            factor_attribution={'factors': factors, 'n_win': sum(r['pnl_pct'] > 0 for r in closed),
                                'n_lose': sum(r['pnl_pct'] <= 0 for r in closed)},
            elapsed_ms=(time.perf_counter()-started)*1000)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return StrategyBacktestResult(run_id=run_id, config=base,
            error='Graham研究未完成：'+str(exc), elapsed_ms=(time.perf_counter()-started)*1000)
