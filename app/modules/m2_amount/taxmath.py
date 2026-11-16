"""价税计算：不含税 ↔ 价税合计、税额、精确合计。

一律 Decimal，量化到 0.01（ROUND_HALF_UP）。合计只在最后一步量化，
中间过程保留原始精度，避免逐行四舍五入后合计差几分。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ... import config

ZERO = Decimal("0")


def q2(value: Decimal) -> Decimal:
    return value.quantize(config.AMOUNT_QUANT, rounding=ROUND_HALF_UP)


def inc_from_ex(ex_tax: Decimal, rate: Decimal) -> Decimal:
    return q2(ex_tax * (Decimal("1") + rate))


def ex_from_inc(inc_tax: Decimal, rate: Decimal) -> Decimal:
    return q2(inc_tax / (Decimal("1") + rate))


def tax_of(ex_tax: Decimal, rate: Decimal) -> Decimal:
    return q2(ex_tax * rate)


def diff(left: Decimal | None, right: Decimal | None) -> Decimal:
    if left is None or right is None:
        return ZERO
    return left - right


def within(left: Decimal | None, right: Decimal | None, tolerance: Decimal) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def sum_amount(rows: list[dict], field: str) -> Decimal:
    total = ZERO
    for row in rows:
        value = row.get(field)
        if value is None:
            continue
        total += value
    return q2(total)


def sum_dec(values) -> Decimal:
    total = ZERO
    for value in values:
        if value is None:
            continue
        total += value
    return q2(total)
