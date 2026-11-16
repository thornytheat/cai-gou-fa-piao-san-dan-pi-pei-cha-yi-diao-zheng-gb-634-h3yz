"""陈旧提交拒绝、输入留存校验与中断恢复判定。

三个规则：
1. 同 batch_key 已存在同版本 done 批次 → 直接复用，不新增行（幂等）；
2. 提交的规则版本比已完成批次旧 → 409 拒绝（陈旧），不覆盖新数据；
3. 重算/重试只认批次留存原件；缺原件的旧批次 422 拒绝，不从现有文件补造。
"""
from __future__ import annotations

from ... import config, errors
from . import state


def assert_not_stale(store, *, batch_key: str, rule_version: str) -> None:
    existing = store.latest_done_by_key(batch_key)
    if existing is None:
        return
    if state.is_stale(existing, rule_version):
        raise errors.Conflict(
            f"批次 {existing['batch_id']} 已用 {existing['rule_version']} 完成，"
            f"拒绝用更旧的 {rule_version} 覆盖",
            code="E_STALE_BATCH",
            detail={"existing_batch": existing["batch_id"],
                    "existing_version": existing["rule_version"],
                    "incoming_version": rule_version},
        )


def reuse_decision(store, *, batch_key: str, rule_version: str) -> dict:
    existing = store.latest_done_by_key(batch_key)
    if existing is None:
        return {"reuse": False, "batch": None, "reason": "无同键批次"}
    if state.is_stale(existing, rule_version):
        return {"reuse": False, "batch": existing, "stale": True,
                "reason": f"同键批次版本更新（{existing['rule_version']}）"}
    return {"reuse": True, "batch": existing, "reason": "同键批次已存在"}


def assert_inputs_saved(store, batch_id: str) -> None:
    """缺少留存原件的旧批次明确拒绝重算，不从现有文件补造。"""
    if store.has_inputs(batch_id):
        return
    raise errors.Unsupported(
        f"批次 {batch_id} 没有留存输入原件，拒绝重算；不从现有文件补造",
        code="E_INPUT_MISSING",
        detail={"batch_id": batch_id,
                "hint": "该批次只保留结果；需要新结果请重新导入当天文件生成新批次"})


def recovery_plan(store, settings, batch_id: str) -> dict:
    """中断恢复判定：running 且租约过期 → 可按留存原件接续；否则给出原因。"""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次不存在：{batch_id}", code="E_BATCH_NOT_FOUND")
    status = batch["status"]
    has_inputs = store.has_inputs(batch_id)
    cutoff = state.lease_cutoff(settings)
    stale_lease = (batch.get("claimed_at") or "") < cutoff
    resumable = status == "running" and stale_lease and has_inputs
    if status == "done" and has_inputs:
        note = "批次已完成，重算按留存原件复用或生成新版本批次"
    elif status == "done":
        note = "批次已完成但没有留存原件，拒绝重算；不从现有文件补造"
    elif status == "running" and not stale_lease:
        note = "批次正在运行，租约未过期，不接续"
    elif status == "running" and not has_inputs:
        note = "批次中断且没有留存原件，拒绝接续"
    elif status == "failed":
        note = "批次失败，可按留存原件重试"
    else:
        note = "按留存原件接续重跑"
    return {
        "batch_id": batch_id,
        "status": status,
        "attempts": int(batch.get("attempts") or 0),
        "has_inputs": has_inputs,
        "lease_expired": stale_lease,
        "resumable": resumable,
        "retryable": status == "failed" and has_inputs,
        "note": note,
    }


def resume_plan(store, batch_id: str) -> dict:
    """断点续跑：只重跑未完成阶段，已完成阶段的结果原样保留。"""
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次不存在：{batch_id}", code="E_BATCH_NOT_FOUND")
    stages = store.load_stages(batch_id)
    done = [row["stage"] for row in stages if row["status"] == "ok"]
    remaining = [stage for stage in config.STAGES if stage not in done]
    return {
        "batch_id": batch_id,
        "status": batch["status"],
        "done_stages": done,
        "remaining_stages": remaining,
        "resumable": batch["status"] != "done" and bool(done),
        "note": "批次已完成，续跑会复用同键结果" if batch["status"] == "done"
                else f"从 {remaining[0] if remaining else '—'} 继续",
    }


def stamp_resume(source_batch: dict, new_batch: dict) -> dict:
    """续跑批次记录来源批次，方便追溯。"""
    merged = dict(new_batch)
    merged["resume_of"] = source_batch["batch_id"]
    return merged
