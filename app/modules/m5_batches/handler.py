"""M5 路由：批次历史、批次详情、重算、失败重试、中断恢复。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import resume, service

router = APIRouter(prefix="/api/batches", tags=["batches"])


class RerunBody(BaseModel):
    rule_version: str | None = None


@router.get("")
def get_batches(request: Request, limit: int = 40) -> dict:
    store = request.app.state.store
    settings = request.app.state.settings
    if limit <= 0 or limit > 200:
        from ... import errors
        raise errors.BadRequest("limit 需在 1..200 之间", code="E_BAD_LIMIT")
    return service.list_payload(store, settings, limit=limit)


@router.get("/stages")
def get_stages() -> dict:
    return {"stages": service.stage_flow()}


@router.get("/{batch_id}")
def get_batch(request: Request, batch_id: str) -> dict:
    return service.detail(request.app.state.store, batch_id)


@router.post("/{batch_id}/rerun")
def post_rerun(request: Request, batch_id: str, body: RerunBody | None = None) -> dict:
    """按批次留存原件重算；缺原件的旧批次返回 422 明确拒绝。"""
    store = request.app.state.store
    settings = request.app.state.settings
    version = body.rule_version if body else None
    return service.rerun(store, settings, batch_id, rule_version=version)


@router.post("/{batch_id}/retry")
def post_retry(request: Request, batch_id: str, body: RerunBody | None = None) -> dict:
    """失败/中断批次重试：沿用留存原件，不重新读源表。"""
    store = request.app.state.store
    settings = request.app.state.settings
    version = body.rule_version if body else None
    return service.retry(store, settings, batch_id, rule_version=version)


@router.get("/{batch_id}/recovery")
def get_recovery(request: Request, batch_id: str) -> dict:
    return resume.recovery_plan(request.app.state.store,
                                request.app.state.settings, batch_id)
