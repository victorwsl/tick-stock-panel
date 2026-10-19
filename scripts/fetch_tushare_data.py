#!/usr/bin/env python3
"""显式拉取缺失的Tushare原始请求并持久化；重复请求从本地读取。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.mining_process_lock import MiningProcessLockError
from app.services.personal_tushare import DATASETS, FetchError, load_env, persist_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True, type=Path, help="独立原始采集目录，不是正在使用的行情分区")
    parser.add_argument("--asset", required=True, choices=("stock", "etf", "index", "calendar", "financial"))
    parser.add_argument("--api", required=True, choices=tuple(DATASETS))
    parser.add_argument("--params", default="{}", help="JSON请求参数；禁止传入凭证")
    parser.add_argument("--fields", required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    try:
        if args.env_file:
            load_env(args.env_file)
        path, receipt, hit = persist_request(args.cache_dir, args.asset, args.api, json.loads(args.params), args.fields)
    except (FetchError, ValueError, TypeError, KeyError, OSError, MiningProcessLockError) as exc:
        kind = exc.kind if isinstance(exc, FetchError) else type(exc).__name__
        print(f"Tushare采集未完成（{kind}）；请检查配置、字段、请求范围及缓存完整性后重试。已有文件保留。", file=sys.stderr)
        return 1
    print(json.dumps({"path": str(path), "rows": receipt["rows"], "status": receipt["status"],
                      "cache_hit": hit, "source": receipt["request"]["source"], "ready_for_backtest": False}, ensure_ascii=False))
    return 0 if receipt["status"] == "staged" else 2


if __name__ == "__main__":
    raise SystemExit(main())
