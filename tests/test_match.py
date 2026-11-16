"""M4 三单匹配：结论计数、三类差异金额、容差、优先级、缺单归因、四账。"""
from __future__ import annotations

import random
from decimal import Decimal

from app import config
from app.modules.m4_match import compare, diffattr, reconcile
from fixtures import expected as X


def _matches(store, run):
    return store.load_matches(run["batch"]["batch_id"])


def _one(matches, key):
    return next(item for item in matches if item["key"] == key)


def test_matched_diff_missing_counts(run, store):
    """单账：15 个键 = 匹配 8 + 差异 3 + 缺单 4。"""
    counts = compare.counts(_matches(store, run))
    assert counts == {"matched": X.MATCHED, "diff": X.DIFF, "missing": X.MISSING}
    assert sum(counts.values()) == X.JUDGED


def test_price_diff_amount(run, store):
    """价差：发票不含税 486.00 对订单 30 × 15.20 = 456.00，差 30.00。"""
    row = _one(_matches(store, run), X.PRICE_DIFF_KEY)
    assert row["verdict"] == "diff"
    assert row["diff_kind"] == "price"
    assert row["price_diff"] == Decimal(X.PRICE_DIFF)
    assert row["diff_amount"] == Decimal(X.PRICE_ADJUST)


def test_qty_diff_from_receipt(run, store):
    """量差：开票 60 件、实收 58 件，量差 2，调整额按实收数量与订单单价算。"""
    row = _one(_matches(store, run), X.QTY_DIFF_KEY)
    assert row["diff_kind"] == "qty"
    assert row["qty_diff"] == Decimal(X.QTY_DIFF)
    assert row["diff_amount"] == Decimal(X.QTY_ADJUST)


def test_tax_diff_beyond_tolerance(run, store):
    """税额差：声明 1700.00 与推算 1693.31 差 6.69，超过 0.02 容差。"""
    row = _one(_matches(store, run), X.TAX_DIFF_KEY)
    assert row["diff_kind"] == "tax"
    assert row["tax_diff"] == Decimal(X.TAX_DIFF)
    assert row["diff_amount"] == Decimal(X.TAX_ADJUST)
    assert abs(row["tax_diff"]) > config.AMOUNT_TOLERANCE


def test_within_tolerance_marked_matched(run, store):
    """容差内的 0.005 不能算差异：PO202611010 仍是匹配。"""
    row = _one(_matches(store, run), X.WITHIN_TOLERANCE_KEY)
    assert row["verdict"] == "matched"
    assert row["expect_amount_inc"] == Decimal("20924.78")
    assert row["inv_amount_inc"] == Decimal("20924.78")
    assert abs(row["inv_amount_inc"] - Decimal("20924.775")) <= config.AMOUNT_TOLERANCE


def test_rule_priority_order_independent(run, store):
    """TRAP-19：把输入行顺序打乱后重建分组，结论与差异类别逐键不变。"""
    po_rows = store.load_po_lines(run["batch"]["batch_id"])
    grn_rows = store.load_grn_lines(run["batch"]["batch_id"])
    inv_rows = store.load_inv_lines(run["batch"]["batch_id"])
    baseline = {row["key"]: (row["verdict"], row["diff_kind"])
                for row in _matches(store, run)}
    rng = random.Random(20261115)
    for _ in range(3):
        shuffled = [list(po_rows), list(grn_rows), list(inv_rows)]
        for rows in shuffled:
            rng.shuffle(rows)
        groups = compare.build_groups(shuffled[0], shuffled[1], shuffled[2])
        decided = compare.decide_all(groups)
        got = {row["key"]: (row["verdict"], row["diff_kind"]) for row in decided}
        assert got == baseline


def test_naive_first_hit_wins_would_flip():
    """反例：朴素实现取「先命中的那条规则」，把命中顺序一换结论就变；
    本项目按 DIFF_RANK 取优先，顺序无关所以两次都是同一个类别。"""
    group = compare.Group(key="P|01", po_no="P", line_no="01")
    group.po = {"po_no": "P", "line_no": "01", "qty": Decimal("10"),
                "unit_price": Decimal("10.00"), "tax_rate": Decimal("0.13"),
                "row_ref": "po:L2"}
    group.grns = [{"qty_received": Decimal("8"), "row_ref": "grn:L2", "grn_no": "G1",
                   "received_at": "2026-11-02"}]
    group.invs = [{"qty": Decimal("10"), "amount_ex_tax": Decimal("120.00"),
                   "amount_inc_tax": Decimal("135.60"), "row_ref": "inv:L2",
                   "invoice_no": "I1"}]
    hits = compare.hits_of(group)
    assert set(hits) == {"qty", "price"}
    first = compare.decide(group)
    flipped = compare.decide(group, rank_table=dict(config.DIFF_RANK))
    assert first["diff_kind"] == flipped["diff_kind"] == "price"
    naive_order = hits[0]
    assert naive_order != "price"


def test_missing_doc_kind_attribution(run, store):
    """缺单要写清缺哪一张：缺订单 2 行、缺入库 1 行、缺发票 1 行。"""
    got = {row["key"]: row["diff_kind"] for row in _matches(store, run)
           if row["verdict"] == "missing"}
    assert got == X.MISSING_KINDS
    labels = {config.DIFF_LABELS[kind] for kind in got.values()}
    assert labels == {"缺采购订单", "缺入库单", "缺发票"}


def test_four_accounts_balanced(run, store):
    """四账自洽：行账 46=39+7、单账 15=8+3+4、金额账平、隔离账平。"""
    payload = reconcile.accounts(
        files=store.load_files(run["batch"]["batch_id"]),
        counters=store.load_totals(run["batch"]["batch_id"]),
        po_rows=store.load_po_lines(run["batch"]["batch_id"]),
        inv_rows=store.load_inv_lines(run["batch"]["batch_id"]),
        matches=_matches(store, run),
        quarantine_rows=store.load_quarantine(run["batch"]["batch_id"]),
    )
    assert payload["balanced"] is True
    accounts = {row["key"]: row for row in payload["accounts"]}
    assert accounts["rows"]["left"] == str(X.RECORDS_READ)
    assert accounts["rows"]["right"] == str(X.ACCEPTED + X.ERROR_QUARANTINE)
    assert accounts["lines"]["left"] == str(X.JUDGED)
    assert accounts["amount"]["left"] == X.INVOICE_TOTAL
    assert accounts["amount"]["diff"] == "0.00"
    assert payload["counts"]["matched"] == X.MATCHED


def test_invoice_total_equals_three_terms(run, store):
    """发票合计 = 匹配合计 + 差异合计 + 缺单合计，三项都从发票行独立重算。"""
    totals = diffattr.totals(_matches(store, run))
    assert totals["matched_total"] == Decimal(X.MATCHED_TOTAL)
    assert totals["diff_total"] == Decimal(X.DIFF_TOTAL)
    assert totals["missing_total"] == Decimal(X.MISSING_TOTAL)
    assert totals["adjust_total"] == Decimal(X.ADJUST_TOTAL)
    three = (totals["matched_total"] + totals["diff_total"] + totals["missing_total"])
    assert three == Decimal(X.INVOICE_TOTAL)


def test_line_key_preserves_leading_zero(run, store):
    """行号按字符串保形：01 不能变成 1，否则匹配键对不上。"""
    keys = [row["key"] for row in _matches(store, run)]
    assert "PO202611001|01" in keys
    assert "PO202611001|1" not in keys
    row = _one(_matches(store, run), "PO202611001|01")
    assert row["line_no"] == "01"


def test_qty_tolerance_zero_boundary():
    """边界：数量容差是 0，差 0.0001 也判量差；补平后才是匹配。"""
    group = compare.Group(key="P|01", po_no="P", line_no="01")
    group.po = {"po_no": "P", "line_no": "01", "qty": Decimal("10"),
                "unit_price": Decimal("10.00"), "tax_rate": Decimal("0.13"),
                "row_ref": "po:L2"}
    group.grns = [{"qty_received": Decimal("10.0000"), "grn_no": "G1",
                   "received_at": "2026-11-02", "row_ref": "grn:L2"}]
    group.invs = [{"qty": Decimal("10.0001"), "amount_ex_tax": Decimal("100.00"),
                   "amount_inc_tax": Decimal("113.00"), "invoice_no": "I1",
                   "row_ref": "inv:L2"}]
    assert compare.hits_of(group) == ["qty"]
    assert compare.decide(group)["verdict"] == "diff"
    group.invs[0]["qty"] = Decimal("10.0000")
    assert compare.hits_of(group) == []
    assert compare.decide(group)["verdict"] == "matched"
