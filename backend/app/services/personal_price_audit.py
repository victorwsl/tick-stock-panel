"""复核原始收盘价冲突：不改价，仅在两条证据一致时隔离daily_basic冲突行。"""
from pathlib import Path

import polars as pl


def verify_close_conflicts(raw: pl.DataFrame, basic: pl.DataFrame, root: Path) -> pl.DataFrame:
    keys = ['ts_code', 'trade_date']
    bad = raw.join(basic.select(*keys, 'basic_close'), on=keys).filter(
        (pl.col('close') - pl.col('basic_close')).abs() > 1e-6)
    if bad.is_empty():
        return bad
    qfq = pl.read_parquet(root / 'daily_qfq.parquet').join(bad.select(keys), on=keys)
    next_reference = raw.sort(keys).with_columns(
        pl.col('pre_close').shift(-1).over('ts_code').alias('next_pre_close')
    ).select(*keys, 'next_pre_close')
    checked = bad.join(qfq.select(*keys, *[pl.col(c).alias('q_' + c) for c in ('open','high','low','close')]), on=keys).join(next_reference, on=keys)
    ratio = pl.col('q_open')/pl.col('open')
    passed = (ratio > 0) & ((pl.col('next_pre_close')-pl.col('close')).abs() <= 1e-6)
    for column in ('high', 'low', 'close'):
        passed &= (pl.col('q_' + column)/ratio-pl.col(column)).abs() <= 1e-6
    checked = checked.with_columns(passed.fill_null(False).alias('corroborated'))
    if checked.height != bad.height or not checked['corroborated'].all():
        raise ValueError('Tushare未复权收盘价与本地daily_basic不一致且缺少一致复核证据，未导入')
    return checked.select(*keys, 'close', 'basic_close', 'next_pre_close', 'corroborated')
