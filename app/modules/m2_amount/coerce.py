"""字段归一：全角转半角、千分位、金额单位、税率写法、日期、单号保形。

每个函数返回 (值, 原因码)，原因码 None 表示这一格干净；不抛异常，
让上层按行收集问题，一行有多个坑也只隔离一次、但每个坑都留原因码。
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from ... import config
from . import contract

SCIENTIFIC_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?[eE][+-]?\d+$")
THOUSANDS_RE = re.compile(r"(?<=\d)[,，](?=\d)")
UNIT_SUFFIXES = ("万元", "千元", "元", "万", "千", "块")
DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y.%m.%d")


def to_halfwidth(text: str) -> str:
    table = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
    table[0x3000] = 0x20
    return str(text or "").translate(table)


def is_sentinel(text: str) -> bool:
    return to_halfwidth(text).strip().lower() in config.SENTINELS


def clean(text: str) -> str:
    return to_halfwidth(text).replace("\u00a0", " ").strip()


def split_unit(text: str) -> tuple[str, str | None]:
    """把「1,234.50万元」拆成 (数字串, 单位)；没写单位返回 None。"""
    body = clean(text).replace("￥", "").replace("¥", "").replace(" ", "")
    for suffix in UNIT_SUFFIXES:
        if body.endswith(suffix) and len(body) > len(suffix):
            head = body[: -len(suffix)]
            if re.fullmatch(r"[+-]?[\d,，.]+", head):
                return head, suffix
    return body, None


def parse_number(text: str) -> tuple[Decimal | None, str | None]:
    body, _unit = split_unit(text)
    if body == "":
        return None, "E_AMOUNT_SENTINEL"
    if is_sentinel(body):
        return None, "E_AMOUNT_SENTINEL"
    body = THOUSANDS_RE.sub("", body)
    try:
        value = Decimal(body)
    except InvalidOperation:
        return None, "E_AMOUNT_UNPARSABLE"
    if not value.is_finite():
        return None, "E_AMOUNT_UNPARSABLE"
    return value, None


def parse_unit(text: str | None) -> tuple[str, str | None]:
    body = clean(text or "")
    if body == "":
        return config.MONEY_UNIT, None
    factor = config.UNIT_TO_YUAN.get(body)
    if factor is None:
        return config.MONEY_UNIT, "E_UNIT_UNKNOWN"
    return body, None


def parse_amount(text: str, default_unit: str = config.MONEY_UNIT
                 ) -> tuple[Decimal | None, str | None]:
    body, inline_unit = split_unit(text)
    unit = inline_unit or default_unit
    factor = config.UNIT_TO_YUAN.get(unit)
    if factor is None:
        return None, "E_UNIT_UNKNOWN"
    value, code = parse_number(body)
    if value is None:
        return None, code
    return (value * factor).quantize(config.AMOUNT_QUANT), None


def parse_qty(text: str) -> tuple[Decimal | None, str | None]:
    value, code = parse_number(text)
    if value is None:
        return None, code
    return value.quantize(config.QTY_QUANT), None


def parse_rate(text: str) -> tuple[Decimal | None, str | None]:
    body = clean(text)
    if body == "" or is_sentinel(body):
        return None, "E_RATE_UNPARSABLE"
    percent = body.endswith("%")
    if percent:
        body = body[:-1]
    value, code = parse_number(body)
    if value is None:
        return None, "E_RATE_UNPARSABLE"
    if percent:
        value = value / Decimal("100")
    elif value > 1:
        # 写成 13 这种整数百分比，按 13% 处理；13 倍税率不存在
        value = value / Decimal("100")
    return value.quantize(config.RATE_QUANT), None


def parse_date(text: str) -> tuple[str | None, str | None]:
    body = clean(text)
    if body == "" or is_sentinel(body):
        return None, "E_DATE_UNPARSABLE"
    head = body.split("T")[0].split(" ")[0]
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(head, fmt).date().isoformat(), None
        except ValueError:
            continue
    return None, "E_DATE_UNPARSABLE"


def parse_key(text: str) -> tuple[str | None, str | None]:
    """单号按字符串保形：不 int() 化、不去前导零、科学计数法原值保留但报错。"""
    body = clean(text)
    if body == "":
        return None, "E_MISSING_FIELD"
    if SCIENTIFIC_RE.match(body):
        return body, "E_KEY_SCIENTIFIC"
    return body, None


def coerce_row(doc_type: str, mapping: dict, cells: list[str]) -> tuple[dict, list[dict]]:
    """把一行原始单元格变成契约字段；返回 (行, 问题列表)。"""
    raw = {}
    for name, position in mapping.items():
        raw[name] = cells[position] if position < len(cells) else ""
    issues: list[dict] = []

    unit = config.MONEY_UNIT
    if "amount_unit" in mapping:
        unit, unit_code = parse_unit(raw.get("amount_unit"))
        if unit_code:
            issues.append({"code": unit_code, "field": "amount_unit",
                           "raw_value": raw.get("amount_unit")})

    row: dict = {}
    for name, _label, kind, required, _aliases in contract.columns(doc_type):
        value = raw.get(name, "")
        if kind == "unit":
            row["unit"] = unit
            continue
        if kind == "text":
            parsed, code = parse_key(value) if contract.is_key_column(doc_type, name) \
                else (clean(value) or None, None)
        elif kind == "amount":
            parsed, code = parse_amount(value, unit)
        elif kind == "qty":
            parsed, code = parse_qty(value)
        elif kind == "rate":
            parsed, code = parse_rate(value)
        elif kind == "date":
            parsed, code = parse_date(value)
        else:
            parsed, code = clean(value) or None, None
        if code:
            issues.append({"code": code, "field": name, "raw_value": value,
                           "detail": contract.label_of(doc_type, name)})
        elif required and (parsed is None or parsed == ""):
            issues.append({"code": "E_MISSING_FIELD", "field": name, "raw_value": value,
                           "detail": contract.label_of(doc_type, name)})
        row[name] = parsed
    row["raw"] = raw
    return row, issues


def preview(kind: str, raw: str, unit: str | None = None) -> dict:
    """单格试算：把归一结果、原因码、原值一起回给页面。"""
    if kind == "amount":
        value, code = parse_amount(raw, unit or config.MONEY_UNIT)
        shown = None if value is None else format(value, "f")
    elif kind == "qty":
        value, code = parse_qty(raw)
        shown = None if value is None else format(value, "f")
    elif kind == "rate":
        value, code = parse_rate(raw)
        shown = None if value is None else format(value, "f")
    elif kind == "date":
        value, code = parse_date(raw)
        shown = value
    elif kind == "text":
        value, code = parse_key(raw)
        shown = value
    else:
        value, code, shown = None, None, clean(raw)
    return {"kind": kind, "raw": raw, "value": shown, "code": code,
            "ok": code is None}
