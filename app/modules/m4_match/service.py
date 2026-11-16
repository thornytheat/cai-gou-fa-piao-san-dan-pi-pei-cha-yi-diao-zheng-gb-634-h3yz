"""M4 编排：匹配结果、差异明细、四账核对的只读装配。

数字对外一律字符串（Decimal 不进 JSON 浮点），前端只做展示格式化。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, errors
from ..m3_quality import quarantine as quarantine_mod
from . import compare, diffattr, reconcile


def money(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value.quantize(config.AMOUNT_QUANT), "f")


def qty(value) -> str:
    """数量展示去掉多余的尾零：100.0000 → 100，58.5000 → 58.5。"""
    if value is None:
        return ""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    text = format(value.normalize(), "f")
    return "0" if text in ("", "-0") else text


def rate_text(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format((value * Decimal("100")).quantize(config.AMOUNT_QUANT), "f") + "%"


def match_row(match: dict) -> dict:
    return {
        "key": match["key"],
        "po_no": match["po_no"],
        "line_no": match["line_no"],
        "verdict": match["verdict"],
        "verdict_label": config.VERDICT_LABELS.get(match["verdict"], match["verdict"]),
        "diff_kind": match.get("diff_kind") or "",
        "diff_label": config.DIFF_LABELS.get(match.get("diff_kind") or "", ""),
        "reasons": list(match.get("reasons") or []),
        "po_qty": qty(match.get("po_qty")),
        "grn_qty": qty(match.get("grn_qty")),
        "inv_qty": qty(match.get("inv_qty")),
        "unit_price": money(match.get("unit_price")),
        "inv_amount_ex": money(match.get("inv_amount_ex")),
        "inv_amount_inc": money(match.get("inv_amount_inc")),
        "expect_amount_ex": money(match.get("expect_amount_ex")),
        "expect_amount_inc": money(match.get("expect_amount_inc")),
        "diff_amount": money(match.get("diff_amount")),
        "qty_diff": qty(match.get("qty_diff")),
        "price_diff": money(match.get("price_diff")),
        "tax_diff": money(match.get("tax_diff")),
        "row_refs": list(match.get("row_refs") or []),
        "note": match.get("note") or "",
    }


def trios(store, batch_id: str) -> dict:
    """三单对照数据：左列订单+入库、右列发票，中间是差异标记。"""
    po_rows = store.load_po_lines(batch_id)
    grn_rows = store.load_grn_lines(batch_id)
    inv_rows = store.load_inv_lines(batch_id)
    matches = store.load_matches(batch_id)
    grouped = compare.build_groups(po_rows, grn_rows, inv_rows)
    by_key = {group.key: group for group in grouped}
    rows = []
    for match in matches:
        group = by_key.get(match["key"])
        rows.append({
            "key": match["key"],
            "po_no": match["po_no"],
            "line_no": match["line_no"],
            "verdict": match["verdict"],
            "verdict_label": config.VERDICT_LABELS.get(match["verdict"], match["verdict"]),
            "diff_kind": match.get("diff_kind") or "",
            "diff_label": config.DIFF_LABELS.get(match.get("diff_kind") or "", ""),
            "po": _po_cell(group.po if group else None),
            "grn": _grn_cell(group),
            "inv": _inv_cell(group),
            "diff_amount": money(match.get("diff_amount")),
            "explain": match_row(match),
        })
    return {
        "rows": rows,
        "counts": compare.counts(matches),
        "totals": {key: money(value) for key, value in diffattr.totals(matches).items()},
        "po_total": len(po_rows),
        "grn_total": len(grn_rows),
        "inv_total": len(inv_rows),
    }


def _po_cell(row: dict | None) -> dict:
    if row is None:
        return {"missing": True, "text": "缺采购订单"}
    return {
        "missing": False,
        "po_no": row["po_no"],
        "line_no": row["line_no"],
        "item_name": row.get("item_name") or "",
        "qty": qty(row.get("qty")),
        "unit_price": money(row.get("unit_price")),
        "tax_rate": rate_text(row.get("tax_rate")),
        "row_ref": row["row_ref"],
    }


def _grn_cell(group) -> dict:
    if group is None or not group.grns:
        return {"missing": True, "text": "缺入库单", "qty": ""}
    return {
        "missing": False,
        "grn_no": ", ".join(sorted(row["grn_no"] for row in group.grns)),
        "qty": qty(group.grn_qty),
        "received_at": ", ".join(sorted({row["received_at"] for row in group.grns})),
        "row_ref": ", ".join(sorted(row["row_ref"] for row in group.grns)),
    }


def _inv_cell(group) -> dict:
    if group is None or not group.invs:
        return {"missing": True, "text": "缺发票", "amount_inc": ""}
    return {
        "missing": False,
        "invoice_no": ", ".join(sorted({row["invoice_no"] for row in group.invs})),
        "qty": qty(group.inv_qty),
        "amount_ex": money(group.inv_ex),
        "amount_inc": money(group.inv_inc),
        "row_ref": ", ".join(sorted(row["row_ref"] for row in group.invs)),
    }


def matches_payload(store, batch_id: str, verdict: str | None = None) -> dict:
    if verdict and verdict not in config.VERDICTS:
        raise errors.BadRequest(
            f"verdict 只能是 {', '.join(config.VERDICTS)}", code="E_BAD_VERDICT")
    matches = store.load_matches(batch_id)
    rows = [match_row(match) for match in matches
            if verdict is None or match["verdict"] == verdict]
    return {
        "rows": rows,
        "counts": compare.counts(matches),
        "by_kind": diffattr.by_kind(matches),
        "totals": {key: money(value) for key, value in diffattr.totals(matches).items()},
        "labels": dict(config.VERDICT_LABELS),
    }


def diffs_payload(store, batch_id: str, kind: str | None = None) -> dict:
    if kind and kind not in config.DIFF_KINDS:
        raise errors.BadRequest("kind 不在差异类别表内", code="E_BAD_DIFF_KIND")
    matches = store.load_matches(batch_id)
    diff_lines = diffattr.diff_lines(matches)
    missing_lines = diffattr.missing_lines(matches)
    if kind:
        diff_lines = [row for row in diff_lines if row["diff_kind"] == kind]
    return {
        "rows": diff_lines,
        "missing": missing_lines,
        "by_kind": diffattr.by_kind(matches),
        "totals": {key: money(value) for key, value in diffattr.totals(matches).items()},
        "labels": dict(config.DIFF_LABELS),
    }


def reconcile_payload(store, batch_id: str) -> dict:
    totals = store.load_totals(batch_id)
    counters = {
        "records_read": totals.get("records_read"),
        "accepted": totals.get("accepted"),
        "blank": totals.get("blank"),
        "header_repeat": totals.get("header_repeat"),
        "grn_accepted": totals.get("grn_accepted"),
    }
    files = store.load_files(batch_id)
    payload = reconcile.accounts(
        files=files, counters=counters,
        po_rows=store.load_po_lines(batch_id),
        inv_rows=store.load_inv_lines(batch_id),
        matches=store.load_matches(batch_id),
        quarantine_rows=quarantine_mod.sort_rows(store.load_quarantine(batch_id)),
    )
    return payload
