"""M2 编排：契约载荷、单格试算、整行归一。

对外只暴露这个 service，其他模块不 import coerce / taxmath 的内部函数。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config, errors
from . import coerce, contract, taxmath

KINDS = ("text", "amount", "qty", "rate", "date", "unit")


def contract_payload(settings) -> dict:
    return contract.contract_payload(settings)


def preview(settings, kind: str, raw: str, unit: str | None = None) -> dict:
    if kind not in KINDS:
        raise errors.BadRequest(f"kind 只能是 {', '.join(KINDS)}", code="E_BAD_KIND")
    result = coerce.preview(kind, raw, unit)
    result["rule_version"] = settings.rule_version
    return result


def coerce_row(doc_type: str, mapping: dict, cells: list[str]) -> tuple[dict, list[dict]]:
    if doc_type not in config.DOC_TYPES:
        raise errors.BadRequest(f"未知单据类型 {doc_type}", code="E_BAD_DOC_TYPE")
    return coerce.coerce_row(doc_type, mapping, cells)


def inc_from_ex(ex_tax: Decimal, rate: Decimal) -> Decimal:
    return taxmath.inc_from_ex(ex_tax, rate)


def expect_amounts(doc: dict) -> tuple[Decimal | None, Decimal | None]:
    """按订单数量 × 不含税单价 与税率推出应到的不含税/价税合计。"""
    qty = doc.get("qty")
    price = doc.get("unit_price")
    rate = doc.get("tax_rate")
    if qty is None or price is None or rate is None:
        return None, None
    ex = taxmath.q2(qty * price)
    return ex, taxmath.inc_from_ex(ex, rate)
