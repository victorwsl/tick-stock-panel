"""显式补数入口；Web和provider永不调用此模块的网络方法。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_parquet
from app.services.personal_tushare import TushareClient
from app.services.personal_tushare import get_token as _token  # noqa: F401 -- 保留既有导入契约


def cache_dir() -> Path:
    from app.config import settings
    return settings.data_dir.resolve() / 'source_cache'



def fetch_missing(days: int = 120) -> dict:
    """从本地交易日历选最近N日，只补缓存缺失日；每次请求间隔至少0.6秒。"""
    from app.plugins.local_parquet.provider import source_path
    if not 65 <= days <= 1000:
        raise ValueError('首次导入交易日数须在65至1000之间')
    dates = (pl.scan_parquet(source_path().with_name('daily_qfq.parquet'))
             .select('trade_date').unique().sort('trade_date').tail(days).collect()
             ['trade_date'].cast(pl.String).to_list())
    if len(dates) < 65:
        raise ValueError('本地不足65个交易日，不能计算首页60日趋势')
    client = TushareClient()
    def call(api, params, fields):
        payload = client.query(api, params, fields)
        data = payload["data"]
        frame = pl.DataFrame(data.get("items") or [], schema=data.get("fields") or [], orient="row")
        if frame.is_empty():
            raise ValueError(f'Tushare {api} 返回空数据')
        if frame.select(pl.struct('ts_code', 'trade_date').is_duplicated().any()).item():
            raise ValueError(f'Tushare {api} 返回重复键')
        return frame

    def one(d):
        counts = {}
        for api, fields in [('daily', 'ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount'),
                            ('adj_factor', 'ts_code,trade_date,adj_factor')]:
            path = cache_dir() / api / f'{d}.parquet'
            if path.exists():
                continue
            frame = call(api, {'trade_date': d}, fields)
            if frame['trade_date'].cast(pl.String).unique().to_list() != [d]:
                raise ValueError('接口交易日期与请求不符')
            # 达到接口单次上限可能被截断，不把不完整数据作为成功缓存。
            if frame.height >= 6000:
                raise ValueError('达到单次6000行上限，需分页后重试，未缓存')
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_parquet(frame, path)
            counts[api] = frame.height
        return counts
    total = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        i = 0
        for offset in range(0, len(dates), 2):
            # 小批提交：失败最多影响两个交易日，不把全历史请求都排进队列。
            for counts in pool.map(one, dates[offset:offset + 2]):
                i += 1
                total += len(counts)
                if i % 10 == 0 or i == len(dates):
                    print(f'股票补数 {i}/{len(dates)} 个交易日；本次新增{total}份缓存', flush=True)
    # 固定核心指数补齐并更新，保留本地其他指数；这里不替换不同指数。
    for symbol in ['000001.SH', '399001.SZ', '399006.SZ', '000680.SH']:
        path = cache_dir() / 'index' / f'{symbol}.parquet'
        if path.exists() and pl.read_parquet(path)['trade_date'].max() >= dates[-1]:
            continue
        frame = call('index_daily', {'ts_code': symbol, 'start_date': '20230101', 'end_date': dates[-1]},
                     'ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount')
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(frame, path)
    return {'days_requested': len(dates), 'new_files': total, 'start': dates[0], 'end': dates[-1]}


def import_stocks(stock_root: Path | None = None) -> dict:
    """校验成功后写入TSP标准仓库；需要停止服务或随后重启以刷新缓存。"""
    import hashlib

    from app.config import settings
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS, compute_enriched
    from app.plugins.local_parquet.provider import LocalParquetProvider, source_path
    from app.services.instrument_sync import _flatten_instruments
    from app.tickflow.repository import DataStore, KlineRepository

    root = source_path().parent
    if stock_root is not None:
        stock_root = stock_root.expanduser().resolve()
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [
        root / 'index_daily.parquet', root / 'stock_basic.parquet', root / 'daily_basic.parquet', root / 'daily_qfq.parquet']}
    if stock_root is not None:
        hashes.update({str(stock_root / name): hashlib.sha256((stock_root / name).read_bytes()).hexdigest()
                       for name in ("daily.parquet", "adj_factor.parquet")})
    provider = LocalParquetProvider(stock_root=stock_root)
    raw_source = provider._stock('daily')
    factors_source = provider._stock('adj_factor')
    keys = ['ts_code', 'trade_date']
    if raw_source.join(factors_source.select(keys), on=keys, how='anti').height:
        raise ValueError('原始日线缺少对应复权因子，未导入')
    start, end = raw_source['trade_date'].min(), raw_source['trade_date'].max()
    existing_parts = list((settings.data_dir / 'kline_daily_enriched').glob('**/*.parquet'))
    if existing_parts:
        existing_start = pl.scan_parquet(existing_parts).select(pl.col('date').min()).collect().item()
        incoming_start = date.fromisoformat(str(start)[:4] + '-' + str(start)[4:6] + '-' + str(start)[6:8])
        if existing_start is not None and incoming_start > existing_start:
            raise ValueError('导入窗口短于现有历史，未覆盖股本和复权事件；请指定完整 --stock-source，增量更新需另行验收')
    expected = (pl.scan_parquet(root / 'daily_qfq.parquet')
                .filter(pl.col('trade_date').is_between(pl.lit(start), pl.lit(end))).select(keys).collect())
    if expected.join(raw_source.select(keys), on=keys, how='anti').height:
        raise ValueError('缓存未覆盖本地同期股票日线键，请先继续补数；未导入')
    basic = (pl.scan_parquet(root / 'daily_basic.parquet').filter(pl.col('trade_date').is_between(pl.lit(start), pl.lit(end)))
             .select(*keys, pl.col('close').alias('basic_close'), 'float_share', 'turnover_rate').collect())
    compare = raw_source.select(*keys, 'close').join(basic.select(*keys, 'basic_close'), on=keys, how='inner')
    error = compare.select((pl.col('close') - pl.col('basic_close')).abs().max()).item()
    price_conflicts = []
    if not compare.height:
        raise ValueError('Tushare未复权收盘价与本地daily_basic不一致，未导入')
    if error > 1e-6:
        if stock_root is None:
            raise ValueError('Tushare未复权收盘价与本地daily_basic不一致，未导入')
        from app.services.personal_price_audit import verify_close_conflicts
        checked = verify_close_conflicts(raw_source, basic, root)
        price_conflicts = checked.to_dicts()
        # 原价保留；冲突daily_basic整行不用于股本/换手率，留下缺口而非填补。
        basic = basic.join(checked.select(keys), on=keys, how='anti')
    compare_rows = compare.height
    del compare, expected, factors_source
    symbols = sorted(raw_source['ts_code'].unique().to_list())
    del raw_source
    raw = provider.get_daily(symbols)
    instruments = provider.get_instruments('stock')
    missing_metadata = sorted(set(symbols) - {x['symbol'] for x in instruments})
    if missing_metadata and stock_root is not None and (stock_root / 'namechange.parquet').is_file():
        name_path = stock_root / 'namechange.parquet'
        hashes[str(name_path)] = hashlib.sha256(name_path.read_bytes()).hexdigest()
        names = (pl.read_parquet(name_path).filter(pl.col('ts_code').is_in(missing_metadata) &
                 (pl.col('start_date') <= str(end))).sort(['ts_code','start_date'])
                 .unique('ts_code', keep='last'))
        for item in names.to_dicts():
            code = item['ts_code']
            instruments.append({'symbol': code, 'code': code.split('.')[0],
                'exchange': code.split('.')[1], 'name': item['name'], 'type': 'stock', 'region': 'CN',
                'ext': {'historical_metadata_gap': True}})
    inst = pl.DataFrame(_flatten_instruments(instruments)).with_columns(
        pl.col('float_shares').cast(pl.Float64), pl.col('total_shares').cast(pl.Float64),
        pl.lit(date.today()).alias('as_of'))
    if set(symbols) - set(inst['symbol'].to_list()):
        raise ValueError('有行情证券缺少名称维表，未导入；不会按当前上市状态删除历史证券')
    events = provider.get_adj_factors(symbols)
    # 日频流通股本快照按当天可见；不把最新股本回填到历史。
    shares = basic.select(pl.col('ts_code').alias('symbol'),
                          pl.col('trade_date').str.strptime(pl.Date, '%Y%m%d').alias('period_end'),
                          pl.col('trade_date').str.strptime(pl.Date, '%Y%m%d').alias('announce_date'),
                          (pl.col('float_share') * 10000).alias('float_shares'),
                          pl.lit('daily_basic_snapshot').alias('source'))
    del basic
    indices = provider.get_instruments('index')
    index_daily = provider.get_daily([r['symbol'] for r in indices], asset_type='index')
    index_enriched = compute_enriched(index_daily)
    parts = []
    # Full local history needs smaller batches to bound indicator intermediates.
    for offset in range(0, len(symbols), 100):
        batch = symbols[offset:offset + 100]
        enriched = compute_enriched(raw.filter(pl.col('symbol').is_in(batch)),
                                    events.filter(pl.col('symbol').is_in(batch)),
                                    inst.filter(pl.col('symbol').is_in(batch)),
                                    shares.filter(pl.col('symbol').is_in(batch)))
        parts.append(enriched.select([c for c in ENRICHED_STORAGE_COLS if c in enriched.columns]))
    enriched = pl.concat(parts, how='diagonal_relaxed')
    # 所有数据校验完成才写派生仓库。原始行情仍只读。
    store = DataStore(settings.data_dir)
    repo = KlineRepository(store)
    try:
        for frame, path in [(inst, settings.data_dir / 'instruments' / 'instruments.parquet'),
                            (shares, settings.data_dir / 'financials' / 'shares' / 'part.parquet'),
                            (events, settings.data_dir / 'adj_factor' / 'local' / 'part.parquet')]:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_parquet(frame, path)
        repo.append_daily(raw)
        repo.append_enriched(enriched)
        repo.save_index_instruments(pl.DataFrame(indices))
        repo.append_index_daily(index_daily)
        repo.append_index_enriched(index_enriched)
    finally:
        store.db.close()
    unchanged = all(hashlib.sha256((root / name).read_bytes()).hexdigest() == value for name, value in hashes.items())
    if not unchanged:
        raise RuntimeError('检测到原始数据同时发生变化，请重新校验本次导入')
    return {'mode': 'local_stock_research', 'rows': raw.height, 'enriched_rows': enriched.height,
            'symbols': len(symbols), 'trading_days': raw['date'].n_unique(),
            'start': str(raw['date'].min()), 'end': str(raw['date'].max()),
            'index_symbols': len(indices), 'index_rows': index_daily.height,
            'close_crosscheck_rows': compare_rows, 'close_max_error': error,
            'quarantined_daily_basic': price_conflicts, 'missing_current_metadata': missing_metadata,
            'source_unchanged': unchanged, 'source_sha256': hashes,
            'stock_execution_verified': False,
            'limitations': ['尚未验收共用资金撮合和分红现金账本', '历史名称/ST状态未逐日还原',
                            '股票历史仅为当前已导入窗口，多年研究需继续补齐']}
