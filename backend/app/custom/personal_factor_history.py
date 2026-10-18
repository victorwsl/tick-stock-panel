"""Persist batch factor screens using the existing atomic backtest archive."""
from fastapi import APIRouter, HTTPException, Request

from app.api.backtest import FactorBatchRequest, factor_batch
from app.config import settings
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services.backtest_history import list_results, load_result, save_result

EXTENSION_ID = "personal.factor_history"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
router = APIRouter(prefix="/api/custom/factor-history", tags=["factor-history"])


def _root():
    # Keep factor runs separate from strategy history without a second storage implementation.
    return settings.data_dir / "factor_research"


@router.post("/run")
def run(req: FactorBatchRequest, request: Request):
    result = factor_batch(req, request)
    return save_result(_root(), result, result.get("config", {}))


@router.get("")
def history():
    return list_results(_root())


@router.get("/{history_id}")
def detail(history_id: str):
    try:
        return load_result(_root(), history_id)
    except FileNotFoundError:
        raise HTTPException(404, "因子筛选记录不存在") from None
    except (OSError, ValueError):
        raise HTTPException(500, "因子筛选记录读取失败，请检查本地文件后重试") from None


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
