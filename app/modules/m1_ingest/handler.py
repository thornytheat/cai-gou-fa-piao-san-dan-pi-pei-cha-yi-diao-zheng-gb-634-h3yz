"""M1 路由：样例清单、触发导入、文件诊断。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ... import errors
from . import service

router = APIRouter(prefix="/api", tags=["ingest"])


class ImportBody(BaseModel):
    files: list[str] | None = None
    rule_version: str | None = None


@router.get("/samples")
def get_samples(request: Request) -> dict:
    settings = request.app.state.settings
    return {"files": service.sample_list(settings),
            "defaults": service.default_names(),
            "rule_version": settings.rule_version}


@router.post("/import")
def post_import(request: Request, body: ImportBody | None = None) -> dict:
    store = request.app.state.store
    settings = request.app.state.settings
    payload = body or ImportBody()
    if payload.files is not None and len(payload.files) == 0:
        raise errors.BadRequest("files 不能是空数组", code="E_UNKNOWN_FILE")
    result = service.run_import(settings=settings, store=store, names=payload.files,
                               rule_version=payload.rule_version)
    batch = result["batch"]
    inputs = store.list_inputs(batch["batch_id"])
    return {
        "batch_id": batch["batch_id"],
        "run_id": batch["run_id"],
        "reused": result["reused"],
        "status": batch["status"],
        "eval_date": batch.get("eval_date") or "",
        "input_check": service.input_check(batch, inputs),
        "counters": result["counters"],
        "stages": result["stages"],
        "quality": result.get("quality"),
        "reconcile": result.get("reconcile"),
        "adjust": result.get("adjust"),
    }


@router.get("/import/files")
def get_files(request: Request, batch_id: str | None = None) -> dict:
    store = request.app.state.store
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise errors.NotFound("还没有运行过批次", code="E_BATCH_NOT_FOUND")
    files = store.load_files(batch["batch_id"])
    inputs = store.list_inputs(batch["batch_id"])
    return {
        "batch_id": batch["batch_id"],
        "files": files,
        "inputs": inputs,
        "input_check": service.input_check(batch, inputs),
        "stages": store.load_stages(batch["batch_id"]),
        "counters": store.load_totals(batch["batch_id"]),
    }
