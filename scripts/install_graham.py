#!/usr/bin/env python3
"""显式安装Graham三组自定义策略与只读本地输入指针；不覆盖人工修改。"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env', override=False)
    from app.config import settings
    from app.plugins.local_parquet.provider import source_path
    from app.strategy.engine import StrategyEngine
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dividend-source', type=Path, required=True)
    args = parser.parse_args()
    if not settings.personal_local_only:
        raise SystemExit('仅允许在PERSONAL_LOCAL_ONLY隔离工作台安装')
    data = settings.data_dir.resolve()
    if not data.is_relative_to((ROOT/'data').resolve()):
        raise SystemExit('派生目录必须在本fork的data/下')
    imported = json.loads((data/'personal_stock_import.json').read_text())
    price = [Path(k) for k in imported['source_sha256'] if Path(k).is_absolute() and Path(k).name == 'daily.parquet']
    if len(price) != 1:
        raise SystemExit('未找到唯一的已导入原始日线快照')
    root = source_path().parents[1]
    paths = {'daily': price[0], 'adj': price[0].with_name('adj_factor.parquet'),
             'limits': price[0].with_name('stk_limit.parquet'), 'names': price[0].with_name('namechange.parquet'),
             'calendar': root/'raw/trade_cal.parquet', 'basic': root/'raw/daily_basic.parquet',
             'stocks': root/'raw/stock_basic.parquet', 'income': root/'raw/income.parquet',
             'balance': root/'raw/balancesheet.parquet', 'cashflow': root/'raw/cashflow.parquet',
             'dividends': args.dividend_source.resolve()}
    for key, path in paths.items():
        if not path.is_file():
            raise SystemExit(f'缺少{key}：{path}')
    payload = json.dumps({'version': 1, 'paths': {k: str(v) for k, v in paths.items()}}, ensure_ascii=False, indent=2)+'\n'
    targets = {data/'graham_inputs/sources.json': payload}
    for variant in ('s0', 's1', 's2'):
        source = ROOT/f'backend/app/strategy/personal/graham_{variant}.py'
        target = data/f'strategies/custom/custom_graham_{variant}.py'
        targets[target] = source.read_text()
        StrategyEngine._load_file(source)  # 所有文件先通过真实安全校验，再落盘。
    for target, text in targets.items():
        if target.exists() and target.read_text() != text:
            raise SystemExit(f'已有不同内容，未覆盖：{target}')
    for target, text in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with target.open('x') as f:
                f.write(text)
    print(json.dumps({'installed': [f'custom_graham_{v}' for v in ('s0','s1','s2')],
                      'data_dir': str(data), 'production_ready': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()
