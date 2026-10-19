#!/usr/bin/env python3
"""通过本仓库Tushare采集器补齐价值动量研究的财务和基准；原始库只读。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

FIELDS = ('ts_code', 'ann_date', 'end_date', 'roe', 'netprofit_margin')


def normalize(frame):
    f = frame[list(FIELDS)].drop_duplicates().copy()
    for c in FIELDS[:3]:
        f[c] = f[c].astype('string')
    valid = f.ann_date.str.fullmatch(r'\d{8}', na=False) & f.end_date.str.fullmatch(r'\d{8}', na=False) & f.ann_date.ge(f.end_date)
    invalid = f[~valid]
    f = f[valid]
    conflict = f.duplicated(list(FIELDS[:3]), keep=False)
    quarantine = pd.concat([invalid, f[conflict]])
    blocked = f[conflict].drop_duplicates(list(FIELDS[:3])).copy()
    blocked[['roe', 'netprofit_margin']] = float('nan')
    return pd.concat([f[~conflict], blocked]).sort_values(list(FIELDS[:3])), quarantine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector-root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--env-file', type=Path, help='读取已有Tushare环境配置')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
    from app.services.personal_tushare import TushareClient, fetch_period_pages, frame_from_payload, load_env, quarter_end_periods
    if args.env_file:
        load_env(args.env_file)

    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'manifest.json').exists():
        raise ValueError('已封存目录不能覆盖')
    client = TushareClient(max_calls_per_min=30)
    frames = []
    for period in quarter_end_periods('20221231', '20260930'):
        path = out / (period + '.parquet')
        if path.exists():
            f = pd.read_parquet(path)
        else:
            f = fetch_period_pages(client, period, FIELDS, page_size=5000)
            if f.empty or not f.end_date.astype(str).eq(period).all():
                raise ValueError('财务季度为空或错位：' + period)
            f.to_parquet(path, index=False)
        frames.append(f)
        print(period, len(f), flush=True)
    clean, quarantine = normalize(pd.concat(frames, ignore_index=True))
    clean.to_parquet(out / 'metrics.parquet', index=False)
    quarantine.to_parquet(out / 'quarantine.parquet', index=False)
    frames = []
    for year in range(2022, 2027):
        path = out / f'index_{year}.parquet'
        if path.exists():
            f = pd.read_parquet(path)
        else:
            f = frame_from_payload(client.query('index_daily', {'ts_code': '000985.CSI', 'start_date': f'{year}0101',
                                'end_date': min(f'{year}1231', '20261016')}, 'ts_code,trade_date,open,close'), 'ts_code,trade_date,open,close')
            if f.empty:
                raise ValueError(f'中证全指{year}数据为空')
            f.to_parquet(path, index=False)
        frames.append(f)
    index = pd.concat(frames, ignore_index=True).sort_values('trade_date')
    if index.duplicated(['ts_code', 'trade_date']).any():
        raise ValueError('指数日期重复')
    index.to_parquet(out / 'index.parquet', index=False)
    manifest = {'fields': FIELDS, 'financial_rows': len(clean), 'quarantined': len(quarantine),
                'index_rows': len(index), 'command': sys.argv, 'source': client.endpoint,
                'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob('*.parquet')},
                'limitation': 'ROE及净利率为供应商累计报告口径，历史原位修订未独立验证。'}
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print('已封存', out, flush=True)


if __name__ == '__main__':
    main()
