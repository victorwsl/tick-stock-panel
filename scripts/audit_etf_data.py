#!/usr/bin/env python3
"""Offline ETF gap audit; all input locations and dates must be explicit."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.etf_research.audit import load_audit, write_report


def main():
    parser = argparse.ArgumentParser(description="ETF只读缺口审计；不联网，不补数，不启动Web")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--registry", type=Path, help="预登记U1/U2的JSON名单")
    parser.add_argument("--suspensions", type=Path, help="带来源证据的ETF停牌Parquet")
    parser.add_argument("--output", type=Path, required=True, help="新的审计报告目录")
    args = parser.parse_args()
    try:
        report = load_audit(args.data_dir, args.calendar, args.start, args.end,
                            registry_file=args.registry, suspension_file=args.suspensions)
        files = [p for p in (args.calendar, args.registry, args.suspensions) if p is not None]
        write_report(report, args.output, data_dir=args.data_dir, protected_files=files)
    except (ValueError, TypeError, KeyError, OSError, pl.exceptions.PolarsError) as exc:
        print(f"ETF审计未完成（{type(exc).__name__}）：请检查输入结构、路径或并发导入；修正后使用新目录重试。", file=sys.stderr)
        return 1
    print(json.dumps({"status": "audit_only", "output": str(args.output), "members": len(report["items"]),
                      "gap_segments": len(report["gaps"]), "calendar_unknown_days": sum(x["days"] for x in report["calendar"]["unknown_ranges"]),
                      "ready_for_backtest": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
