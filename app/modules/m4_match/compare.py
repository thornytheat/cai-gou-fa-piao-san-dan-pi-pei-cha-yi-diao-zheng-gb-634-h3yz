"""三单匹配：按 (采购单号, 行号) 分组，命中规则全部收集后按固定优先级取结论。

判定不写成 if/elif 链：先把所有命中的差异规则收进一个集合，再按
`config.DIFF_RANK` 取最优先的一条。这样规则书写顺序变了，结论不会变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ... import config
from ..m2_amount import taxmath

ZERO = Decimal("0")


@dataclass
class Group:
    key: str
    po_no: str
    line_no: str
    po: dict | None = None
    grns: list = field(default_factory=list)
    invs: list = field(default_factory=list)

    @property
    def grn_qty(self) -> Decimal | None:
        if not self.grns:
            return None
        return sum((row.get("qty_received") or ZERO) for row in self.grns)

    @property
    def inv_qty(self) -> Decimal | None:
        if not self.invs:
            return None
        return sum((row.get("qty") or ZERO) for row in self.invs)

    @property
    def inv_ex(self) -> Decimal | None:
        if not self.invs:
            return None
        return taxmath.sum_dec(row.get("amount_ex_tax") for row in self.invs)

    @property
    def inv_inc(self) -> Decimal | None:
        if not self.invs:
            return None
        return taxmath.sum_dec(row.get("amount_inc_tax") for row in self.invs)

    def row_refs(self) -> list[str]:
        refs = []
        if self.po:
            refs.append(self.po["row_ref"])
        refs.extend(row["row_ref"] for row in sorted(self.grns, key=lambda r: r["row_ref"]))
        refs.extend(row["row_ref"] for row in sorted(self.invs, key=lambda r: r["row_ref"]))
        return refs


def key_of(po_no: str, line_no: str) -> str:
    return f"{po_no}|{line_no}"


def build_groups(po_rows: list[dict], grn_rows: list[dict],
                 inv_rows: list[dict]) -> list[Group]:
    groups: dict[str, Group] = {}
    for row in po_rows:
        key = key_of(row["po_no"], row["line_no"])
        group = groups.setdefault(key, Group(key=key, po_no=row["po_no"],
                                             line_no=row["line_no"]))
        group.po = row
    for row in grn_rows:
        key = key_of(row["po_no"], row["line_no"])
        group = groups.setdefault(key, Group(key=key, po_no=row["po_no"],
                                            line_no=row["line_no"]))
        group.grns.append(row)
    for row in inv_rows:
        key = key_of(row["po_no"], row["line_no"])
        group = groups.setdefault(key, Group(key=key, po_no=row["po_no"],
                                            line_no=row["line_no"]))
        group.invs.append(row)
    for group in groups.values():
        group.grns.sort(key=lambda r: r["row_ref"])
        group.invs.sort(key=lambda r: r["row_ref"])
    return [groups[key] for key in sorted(groups)]


def hits_of(group: Group, rank_table: dict | None = None) -> list[str]:
    """收集本组命中的全部差异类别；不做短路，便于一次看全所有差异。"""
    hits: list[str] = []
    if group.po is None:
        return ["missing_po"]
    if not group.grns:
        hits.append("missing_grn")
    if not group.invs:
        hits.append("missing_inv")
    if hits:
        return hits
    qty_diff = (group.inv_qty or ZERO) - (group.grn_qty or ZERO)
    if abs(qty_diff) > config.QTY_TOLERANCE:
        hits.append("qty")
    expect_ex, expect_inc = _expect(group)
    inv_ex = group.inv_ex or ZERO
    inv_inc = group.inv_inc or ZERO
    if expect_ex is not None:
        if abs(inv_ex - expect_ex) > config.AMOUNT_TOLERANCE:
            hits.append("price")
    if expect_inc is not None:
        if abs(inv_inc - expect_inc) > config.AMOUNT_TOLERANCE:
            hits.append("tax")
    return hits


def _expect_inc(group: Group) -> Decimal | None:
    """税额差的对照值：由发票声明的不含税金额按税率推算价税合计。"""
    if group.po is None or group.inv_ex is None:
        return None
    rate = group.po.get("tax_rate")
    if rate is None:
        return None
    return taxmath.inc_from_ex(group.inv_ex, rate)


def _expect(group: Group) -> tuple[Decimal | None, Decimal | None]:
    if group.po is None or not group.invs:
        return None, None
    qty = group.inv_qty or ZERO
    price = group.po.get("unit_price")
    rate = group.po.get("tax_rate")
    if price is None or rate is None:
        return None, None
    expect_ex = taxmath.q2(qty * price)
    return expect_ex, _expect_inc(group)


def decide(group: Group, *, rank_table: dict | None = None) -> dict:
    ranks = rank_table or config.DIFF_RANK
    hits = hits_of(group, ranks)
    expect_ex, expect_inc = _expect(group)
    payable_ex, payable_inc = _payable(group)
    inv_qty = group.inv_qty
    grn_qty = group.grn_qty
    qty_diff = (inv_qty - grn_qty) if (inv_qty is not None and grn_qty is not None) else None
    price_diff = None
    if group.inv_ex is not None and expect_ex is not None:
        price_diff = group.inv_ex - expect_ex
    tax_diff = None
    if group.inv_inc is not None and expect_inc is not None:
        tax_diff = group.inv_inc - expect_inc

    if any(hit.startswith("missing_") for hit in hits):
        verdict = "missing"
    elif hits:
        verdict = "diff"
    else:
        verdict = "matched"
    diff_kind = min(hits, key=lambda kind: ranks.get(kind, 99)) if hits else None

    diff_amount = ZERO
    if group.inv_inc is not None and payable_inc is not None:
        diff_amount = group.inv_inc - payable_inc

    return {
        "key": group.key,
        "po_no": group.po_no,
        "line_no": group.line_no,
        "verdict": verdict,
        "diff_kind": diff_kind,
        "reasons": sorted(hits, key=lambda kind: (ranks.get(kind, 99), kind)),
        "po_qty": (group.po or {}).get("qty"),
        "grn_qty": grn_qty,
        "inv_qty": inv_qty,
        "unit_price": (group.po or {}).get("unit_price"),
        "inv_amount_ex": group.inv_ex,
        "inv_amount_inc": group.inv_inc,
        "expect_amount_ex": expect_ex,
        "expect_amount_inc": expect_inc,
        "diff_amount": diff_amount,
        "qty_diff": qty_diff,
        "price_diff": price_diff,
        "tax_diff": tax_diff,
        "row_refs": group.row_refs(),
        "note": config.DIFF_LABELS.get(diff_kind or "", "") if diff_kind else "",
    }


def _payable(group: Group) -> tuple[Decimal | None, Decimal | None]:
    """按「实收数量 × 订单单价」算应付；缺单时返回 None。"""
    if group.po is None or group.grn_qty is None:
        return None, None
    price = group.po.get("unit_price")
    rate = group.po.get("tax_rate")
    if price is None or rate is None:
        return None, None
    ex = taxmath.q2(group.grn_qty * price)
    return ex, taxmath.inc_from_ex(ex, rate)


def decide_all(groups: list[Group], *, rank_table: dict | None = None) -> list[dict]:
    return [decide(group, rank_table=rank_table) for group in groups]


def counts(matches: list[dict]) -> dict:
    out = {verdict: 0 for verdict in config.VERDICTS}
    for match in matches:
        out[match["verdict"]] = out.get(match["verdict"], 0) + 1
    return out
