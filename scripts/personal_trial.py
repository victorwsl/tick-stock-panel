#!/usr/bin/env python3
"""个人试用入口：只读检查、本地股票与指数导入、单服务启动；不杀进程。"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    from app.config import settings
    from app.plugins.local_parquet.provider import LocalParquetProvider, source_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["review-strategy", "install-strategy", "check", "import-financial", "import-index", "import-stock", "sync-stock", "import-sectors", "import-regime", "import-etf", "sync-etf", "serve"])
    parser.add_argument("--days", type=int, default=120, help="补股票数据的交易日数，默认120")
    parser.add_argument("--etf-source", action="append", type=Path, default=[], help="包含原始价、qfq价及累计因子的本地ETF研究文件，可重复")
    parser.add_argument("--stock-source", type=Path, help="只读原始日线和累计复权因子快照目录；仅import-stock使用")
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-10-16")
    args = parser.parse_args()
    if args.stock_source is not None and args.action != "import-stock":
        parser.error("--stock-source 仅用于 import-stock；不会先联网再忽略补数缓存")
    if not settings.personal_local_only:
        raise SystemExit("本入口要求 PERSONAL_LOCAL_ONLY=true")
    data_dir = settings.data_dir.resolve()
    source_root = source_path().parents[1]
    if data_dir == source_root or data_dir.is_relative_to(source_root):
        raise SystemExit("试用写入目录不能位于原始市场数据根目录")
    if not data_dir.is_relative_to((ROOT / "data").resolve()):
        raise SystemExit("试用写入目录须位于本 fork 的 data/ 下")
    import_lock = None
    if args.action.startswith(("import-", "sync-")):
        import fcntl
        data_dir.mkdir(parents=True, exist_ok=True)
        import_lock = (data_dir / ".personal_import.lock").open("a")
        try:
            fcntl.flock(import_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("另一个本地导入正在运行，请结束后重试；未开始新导入") from None
    if args.action == "review-strategy":
        from datetime import date
        from app.services.personal_zhixing_review import review_strategy
        print(json.dumps(review_strategy(data_dir, date.fromisoformat(args.start), date.fromisoformat(args.end)), ensure_ascii=False, indent=2))
        return
    if args.action == "install-strategy":
        from app.services.personal_zhixing import install_strategy
        print(json.dumps(install_strategy(data_dir), ensure_ascii=False, indent=2))
        return
    if args.action == "import-financial":
        from app.services.personal_local_financials import import_financials
        result = import_financials()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.action in {"import-sectors", "import-regime", "import-etf", "sync-etf"}:
        from app.services.personal_local_assets import import_etfs, import_regime, import_sectors, seed_etfs, sync_etfs
        if args.action == "import-sectors":
            result = import_sectors()
        elif args.action == "import-regime":
            result = import_regime()
        else:
            if args.etf_source:
                seed_etfs(args.etf_source)
            if args.action == "sync-etf":
                sync_etfs()
            result = import_etfs()
        (data_dir / ("personal_" + args.action.replace("-", "_") + ".json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    if args.action in {"import-stock", "sync-stock"}:
        from app.services.personal_local_data import fetch_missing, import_stocks
        if args.action == "sync-stock":
            fetch_missing(args.days)
        result = import_stocks(stock_root=args.stock_source)
        (data_dir / "personal_stock_import.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    provider = LocalParquetProvider()
    instruments = provider.get_instruments("index")
    symbols = [row["symbol"] for row in instruments]
    daily = provider.get_daily(symbols, asset_type="index")
    result = {
        "mode": "local_index_trial", "source": str(source_path()),
        "data_dir": str(data_dir), "rows": daily.height, "symbols": symbols,
        "start": str(daily["date"].min()), "end": str(daily["date"].max()),
        "stock_backtest_ready": False,
    }
    if args.action == "import-index":
        import polars as pl
        from app.indicators.pipeline import compute_indicators
        from app.tickflow.repository import DataStore, KlineRepository
        original_hash = hashlib.sha256(source_path().read_bytes()).hexdigest()
        store = DataStore(data_dir)
        repo = KlineRepository(store)
        try:
            repo.save_index_instruments(pl.DataFrame(instruments))
            repo.append_index_daily(daily)
            repo.append_index_enriched(compute_indicators(daily))
            assert original_hash == hashlib.sha256(source_path().read_bytes()).hexdigest()
            result["source_sha256"] = original_hash
            result["source_unchanged"] = True
            result["status"] = "index_imported_stock_execution_unverified"
            (data_dir / "personal_import.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            store.db.close()
    elif args.action == "serve":
        if settings.host != "127.0.0.1":
            raise SystemExit("试用仅监听127.0.0.1；远程访问方式另行配置")
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", settings.port)) == 0:
                raise SystemExit(f"端口{settings.port}已被占用；未结束任何进程")
        if not (settings.static_dir / "index.html").is_file():
            raise SystemExit("请先构建前端；frontend/dist/index.html不存在")
        import uvicorn
        uvicorn.run("app.main:app", host=settings.host, port=settings.port)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
