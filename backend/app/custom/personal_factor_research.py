"""Personal frozen-factor verification, through the supported router extension."""
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BeforeValidator

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services import personal_factor_research as research

EXTENSION_ID = "personal.factor_research"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION
router = APIRouter(prefix="/api/custom/factor-research", tags=["personal-factor-research"])


def _data(request: Request) -> Path:
    return request.app.state.repo.store.data_dir


def _call(fn, *args):
    try:
        return fn(*args)
    except FileNotFoundError:
        raise HTTPException(404, "因子实验不存在，请先导入冻结研究数据") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("")
def studies(request: Request):
    return {"studies": _call(research.list_studies, _data(request))}


Horizon = Annotated[Literal[10, 20, 60, 120, 250], BeforeValidator(int)]
TopK = Annotated[Literal[1, 3, 5], BeforeValidator(int)]
Cohort = Literal["own_mature", "common_250d_mature"]


@router.get("/{study_id}")
def summary(study_id: str, request: Request, horizon: Horizon = 10, k: TopK = 3,
            cohort: Cohort = "own_mature"):
    return _call(research.summary, _data(request), study_id, horizon, k, cohort)


@router.get("/{study_id}/detail")
def detail(study_id: str, request: Request, factor: str, period: str, horizon: Horizon = 10, k: TopK = 3,
           cohort: Cohort = "own_mature"):
    return _call(research.detail, _data(request), study_id, factor, horizon, k, cohort, period)


@router.post("/{study_id}/verify")
def verify(study_id: str, request: Request):
    return _call(research.verify, _data(request), study_id)


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
