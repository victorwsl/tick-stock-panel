"""Strategy annotations through the supported backend extension router."""
from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services.research_metadata import (
    InvalidParentError,
    StrategyMetadataPatch,
    list_strategy_metadata,
    patch_strategy_metadata,
)

EXTENSION_ID = "personal.research_management"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
router = APIRouter(prefix="/api/research/strategies", tags=["research"])


def _strategy_ids(request: Request) -> set[str]:
    return {item["id"] for item in request.app.state.strategy_engine.list_strategies(include_research=True)}


@router.get("")
def list_metadata(request: Request):
    try:
        return {"items": list_strategy_metadata(settings.data_dir, _strategy_ids(request))}
    except (OSError, ValueError):
        raise HTTPException(500, "策略标注读取失败，请检查本地文件") from None


@router.patch("/{strategy_id}")
def patch_metadata(strategy_id: str, patch: StrategyMetadataPatch, request: Request):
    try:
        return patch_strategy_metadata(settings.data_dir, strategy_id, patch, _strategy_ids(request))
    except FileNotFoundError:
        raise HTTPException(404, "策略不存在") from None
    except InvalidParentError as exc:
        raise HTTPException(422, str(exc)) from None
    except (OSError, ValueError):
        raise HTTPException(500, "策略标注保存失败，请检查本地文件和数据目录权限") from None


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
