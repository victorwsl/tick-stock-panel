"""Local immutable strategy backtests; publish result and summary together."""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)
_ID = re.compile(r"[0-9a-f]{32}")


def save_result(data_dir: Path, result: dict, config: dict) -> dict:
    """A save failure must not discard the calculated result or look like success."""
    if result.get("error"):
        return result
    root = Path(data_dir) / "backtest_history"
    tmp = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        history_id = uuid4().hex
        snapshot = {**result, "history_id": history_id,
                    "config": {**config, **result.get("config", {})}}
        summary = {
            "id": history_id, "run_id": result.get("run_id"),
            "saved_at": datetime.now(UTC).isoformat(),
            "name": result.get("strategy_info", {}).get("name") or config.get("strategy_id", "策略"),
            "start": snapshot["config"].get("start"),
            "end": snapshot["config"].get("end"),
            "asset_type": config.get("asset_type", "stock"),
            "mode": config.get("mode", "position"),
            "trade_count": len(result.get("trades", [])),
        }
        tmp = Path(tempfile.mkdtemp(prefix=".saving-", dir=root))
        for name, payload in (("result", snapshot), ("summary", summary)):
            with (tmp / f"{name}.json").open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, allow_nan=False)
        tmp.rename(root / history_id)
        return snapshot
    except (OSError, ValueError, TypeError):
        logger.exception("Failed to persist strategy backtest")
        return {**result, "history_error": "回测已完成，但历史记录保存失败，请检查磁盘空间和数据目录权限。"}
    finally:
        if tmp is not None and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def list_results(data_dir: Path) -> dict:
    items, unavailable = [], 0
    for folder in (Path(data_dir) / "backtest_history").glob("*"):
        if not _ID.fullmatch(folder.name):
            continue
        try:
            items.append(json.loads((folder / "summary.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            unavailable += 1
            logger.warning("Unreadable backtest summary: %s", folder.name)
    items.sort(key=lambda item: item["saved_at"], reverse=True)
    return {"items": items, "unavailable": unavailable}


def load_result(data_dir: Path, history_id: str) -> dict:
    if not _ID.fullmatch(history_id):
        raise FileNotFoundError(history_id)
    return json.loads((Path(data_dir) / "backtest_history" / history_id / "result.json").read_text(encoding="utf-8"))
