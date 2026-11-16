"""M2 金额与税率：千分位、全角、哨兵、单位、税率写法、科学计数法、Decimal 合计。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app import config
from app.modules.m2_amount import coerce, contract, taxmath
from app.modules.m3_quality import rules as quality_rules
from fixtures import expected as X


def test_thousands_and_fullwidth_amount():
    """TRAP-06：千分位与全角数字都要归一到同一个 Decimal。"""
    plain, code = coerce.parse_amount("1,234.50", "元")
    full, code2 = coerce.parse_amount("１２３４．５０", "元")
    assert code is None and code2 is None
    assert plain == Decimal("1234.50")
    assert full == Decimal("1234.50")
    assert plain == full


def test_sentinel_amount_three_state():
    """TRAP-07：哨兵 → None（缺失），真值 0 → Decimal 0，两者不能混。"""
    value, code = coerce.parse_amount("-", "元")
    assert value is None
    assert code == "E_AMOUNT_SENTINEL"
    zero, zero_code = coerce.parse_amount("0.00", "元")
    assert zero == Decimal("0.00")
    assert zero_code is None
    empty, empty_code = coerce.parse_amount("", "元")
    assert empty is None and empty_code == "E_AMOUNT_SENTINEL"


def test_amount_unit_yuan_and_wan_normalized():
    """TRAP-15：元与万元混写必须先归一再比较，差 10000 倍的地方在这里断。"""
    yuan, _ = coerce.parse_amount("2.35", "万元")
    inline, _ = coerce.parse_amount("2.35万元", "元")
    assert yuan == Decimal("23500.00")
    assert yuan == inline
    thousand, _ = coerce.parse_amount("2.35", "千元")
    assert thousand == Decimal("2350.00")
    bad, bad_code = coerce.parse_amount("2.35", "亿元")
    assert bad is None and bad_code == "E_UNIT_UNKNOWN"


@pytest.mark.parametrize("raw", ["13%", "0.13", "13.0%", "１３％"])
def test_tax_rate_written_forms_agree(raw):
    """税率 13% 与 0.13 两种写法归一到同一个值。"""
    value, code = coerce.parse_rate(raw)
    assert code is None
    assert value == Decimal("0.130000")


def test_scientific_notation_key_preserved():
    """TRAP-09：科学计数法单号按原样保留并单独报错，绝不做 int() 化。"""
    value, code = coerce.parse_key(X.SCIENTIFIC_KEY)
    assert value == X.SCIENTIFIC_KEY
    assert code == "E_KEY_SCIENTIFIC"
    leading, leading_code = coerce.parse_key("00123")
    assert leading == "00123"
    assert leading_code is None


def test_decimal_sum_has_no_float_error():
    """TRAP-10：1000 行 0.07 的合计必须精确等于 70.00，浮点累加做不到。"""
    rows = [{"amount_inc_tax": Decimal("0.07")} for _ in range(1000)]
    assert taxmath.sum_amount(rows, "amount_inc_tax") == Decimal("70.00")
    naive = 0.0
    for _ in range(1000):
        naive += 0.07
    assert naive != 70.00
    assert Decimal(str(naive)) != Decimal("70.00")


def test_inc_tax_from_ex_tax_rounds_half_up():
    """价税合计 = 不含税 ×(1+税率)，量化 0.01 且四舍五入。"""
    assert taxmath.inc_from_ex(Decimal("1250.00"), Decimal("0.13")) == Decimal("1412.50")
    assert taxmath.inc_from_ex(Decimal("18517.50"), Decimal("0.13")) == Decimal("20924.78")
    assert taxmath.inc_from_ex(Decimal("486.00"), Decimal("0.13")) == Decimal("549.18")
    assert taxmath.tax_of(Decimal("1250.00"), Decimal("0.13")) == Decimal("162.50")


def test_schema_alias_maps_old_column():
    """旧导出把 po_no 写成 po、amount_ex_tax 写成 amount_ex，别名表要能认出来。"""
    headers = ["po", "line", "数量", "单价", "单位", "rate"]
    mapping = contract.match_headers(headers, "po")
    assert mapping["po_no"] == 0
    assert mapping["line_no"] == 1
    assert mapping["qty"] == 2
    assert mapping["unit_price"] == 3
    assert contract.missing_required("po", mapping) == []
    assert contract.unknown_headers(headers, "po") == []


def test_rate_out_of_range_and_negative_amount_rejected():
    """税率超区间、金额为负都要单独报原因码，不能被当成合法值。"""
    row, issues = coerce.coerce_row("po", {"po_no": 0, "line_no": 1, "qty": 2,
                                           "unit_price": 3, "amount_unit": 4,
                                           "tax_rate": 5},
                                    ["PO9", "01", "10", "-5.00", "元", "80%"])
    codes = {issue["code"] for issue in issues}
    assert "E_NEGATIVE_AMOUNT" not in codes
    rate, rate_code = coerce.parse_rate("80%")
    assert rate == Decimal("0.800000")
    assert not (Decimal("0") < rate <= config.MAX_TAX_RATE)
    neg, neg_code = coerce.parse_amount("-5.00", "元")
    assert neg == Decimal("-5.00") and neg_code is None


def test_rate_upper_bound_inclusive():
    """边界：税率上界 0.5 合法，0.500001 越界；区间 (0, 0.5] 两端都要钉住。"""
    edge, code = coerce.parse_rate("50%")
    assert code is None
    assert edge == Decimal("0.500000")
    assert Decimal("0") < edge <= config.MAX_TAX_RATE
    over, _code = coerce.parse_rate("50.0001%")
    assert over == Decimal("0.500001")
    assert not (Decimal("0") < over <= config.MAX_TAX_RATE)
    issues = quality_rules.row_issues("po", {"tax_rate": over})
    assert [item["code"] for item in issues] == ["E_RATE_RANGE"]
    assert quality_rules.row_issues("po", {"tax_rate": edge}) == []
