"""M5 编排：历史列表、批次详情、重算、失败重试。

重算与重试都走同一条流水线（`m1.service.run_import`），区别只在输入来源：
- 重算读批次留存的原件与原评估日，源表被替换或移走都不影响；
- 失败重试沿用失败批次自己的原件，不重新读源表；
- 新导入才按文件名读当天更正件。
"""
from __future__ import annotations

from ... import config, errors
from ..m1_ingest import service as ingest_service
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
    inputs = store.list_inputs(batch_id)
    return {
        "batch": batch,
        "totals": totals,
        "files": store.load_files(batch_id),
        "stages": store.load_stages(batch_id),
        "resume": resume.resume_plan(store, batch_id),
        "manifest": store.load_manifest(batch_id),
        "inputs": inputs,
        "input_check": ingest_service.input_check(batch, inputs),
    }


def _source_batch(store, batch_id: str) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次不存在：{batch_id}", code="E_BATCH_NOT_FOUND")
    return batch


def recovery(store, settings, batch_id: str) -> dict:
    return resume.recovery_plan(store, settings, batch_id)


def rerun(store, settings, batch_id: str, rule_version: str | None = None) -> dict:
    """按批次留存的原件重算：不读源表；已完成同键同版本直接复用。"""
    batch = _source_batch(store, batch_id)
    version = rule_version or batch["rule_version"]
    resume.assert_not_stale(store, batch_key=batch["batch_key"], rule_version=version)
    resume.assert_inputs_saved(store, batch_id)

    result = ingest_service.run_import(settings=settings, store=store,
                                       rule_version=version,
                                       input_batch_id=batch_id)
    return {
        "reused": result["reused"],
        "batch_id": result["batch"]["batch_id"],
        "source_batch_id": batch_id,
        "same_batch": result["batch"]["batch_id"] == batch_id,
        "rule_version": version,
        "replayed": bool(result.get("replayed") or result["reused"]),
        "input_batch_id": batch_id,
        "note": _rerun_note(result, batch_id),
    }


def retry(store, settings, batch_id: str, rule_version: str | None = None) -> dict:
    """失败重试：沿用失败批次的留存原件，不重新采信源表。"""
    batch = _source_batch(store, batch_id)
    if batch["status"] not in ("failed", "running"):
        raise errors.Conflict(
            f"批次 {batch_id} 状态为 {batch['status']}，只有失败或中断的批次才需要重试",
            code="E_BATCH_NOT_RETRYABLE",
            detail={"batch_id": batch_id, "status": batch["status"]})
    version = rule_version or batch["rule_version"]
    resume.assert_not_stale(store, batch_key=batch["batch_key"], rule_version=version)
    resume.assert_inputs_saved(store, batch_id)
    result = ingest_service.run_import(settings=settings, store=store,
                                       rule_version=version, resume_of=batch_id)
    return {
        "reused": result["reused"],
        "batch_id": result["batch"]["batch_id"],
        "source_batch_id": batch_id,
        "same_batch": result["batch"]["batch_id"] == batch_id,
        "rule_version": version,
        "attempt": int(result["batch"].get("attempts") or 1),
        "replayed": bool(result.get("replayed")),
        "note": "沿用留存原件重试" if not result["reused"] else "同键结果已存在，复用",
    }


def _rerun_note(result: dict, batch_id: str) -> str:
    if result["reused"]:
        return "同键同版本批次已存在，复用结果"
    if result["batch"]["batch_id"] == batch_id:
        return "按留存原件重算，结果落回同一批次"
    return "按留存原件重算，生成新批次"


def stage_flow() -> list[dict]:
    return [{"stage": stage, "label": config.STAGE_LABELS[stage]}
            for stage in config.STAGES]
