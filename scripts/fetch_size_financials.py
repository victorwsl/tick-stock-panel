#!/usr/bin/env python3
"""Fetch only the missing CH earnings inputs using this repository's Tushare collector.

Run with this repository's backend Python environment. Output is an isolated source cache;
the canonical financial store and production factor library are never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

FIELDS = ('ts_code', 'ann_date', 'end_date', 'profit_dedt', 'update_flag')
KEY = ['ts_code', 'ann_date', 'end_date']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize(frame):
    missing = set(FIELDS)-set(frame.columns)
    if missing:
        raise ValueError('财务接口缺字段：'+','.join(sorted(missing)))
    f = frame[list(FIELDS)].copy()
    for c in [*KEY, 'update_flag']:
        f[c] = f[c].astype('string')
    f['profit_dedt'] = pd.to_numeric(f.profit_dedt, errors='raise')
    valid = (f.ann_date.str.fullmatch(r'\d{8}', na=False)
             & f.end_date.str.fullmatch(r'\d{8}', na=False)
             & f.ann_date.ge(f.end_date))
    invalid = f.loc[~valid].copy()
    f = f.loc[valid].drop_duplicates()
    # A current update flag cannot establish when an in-place revision was known.
    # Quarantine conflicting values under the same announcement key, never pick one.
    conflicts = f.groupby(KEY, dropna=False).profit_dedt.nunique(dropna=False)
    conflict_keys = conflicts.loc[conflicts > 1].reset_index()[KEY]
    f = f.merge(conflict_keys.assign(_conflict=True), on=KEY, how='left', validate='m:1')
    quarantine = pd.concat([invalid, f.loc[f._conflict.eq(True)].drop(columns='_conflict')], ignore_index=True)
    clean = f.loc[~f._conflict.eq(True)].drop(columns='_conflict').drop_duplicates(KEY)
    # Keep a missing-value marker for a known but conflicting announcement. Dropping
    # its key would let the research reader silently fall back to an older quarter.
    blocked = conflict_keys.assign(profit_dedt=float('nan'), update_flag=pd.NA)
    clean = pd.concat([clean, blocked], ignore_index=True)
    return clean.sort_values(['end_date','ts_code','ann_date']), quarantine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector-root', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--env-file', type=Path, help='读取已有Tushare环境配置')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--start', default='20141231')
    parser.add_argument('--end', default='20260930')
    parser.add_argument('--max-calls-per-min', type=int, default=30)
    parser.add_argument('--cached-periods', type=Path, help='Reuse previously fetched raw quarter files for revalidation')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
    from app.services.personal_tushare import (
        TushareClient,
        fetch_period_pages,
        load_env,
        quarter_end_periods,
    )
    if args.env_file:
        load_env(args.env_file)

    periods = quarter_end_periods(args.start, args.end)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output/'manifest.json').exists():
        raise ValueError('已封存的采集目录不能覆盖，请指定新目录')
    pages = output/'periods'
    pages.mkdir(exist_ok=True)
    client = TushareClient(max_calls_per_min=args.max_calls_per_min)
    frames, audit = [], []
    for i, period in enumerate(periods, 1):
        target = pages/(period+'.parquet')
        if target.exists():
            frame = pd.read_parquet(target)
        elif args.cached_periods and (args.cached_periods/target.name).is_file():
            frame = pd.read_parquet(args.cached_periods/target.name)
            frame.to_parquet(target, index=False)
        else:
            frame = fetch_period_pages(client, period, FIELDS, page_size=5000)
            if frame.empty or not frame.end_date.astype(str).eq(period).all():
                raise ValueError('季度数据为空或报告期错位：'+period)
            stage = target.with_suffix('.next.parquet')
            frame.to_parquet(stage, index=False)
            stage.replace(target)
        clean, quarantine = normalize(frame)
        valid = int(clean.profit_dedt.notna().sum())
        audit.append(dict(period=period, rows=len(frame), clean_rows=len(clean),
                          valid_earnings=valid, quarantined=len(quarantine)))
        frames.append(frame)
        print(f'{i}/{len(periods)} {period}: rows={len(frame)} earnings={valid} quarantine={len(quarantine)}', flush=True)
    clean, quarantine = normalize(pd.concat(frames, ignore_index=True))
    clean.to_parquet(output/'fina_indicator.parquet', index=False)
    quarantine.to_parquet(output/'quarantine.parquet', index=False)
    pd.DataFrame(audit).to_csv(output/'coverage.csv', index=False)
    manifest = dict(status='fetched_validated', fetched_at=datetime.now(UTC).isoformat(),
                    api='fina_indicator_vip', fields=FIELDS, start=args.start, end=args.end,
                    rows=len(clean), nonnull_earnings=int(clean.profit_dedt.notna().sum()),
                    quarantined=len(quarantine), command=sys.argv, code_sha256=sha(__file__),
                    source=client.endpoint,
                    collector_sha256=sha(Path(__file__).resolve().parents[1]/'backend/app/services/personal_tushare.py'),
                    files={str(p.relative_to(output)):sha(p) for p in output.rglob('*') if p.is_file()},
                    limitation='公告版本保留；供应商是否事后原位改写历史值仍未独立核实。')
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print('已封存：'+str(output/'fina_indicator.parquet'), flush=True)


if __name__ == '__main__':
    main()
