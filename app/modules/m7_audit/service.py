"""M7 编排：读两份复核台账（供应商主数据、红字发票清单）→ 重复组 / 红冲 / 供应商汇总。

只读 store 与样例目录，不改任何批次数据：复核结论不落库，避免把核对结果写回原始流水。
数字对外一律字符串，页面与接口同源。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ... import errors, reasons
from ..m1_ingest import decode, reader, sniff
from ..m2_amount import taxmath
from ..m4_match import service as match_service
from . import dups, redline, rollup

MASTER_FILE = "supplier_master.csv"
CREDIT_FILE = "credit_notes.csv"
BATCH_LIMIT = 20
ZERO = Decimal("0")


def _batch(store, batch_id: str | None) -> dict:
    batch = store.get_batch(batch_id) if batch_id else store.latest_batch()
    if batch is None:
        raise errors.NotFound("还没有运行过批次", code="E_BATCH_NOT_FOUND")
    return batch


def _enrich(issue: dict) -> dict:
    reason = reasons.get(issue["code"])
    enriched = dict(issue)
    enriched["level"] = reason.level
    enriched["title"] = reason.title
    enriched["hint"] = reason.hint
    return enriched


def _read_sample(settings, name: str) -> dict:
    """读一份台账：编码回退、分隔符识别、按记录编号，和 M1 采信走同一套实现。"""
    path = Path(settings.sample_dir) / name
    base = {"name": name, "exists": path.exists(), "headers": [], "records": [],
            "encoding": None, "bytes": 0, "segments": 1, "code": None}
    if not path.exists():
        return {**base, "code": "E_FILE_MISSING"}
    raw = path.read_bytes()
    try:
        decoded = decode.decode_bytes(raw)
    except errors.InvoiceError:
        return {**base, "bytes": len(raw), "code": "E_DECODE_FAILED"}
    delimiter = sniff.guess_delimiter(decoded.text)
    table = reader.parse_table(decoded.text, delimiter)
    return {"name": name, "exists": True, "headers": table.headers,
            "records": table.records, "encoding": decoded.encoding,
            "bytes": len(raw), "segments": table.segment_count,
            "blank": table.blank_count, "code": None}


def master_payload(settings) -> dict:
    source = _read_sample(settings, MASTER_FILE)
    issues: list[dict] = []
    if source["code"]:
        issues.append({"code": source["code"], "detail": source["name"]})
    mapping = rollup.header_map(source["headers"])
    missing = [name for name in rollup.MASTER_REQUIRED if name not in mapping]
    master: dict = {}
    if missing:
        issues.append({"code": "E_MASTER_HEADER", "detail": "缺列 " + " ".join(missing)})
    else:
        master, master_issues = rollup.parse_master(
            [record.cells for record in source["records"]], mapping)
        issues.extend(master_issues)
    codes = sorted({entry["supplier_code"] for entry in master.values()})
    return {
        "file": source["name"],
        "exists": source["exists"],
        "encoding": source["encoding"],
        "rows": len(source["records"]),
        "suppliers": len(codes),
        "po_registered": len(master),
        "master": master,
        "issues": [_enrich(issue) for issue in issues],
    }


def credits_payload(settings, inv_rows: list[dict]) -> dict:
    source = _read_sample(settings, CREDIT_FILE)
    file_issues: list[dict] = []
    if source["code"]:
        file_issues.append({"code": source["code"], "detail": source["name"]})
    mapping = redline.header_map(source["headers"])
    missing = [name for name in redline.CREDIT_REQUIRED if name not in mapping]
    rows: list[dict] = []
    rejected: list[dict] = []
    if missing:
        file_issues.append({"code": "E_CREDIT_HEADER",
                            "detail": "缺列 " + " ".join(missing)})
    else:
        for record in source["records"]:
            row, row_issues = redline.parse_credit_row(record.cells, mapping)
            row_ref = f"{source['name']}:L{record.line_no}"
            enriched = [_enrich(issue) for issue in row_issues]
            if row is None or redline.blocking(row_issues):
                rejected.append({"row_ref": row_ref, "raw": list(record.cells),
                                 "issues": enriched})
                continue
            row["row_ref"] = row_ref
            row["issues"] = enriched
            rows.append(row)
    paired = redline.pair(rows, inv_rows)
    totals = redline.totals(paired)
    return {
        "file": source["name"],
        "exists": source["exists"],
        "encoding": source["encoding"],
        "rows": paired,
        "rejected": rejected,
        "totals": totals,
        "file_issues": [_enrich(issue) for issue in file_issues],
        "reason_counts": redline.counts(
            [issue for row in paired for issue in row.get("issues") or []]
            + [issue for item in rejected for issue in item["issues"]]),
    }


def _dup_groups(store) -> list[dict]:
    rows: list[dict] = []
    for batch in store.list_batches(limit=BATCH_LIMIT):
        if batch.get("status") != "done":
            continue
        for row in store.load_inv_lines(batch["batch_id"]):
            rows.append({**row, "batch_id": batch["batch_id"],
                         "input_digest": batch["input_digest"]})
    return dups.detect(rows)


def _account(key: str, label: str, left_label: str, left, right_label: str, right,
             *, money: bool = False) -> dict:
    if money:
        difference = (right or ZERO) - (left or ZERO)
        balanced = abs(difference) <= Decimal("0.02")
        left_show = match_service.money(left)
        right_show = match_service.money(right)
        diff_show = match_service.money(difference)
    else:
        difference = int(right or 0) - int(left or 0)
        balanced = difference == 0
        left_show = match_service.qty(left)
        right_show = match_service.qty(right)
        diff_show = match_service.qty(difference)
    return {"key": key, "label": label, "left_label": left_label, "left": left_show,
            "right_label": right_label, "right": right_show, "diff": diff_show,
            "balanced": balanced, "money": money}


def _checks(*, totals: dict, inv_rows: list[dict], grn_rows: list[dict],
            adjustment_total: Decimal, diff_total: Decimal,
            credit_totals: dict) -> list[dict]:
    """四条守恒：净额、发票行数、入库数量、差异→调整额。不平就显式写差额。"""
    invoice_total = taxmath.sum_dec(row.get("amount_inc_tax") for row in inv_rows)
    red_total = credit_totals["accepted_total"]
    net_account = _account("net", "净额账", "发票价税合计 + 已冲抵红字",
                           invoice_total + red_total, "供应商净额合计",
                           totals["net"], money=True)
    net_account["note"] = f"红字已冲抵 {match_service.money(red_total)}"

    line_account = _account("lines", "行账", "本批发票行数", len(inv_rows),
                            "供应商发票行合计", totals["inv_lines"])
    line_account["note"] = "每张发票行必须落到一个供应商，含未登记"

    grn_total = sum((row.get("qty_received") or ZERO for row in grn_rows), ZERO)
    qty_account = _account("grn", "入库账", "入库行实收合计", grn_total,
                           "对上订单 + 无订单", totals["received"] + totals["orphan"],
                           money=False)
    qty_account["note"] = (f"订单 {match_service.qty(totals['ordered'])} · 未入库 "
                           f"{match_service.qty(totals['remaining'])} · 无订单 "
                           f"{match_service.qty(totals['orphan'])}")

    adjust_account = _account("adjust", "调整账", "差异金额合计", diff_total,
                              "调整额合计", adjustment_total, money=True)
    adjust_account["note"] = "缺单不产生调整额"

    rows = [net_account, line_account, qty_account, adjust_account]
    return rows


def payload(settings, store, batch_id: str | None = None,
            dup_kind: str | None = None) -> dict:
    if dup_kind and dup_kind not in dups.KINDS:
        raise errors.BadRequest(
            f"kind 只能是 {', '.join(dups.KINDS)}", code="E_BAD_DUP_KIND")
    batch = _batch(store, batch_id)
    resolved = batch["batch_id"]
    po_rows = store.load_po_lines(resolved)
    grn_rows = store.load_grn_lines(resolved)
    inv_rows = store.load_inv_lines(resolved)
    matches = store.load_matches(resolved)
    adjustments = store.load_adjustments(resolved)

    master_info = master_payload(settings)
    credit_info = credits_payload(settings, inv_rows)
    summary = rollup.rollup(po_rows=po_rows, grn_rows=grn_rows, inv_rows=inv_rows,
                            matches=matches, adjustments=adjustments,
                            reds=credit_info["rows"], master=master_info["master"])
    groups_all = _dup_groups(store)
    groups = dups.filter_kind(groups_all, dup_kind)

    adjustment_total = sum((row.get("alloc_amount") or ZERO for row in adjustments), ZERO)
    diff_total = sum((row.get("diff_amount") or ZERO for row in matches
                      if row["verdict"] == "diff"), ZERO)
    checks = _checks(totals=summary["totals"], inv_rows=inv_rows,
                     grn_rows=grn_rows, adjustment_total=adjustment_total,
                     diff_total=diff_total, credit_totals=credit_info["totals"])

    suppliers = [_supplier_row(row) for row in summary["rows"]]
    return {
        "batch_id": resolved,
        "rule_version": batch["rule_version"],
        "suppliers": suppliers,
        "supplier_totals": _money_totals(summary["totals"]),
        "master": {key: master_info[key] for key in
                   ("file", "exists", "encoding", "rows", "suppliers",
                    "po_registered", "issues")},
        "credits": {
            "file": credit_info["file"],
            "exists": credit_info["exists"],
            "encoding": credit_info["encoding"],
            "rows": [_credit_row(row) for row in credit_info["rows"]],
            "rejected": credit_info["rejected"],
            "totals": _credit_totals(credit_info["totals"]),
            "file_issues": credit_info["file_issues"],
            "reason_counts": credit_info["reason_counts"],
        },
        "dup_groups": groups,
        "dup_counts": dups.counts(groups_all),
        "dup_kinds": dups.kind_options(groups_all),
        "dup_kind": dup_kind or "",
        "grn_splits": [_split_row(row) for row in summary["grn_splits"]],
        "rate_shifts": [_shift_row(row) for row in summary["shifts"]],
        "checks": checks,
        "balanced": all(row["balanced"] for row in checks),
    }


def _supplier_row(row: dict) -> dict:
    return {
        "supplier_code": row["supplier_code"],
        "supplier_name": row["supplier_name"],
        "status": row["status"],
        "suspended": row["suspended"],
        "po_nos": row["po_nos"],
        "po_lines": row["po_lines"],
        "grn_lines": row["grn_lines"],
        "inv_lines": row["inv_lines"],
        "ordered": match_service.qty(row["ordered"]),
        "received": match_service.qty(row["received"]),
        "orphan": match_service.qty(row["orphan"]),
        "remaining": match_service.qty(row["remaining"]),
        "inv_inc": match_service.money(row["inv_inc"]),
        "red_total": match_service.money(row["red_total"]),
        "red_pending": match_service.money(row["red_pending"]),
        "red_over": row["red_over"],
        "net": match_service.money(row["net"]),
        "diff_total": match_service.money(row["diff_total"]),
        "adjust_total": match_service.money(row["adjust_total"]),
        "rate_shift": row["rate_shift"],
        "rate_moved": match_service.money(row["rate_moved"]),
        "matched": row["matched"],
        "diff": row["diff"],
        "missing": row["missing"],
    }


def _money_totals(totals: dict) -> dict:
    out = dict(totals)
    for key in ("ordered", "received", "orphan", "remaining"):
        out[key] = match_service.qty(totals[key])
    for key in ("inv_inc", "red_total", "red_pending", "net", "diff_total",
                "adjust_total", "rate_moved"):
        out[key] = match_service.money(totals[key])
    return out


def _credit_totals(totals: dict) -> dict:
    out = dict(totals)
    out["accepted_total"] = match_service.money(totals["accepted_total"])
    out["pending_total"] = match_service.money(totals["pending_total"])
    return out


def _credit_row(row: dict) -> dict:
    return {
        "credit_no": row.get("credit_no") or "",
        "original_no": row.get("original_no") or "",
        "issue_date": row.get("issue_date") or "",
        "po_no": row.get("po_no") or "",
        "amount_ex": match_service.money(row.get("amount_ex")),
        "amount_inc": match_service.money(row.get("amount_inc")),
        "unit": row.get("unit") or "元",
        "status": row.get("status") or "",
        "offset": bool(row.get("offset")),
        "over": bool(row.get("over")),
        "original_inc": match_service.money(row.get("original_inc")),
        "remaining": match_service.money(row.get("remaining")),
        "matched_invoice": row.get("matched_invoice") or "",
        "code": row.get("code") or "",
        "detail": row.get("detail") or "",
        "row_ref": row.get("row_ref") or "",
        "issues": row.get("issues") or [],
    }


def _split_row(row: dict) -> dict:
    return {
        "po_no": row["po_no"],
        "line_no": row["line_no"],
        "item_name": row["item_name"],
        "ordered": match_service.qty(row["ordered"]),
        "received": match_service.qty(row["received"]),
        "remaining": match_service.qty(row["remaining"]),
        "batch_count": row["batch_count"],
        "over": row["over"],
        "balanced": row["balanced"],
        "batches": [{"grn_no": item["grn_no"], "qty": match_service.qty(item["qty"]),
                     "received_at": item["received_at"],
                     "warehouse": item["warehouse"], "row_ref": item["row_ref"]}
                    for item in row["batches"]],
    }


def _shift_row(row: dict) -> dict:
    return {
        "po_no": row["po_no"],
        "line_no": row["line_no"],
        "invoice_no": row["invoice_no"],
        "po_rate": match_service.rate_text(row["po_rate"]),
        "inv_rate": match_service.rate_text(row["inv_rate"]),
        "inc_by_invoice_rate": match_service.money(row["inc_by_invoice_rate"]),
        "inc_by_order_rate": match_service.money(row["inc_by_order_rate"]),
        "moved_amount": match_service.money(row["moved_amount"]),
        "row_ref": row["row_ref"],
    }


def dups_payload(settings, store, batch_id: str | None = None,
                 kind: str | None = None) -> dict:
    payload_data = payload(settings, store, batch_id, dup_kind=kind)
    return {
        "batch_id": payload_data["batch_id"],
        "groups": payload_data["dup_groups"],
        "counts": payload_data["dup_counts"],
        "kinds": payload_data["dup_kinds"],
        "kind": kind or "",
    }


def vendors_payload(settings, store, batch_id: str | None = None) -> dict:
    payload_data = payload(settings, store, batch_id)
    return {
        "batch_id": payload_data["batch_id"],
        "suppliers": payload_data["suppliers"],
        "totals": payload_data["supplier_totals"],
        "checks": payload_data["checks"],
        "balanced": payload_data["balanced"],
        "grn_splits": payload_data["grn_splits"],
        "rate_shifts": payload_data["rate_shifts"],
        "credits": payload_data["credits"]["totals"],
        "master": payload_data["master"],
    }
