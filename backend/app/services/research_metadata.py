"""Small, atomic local annotations, separate from immutable research results."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

metadata_lock = RLock()
STRATEGY_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class InvalidParentError(ValueError):
    pass


class HistoryMetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=4000)
    favorite: StrictBool = False


class StrategyMetadataPatch(HistoryMetadataPatch):
    category: Literal["smallcap", "dividend", "trend", "other"] = "other"
    parent_id: str | None = Field(default=None, max_length=128, pattern=STRATEGY_ID.pattern)


def read_metadata(path: Path) -> dict:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("标注文件不能是符号链接")
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("标注文件格式错误")
    return value


def write_metadata(path: Path, value: dict) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("标注路径不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".metadata-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def list_strategy_metadata(data_dir: Path, strategy_ids: set[str]) -> dict:
    with metadata_lock:
        stored = read_metadata(Path(data_dir) / "research_metadata" / "strategies.json")
        return {sid: StrategyMetadataPatch.model_validate(meta).model_dump()
                for sid, meta in stored.items() if sid in strategy_ids}


def patch_strategy_metadata(data_dir: Path, strategy_id: str, patch: StrategyMetadataPatch,
                            strategy_ids: set[str]) -> dict:
    if not STRATEGY_ID.fullmatch(strategy_id) or strategy_id not in strategy_ids:
        raise FileNotFoundError(strategy_id)
    path = Path(data_dir) / "research_metadata" / "strategies.json"
    with metadata_lock:
        stored = read_metadata(path)
        current = StrategyMetadataPatch.model_validate(stored.get(strategy_id, {})).model_dump()
        updated = {**current, **patch.model_dump(exclude_unset=True)}
        parent = updated["parent_id"]
        visited = {strategy_id}
        while parent is not None:
            if parent not in strategy_ids:
                raise InvalidParentError("父策略不存在")
            if parent in visited:
                raise InvalidParentError("父策略关系不能形成循环")
            visited.add(parent)
            parent = StrategyMetadataPatch.model_validate(stored.get(parent, {})).parent_id
        stored[strategy_id] = updated
        write_metadata(path, stored)
        return {"id": strategy_id, **updated}
