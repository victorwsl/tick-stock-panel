"""个人本地板块快照与ETF导入；网络只在显式sync入口使用。"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date
from pathlib import Path

import polars as pl

from app.config import settings
from app.plugins.local_parquet.provider import LocalParquetProvider, _unique, source_path
from app.services.fs_utils import atomic_write_parquet
from app.services.personal_local_data import cache_dir


def import_sectors() -> dict:
    from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet
    root = source_path().parent
    members = pl.read_parquet(root / 'ths_concept_member.parquet')
    industries = pl.read_parquet(root / 'sw_member.parquet')
    asof = pl.scan_parquet(root / 'daily_qfq.parquet').select(pl.col('trade_date').max()).collect().item()
    active = industries.filter(
        (pl.col('in_date').is_null() | (pl.col('in_date') <= asof)) &
        (pl.col('out_date').is_null() | (pl.col('out_date') > asof)))
    industry = active.select(pl.col('ts_code').alias('symbol'), pl.col('l1_name').alias('industry')).unique()
    if industry['symbol'].n_unique() != industry.height:
        raise ValueError('同一股票存在多个有效申万一级行业，未导入')
    grouped = members.group_by('con_code').agg(pl.col('concept_name').drop_nulls().unique().sort())
    concept = pl.DataFrame([{'symbol': r['con_code'], 'concept': json.dumps(r['concept_name'], ensure_ascii=False)}
                            for r in grouped.to_dicts()])
    universe = pl.read_parquet(root / 'stock_basic.parquet').select(pl.col('ts_code').alias('symbol'))
    frame = universe.join(industry, on='symbol', how='left').join(concept, on='symbol', how='left')
    config = ExtConfig(id='personal_local_memberships', label='本地概念与申万一级行业', mode='snapshot',
                       fields=[ExtField('industry', label='行业'), ExtField('concept', label='概念')],
                       description=f'展示快照；行情截至{asof}。概念成员缺少生效历史，不可用于历史因子回填；热度为成分股等权涨跌幅。')
    store = ExtConfigStore(settings.data_dir)
    existing = store.get(config.id)
    if existing and existing.label != config.label:
        raise ValueError('扩展配置ID已被其他配置占用，未覆盖')
    store.upsert(config)
    write_ext_parquet(frame, config, settings.data_dir)
    return {'asof': asof, 'rows': frame.height, 'industry_groups': industry['industry'].n_unique(),
            'concept_groups': members['concept_name'].n_unique(),
            'industry_members': industry.height, 'concept_members': grouped.height,
            'historical_concept_pit': False}


def seed_etfs(paths: list[Path]) -> dict:
    """只接收同时含原始价、qfq校验列、累计因子的研究文件；不把qfq价当成交价。"""
    if not paths:
        raise ValueError('请用 --etf-source 明确指定本地研究文件')
    frames, hashes = [], {}
    for path in paths:
        frame = _unique(pl.read_parquet(path))
        required = {'ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount',
                    'adj_factor', 'qfq_close', 'asset_name', 'list_date'}
        if not required <= set(frame.columns):
            raise ValueError('ETF来源必须同时包含原始OHLC、累计因子和qfq_close校验列')
        numeric = ['open', 'high', 'low', 'close', 'adj_factor', 'qfq_close']
        if frame.filter(pl.any_horizontal(pl.col(c).is_null() | ~pl.col(c).is_finite() | (pl.col(c) <= 0) for c in numeric)).height:
            raise ValueError('ETF价格与复权因子必须为正的有限值')
        frame = frame.sort(['ts_code', 'trade_date'])
        expected = pl.col('close') * pl.col('adj_factor') / pl.col('adj_factor').last().over('ts_code')
        error = frame.select((expected - pl.col('qfq_close')).abs().max()).item()
        if error is None or error > 1e-7:
            raise ValueError('ETF原始价与前复权价关系校验失败')
        if frame.filter(~pl.col('asset_name').str.contains('ETF')).height:
            raise ValueError('本入口只导入明确标注ETF的样本')
        frames.append(frame)
        hashes[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
    # 不同研究截止日期可重叠；原始价和因子必须一致才允许合并。
    combined = pl.concat(frames, how='diagonal_relaxed')
    conflicts = combined.group_by('ts_code', 'trade_date').agg(
        [pl.col(c).n_unique().alias(c) for c in ['open', 'high', 'low', 'close', 'adj_factor']])
    if conflicts.filter(pl.any_horizontal(pl.col(c) > 1 for c in ['open', 'high', 'low', 'close', 'adj_factor'])).height:
        raise ValueError('本地ETF研究文件的重叠行情不一致，未自动挑选')
    combined = combined.unique(['ts_code', 'trade_date'], keep='last').sort(['ts_code', 'trade_date'])
    # 在任何写入前检查已有缓存，避免原价与复权因子来自不同版本。
    for table, columns in [('fund_daily', ['open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount']),
                           ('fund_adj', ['adj_factor'])]:
        for key, group in combined.group_by('ts_code'):
            path = cache_dir() / table / f'{key[0]}.parquet'
            if path.exists():
                overlap = pl.read_parquet(path).join(group, on=['ts_code', 'trade_date'], suffix='_new')
                mismatch = pl.any_horizontal(
                    (pl.col(c).is_null() != pl.col(c + '_new').is_null()) |
                    ((pl.col(c) - pl.col(c + '_new')).abs() > 1e-7) for c in columns)
                if overlap.filter(mismatch).height:
                    raise ValueError(f'{key[0]} ETF缓存与输入冲突，未写入')
    for table, columns in [('fund_daily', ['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'vol', 'amount']),
                           ('fund_adj', ['ts_code', 'trade_date', 'adj_factor'])]:
        for key, group in combined.select(columns).group_by('ts_code'):
            out = cache_dir() / table / f'{key[0]}.parquet'
            if out.exists():
                old = pl.read_parquet(out)
                group = pl.concat([group, old], how='diagonal_relaxed').unique(['ts_code', 'trade_date'], keep='last')
            out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_parquet(group.sort(['ts_code', 'trade_date']), out)
    meta = combined.group_by('ts_code').agg(pl.col('asset_name').last().alias('name'), pl.col('list_date').first())
    out = cache_dir() / 'fund_basic.parquet'
    if out.exists():
        meta = pl.concat([pl.read_parquet(out), meta], how='diagonal_relaxed').unique('ts_code', keep='last')
    atomic_write_parquet(meta, out)
    result = {'sources': hashes, 'symbols': meta.height, 'seed_rows': combined.height,
              'universe': '显式本地ETF研究样本，不代表历史全市场，存在选样及幸存者偏差'}
    (cache_dir() / 'etf_sources.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def sync_etfs() -> dict:
    import httpx

    from app.services.personal_local_data import _token
    token = _token()
    endpoint = os.environ.get('TUSHARE_API_URL') or os.environ.get('tushare_api_url', 'https://tt.xiaodefa.cn')  # noqa: SIM112
    target = pl.scan_parquet(source_path().with_name('daily_qfq.parquet')).select(pl.col('trade_date').max()).collect().item()
    provider = LocalParquetProvider()
    counts = 0
    for symbol in [r['symbol'] for r in provider.get_instruments('etf')]:
        raw_path = cache_dir() / 'fund_daily' / f'{symbol}.parquet'
        start = pl.read_parquet(raw_path)['trade_date'].max()
        if start >= target:
            continue
        pending = []
        for api, fields in [('fund_daily', 'ts_code,trade_date,open,high,low,close,pre_close,vol,amount'),
                            ('fund_adj', 'ts_code,trade_date,adj_factor')]:
            if (date.fromisoformat(f'{target[:4]}-{target[4:6]}-{target[6:]}') -
                date.fromisoformat(f'{start[:4]}-{start[4:6]}-{start[6:]}')).days > 1000:
                raise ValueError('ETF缺口超过1000自然日，须分段补数，未请求')
            for attempt in range(3):
                time.sleep(0.65 + attempt * 2)
                try:
                    response = httpx.post(endpoint, json={'api_name': api, 'token': token,
                        'params': {'ts_code': symbol, 'start_date': start, 'end_date': target}, 'fields': fields},
                        timeout=60, trust_env=False)
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get('code') != 0:
                        raise ValueError('API错误')
                    data = payload.get('data') or {}
                    new = pl.DataFrame(data.get('items') or [], schema=data.get('fields') or [], orient='row')
                    if new.is_empty() or new.height >= 2000:
                        raise ValueError('空数据或达到截断门槛')
                    _unique(new)
                    if set(new['ts_code']) != {symbol} or new['trade_date'].max() > target or new['trade_date'].min() < start:
                        raise ValueError('返回代码或日期不符')
                    break
                except (httpx.HTTPError, ValueError):
                    if attempt == 2:
                        raise RuntimeError(f'ETF补数失败：{api} {symbol}；未写入该证券新数据') from None
            path = cache_dir() / api / f'{symbol}.parquet'
            old = pl.read_parquet(path)
            overlap = old.join(new, on=['ts_code', 'trade_date'], suffix='_new')
            columns = ['adj_factor'] if api == 'fund_adj' else ['open', 'high', 'low', 'close']
            if overlap.filter(pl.any_horizontal((pl.col(c) - pl.col(c + '_new')).abs() > 1e-7 for c in columns)).height:
                raise ValueError(f'{symbol} 补数与已有原始价/因子冲突，未覆盖')
            merged = pl.concat([old, new], how='diagonal_relaxed').unique(['ts_code', 'trade_date'], keep='last')
            pending.append((merged.sort(['ts_code', 'trade_date']), path))
        daily, factors = pending[0][0], pending[1][0]
        if daily.join(factors.select('ts_code', 'trade_date'), on=['ts_code', 'trade_date'], how='anti').height:
            raise ValueError(f'{symbol} 缺少复权因子，未保存')
        for frame, path in pending:
            atomic_write_parquet(frame, path)
        counts += 1
        print(f'ETF补数 {symbol} 截至{daily["trade_date"].max()}', flush=True)
    return {'updated_symbols': counts, 'target': target}


def import_etfs() -> dict:
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS, compute_enriched
    from app.services.instrument_sync import _flatten_instruments
    from app.tickflow.repository import DataStore, KlineRepository
    provider = LocalParquetProvider()
    instruments = provider.get_instruments('etf')
    symbols = [r['symbol'] for r in instruments]
    raw = provider.get_daily(symbols, asset_type='etf')
    factors = provider._stock('fund_adj')
    source = provider._stock('fund_daily')
    if source.join(factors.select('ts_code', 'trade_date'), on=['ts_code', 'trade_date'], how='anti').height:
        raise ValueError('ETF日线缺少复权因子，未导入')
    if raw.filter(pl.any_horizontal(pl.col(c).is_null() | ~pl.col(c).is_finite() for c in ['open', 'high', 'low', 'close'])).height:
        raise ValueError('ETF价格包含空值或非有限值')
    if raw.filter((pl.col('low') <= 0) | (pl.col('high') < pl.max_horizontal('open', 'close')) |
                  (pl.col('low') > pl.min_horizontal('open', 'close'))).height:
        raise ValueError('ETF OHLC价格不合法')
    events = provider.get_adj_factors(symbols, asset_type='etf')
    enriched = compute_enriched(raw, events)
    enriched = enriched.select([c for c in ENRICHED_STORAGE_COLS if c in enriched.columns])
    inst = pl.DataFrame(_flatten_instruments(instruments))
    store = DataStore(settings.data_dir)
    repo = KlineRepository(store)
    try:
        path = settings.data_dir / 'adj_factor_etf' / 'local' / 'part.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(events, path)
        repo.save_etf_instruments(inst)
        repo.append_etf_daily(raw)
        repo.append_etf_enriched(enriched)
    finally:
        store.db.close()
    return {'symbols': len(symbols), 'rows': raw.height, 'start': str(raw['date'].min()), 'end': str(raw['date'].max()),
            'per_symbol': raw.group_by('symbol').agg(pl.col('date').min().alias('start'),
                  pl.col('date').max().alias('end'), pl.len()).sort('symbol').to_dicts(),
            'execution_verified': False, 'universe_complete': False}


def import_regime() -> dict:
    """沿用TSP市场环境算法，只补隔离库，不生成概念历史主线。"""
    from app.services import regime_builder
    from app.tickflow.repository import DataStore, KlineRepository
    bounds = pl.scan_parquet(str(settings.data_dir / 'kline_daily_enriched/**/*.parquet')).select(
        pl.col('date').min().alias('start'), pl.col('date').max().alias('end')).collect().row(0)
    store = DataStore(settings.data_dir)
    repo = KlineRepository(store)
    try:
        frame = regime_builder.run_regime_batch(repo, start=bounds[0], end=bounds[1])
        if frame.is_empty():
            raise ValueError('市场环境计算没有产生结果')
        regime_builder.upsert_regime_history(settings.data_dir, frame)
        regime_builder.refresh_phase_labels(settings.data_dir)
        return regime_builder.get_regime_coverage(settings.data_dir)
    finally:
        store.db.close()
