"""M3 路由：隔离区、隔离 CSV 导出。"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from . import service

router = APIRouter(prefix="/api/quarantine", tags=["quality"])


@router.get("")
def get_quarantine(request: Request, batch_id: str | None = None) -> dict:
    return service.payload(request.app.state.store, batch_id)


@router.get("/export.csv")
def get_quarantine_csv(request: Request, batch_id: str | None = None) -> Response:
    body, name = service.export_bytes(request.app.state.store, batch_id)
    return Response(content=body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
