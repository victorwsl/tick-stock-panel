#!/usr/bin/env python3
"""Explicit ETF ingestion: C07 audit -> single-fund probe -> resumable private staging."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.etf_research.audit import digest, safe_path
from app.services.etf_research.fetch import TushareClient, make_plan, run_fetch
from app.services.mining_process_lock import MiningProcessLockError
from app.services.personal_tushare import get_endpoint, get_token, load_env


def main():
    parser = argparse.ArgumentParser(description="ETF显式暂存下载，不修改原缓存或启动Web")
    parser.add_argument("--audit", required=True, type=Path, help="C07已封存的audit.json")
    parser.add_argument("--data-dir", required=True, type=Path, help="独立试用DATA_DIR")
    parser.add_argument("--fetch-id", required=True)
    parser.add_argument("--phase", choices=("probe", "gaps"), default="probe")
    parser.add_argument("--probe-dir", type=Path, help="gaps阶段须提供同一审计的完整小请求暂存目录")
    parser.add_argument("--max-calls", type=int, default=5, help="本次网络请求总预算，包含重试；相同ID可续传")
    parser.add_argument("--env-file", type=Path, help="只读取已有Tushare配置，不复制到产物")
    args = parser.parse_args()
    try:
        audit_file = safe_path(args.audit)
        manifest = json.loads(safe_path(audit_file.parent / "manifest.json").read_text())
        signature = digest(audit_file)
        if manifest.get("status") != "audit_only" or manifest.get("files", {}).get("audit.json") != signature:
            raise ValueError("C07审计清单或文件哈希不符")
        plan = make_plan(json.loads(audit_file.read_text()), signature, args.phase)
        if args.env_file:
            load_env(safe_path(args.env_file))
        client = TushareClient(get_token(), endpoint=get_endpoint())
        result = run_fetch(args.data_dir, args.fetch_id, plan, client, max_calls=args.max_calls, probe_dir=args.probe_dir)
    except KeyboardInterrupt:
        print("ETF采集已中断，有效分块保留；使用相同参数和暂存编号继续。", file=sys.stderr)
        return 130
    except (ValueError, TypeError, KeyError, OSError, MiningProcessLockError) as exc:
        print(f"ETF采集未完成（{type(exc).__name__}）：请检查凭证、已核对的服务地址、输入文件和暂存计划；修正后重试。", file=sys.stderr)
        return 1
    counts = Counter(x["status"] for x in result["records"].values())
    print(json.dumps({"status": result["status"], "calls": result["calls_this_run"], "chunks": counts,
                      "ready_for_backtest": False}, ensure_ascii=False))
    if result["status"] != "staged_complete":
        print("ETF采集尚未完成：请检查requests.json中的状态；有效分块保留，旧数据仍可使用。", file=sys.stderr)
    return {"staged_complete": 0, "partial": 2, "needs_review": 2, "blocked": 3}.get(result["status"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
