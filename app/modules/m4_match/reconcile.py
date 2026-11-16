"""四账核对：行账 / 单账 / 金额账 / 隔离账。

每一账都从原始数据独立重算一次，再和匹配结果比；不平就显式写差额，
不把差额抹平当通过。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, reasons
from ..m2_amount import taxmath
from ..m3_quality import quarantine as quarantine_mod
from . import compare, diffattr

ZERO = Decimal("0")


def _account(key: str, label: str, left_label: str, left, right_label: str, right,
             *, money: bool = False) -> dict:
    if money:
        difference = (right or ZERO) - (left or ZERO)
        balanced = abs(difference) <= config.AMOUNT_TOLERANCE
        left_show = format(left if left is not None else ZERO, "f")
        right_show = format(right if right is not None else ZERO, "f")
        diff_show = format(difference, "f")
    else:
        difference = (right or 0) - (left or 0)
        balanced = difference == 0
        left_show = str(left if left is not None else 0)
        right_show = str(right if right is not None else 0)
        diff_show = str(difference)
    return {
        "key": key,
        "label": label,
        "left_label": left_label,
        "left": left_show,
        "right_label": right_label,
        "right": right_show,
        "diff": diff_show,
        "balanced": balanced,
        "money": money,
    }


def accounts(*, files: list[dict], counters: dict, po_rows: list[dict],
             inv_rows: list[dict], matches: list[dict],
             quarantine_rows: list[dict]) -> dict:
    records_read = int(counters.get("records_read") or 0)
    accepted = int(counters.get("accepted") or 0)
    quarantined = len(quarantine_mod.row_level(quarantine_rows))
    blank = int(counters.get("blank") or 0)
    repeats = int(counters.get("header_repeat") or 0)

    row_account = _account("rows", "行账", "数据记录数", records_read,
                           "采纳 + 隔离", accepted + quarantined)
    row_account["note"] = f"空行 {blank} 行、重复表头 {repeats} 行另计"

    verdict_counts = compare.counts(matches)
    judged = len(matches)
    doc_account = _account(
        "lines", "单账", "三单键数", judged,
        "匹配 + 差异 + 缺单",
        verdict_counts.get("matched", 0) + verdict_counts.get("diff", 0)
        + verdict_counts.get("missing", 0))

    invoice_total = taxmath.sum_dec(row.get("amount_inc_tax") for row in inv_rows)
    totals = diffattr.totals(matches)
    money_account = _account(
        "amount", "金额账", "发票价税合计", invoice_total,
        "匹配合计 + 差异合计 + 缺单合计",
        totals["matched_total"] + totals["diff_total"] + totals["missing_total"],
        money=True)
    money_account["note"] = "缺单也有发票金额，必须计入合计，不能抹平"

    isolation_total = len(quarantine_rows)
    distribution_total = sum(row.get("count", 0)
                             for row in reasons.distribution(quarantine_rows))
    isolation_account = _account("isolation", "隔离账", "隔离行数", isolation_total,
                                 "原因码分布合计", distribution_total)

    all_accounts = [row_account, doc_account, money_account, isolation_account]
    return {
        "accounts": all_accounts,
        "balanced": all(row["balanced"] for row in all_accounts),
        "unbalanced": [row["key"] for row in all_accounts if not row["balanced"]],
        "counts": {
            "records_read": records_read,
            "accepted": accepted,
            "quarantined": quarantined,
            "blank": blank,
            "header_repeat": repeats,
            "judged": judged,
            "matched": verdict_counts.get("matched", 0),
            "diff": verdict_counts.get("diff", 0),
            "missing": verdict_counts.get("missing", 0),
        },
        "totals": {key: format(value, "f") for key, value in totals.items()},
        "invoice_total": format(invoice_total, "f"),
        "po_lines": len(po_rows),
        "grn_lines": int(counters.get("grn_accepted") or 0),
        "inv_lines": len(inv_rows),
    }
