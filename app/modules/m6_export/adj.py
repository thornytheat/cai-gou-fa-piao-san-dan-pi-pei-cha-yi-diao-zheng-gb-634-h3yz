"""调整单：把差异金额按权重分摊到采购单，尾差用最大余额法分配并守恒。

分摊到分：先按权重取整到 0.01 的整数倍（向下取整），差额按「小数部分降序、
单号为次序」逐分补，分完合计必然等于差异总额。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from ... import config
from ..m4_match import diffattr

ZERO = Decimal("0")
CENT = Decimal("0.01")


def allocate(total: Decimal, weights: dict[str, Decimal]) -> dict:
    """最大余额法：返回 {目标号: 分摊额}，合计严格等于 total。"""
    targets = sorted(weights)
    if not targets:
        return {}
    if total == ZERO:
        return {target: ZERO for target in targets}
    positive = {t: (weights[t] if weights[t] and weights[t] > 0 else ZERO) for t in targets}
    weight_sum = sum(positive.values(), ZERO)
    if weight_sum == ZERO:
        positive = {t: Decimal("1") for t in targets}
        weight_sum = Decimal(len(targets))
    total_cents = int((total / CENT).to_integral_value(rounding=ROUND_FLOOR))
    raw = {t: total * positive[t] / weight_sum for t in targets}
    floor_cents = {t: int((raw[t] / CENT).to_integral_value(rounding=ROUND_FLOOR))
                   for t in targets}
    remainder = total_cents - sum(floor_cents.values())
    ordered = sorted(targets, key=lambda t: (-(raw[t] - floor_cents[t] * CENT), t))
    for index in range(remainder):
        floor_cents[ordered[index % len(ordered)]] += 1
    return {t: (Decimal(floor_cents[t]) * CENT).quantize(CENT) for t in targets}


def weights_of(matches: list[dict], inv_rows: list[dict]) -> dict:
    """权重 = 该采购单下采纳发票的不含税金额合计（Decimal 元）。"""
    by_po: dict[str, Decimal] = {}
    for row in inv_rows:
        po_no = row["po_no"]
        by_po[po_no] = by_po.get(po_no, ZERO) + (row.get("amount_ex_tax") or ZERO)
    if not by_po:
        return {}
    return {po: by_po[po] for po in sorted(by_po)}


def diff_pos(matches: list[dict]) -> list[str]:
    return sorted({match["po_no"] for match in matches if match["verdict"] == "diff"})


def build_sheet(matches: list[dict], inv_rows: list[dict], *,
                batch_id: str) -> tuple[list[dict], dict]:
    """只为有差异的采购单开调整行；缺单不产生调整额。"""
    positions = diff_pos(matches)
    totals = diffattr.totals(matches)
    total = totals["adjust_total"]
    all_weights = weights_of(matches, inv_rows)
    weights = {po: all_weights.get(po, ZERO) for po in positions}
    shares = allocate(total, weights) if positions else {}
    evidence: dict[str, list[str]] = {po: [] for po in positions}
    kinds: dict[str, list[str]] = {po: [] for po in positions}
    for match in matches:
        if match["verdict"] != "diff":
            continue
        evidence[match["po_no"]].extend(match.get("row_refs") or [])
        if match.get("diff_kind"):
            kinds[match["po_no"]].append(match["diff_kind"])
    rows = []
    for index, po_no in enumerate(positions, start=1):
        rows.append({
            "adj_no": f"ADJ-{batch_id[-6:]}-{index:02d}",
            "target_no": po_no,
            "po_no": po_no,
            "weight": weights[po_no],
            "alloc_amount": shares.get(po_no, ZERO),
            "diff_kind": kinds[po_no][0] if kinds[po_no] else None,
            "evidence": sorted(set(evidence[po_no])),
            "note": config.DIFF_LABELS.get(kinds[po_no][0], "") if kinds[po_no] else "",
        })
    allocated = sum((row["alloc_amount"] for row in rows), ZERO)
    summary = {
        "total": format(total.quantize(CENT), "f"),
        "allocated": format(allocated.quantize(CENT), "f"),
        "remainder": format((total - allocated).quantize(CENT), "f"),
        "lines": len(rows),
        "largest_receipt": format(max((row["alloc_amount"] for row in rows), default=ZERO), "f"),
        "balanced": (total - allocated).quantize(CENT) == ZERO,
    }
    return rows, summary
