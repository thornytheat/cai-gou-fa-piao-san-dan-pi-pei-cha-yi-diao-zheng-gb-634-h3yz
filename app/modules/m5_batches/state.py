"""批次号、水位与租约：batch_key 由输入摘要 + 评估日决定，可复算。

同一组输入在同一评估日下只有唯一的 batch_key，这就是幂等与并发只落一次账的锚点。
"""
from __future__ import annotations

import datetime as dt
import uuid

from ... import config, manifest

# 运行租约：超过这个时长仍是 running 的批次视为进程中断，允许接续重跑。
# 只影响「中断恢复」，不影响同一输入的并发请求（并发请求在租约内一律拒绝）。
LEASE_SECONDS = 900


def batch_key(input_digest: str, eval_date) -> str:
    """键只认输入摘要与评估日：同一份输入换版本要落在同一个键上，
    版本差异由「复用 / 新批次 / 陈旧拒绝」三条规则分流。"""
    return manifest.digest_parts([f"input={input_digest}", f"eval={eval_date}"])


def parse_eval_date(value) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        return None


def lease_cutoff(settings, now: dt.datetime | None = None) -> str:
    """租约起点：claimed_at 早于此刻的 running 批次判为中断，可以接续。"""
    seconds = getattr(settings, "lease_seconds", LEASE_SECONDS)
    moment = now or dt.datetime.now(config.UTC)
    return (moment - dt.timedelta(seconds=int(seconds))).isoformat(timespec="seconds")


def new_batch_id() -> str:
    stamp = dt.datetime.now(config.UTC).strftime("%Y%m%d%H%M%S")
    return f"B{stamp}{uuid.uuid4().hex[:4]}"


def version_tuple(rule_version: str) -> tuple:
    """`invoice3way/1.2.0` → (1, 2, 0)；解析不出来的按 (0, 0, 0)。"""
    body = (rule_version or "").split("/")[-1]
    parts = []
    for piece in body.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_stale(existing: dict, incoming_version: str) -> bool:
    """已完成的批次版本更新时，更旧的版本不允许覆盖它。"""
    return version_tuple(incoming_version) < version_tuple(existing["rule_version"])


def watermark(store) -> dict:
    """水位只指向已完成批次；未完成/失败的运行不推进水位。"""
    batches = [batch for batch in store.list_batches(limit=20)
               if batch.get("status") == "done"]
    if not batches:
        return {"batch_id": None, "finished_at": None, "records": 0}
    latest = batches[0]
    totals = store.load_totals(latest["batch_id"])
    return {
        "batch_id": latest["batch_id"],
        "finished_at": latest.get("finished_at"),
        "records": int(totals.get("records_read") or 0),
    }


def monotonic(check: list[dict]) -> bool:
    """水位不回退：按 finished_at 升序，批次创建时间必须单调不减。"""
    stamps = [row.get("created_at") for row in check]
    return stamps == sorted(s for s in stamps if s is not None)
