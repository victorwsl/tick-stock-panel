#!/usr/bin/env python3
"""安装红利增强日频研究策略；仅本地、幂等、不覆盖已有文件。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env',override=False)
    from app.config import settings
    from app.services.personal_dividend import install
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-source',type=Path,help='仅记录原文件SHA256，不执行或复制聚宽代码')
    parser.add_argument('--index-supplement',type=Path,help='显式本地指数补录文件，只补缺失行情，冲突中止')
    args = parser.parse_args()
    if not settings.personal_local_only:
        raise SystemExit('仅允许在PERSONAL_LOCAL_ONLY隔离研究工作台安装')
    data = settings.data_dir.resolve()
    if not data.is_relative_to((ROOT/'data').resolve()):
        raise SystemExit('派生目录必须在本fork的data/下')
    try:
        print(json.dumps(install(data,args.original_source,args.index_supplement),ensure_ascii=False))
    except (ValueError,OSError,KeyError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
