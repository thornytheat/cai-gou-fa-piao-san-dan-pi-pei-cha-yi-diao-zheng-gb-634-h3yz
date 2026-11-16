"""M3 编排：隔离区列表、原因码分布、指标、隔离 CSV。

只读 store；隔离行在写入时就已经带好原因码、行号、字段与原值。
"""
from __future__ import annotations

from ... import errors, reasons
from . import metrics, quarantine


def _batch(store, batch_id: str | None) -> dict:
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise errors.NotFound("还没有运行过批次", code="E_BATCH_NOT_FOUND")
    return batch


def payload(store, batch_id: str | None = None) -> dict:
    batch = _batch(store, batch_id)
    rows = quarantine.sort_rows(store.load_quarantine(batch["batch_id"]))
    files = store.load_files(batch["batch_id"])
    summary = metrics.summary(files, rows)
    return {
        "batch_id": batch["batch_id"],
        "rows": rows,
        "distribution": quarantine.distribution(rows),
        "fields": quarantine.field_counts(rows),
        "quality": summary,
        "gate": metrics.gate(summary),
        "levels": reasons.LEVEL_LABELS,
        "catalog": reasons.catalog(),
    }


def csv_text(store, batch_id: str | None = None) -> tuple[str, str]:
    batch = _batch(store, batch_id)
    rows = quarantine.sort_rows(store.load_quarantine(batch["batch_id"]))
    return quarantine.to_csv(rows), f"{batch['batch_id']}-{quarantine.EXPORT_NAME}"


def export_bytes(store, batch_id: str | None = None) -> tuple[bytes, str]:
    text, name = csv_text(store, batch_id)
    return quarantine.encode_csv(text), name
