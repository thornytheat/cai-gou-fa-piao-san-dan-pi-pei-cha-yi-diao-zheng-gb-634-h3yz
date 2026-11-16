"""差异归因：把每条差异拆成量差 / 价差 / 税额差三类金额，并挂上证据行号。

金额一律 Decimal 元 2 位。缺单不产生金额，只产生缺哪张单。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config

ZERO = Decimal("0")


def breakdown(match: dict) -> dict:
    qty_diff = match.get("qty_diff")
    price_diff = match.get("price_diff")
    tax_diff = match.get("tax_diff")
    parts = []
    if qty_diff:
        parts.append({"kind": "qty", "label": config.DIFF_LABELS["qty"],
                      "value": str(qty_diff)})
    if price_diff:
        parts.append({"kind": "price", "label": config.DIFF_LABELS["price"],
                      "value": str(price_diff)})
    if tax_diff:
        parts.append({"kind": "tax", "label": config.DIFF_LABELS["tax"],
                      "value": str(tax_diff)})
    return {
        "key": match["key"],
        "po_no": match["po_no"],
        "line_no": match["line_no"],
        "verdict": match["verdict"],
        "diff_kind": match.get("diff_kind"),
        "diff_label": config.DIFF_LABELS.get(match.get("diff_kind") or "", ""),
        "diff_amount": match.get("diff_amount"),
        "parts": parts,
        "reasons": list(match.get("reasons") or []),
        "reasons_label": [config.DIFF_LABELS.get(kind, kind)
                          for kind in (match.get("reasons") or [])],
        "row_refs": list(match.get("row_refs") or []),
        "evidence": _evidence(match),
    }


def _evidence(match: dict) -> str:
    refs = match.get("row_refs") or []
    return " / ".join(refs) if refs else "—"


def diff_lines(matches: list[dict]) -> list[dict]:
    return [breakdown(match) for match in matches if match["verdict"] == "diff"]


def missing_lines(matches: list[dict]) -> list[dict]:
    return [breakdown(match) for match in matches if match["verdict"] == "missing"]


def by_kind(matches: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for match in matches:
        if match["verdict"] != "diff":
            continue
        kind = match.get("diff_kind") or "unknown"
        bucket = buckets.setdefault(kind, {"kind": kind,
                                           "label": config.DIFF_LABELS.get(kind, kind),
                                           "count": 0, "amount": ZERO})
        bucket["count"] += 1
        bucket["amount"] += match.get("diff_amount") or ZERO
    return [{"kind": buckets[k]["kind"], "label": buckets[k]["label"],
             "count": buckets[k]["count"], "amount": str(buckets[k]["amount"])}
            for k in sorted(buckets, key=lambda kind: config.DIFF_RANK.get(kind, 99))]


def totals(matches: list[dict]) -> dict:
    matched_total = ZERO
    diff_total = ZERO
    missing_total = ZERO
    for match in matches:
        amount = match.get("inv_amount_inc") or ZERO
        if match["verdict"] == "matched":
            matched_total += amount
        elif match["verdict"] == "diff":
            diff_total += amount
        else:
            missing_total += amount
    adjust_total = sum((match.get("diff_amount") or ZERO
                        for match in matches if match["verdict"] == "diff"), ZERO)
    return {
        "matched_total": matched_total,
        "diff_total": diff_total,
        "missing_total": missing_total,
        "adjust_total": adjust_total,
    }


def kind_text(kind: str | None) -> str:
    return config.DIFF_LABELS.get(kind or "", "—")
