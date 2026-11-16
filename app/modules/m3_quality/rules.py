"""M3 行级校验：数量/金额/税率/必填，以及跨行的发票号冲突判定。

每个规则返回 (原因码, 字段, 说明)，不做短路：一行命中多条就都记下来，
隔离时按原因码级别取一条主结论，其余进明细。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m2_amount import taxmath

RULE_ORDER = ("required", "sign", "rate", "over_receipt", "dup_key")


def row_issues(doc_type: str, row: dict) -> list[dict]:
    issues: list[dict] = []
    for field in ("qty", "qty_received", "amount_ex_tax", "amount_inc_tax", "unit_price"):
        value = row.get(field)
        if field in ("qty", "qty_received") and value is not None:
            if value <= 0:
                issues.append(_issue("E_QTY_NONPOSITIVE", field, row))
        if field in ("amount_ex_tax", "amount_inc_tax", "unit_price") and value is not None:
            if value < 0:
                issues.append(_issue("E_NEGATIVE_AMOUNT", field, row))
    rate = row.get("tax_rate")
    if rate is not None and not (Decimal("0") < rate <= config.MAX_TAX_RATE):
        issues.append(_issue("E_RATE_RANGE", "tax_rate", row))
    return issues


def _issue(code: str, field: str, row: dict) -> dict:
    raw = (row.get("raw") or {}).get(field)
    return {
        "code": code,
        "field": field,
        "raw_value": "" if raw is None else str(raw),
        "detail": f"{code}·{field}",
        "level": reasons.level_of(code),
        "title": reasons.get(code).title,
        "hint": reasons.get(code).hint,
    }


def primary(issues: list[dict]) -> dict | None:
    """主结论：级别优先（error > warn > info），同级按 RULE_ORDER 定序，与书写顺序无关。"""
    if not issues:
        return None
    return sorted(issues, key=lambda item: (
        reasons.LEVEL_ORDER[item.get("level", reasons.WARN)],
        _rule_rank(item["code"]),
        item["code"]))[0]


def _rule_rank(code: str) -> int:
    for position, key in enumerate(RULE_ORDER):
        if key == "required" and code.startswith("E_MISSING"):
            return position
        if key == "sign" and code in ("E_QTY_NONPOSITIVE", "E_NEGATIVE_AMOUNT"):
            return position
        if key == "rate" and code.startswith("E_RATE"):
            return position
        if key == "dup_key" and code.startswith("E_DUP"):
            return position
    return len(RULE_ORDER)


def blocking(issues: list[dict]) -> bool:
    return any(item.get("level") == reasons.ERROR for item in issues)


def dup_invoice_conflicts(inv_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一发票号多行：金额一致 → 保留首行（info）；金额不同 → 全部隔离（error）。

    保留哪条不由读写顺序决定：先按 (invoice_no, po_no, line_no, row_ref) 定序。
    """
    grouped: dict[str, list[dict]] = {}
    for row in inv_rows:
        grouped.setdefault(row.get("invoice_no") or "", []).append(row)
    conflicts: list[dict] = []
    deduped_notes: list[dict] = []
    for invoice_no in sorted(grouped):
        rows = sorted(grouped[invoice_no],
                      key=lambda r: (r["po_no"], r["line_no"], r["row_ref"]))
        if len(rows) < 2:
            continue
        amounts = {_amount_key(row) for row in rows}
        if len(amounts) == 1:
            for row in rows[1:]:
                deduped_notes.append({"row_ref": row["row_ref"], "code": "E_DUP_KEY_SAME",
                                      "invoice_no": invoice_no})
            continue
        for row in rows:
            conflicts.append({
                "row_ref": row["row_ref"],
                "code": "E_DUP_KEY_CONFLICT",
                "invoice_no": invoice_no,
                "detail": f"发票号 {invoice_no} 出现 {len(rows)} 行且金额不同",
                "amounts": sorted(str(a) for a in amounts),
            })
    return conflicts, deduped_notes


def _amount_key(row: dict) -> Decimal:
    return row.get("amount_inc_tax") or Decimal("0")


def over_receipt(po_row: dict, received: Decimal | None) -> bool:
    if received is None or po_row.get("qty") in (None, Decimal("0")):
        return False
    return received > po_row["qty"] * config.OVER_RECEIPT_RATIO


def blocking_codes(issues: list[dict]) -> list[str]:
    return sorted({item["code"] for item in issues if item.get("level") == reasons.ERROR})


def amount_issue(issues: list[dict]) -> bool:
    return any(item["code"] in ("E_AMOUNT_SENTINEL", "E_AMOUNT_UNPARSABLE",
                                "E_UNIT_UNKNOWN") for item in issues)


def total_of(rows: list[dict], field: str) -> Decimal:
    return taxmath.sum_amount(rows, field)
