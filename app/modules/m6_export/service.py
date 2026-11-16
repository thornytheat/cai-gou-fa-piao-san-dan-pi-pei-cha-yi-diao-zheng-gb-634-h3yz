"""M6 编排：调整单、证据行、运行清单的只读装配与导出字节。

四种导出共用同一个批次上下文：页面看到什么，导出就是什么。
"""
from __future__ import annotations

from ... import errors
from ..m3_quality import service as quality_service
from ..m4_match import diffattr, service as match_service
from ..m4_match import reconcile as reconcile_mod
from ..m5_batches import state as batch_state
from . import adj, csv_out, manifest_out

KINDS = ("adjustment", "matches", "quarantine", "manifest", "reconcile")


def _batch(store, batch_id: str | None) -> dict:
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise errors.NotFound("还没有运行过批次", code="E_BATCH_NOT_FOUND")
    return batch


def payload(store, batch_id: str | None = None) -> dict:
    batch = _batch(store, batch_id)
    resolved = batch["batch_id"]
    adjustments = store.load_adjustments(resolved)
    matches = store.load_matches(resolved)
    inv_rows = store.load_inv_lines(resolved)
    _, summary = adj.build_sheet(matches, inv_rows, batch_id=resolved)
    manifest = store.load_manifest(resolved) or {}
    return {
        "batch_id": resolved,
        "run_id": batch["run_id"],
        "rule_version": batch["rule_version"],
        "adjustments": adjustments,
        "adjust_summary": summary,
        "diff_lines": diffattr.diff_lines(matches),
        "by_kind": diffattr.by_kind(matches),
        "totals": {key: match_service.money(value)
                   for key, value in diffattr.totals(matches).items()},
        "manifest_rows": manifest_out.summary_rows(manifest),
        "manifest_inputs": manifest_out.input_rows(manifest),
        "kinds": [{"key": kind, "label": _label(kind)} for kind in KINDS],
        "watermark": batch_state.watermark(store),
    }


def _label(kind: str) -> str:
    return {
        "adjustment": "调整单",
        "matches": "匹配证据行",
        "quarantine": "隔离区",
        "manifest": "运行清单",
        "reconcile": "四账",
    }.get(kind, kind)


def export_bytes(store, kind: str, batch_id: str | None = None) -> tuple[bytes, str, str]:
    if kind not in KINDS:
        raise errors.BadRequest(f"kind 只能是 {', '.join(KINDS)}", code="E_BAD_EXPORT_KIND")
    batch = _batch(store, batch_id)
    resolved = batch["batch_id"]
    if kind == "quarantine":
        body, name = quality_service.export_bytes(store, resolved)
        return body, name, "text/csv; charset=utf-8"
    if kind == "manifest":
        manifest = store.load_manifest(resolved) or {}
        text = manifest_out.to_json(manifest)
        return text.encode("utf-8"), manifest_out.filename(resolved), "application/json"
    if kind == "reconcile":
        payload_reconcile = match_service.reconcile_payload(store, resolved)
        text = manifest_out.to_json(payload_reconcile)
        return (text.encode("utf-8"), f"{resolved}-reconcile.json",
                "application/json")
    if kind == "matches":
        matches = store.load_matches(resolved)
        rows = [match_service.match_row(match) for match in matches]
        text = csv_out.matches_csv(rows)
        return csv_out.encode(text), f"{resolved}-matches.csv", "text/csv; charset=utf-8"
    adjustments = store.load_adjustments(resolved)
    _, summary = adj.build_sheet(store.load_matches(resolved),
                                 store.load_inv_lines(resolved), batch_id=resolved)
    text = csv_out.adjustment_csv(adjustments, summary)
    return (csv_out.encode(text), f"{resolved}-adjustment.csv", "text/csv; charset=utf-8")


def reconcile_payload(store, batch_id: str | None = None) -> dict:
    batch = _batch(store, batch_id)
    resolved = batch["batch_id"]
    totals = store.load_totals(resolved)
    return reconcile_mod.accounts(
        files=store.load_files(resolved),
        counters={"records_read": totals.get("records_read"),
                  "accepted": totals.get("accepted"),
                  "blank": totals.get("blank"),
                  "header_repeat": totals.get("header_repeat"),
                  "grn_accepted": totals.get("grn_accepted")},
        po_rows=store.load_po_lines(resolved),
        inv_rows=store.load_inv_lines(resolved),
        matches=store.load_matches(resolved),
        quarantine_rows=store.load_quarantine(resolved),
    )
