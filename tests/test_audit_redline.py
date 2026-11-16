"""M7 红字发票：负数写法、冲抵状态、未冲抵挂待确认、红冲不计入匹配。

TRAP-27 在这里钉死：红字行不参与三单匹配，但必须进供应商汇总做冲减；
朴素实现只汇总正数发票，供应商净额会虚高。
"""
from __future__ import annotations

from decimal import Decimal

from app.modules.m7_audit import redline, service as audit_service
from fixtures import audit_expected as A
from fixtures import expected as X


def _payload(client_with_run) -> dict:
    response = client_with_run.get("/api/audit")
    assert response.status_code == 200
    return response.json()


def _red(payload: dict, credit_no: str) -> dict:
    return next(row for row in payload["credits"]["rows"] if row["credit_no"] == credit_no)


def test_signed_amount_forms_parse_to_negative():
    """负数四种写法：括号、全角负号、千分位、万元后缀，全部归到 Decimal 负数。"""
    paren, code = redline.parse_signed_amount("(250.00)", "元")
    assert (paren, code) == (Decimal("-250.00"), None)
    full, _ = redline.parse_signed_amount("－76.00", "元")
    assert full == Decimal("-76.00")
    thousands, _ = redline.parse_signed_amount("-3,487.46", "元")
    assert thousands == Decimal("-3487.46")
    wan, _ = redline.parse_signed_amount("-0.678", "万元")
    assert wan == Decimal("-6780.00")
    positive, code2 = redline.parse_signed_amount("2469.00", "元")
    assert positive == Decimal("2469.00") and code2 is None
    sentinel, sentinel_code = redline.parse_signed_amount("N/A", "元")
    assert sentinel is None and sentinel_code == "E_AMOUNT_SENTINEL"


def test_red_rows_do_not_join_matching(client_with_run):
    """TRAP-27：红字行不进匹配键，匹配结论仍是 8 / 3 / 5，发票行仍是 14。"""
    payload = _payload(client_with_run)
    totals = payload["supplier_totals"]
    assert totals["matched"] == X.MATCHED
    assert totals["diff"] == X.DIFF
    assert totals["missing"] == X.MISSING
    assert totals["inv_lines"] == X.INV_ACCEPTED
    assert payload["credits"]["totals"]["rows"] == 7


def test_partial_red_keeps_remaining_amount(client_with_run):
    """部分冲红：剩余可冲 = 原发票价税合计 + 红字金额。"""
    payload = _payload(client_with_run)
    for credit_no, (amount, remaining) in A.PARTIAL_REDS.items():
        row = _red(payload, credit_no)
        assert row["amount_inc"] == amount
        assert row["remaining"] == remaining
        assert row["status"] == "部分冲红"
        assert row["offset"] is True
        assert Decimal(row["remaining"]) > 0


def test_over_red_flagged_with_negative_remaining(client_with_run):
    """超额冲抵：红字绝对值大于原发票，剩余可冲为负并单独标出。"""
    payload = _payload(client_with_run)
    for credit_no, (amount, remaining) in A.OVER_REDS.items():
        row = _red(payload, credit_no)
        assert row["amount_inc"] == amount
        assert row["remaining"] == remaining
        assert Decimal(row["remaining"]) < 0
        assert row["status"] == "超额冲抵"
        assert row["code"] == "E_RED_OVER"
        assert row["over"] is True
    assert payload["credits"]["totals"]["over_count"] == len(A.OVER_REDS)


def test_red_unit_conversion_and_paren_amount(client_with_run):
    """万元单位与括号负数都要先归一再配对，不能按原字面比较。"""
    payload = _payload(client_with_run)
    unit_row = _red(payload, A.UNIT_RED)
    assert unit_row["amount_inc"] == "-6780.00"
    assert unit_row["unit"] == "万元"
    paren_row = _red(payload, A.NEGATIVE_PAREN_RED)
    assert paren_row["amount_inc"] == "-282.50"
    assert paren_row["matched_invoice"] == "INV-2601"


def test_unmatched_red_goes_pending_not_net(client_with_run):
    """原发票号不存在 → 未冲抵，挂待确认，不进净额。"""
    credit_no, amount = A.PENDING_RED
    payload = _payload(client_with_run)
    row = _red(payload, credit_no)
    assert row["status"] == "未冲抵"
    assert row["offset"] is False
    assert row["remaining"] == ""
    assert row["code"] == "E_RED_UNMATCHED"
    assert payload["credits"]["totals"]["pending_total"] == amount
    net_check = next(item for item in payload["checks"] if item["key"] == "net")
    assert net_check["right"] == A.NET_TOTAL
    assert amount not in (net_check["left"], net_check["right"])


def test_positive_and_sentinel_red_rows_rejected(client_with_run):
    """红字写成正数或占位符的行被拒绝，还给原因码，不进冲减。"""
    payload = _payload(client_with_run)
    rejected = payload["credits"]["rejected"]
    assert len(rejected) == len(A.REJECTED_REDS)
    got = {(item["row_ref"], issue["code"])
           for item in rejected for issue in item["issues"]}
    for expected in A.REJECTED_REDS:
        assert expected in got
    assert payload["credits"]["totals"]["rows"] == 7
    codes = {issue["code"] for item in rejected for issue in item["issues"]}
    assert "E_RED_NOT_NEGATIVE" in codes
    assert "E_RED_SENTINEL" in codes


def test_red_total_reduces_supplier_net(client_with_run):
    """TRAP-27 的反例：只加正数会得到 265550.00，冲减后净额是 252272.50。"""
    payload = _payload(client_with_run)
    row = next(item for item in payload["suppliers"]
               if item["supplier_code"] == "SUP-05")
    assert row["inv_inc"] == "265550.00"
    assert row["red_total"] == "-13277.50"
    assert row["net"] == "252272.50"
    naive = Decimal(row["inv_inc"])
    assert naive != Decimal(row["net"])
    assert Decimal(row["net"]) == naive + Decimal(row["red_total"])


def test_red_ledger_carries_file_and_encoding(client_with_run):
    """红字清单的文件名与编码随载荷返回，页面能标出读的是哪一份。"""
    payload = _payload(client_with_run)
    credits = payload["credits"]
    assert credits["file"] == A.CREDIT_FILE
    assert credits["exists"] is True
    assert credits["encoding"] in ("utf-8", "utf-8-sig", "gb18030")
    assert credits["totals"]["accepted_total"] == A.RED_ACCEPTED


def test_audit_payload_requires_a_batch(client):
    """还没有批次时复核接口 404，而不是返回一堆 0。"""
    missing = client.get("/api/audit")
    assert missing.status_code == 404
    assert missing.json()["error"] == "E_BATCH_NOT_FOUND"
    vendors = client.get("/api/audit/vendors")
    assert vendors.status_code == 404


def test_credit_payload_standalone(settings, store, run):
    """红字载荷可以脱离页面单独取：配对结果与页面同源。"""
    inv_rows = store.load_inv_lines(run["batch"]["batch_id"])
    payload = audit_service.credits_payload(settings, inv_rows)
    assert payload["totals"]["rows"] == 7
    assert payload["totals"]["offset_count"] == 6
    assert payload["totals"]["pending_count"] == 1
    assert payload["totals"]["partial_count"] == 4
    assert payload["totals"]["accepted_total"] == Decimal(A.RED_ACCEPTED)
