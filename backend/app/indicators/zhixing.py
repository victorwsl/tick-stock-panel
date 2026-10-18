"""知行趋势线：白线双EMA10，黄线MA14/28/57/114均值，长期MA114。"""
import polars as pl


def add_zhixing_lines(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty():
        return frame
    frame = frame.sort(['symbol', 'date'])
    return frame.with_columns(
        pl.col('close').ewm_mean(span=10, adjust=False).over('symbol').alias('_zx_ema10'),
        (sum(pl.col('close').rolling_mean(n).over('symbol') for n in (14, 28, 57, 114)) / 4).alias('zx_yellow'),
        pl.col('close').rolling_mean(114).over('symbol').alias('zx_long'),
    ).with_columns(
        pl.col('_zx_ema10').ewm_mean(span=10, adjust=False).over('symbol').alias('zx_white'),
    ).drop('_zx_ema10')
