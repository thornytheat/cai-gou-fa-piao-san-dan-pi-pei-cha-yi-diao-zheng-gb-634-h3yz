"""M7 供应商汇总：主数据归集、别名合并、未登记兜底、分批入库、跨税率、守恒。

TRAP-28 在这里钉死：一个采购行来了多批入库时，实收必须各批相加；
只取最后一批或最大一批，未入库数量与超收判断都会错。
"""
from __future__ import annotations

import random
from decimal import Decimal

from app.modules.m7_audit import rollup
from fixtures import audit_expected as A
from fixtures import expected as X


def _payload(client_with_run) -> dict:
    response = client_with_run.get("/api/audit")
    assert response.status_code == 200
    return response.json()


def _supplier(payload: dict, code: str) -> dict:
    return next(row for row in payload["suppliers"] if row["supplier_code"] == code)


def test_supplier_rows_follow_master(client_with_run):
    """10 家供应商、编码与名称来自主数据，行数与红字清单无关。"""
    payload = _payload(client_with_run)
    assert payload["master"]["suppliers"] == A.MASTER_SUPPLIERS
    assert payload["master"]["po_registered"] == A.MASTER_PO_REGISTERED
    assert len(payload["suppliers"]) == len(A.SUPPLIERS)
    for code, expected in A.SUPPLIERS.items():
        row = _supplier(payload, code)
        name, po_lines, grn_lines, inv_lines, inc, red, net, diff, adjust = expected
        assert row["supplier_name"] == name
        assert row["po_lines"] == po_lines
        assert row["grn_lines"] == grn_lines
        assert row["inv_lines"] == inv_lines
        assert row["inv_inc"] == inc
        assert row["red_total"] == red
        assert row["net"] == net
        assert row["diff_total"] == diff
        assert row["adjust_total"] == adjust


def test_alias_merged_to_smallest_name(client_with_run):
    """同一编码两个名称，代表名按名称排序取第一个，与读入顺序无关。"""
    payload = _payload(client_with_run)
    row = _supplier(payload, A.ALIAS_CODE)
    assert row["supplier_name"] == A.ALIAS_NAME
    names = {item["supplier_name"] for item in payload["suppliers"]
             if item["supplier_code"] == A.ALIAS_CODE}
    assert names == {A.ALIAS_NAME}


def test_unregistered_supplier_bucket(client_with_run):
    """主数据里没有的采购单归到「未登记」，不并到相邻供应商。"""
    payload = _payload(client_with_run)
    row = _supplier(payload, "—")
    assert row["supplier_name"] == A.UNKNOWN_LABEL
    assert row["po_nos"] == "PO202611015"
    assert row["po_lines"] == 1
    assert row["grn_lines"] == 1
    assert row["inv_lines"] == 0
    assert payload["supplier_totals"]["unknown"] == 1


def test_suspended_supplier_marked(client_with_run):
    """停用供应商照样汇总，但标出停用状态。"""
    payload = _payload(client_with_run)
    row = _supplier(payload, A.SUSPENDED_CODE)
    assert row["status"] == "停用"
    assert row["suspended"] is True
    assert payload["supplier_totals"]["suspended"] == 1


def test_net_conserves_invoice_total(client_with_run):
    """不变量：Σ 供应商净额 = 发票价税合计 + 已冲抵红字，四账全平。"""
    payload = _payload(client_with_run)
    net = sum((Decimal(row["net"]) for row in payload["suppliers"]), Decimal("0"))
    red = sum((Decimal(row["red_total"]) for row in payload["suppliers"]), Decimal("0"))
    inc = sum((Decimal(row["inv_inc"]) for row in payload["suppliers"]), Decimal("0"))
    assert format(inc, "f") == X.INVOICE_TOTAL
    assert format(red, "f") == A.RED_ACCEPTED
    assert format(net, "f") == A.NET_TOTAL
    assert net == inc + red
    checks = {row["key"]: row for row in payload["checks"]}
    assert payload["balanced"] is True
    assert checks["net"]["diff"] == "0.00"
    assert checks["lines"]["left"] == str(X.INV_ACCEPTED)
    assert checks["adjust"]["diff"] == "0.00"


def test_grn_partial_receipt_split(client_with_run):
    """TRAP-28：PO202611003 订 60 收 58，未入库 2；超收行未入库为负。"""
    payload = _payload(client_with_run)
    rows = {f"{row['po_no']}|{row['line_no']}": row for row in payload["grn_splits"]}
    partial = rows[A.GRN_PARTIAL]
    ordered, received, remaining = A.GRN_PARTIAL_QTY
    assert partial["ordered"] == ordered
    assert partial["received"] == received
    assert partial["remaining"] == remaining
    assert partial["batch_count"] == 1
    over = rows[A.GRN_OVER]
    ordered, received, remaining = A.GRN_OVER_QTY
    assert over["ordered"] == ordered
    assert over["received"] == received
    assert over["remaining"] == remaining
    assert over["over"] is True


def test_grn_orphan_inbound_not_folded_into_line(client_with_run):
    """入库单还在、采购订单行被隔离：这批不能算到任何采购行的实收上。"""
    payload = _payload(client_with_run)
    totals = payload["supplier_totals"]
    assert totals["ordered"] == A.GRN_ORDERED
    assert totals["received"] == A.GRN_RECEIVED
    assert totals["orphan"] == A.GRN_ORPHAN
    assert totals["remaining"] == A.GRN_REMAINING
    assert int(totals["remaining"]) == sum(int(row["remaining"])
                                          for row in payload["grn_splits"])
    supplier = next(row for row in payload["suppliers"]
                    if row["supplier_code"] == A.SUSPENDED_CODE)
    assert supplier["orphan"] == A.GRN_ORPHAN
    assert supplier["received"] == "0"
    keys = {f"{row['po_no']}|{row['line_no']}" for row in payload["grn_splits"]}
    assert A.GRN_ORPHAN_KEY not in keys
    assert all(part["grn_no"] != A.GRN_ORPHAN_NO
               for row in payload["grn_splits"] for part in row["batches"])


def test_grn_multi_batch_sum_conserves():
    """TRAP-28 反例：三批入库 40 + 18 + 2，朴素只取最后一批得 2，按批相加得 60。"""
    grns = [
        {"grn_no": "G1", "qty_received": Decimal("40"), "received_at": "2026-11-02",
         "warehouse": "一号库", "row_ref": "g:L2"},
        {"grn_no": "G2", "qty_received": Decimal("18"), "received_at": "2026-11-05",
         "warehouse": "一号库", "row_ref": "g:L3"},
        {"grn_no": "G3", "qty_received": Decimal("2"), "received_at": "2026-11-09",
         "warehouse": "二号库", "row_ref": "g:L4"},
    ]
    parts = rollup.split_batches(Decimal("60"), grns)
    assert parts["batch_count"] == 3
    assert parts["received"] == Decimal("60")
    assert parts["remaining"] == Decimal("0")
    assert parts["balanced"] is True
    assert sum(item["qty"] for item in parts["batches"]) == parts["received"]
    assert parts["batches"][-1]["qty"] == Decimal("2")
    assert parts["received"] != parts["batches"][-1]["qty"]
    assert [item["grn_no"] for item in parts["batches"]] == ["G1", "G2", "G3"]


def test_grn_split_ignores_input_order():
    """分批顺序按入库日期 + 单号定，打乱输入顺序结果一致。"""
    grns = [
        {"grn_no": "G2", "qty_received": Decimal("18"), "received_at": "2026-11-05",
         "warehouse": "", "row_ref": "g:L3"},
        {"grn_no": "G1", "qty_received": Decimal("40"), "received_at": "2026-11-02",
         "warehouse": "", "row_ref": "g:L2"},
        {"grn_no": "G3", "qty_received": Decimal("2"), "received_at": "2026-11-09",
         "warehouse": "", "row_ref": "g:L4"},
    ]
    baseline = rollup.split_batches(Decimal("60"), grns)
    shuffled = list(grns)
    random.Random(7).shuffle(shuffled)
    again = rollup.split_batches(Decimal("60"), shuffled)
    assert [item["grn_no"] for item in again["batches"]] == ["G1", "G2", "G3"]
    assert again["received"] == baseline["received"]
    assert again["remaining"] == baseline["remaining"]


def test_rate_shift_recomputes_by_invoice_rate():
    """同一采购单跨税率变更：按发票税率重算应付，迁移额单独给。"""
    po_rows = [{"key": "P1|01", "po_no": "P1", "line_no": "01",
                "qty": Decimal("10"), "unit_price": Decimal("100.00"),
                "tax_rate": Decimal("0.130000"), "row_ref": "po:L2"}]
    inv_rows = [{"key": "P1|01", "po_no": "P1", "line_no": "01",
                 "invoice_no": "INV-1", "qty": Decimal("10"),
                 "amount_ex_tax": Decimal("1000.00"),
                 "amount_inc_tax": Decimal("1090.00"),
                 "tax_rate": Decimal("0.090000"), "row_ref": "inv:L2"}]
    events = rollup.rate_shifts(inv_rows, po_rows)
    assert len(events) == 1
    event = events[0]
    assert event["po_rate"] == Decimal("0.130000")
    assert event["inv_rate"] == Decimal("0.090000")
    assert event["inc_by_invoice_rate"] == Decimal("1090.00")
    assert event["inc_by_order_rate"] == Decimal("1130.00")
    assert event["moved_amount"] == Decimal("-40.00")
    assert rollup.rate_shifts(inv_rows, po_rows) == events


def test_rollup_order_independent(run, store, settings):
    """打乱三单行顺序，供应商汇总逐家一致（顺序无关）。"""
    batch_id = run["batch"]["batch_id"]
    master, _issues = rollup.parse_master(
        [cells for cells in _master_cells(settings)], _master_mapping(settings))
    po_rows = store.load_po_lines(batch_id)
    grn_rows = store.load_grn_lines(batch_id)
    inv_rows = store.load_inv_lines(batch_id)
    matches = store.load_matches(batch_id)
    adjustments = store.load_adjustments(batch_id)
    baseline = rollup.rollup(po_rows=po_rows, grn_rows=grn_rows, inv_rows=inv_rows,
                             matches=matches, adjustments=adjustments, reds=[],
                             master=master)
    rng = random.Random(20261115)
    shuffled = [list(po_rows), list(grn_rows), list(inv_rows)]
    for rows in shuffled:
        rng.shuffle(rows)
    again = rollup.rollup(po_rows=shuffled[0], grn_rows=shuffled[1],
                          inv_rows=shuffled[2], matches=matches,
                          adjustments=adjustments, reds=[], master=master)
    assert [row["supplier_code"] for row in again["rows"]] == [
        row["supplier_code"] for row in baseline["rows"]]
    assert [row["net"] for row in again["rows"]] == [row["net"] for row in baseline["rows"]]


def _master_mapping(settings):
    from app.modules.m7_audit import service as audit_service
    source = audit_service._read_sample(settings, A.MASTER_FILE)
    return rollup.header_map(source["headers"])


def _master_cells(settings):
    from app.modules.m7_audit import service as audit_service
    source = audit_service._read_sample(settings, A.MASTER_FILE)
    return [record.cells for record in source["records"]]


def test_vendors_api_and_page(client_with_run):
    """供应商汇总接口与页面同源；页面同时给出重复组与红字两个板块。"""
    payload = client_with_run.get("/api/audit/vendors").json()
    assert payload["batch_id"]
    assert len(payload["suppliers"]) == len(A.SUPPLIERS)
    assert payload["totals"]["net"] == A.NET_TOTAL
    assert payload["balanced"] is True
    page = client_with_run.get("/audit")
    assert page.status_code == 200
    assert "供应商汇总" in page.text
    assert "红字发票" in page.text
    assert "守恒检查" in page.text
    assert A.NET_TOTAL in page.text
    assert "E_RED_OVER" in page.text
