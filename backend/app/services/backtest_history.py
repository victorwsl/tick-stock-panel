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

from app.services.research_metadata import (
    HistoryMetadataPatch,
    metadata_lock,
    read_metadata,
    write_metadata,
)

logger = logging.getLogger(__name__)
_ID = re.compile(r"[0-9a-f]{32}")


def save_result(data_dir: Path, result: dict, config: dict) -> dict:
    """A save failure must not discard the calculated result or look like success."""
    if result.get("error"):
        return result
    root = Path(data_dir) / "backtest_history"
    tmp = None
    try:
        if root.is_symlink():
            raise ValueError("历史目录不能是符号链接")
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
            "strategy_id": snapshot["config"].get("strategy_id"),
            "stats": {key: result.get("stats", {}).get(key)
                      for key in ("total_return", "max_drawdown", "win_rate")},
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


def _folder(data_dir: Path, history_id: str) -> Path:
    root = Path(data_dir) / "backtest_history"
    folder = root / history_id
    if (not _ID.fullmatch(history_id) or root.is_symlink()
            or folder.is_symlink() or not folder.is_dir()):
        raise FileNotFoundError(history_id)
    return folder


def _summary(folder: Path) -> dict:
    path = folder / "summary.json"
    if path.is_symlink():
        raise ValueError("历史摘要不能是符号链接")
    summary = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict) or not isinstance(summary.get("saved_at"), str):
        raise ValueError("历史摘要格式错误")
    stats = summary.get("stats") or {}
    if not isinstance(stats, dict):
        raise ValueError("历史统计格式错误")
    return {**summary, "id": folder.name, "strategy_id": summary.get("strategy_id"),
            "stats": {key: stats.get(key)
                      for key in ("total_return", "max_drawdown", "win_rate")}}


def list_results(data_dir: Path) -> dict:
    items, unavailable = [], 0
    root = Path(data_dir) / "backtest_history"
    if root.is_symlink():
        return {"items": [], "unavailable": 1}
    with metadata_lock:
        for folder in root.glob("*"):
            if not _ID.fullmatch(folder.name):
                continue
            try:
                folder = _folder(data_dir, folder.name)
                summary = _summary(folder)
                try:
                    meta = HistoryMetadataPatch.model_validate(read_metadata(folder / "metadata.json"))
                    summary.update(meta.model_dump())
                except (OSError, ValueError):
                    summary.update(HistoryMetadataPatch().model_dump())
                    summary["metadata_error"] = "历史标注读取失败，请检查记录文件"
                    unavailable += 1
                items.append(summary)
            except (OSError, ValueError):
                unavailable += 1
                logger.warning("Unreadable backtest summary: %s", folder.name)
    items.sort(key=lambda item: item["saved_at"], reverse=True)
    return {"items": items, "unavailable": unavailable}


def load_result(data_dir: Path, history_id: str) -> dict:
    with metadata_lock:
        path = _folder(data_dir, history_id) / "result.json"
        if path.is_symlink():
            raise FileNotFoundError(history_id)
        return json.loads(path.read_text(encoding="utf-8"))


def patch_result_metadata(data_dir: Path, history_id: str, patch: HistoryMetadataPatch) -> dict:
    with metadata_lock:
        folder = _folder(data_dir, history_id)
        summary = _summary(folder)
        current = HistoryMetadataPatch.model_validate(read_metadata(folder / "metadata.json")).model_dump()
        updated = {**current, **patch.model_dump(exclude_unset=True)}
        write_metadata(folder / "metadata.json", updated)
        return {**summary, **updated}


def delete_result(data_dir: Path, history_id: str) -> dict:
    with metadata_lock:
        folder = _folder(data_dir, history_id)
        shutil.rmtree(folder)
        return {"id": history_id}
