"""M7 路由：复核台账（重复发票、红字冲减、供应商汇总）。

`GET /api/audit` 一次给全页数据；重复组与供应商汇总各有单独入口，便于导出与脚本调用。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from . import service

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def get_audit(request: Request, batch_id: str | None = None,
              kind: str | None = None) -> dict:
    settings = request.app.state.settings
    store = request.app.state.store
    return service.payload(settings, store, batch_id, dup_kind=kind or None)


@router.get("/dups")
def get_dups(request: Request, batch_id: str | None = None,
             kind: str | None = None) -> dict:
    settings = request.app.state.settings
    store = request.app.state.store
    return service.dups_payload(settings, store, batch_id, kind)


@router.get("/vendors")
def get_vendors(request: Request, batch_id: str | None = None) -> dict:
    settings = request.app.state.settings
    store = request.app.state.store
    return service.vendors_payload(settings, store, batch_id)
