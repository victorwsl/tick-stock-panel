"""真实活跃市值的只读展示与收盘开仓门禁，共用同一状态机。"""
from __future__ import annotations

import hashlib
from datetime import date

import numpy as np
import polars as pl

from app.indicators.zhixing import add_zhixing_lines
from app.plugins.local_parquet.provider import LocalParquetProvider, source_path

LABELS = {'risk_off': '防守', 'probe': '试探', 'confirmed': '确认', 'unknown': '缺数据'}


def source_fingerprint() -> str:
    path = source_path().with_name('compass_0amv.parquet')
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'missing'


def calculate_states(frame: pl.DataFrame) -> pl.DataFrame:
    frame = frame.sort('date').with_columns(
        (pl.col('close') / pl.col('close').shift(1) - 1).alias('r1'),
        (pl.col('close') / pl.col('close').shift(3) - 1).alias('r3'),
    )
    state = 'risk_off'
    states = []
    for r1, r3 in frame.select('r1', 'r3').iter_rows():
        if r1 is not None and r1 <= -0.023:
            state = 'risk_off'
        elif state == 'risk_off' and r1 is not None and r1 >= 0.04:
            state = 'confirmed' if r3 is not None and r3 >= 0.06 else 'probe'
        elif state == 'probe' and r3 is not None and r3 >= 0.06:
            state = 'confirmed'
        states.append(state)
    return frame.with_columns(pl.Series('amv_state', states))


def load_history() -> pl.DataFrame:
    return calculate_states(LocalParquetProvider().get_active_mv())


def history_payload(start: date | None = None, end: date | None = None, limit: int = 250) -> dict:
    fingerprint = source_fingerprint()
    frame = load_history()
    if fingerprint != source_fingerprint():
        raise ValueError('0AMV正在同步，请同步完成后重试')
    total = frame.height
    earliest = str(frame['date'].min()) if total else None
    latest = str(frame['date'].max()) if total else None
    frame = add_zhixing_lines(frame.with_columns(pl.lit('0AMV').alias('symbol')))
    if start:
        frame = frame.filter(pl.col('date') >= start)
    if end:
        frame = frame.filter(pl.col('date') <= end)
    if start is None and end is None and limit:
        frame = frame.tail(limit)
    cols = ['date', 'open', 'high', 'low', 'close', 'zx_white', 'zx_yellow', 'zx_long', 'r1', 'r3', 'amv_state']
    rows = frame.select(cols).with_columns(pl.col('date').cast(pl.String)).to_dicts()
    return {'rows': rows, 'total': total, 'earliest': earliest, 'latest': latest,
            'source_sha256': fingerprint, 'unit': '指南针原始刻度（未换算）',
            'rule': '单日+4%启动；三日+6%确认；单日−2.3%防守；收盘确认，次日开盘生效'}


def entry_mask(labels, mode: str) -> np.ndarray:
    if mode not in {'active', 'confirmed'}:
        raise ValueError('未知0AMV过滤模式，仅支持active/confirmed')
    # 必须先算完整历史状态，再裁剪；窗口起点不能重置为防守。
    fingerprint = source_fingerprint()
    states = {str(r['date']): r['amv_state'] for r in load_history().iter_rows(named=True)}
    if fingerprint != source_fingerprint():
        raise ValueError('0AMV正在同步，请同步完成后重试')
    allowed = {'probe', 'confirmed'} if mode == 'active' else {'confirmed'}
    # 此处索引是T日信号，不是成交日。引擎随后整体移到T+1，不能再shift一次。
    return np.array([states.get(str(label)[:10], 'unknown') in allowed for label in labels], dtype=bool)
