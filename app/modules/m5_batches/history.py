"""批次历史：每次运行一行，带输入摘要、版本、行数与差异数。

历史只读 store，不做业务计算；页面和导出都用同一份结构。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config


def row_of(batch: dict, totals: dict, files: list[dict]) -> dict:
    files_label = ", ".join(f["name"] for f in files) or "—"
    return {
        "batch_id": batch["batch_id"],
        "batch_key": batch["batch_key"][:12],
        "run_id": batch["run_id"],
        "status": batch["status"],
        "rule_version": batch["rule_version"],
        "created_at": batch["created_at"],
        "finished_at": batch.get("finished_at") or "—",
        "input_digest": batch["input_digest"][:16],
        "files": files_label,
        "records": int(totals.get("records_read") or 0),
        "accepted": int(totals.get("accepted") or 0),
        "quarantined": int(totals.get("quarantine_rows") or 0),
        "matched": int(totals.get("matched") or 0),
        "diff": int(totals.get("diff") or 0),
        "missing": int(totals.get("missing") or 0),
        "adjust_total": totals.get("adjust_total") or "0.00",
        "balanced": totals.get("balanced") == "1",
        "resume_of": batch.get("resume_of") or "",
    }


def build(store, limit: int = 40) -> list[dict]:
    rows = []
    for batch in store.list_batches(limit=limit):
        totals = store.load_totals(batch["batch_id"])
        files = store.load_files(batch["batch_id"])
        rows.append(row_of(batch, totals, files))
    return rows


def summary(rows: list[dict]) -> dict:
    return {
        "batches": len(rows),
        "done": sum(1 for row in rows if row["status"] == "done"),
        "records": sum(row["records"] for row in rows),
        "adjust_total": sum((Decimal(row["adjust_total"]) for row in rows), Decimal("0")),
        "versions": sorted({row["rule_version"] for row in rows}),
        "stage_flow": config.STAGE_FLOW,
    }
