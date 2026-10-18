#!/usr/bin/env python3
"""显式安装聚宽小市值复现策略；复用已登记的只读历史输入，不覆盖手工修改。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env',override=False)
    from app.config import settings
    from app.strategy.engine import StrategyEngine
    if not settings.personal_local_only:
        raise SystemExit('仅允许在PERSONAL_LOCAL_ONLY隔离面板安装')
    data=settings.data_dir.resolve()
    if not data.is_relative_to((ROOT/'data').resolve()) or not (data/'graham_inputs/sources.json').is_file():
        raise SystemExit('须先登记只读历史输入 sources.json；请参考Graham安装说明')
    source=ROOT/'backend/app/strategy/personal/joinquant_smallcap.py'
    StrategyEngine._load_file(source)
    target=data/'strategies/custom/custom_joinquant_smallcap.py'
    if target.exists() and target.read_text()!=source.read_text():
        raise SystemExit('已有不同策略内容，未覆盖：'+str(target))
    target.parent.mkdir(parents=True,exist_ok=True)
    if not target.exists():
        with target.open('x') as f:
            f.write(source.read_text())
    print('已安装：custom_joinquant_smallcap')


if __name__=='__main__':
    main()
