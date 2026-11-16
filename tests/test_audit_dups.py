"""M7 跨批次重复报销：同号不同额、同号同额、同额不同号，以及不误报。

重复只看不同输入摘要的批次：同一份输入换版本重跑不算再次报销。
"""
from __future__ import annotations

import pathlib
import random
import shutil
from dataclasses import replace
from decimal import Decimal

from app.modules.m1_ingest import service as ingest
from app.modules.m7_audit import dups, service as audit_service
from fixtures import audit_expected as A


ROOT = pathlib.Path(__file__).resolve().parent.parent


def _patched_samples(settings, tmp_path, edits: dict[str, str]):
    """复制样例目录并改几张发票，制造第二次报送。"""
    dst = tmp_path / "samples"
    shutil.copytree(pathlib.Path(settings.sample_dir), dst)
    path = dst / "invoices_dirty.csv"
    text = path.read_text(encoding="utf-8")
    for old, new in edits.items():
        assert old in text, old
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    return replace(settings, sample_dir=str(dst))


def _two_batches(settings, store, tmp_path) -> dict:
    ingest.run_import(settings=settings, store=store)
    patched = _patched_samples(settings, tmp_path, {
        "INV-2601,2026-11-10,PO202611001,01,100,1250.00,13%,1412.50,元":
            "INV-2601,2026-11-10,PO202611001,01,100,1250.00,13%,1500.00,元",
        "INV-2608,": "INV-2699,",
    })
    ingest.run_import(settings=patched, store=store)
    assert len(store.list_batches()) == 2
    return audit_service.payload(patched, store)


def _of_kind(payload: dict, kind: str) -> list[dict]:
    return [group for group in payload["dup_groups"] if group["kind"] == kind]


def test_single_batch_has_no_duplicate(client_with_run):
    """只有一批时重复组为空：没有可比记录就不能报重复。"""
    payload = client_with_run.get("/api/audit/dups").json()
    assert payload["groups"] == []
    assert payload["counts"]["groups"] == 0
    assert payload["batch_id"]


def test_same_number_different_amount_across_batches(settings, store, tmp_path):
    """同号不同额：INV-2601 在两批里金额 1412.50 / 1500.00，两行都进重复组。"""
    payload = _two_batches(settings, store, tmp_path)
    groups = _of_kind(payload, "same_no_diff_amount")
    assert [group["key"] for group in groups] == [A.DUP_SAME_NO]
    group = groups[0]
    assert group["label"] == "同号不同额"
    assert group["batches"] == 2
    assert group["count"] == 2
    amounts = sorted(item["amount_inc"] for item in group["rows"])
    assert amounts == sorted(Decimal(value) for value in A.DUP_SAME_NO_AMOUNTS)
    assert "credit_notes" not in group["evidence"]


def test_same_amount_different_number_across_batches(settings, store, tmp_path):
    """同额不同号：687.04 在第二批换成 INV-2699，金额一致号码不同。"""
    payload = _two_batches(settings, store, tmp_path)
    groups = _of_kind(payload, "same_amount_diff_no")
    assert [group["key"] for group in groups] == [A.DUP_SAME_AMOUNT]
    numbers = sorted(item["invoice_no"] for item in groups[0]["rows"])
    assert numbers == sorted(A.DUP_SAME_AMOUNT_NOS)


def test_same_number_same_amount_counts_as_repeat(settings, store, tmp_path):
    """同号同额：第二批其余发票原样再现，逐号各成一组。"""
    payload = _two_batches(settings, store, tmp_path)
    groups = _of_kind(payload, "same_no_same_amount")
    assert len(groups) == A.DUP_SAME_BOTH_GROUPS
    assert all(group["count"] == 2 for group in groups)
    assert payload["dup_counts"]["groups"] == (
        A.DUP_SAME_BOTH_GROUPS + 2)


def test_rerun_with_new_version_is_not_repeat(settings, store):
    """同一份输入换规则版本重跑，不是第二次报销：重复组必须为空。"""
    ingest.run_import(settings=settings, store=store)
    ingest.run_import(settings=settings, store=store,
                      rule_version="invoice3way/1.3.0")
    assert len(store.list_batches()) == 2
    payload = audit_service.payload(settings, store)
    assert payload["dup_groups"] == []


def test_same_batch_conflict_not_reported_again():
    """TRAP-14 已由 M3 处理：同批次同号不同额不进重复组，避免两处结论打架。"""
    rows = [
        {"batch_id": "B1", "input_digest": "d1", "invoice_no": "INV-1",
         "amount_inc_tax": Decimal("10.00"), "po_no": "P1", "line_no": "01",
         "row_ref": "inv.csv:L2"},
        {"batch_id": "B1", "input_digest": "d1", "invoice_no": "INV-1",
         "amount_inc_tax": Decimal("12.00"), "po_no": "P1", "line_no": "01",
         "row_ref": "inv.csv:L3"},
    ]
    assert dups.detect(rows) == []


def test_invoice_no_normalized_before_compare():
    """发票号先归一：全角、空格、大小写不同也要认成同一张。"""
    rows = [
        {"batch_id": "B1", "input_digest": "d1", "invoice_no": "INV-2601",
         "amount_inc_tax": Decimal("1412.50"), "po_no": "P1", "line_no": "01",
         "row_ref": "a.csv:L2"},
        {"batch_id": "B2", "input_digest": "d2", "invoice_no": " ｉｎｖ-2601 ",
         "amount_inc_tax": Decimal("1412.50"), "po_no": "P1", "line_no": "01",
         "row_ref": "b.csv:L2"},
    ]
    groups = dups.detect(rows)
    assert [group["kind"] for group in groups] == ["same_no_same_amount"]
    assert groups[0]["key"] == "INV-2601"
    assert dups.normalize_no(" ｉｎｖ-2601 ") == "INV-2601"


def test_duplicate_detection_order_independent(settings, store, tmp_path):
    """打乱输入顺序，重复组逐字不变（顺序无关）。"""
    payload = _two_batches(settings, store, tmp_path)
    rows = []
    for batch in store.list_batches():
        for row in store.load_inv_lines(batch["batch_id"]):
            rows.append({**row, "batch_id": batch["batch_id"],
                         "input_digest": batch["input_digest"]})
    baseline = dups.detect(rows)
    rng = random.Random(20261115)
    shuffled = list(rows)
    rng.shuffle(shuffled)
    again = dups.detect(shuffled)
    assert [group["key"] for group in again] == [group["key"] for group in baseline]
    assert [group["kind"] for group in again] == [group["kind"] for group in baseline]
    assert payload["dup_counts"]["groups"] == len(baseline)


def test_dup_kind_filter_and_bad_kind(client_with_run):
    """类别过滤只留该类别；类别不在表内返回 400。"""
    ok = client_with_run.get("/api/audit/dups?kind=same_no_diff_amount")
    assert ok.status_code == 200
    assert ok.json()["kind"] == "same_no_diff_amount"
    assert all(group["kind"] == "same_no_diff_amount" for group in ok.json()["groups"])
    bad = client_with_run.get("/api/audit/dups?kind=lucky")
    assert bad.status_code == 400
    assert bad.json()["error"] == "E_BAD_DUP_KIND"
