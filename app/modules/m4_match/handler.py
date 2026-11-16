"""M4 路由：三单对照、匹配明细、差异明细、四账。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ... import config, errors, web
from . import service

router = APIRouter(prefix="/api", tags=["match"])


def _batch_id(request: Request, batch_id: str | None) -> str:
    if batch_id:
        batch = request.app.state.store.get_batch(batch_id)
        if batch is None:
            raise errors.NotFound(f"批次不存在：{batch_id}", code="E_BATCH_NOT_FOUND")
        return batch_id
    batch = web.get_ctx(request).current_batch(None)
    if batch is None:
        raise errors.NotFound("还没有运行过批次", code="E_BATCH_NOT_FOUND")
    return batch["batch_id"]


@router.get("/trios")
def get_trios(request: Request, batch_id: str | None = None) -> dict:
    store = request.app.state.store
    resolved = _batch_id(request, batch_id)
    payload = service.trios(store, resolved)
    payload["batch_id"] = resolved
    return payload


@router.get("/matches")
def get_matches(request: Request, batch_id: str | None = None,
                verdict: str | None = None) -> dict:
    store = request.app.state.store
    resolved = _batch_id(request, batch_id)
    payload = service.matches_payload(store, resolved, verdict=verdict)
    payload["batch_id"] = resolved
    payload["verdicts"] = [{"key": key, "label": config.VERDICT_LABELS[key]}
                           for key in config.VERDICTS]
    return payload


@router.get("/diffs")
def get_diffs(request: Request, batch_id: str | None = None,
              kind: str | None = None) -> dict:
    store = request.app.state.store
    resolved = _batch_id(request, batch_id)
    payload = service.diffs_payload(store, resolved, kind=kind)
    payload["batch_id"] = resolved
    payload["kinds"] = [{"key": key, "label": config.DIFF_LABELS[key]}
                        for key in config.DIFF_KINDS]
    return payload


@router.get("/reconcile")
def get_reconcile(request: Request, batch_id: str | None = None) -> dict:
    store = request.app.state.store
    resolved = _batch_id(request, batch_id)
    payload = service.reconcile_payload(store, resolved)
    payload["batch_id"] = resolved
    return payload
