"""M6 路由：导出入口与文件下载。"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from . import service

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("")
def get_export(request: Request, batch_id: str | None = None) -> dict:
    return service.payload(request.app.state.store, batch_id)


@router.get("/download")
def get_download(request: Request, kind: str = "adjustment",
                 batch_id: str | None = None) -> Response:
    body, name, media = service.export_bytes(request.app.state.store, kind, batch_id)
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
