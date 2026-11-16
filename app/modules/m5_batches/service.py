"""M5 编排：历史列表、批次详情、幂等重跑、陈旧拒绝。

重跑走的是同一条流水线（`m1.service.run_import`），没有单独的重跑分支：
输入摘要不变 → batch_key 不变 → 命中已有批次，返回同一个 batch_id。
"""
from __future__ import annotations

from ... import config, errors
from . import history, resume, state


def list_payload(store, settings, limit: int = 40) -> dict:
    rows = history.build(store, limit=limit)
    summary = history.summary(rows)
    summary["adjust_total"] = format(summary["adjust_total"], "f")
    summary["watermark"] = state.watermark(store)
    summary["rule_version"] = settings.rule_version
    return {"rows": rows, "summary": summary}


def detail(store, batch_id: str) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次不存在：{batch_id}", code="E_BATCH_NOT_FOUND")
    totals = store.load_totals(batch_id)
    return {
        "batch": batch,
        "totals": totals,
        "files": store.load_files(batch_id),
        "stages": store.load_stages(batch_id),
        "resume": resume.resume_plan(store, batch_id),
        "manifest": store.load_manifest(batch_id),
    }


def rerun(store, settings, batch_id: str, rule_version: str | None = None) -> dict:
    """幂等重跑：不新增行；版本更旧时 409，不覆盖新数据。"""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次不存在：{batch_id}", code="E_BATCH_NOT_FOUND")
    version = rule_version or batch["rule_version"]
    resume.assert_not_stale(store, batch_key=batch["batch_key"], rule_version=version)
    from ..m1_ingest import service as ingest_service

    files = [f["name"] for f in store.load_files(batch_id)]
    params = batch.get("params") or {}
    names = params.get("files") or files
    result = ingest_service.run_import(settings=settings, store=store, names=names,
                                       rule_version=version,
                                       resume_of=batch["batch_id"])
    return {
        "reused": result["reused"],
        "batch_id": result["batch"]["batch_id"],
        "source_batch_id": batch_id,
        "same_batch": result["batch"]["batch_id"] == batch_id,
        "rule_version": version,
        "note": "同键批次已存在，复用结果" if result["reused"] else "已生成新批次",
    }


def stage_flow() -> list[dict]:
    return [{"stage": stage, "label": config.STAGE_LABELS[stage]}
            for stage in config.STAGES]
