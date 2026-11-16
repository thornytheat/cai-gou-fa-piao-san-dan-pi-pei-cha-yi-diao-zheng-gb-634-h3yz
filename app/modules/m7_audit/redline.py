"""红字发票：签负数解析、清单校验、与原发票配对、汇总冲减。

四条硬规则：
1. 红字金额必须为负。`(250.00)`、`－76.00`、`-3,487.46`、`-0.68万元` 都是负数写法，
   先归一符号再折算单位；写成正数的红字行是清单错误，不能拿去冲减。
2. 原发票号在三单表里找不到 → 「未冲抵」，挂待确认，不进净额；不静默冲减任何供应商。
3. 冲减金额绝对值大于原发票金额 → 「超额冲抵」，仍进净额但单独计数，供人工复核。
4. 红字行不参与三单匹配（不进匹配键），只进供应商汇总做冲减（TRAP-27）。
"""
from __future__ import annotations

from decimal import Decimal

from ..m1_ingest import sniff
from ..m2_amount import coerce, taxmath

ZERO = Decimal("0")
CENT = Decimal("0.01")

# 会计与全角写法里的负号：to_halfwidth 覆盖 U+FF0D，U+2212 需要单独映射
EXTRA_MINUS = {"\u2212": "-", "\u2013": "-", "\u2014": "-", "\ufe63": "-",
               "\uff0d": "-"}
PAREN_PAIRS = (("(", ")"), ("（", "）"))

STATUS_PENDING = "未冲抵"
STATUS_PARTIAL = "部分冲红"
STATUS_FULL = "全额冲抵"
STATUS_OVER = "超额冲抵"

CREDIT_COLUMNS = {
    "credit_no": ("红字单号", "credit_no", "红字发票号", "单号"),
    "original_no": ("原发票号", "original_no", "original_invoice", "对应发票号"),
    "issue_date": ("开票日期", "issue_date", "date"),
    "po_no": ("采购单号", "po_no", "po"),
    "qty": ("数量", "qty", "quantity"),
    "amount_ex": ("不含税金额", "amount_ex", "amount_ex_tax", "未税金额"),
    "tax_rate": ("税率", "tax_rate", "rate"),
    "amount_inc": ("价税合计", "amount_inc", "amount_inc_tax", "含税金额"),
    "amount_unit": ("金额单位", "amount_unit", "unit", "单位"),
}
CREDIT_REQUIRED = ("credit_no", "amount_inc")


def header_map(headers: list[str]) -> dict:
    index: dict[str, str] = {}
    for canonical, aliases in CREDIT_COLUMNS.items():
        for alias in aliases:
            index[sniff.normalize_header(alias)] = canonical
    mapping: dict[str, int] = {}
    for position, header in enumerate(headers):
        canonical = index.get(sniff.normalize_header(header))
        if canonical and canonical not in mapping:
            mapping[canonical] = position
    return mapping


def _last_parenthesized(text: str) -> bool:
    body = coerce.to_halfwidth(text).strip()
    return len(body) > 2 and body[0] == "(" and body[-1] == ")"


def parse_signed_amount(text, default_unit: str = "元"):
    """签金额解析：括号负数、全角负号、千分位、单位后缀。

    返回 (Decimal | None, 原因码 | None)。哨兵返回 None + `E_AMOUNT_SENTINEL`，
    与「解析失败」分开，上层据此决定是挂起还是算清单错误。
    """
    raw = str(text or "")
    for full, half in EXTRA_MINUS.items():
        raw = raw.replace(full, half)
    body = coerce.clean(raw).replace(" ", "")
    if body == "" or coerce.is_sentinel(body):
        return None, "E_AMOUNT_SENTINEL"
    negative = False
    if _last_parenthesized(body):
        body = body[1:-1].strip()
        negative = True
    value, code = coerce.parse_amount(body, default_unit)
    if value is None:
        return None, code or "E_AMOUNT_UNPARSABLE"
    if negative:
        value = -abs(value)
    return value.quantize(CENT), None


def parse_credit_row(cells: list[str], mapping: dict, *, amount_unit: str = "元"
                     ) -> tuple[dict | None, list[dict]]:
    """一行红字清单 → 归一后的红字行；问题按原因码收集，不抛异常。"""

    def cell(name: str) -> str:
        position = mapping.get(name)
        if position is None or position >= len(cells):
            return ""
        return cells[position]

    issues: list[dict] = []
    unit, unit_code = coerce.parse_unit(cell("amount_unit"))
    if unit_code:
        issues.append({"code": unit_code, "field": "amount_unit",
                       "raw_value": cell("amount_unit")})
    credit_no, credit_code = coerce.parse_key(cell("credit_no"))
    if credit_code:
        issues.append({"code": credit_code, "field": "credit_no",
                       "raw_value": cell("credit_no")})
    original_no, _original_code = coerce.parse_key(cell("original_no"))
    po_no, po_code = coerce.parse_key(cell("po_no"))
    if po_code and po_code != "E_MISSING_FIELD":
        issues.append({"code": po_code, "field": "po_no", "raw_value": cell("po_no")})
    elif not coerce.clean(cell("po_no")):
        issues.append({"code": "E_MISSING_FIELD", "field": "po_no",
                       "raw_value": cell("po_no")})
    issue_date, _date_code = coerce.parse_date(cell("issue_date"))
    qty, _qty_code = coerce.parse_qty(cell("qty"))
    rate, _rate_code = coerce.parse_rate(cell("tax_rate"))

    amount_ex, ex_code = parse_signed_amount(cell("amount_ex"), unit)
    if ex_code and ex_code != "E_AMOUNT_UNPARSABLE":
        issues.append({"code": ex_code, "field": "amount_ex",
                       "raw_value": cell("amount_ex")})
    amount_inc, inc_code = parse_signed_amount(cell("amount_inc"), unit)
    if inc_code == "E_AMOUNT_SENTINEL":
        issues.append({"code": "E_RED_SENTINEL", "field": "amount_inc",
                       "raw_value": cell("amount_inc")})
    elif inc_code is not None:
        issues.append({"code": "E_RED_UNPARSABLE", "field": "amount_inc",
                       "raw_value": cell("amount_inc")})
    elif amount_inc is not None and amount_inc >= ZERO:
        issues.append({"code": "E_RED_NOT_NEGATIVE", "field": "amount_inc",
                       "raw_value": cell("amount_inc"),
                       "detail": f"红字价税合计应小于 0，实际 {format(amount_inc, 'f')}"})

    row = {
        "credit_no": credit_no or "",
        "original_no": original_no or "",
        "issue_date": issue_date or "",
        "po_no": po_no or "",
        "qty": qty,
        "amount_ex": amount_ex,
        "tax_rate": rate,
        "amount_inc": amount_inc,
        "unit": unit,
        "raw": {"amount_inc": cell("amount_inc"), "original_no": cell("original_no")},
    }
    return row, issues


def blocking(issues: list[dict]) -> bool:
    return any(issue["code"] in ("E_RED_SENTINEL", "E_RED_UNPARSABLE",
                                 "E_RED_NOT_NEGATIVE") for issue in issues)


def pair_one(red: dict, inv_by_no: dict, tolerance=Decimal("0.02")) -> dict:
    """一行红字对一张原发票：定冲抵状态与剩余可冲金额。

    `offset` 表示这一行是否计入供应商净额：只有找到原发票的行才算数，
    未冲抵的行挂在待确认里，不会被静默冲减。
    """
    key = normalize_no(red.get("original_no"))
    original = inv_by_no.get(key)
    amount = red.get("amount_inc")
    if original is None:
        return {**red, "status": STATUS_PENDING, "offset": False, "over": False,
                "original_inc": None, "remaining": None,
                "code": "E_RED_UNMATCHED", "matched_invoice": "",
                "detail": f"原发票号 {red.get('original_no') or '—'} 不在本批发票里"}
    original_inc = original.get("amount_inc_tax")
    if original_inc is None or amount is None:
        return {**red, "status": STATUS_PENDING, "offset": False, "over": False,
                "original_inc": original_inc, "remaining": None,
                "code": "E_RED_UNPARSABLE", "matched_invoice": "",
                "detail": "价税合计缺失，无法配对"}
    over = abs(amount) - original_inc > tolerance
    remaining = taxmath.q2(original_inc + amount)
    if over:
        status, code = STATUS_OVER, "E_RED_OVER"
        detail = f"冲减 {format(abs(amount), 'f')} 超过原发票 {format(original_inc, 'f')}"
    elif abs(abs(amount) - original_inc) <= tolerance:
        status, code = STATUS_FULL, None
        detail = "全额冲抵"
    else:
        status, code = STATUS_PARTIAL, "E_RED_PARTIAL"
        detail = f"剩余可冲 {format(remaining, 'f')}"
    return {**red, "status": status, "offset": True, "over": over,
            "original_inc": original_inc, "remaining": remaining,
            "code": code, "detail": detail,
            "matched_invoice": original.get("invoice_no")}


def normalize_no(text) -> str:
    """发票号归一：全角转半角、去空白、转大写；比较用归一值，展示用原值。"""
    return coerce.clean(text).replace(" ", "").upper()


def pair(reds: list[dict], inv_rows: list[dict]) -> list[dict]:
    inv_by_no: dict[str, dict] = {}
    for row in sorted(inv_rows, key=lambda r: (r.get("invoice_no") or "", r["row_ref"])):
        inv_by_no.setdefault(normalize_no(row.get("invoice_no")), row)
    return [pair_one(red, inv_by_no) for red in reds]


def totals(rows: list[dict]) -> dict:
    """红字合计：已冲抵进净额，未冲抵挂待确认，两者分开显示。"""
    accepted = sum((row.get("amount_inc") or ZERO) for row in rows if row.get("offset"))
    pending = sum((row.get("amount_inc") or ZERO) for row in rows
                  if row.get("status") == STATUS_PENDING)
    return {
        "rows": len(rows),
        "accepted_total": accepted,
        "pending_total": pending,
        "offset_count": sum(1 for row in rows if row.get("offset")),
        "partial_count": sum(1 for row in rows if row.get("status") == STATUS_PARTIAL),
        "full_count": sum(1 for row in rows if row.get("status") == STATUS_FULL),
        "pending_count": sum(1 for row in rows if row.get("status") == STATUS_PENDING),
        "over_count": sum(1 for row in rows if row.get("over")),
    }


def counts(issues_by_code: list[dict]) -> list[dict]:
    tally: dict[str, int] = {}
    for issue in issues_by_code:
        tally[issue["code"]] = tally.get(issue["code"], 0) + 1
    return [{"code": code, "count": tally[code]} for code in sorted(tally)]
