"""M3 质量与隔离：重复发票号冲突、原因码分布、隔离 CSV、指标、警告不隔离。"""
from __future__ import annotations

from decimal import Decimal

from app import reasons
from app.modules.m3_quality import metrics, quarantine, rules
from fixtures import expected as X


def _quarantine(store, run) -> list[dict]:
    return store.load_quarantine(run["batch"]["batch_id"])


def _by_code(rows, code):
    return [row for row in rows if row["code"] == code]


def test_duplicate_invoice_no_conflict_isolated(run, store):
    """TRAP-14：同一发票号两行金额不同 → 两行都隔离，不猜哪条为准。"""
    conflicts = _by_code(_quarantine(store, run), "E_DUP_KEY_CONFLICT")
    assert len(conflicts) == 2
    assert {row["raw_value"] for row in conflicts} == {"INV-2613"}
    assert all("金额不同" in (row["detail"] or "") for row in conflicts)
    inv_keys = {row["key"] for row in store.load_inv_lines(run["batch"]["batch_id"])}
    assert "PO202611014|01" not in inv_keys
    matches = store.load_matches(run["batch"]["batch_id"])
    row = next(item for item in matches if item["key"] == "PO202611014|01")
    assert row["verdict"] == "missing"
    assert row["diff_kind"] == X.MISSING_KINDS["PO202611014|01"]


def test_same_amount_duplicate_keeps_first():
    """金额一致的重复发票号只保留首行，记 INFO，不阻断。"""
    rows = [
        {"invoice_no": "A1", "po_no": "P1", "line_no": "01", "row_ref": "f:L2",
         "amount_inc_tax": Decimal("10.00")},
        {"invoice_no": "A1", "po_no": "P1", "line_no": "01", "row_ref": "f:L3",
         "amount_inc_tax": Decimal("10.00")},
    ]
    conflicts, deduped = rules.dup_invoice_conflicts(rows)
    assert conflicts == []
    assert [item["row_ref"] for item in deduped] == ["f:L3"]
    assert deduped[0]["code"] == "E_DUP_KEY_SAME"


def test_quarantine_distribution_sorted_by_level(run, store):
    """分布按级别（隔离 → 提示 → 记录）再按原因码排序，计数与样例一致。"""
    rows = _quarantine(store, run)
    distribution = quarantine.distribution(rows)
    levels = [item["level"] for item in distribution]
    assert levels == sorted(levels, key=lambda level: reasons.LEVEL_ORDER[level])
    error_counts = {item["code"]: item["count"] for item in distribution
                    if item["level"] == "error"}
    assert error_counts == X.QUARANTINE_CODES


def test_quarantine_csv_has_row_ref_and_raw_value(run, store):
    """隔离 CSV 每行都要有原因码 + 行号 + 原值，财务能照着复核。"""
    rows = quarantine.sort_rows(_quarantine(store, run))
    text = quarantine.to_csv(rows)
    lines = text.splitlines()
    assert lines[0] == "原因码,级别,阶段,单据,文件,行号,字段,原值,说明"
    body = "\n".join(lines[1:])
    assert "E_TRUNCATED_TAIL" in body
    assert "receipts_gb18030.csv:L" in body
    assert "GRN-9013" in body
    assert "N/A" in body
    assert X.SCIENTIFIC_KEY in body
    encoded = quarantine.encode_csv(text)
    assert encoded.startswith(b"\xef\xbb\xbf")


def test_quality_metrics_pass_rate_per_doc(run, store):
    """按单据算通过率：13/15、12/13、14/18，分母是记录数不是采纳数。"""
    files = store.load_files(run["batch"]["batch_id"])
    rows = store.load_quarantine(run["batch"]["batch_id"])
    summary = metrics.summary(files, rows)
    by_doc = {item["doc_type"]: item for item in summary["by_doc"]}
    assert by_doc["po"]["records"] == 16 and by_doc["po"]["accepted"] == X.PO_ACCEPTED
    assert by_doc["po"]["pass_rate"] == Decimal("87.50")
    assert by_doc["grn"]["pass_rate"] == Decimal("92.86")
    assert by_doc["inv"]["pass_rate"] == Decimal("77.78")
    assert summary["accepted"] + summary["quarantined"] == summary["records"]
    assert summary["records"] == X.RECORDS_READ


def test_warning_rows_not_counted_as_quarantine(run, store):
    """提示级记录（编码回退、超收）不算隔离行，隔离只数 error 级。"""
    rows = _quarantine(store, run)
    errors = [row for row in rows if row["level"] == "error"]
    warns = [row for row in rows if row["level"] == "warn"]
    assert len(errors) == X.ERROR_QUARANTINE
    assert len(warns) >= 1
    assert len(quarantine.row_level(rows)) == X.ERROR_QUARANTINE
    assert len(quarantine.file_level(rows)) == len(rows) - X.ERROR_QUARANTINE


def test_over_receipt_warn_does_not_isolate(run, store):
    """实收超订单 5% 只提示不改结论：PO202611002 仍是匹配。"""
    rows = _quarantine(store, run)
    assert not [row for row in rows if row["code"] == "E_OVER_RECEIPT"]
    matches = store.load_matches(run["batch"]["batch_id"])
    row = next(item for item in matches if item["key"] == "PO202611002|01")
    assert row["verdict"] == "matched"
    assert row["grn_qty"] == Decimal("86.0000")
    assert row["po_qty"] == Decimal("80.0000")
    po_row = next(item for item in store.load_po_lines(run["batch"]["batch_id"])
                  if item["key"] == "PO202611002|01")
    assert rules.over_receipt(po_row, Decimal("86")) is True
    assert rules.over_receipt(po_row, Decimal("84")) is False


def test_quarantine_order_is_deterministic(run, store):
    """同一批隔离行排序两次完全一致，重跑不会换顺序。"""
    rows = _quarantine(store, run)
    first = quarantine.sort_rows(rows)
    second = quarantine.sort_rows(list(reversed(rows)))
    assert [row["row_ref"] for row in first] == [row["row_ref"] for row in second]
    assert [row["code"] for row in first] == [row["code"] for row in second]


def test_three_rows_same_number_all_conflict():
    """反例：同一发票号三行三种金额，三行全部进冲突，不能只留前两条。"""
    rows = [
        {"invoice_no": "INV-X", "po_no": "P1", "line_no": "01",
         "row_ref": "f:L2", "amount_inc_tax": Decimal("10.00")},
        {"invoice_no": "INV-X", "po_no": "P1", "line_no": "01",
         "row_ref": "f:L3", "amount_inc_tax": Decimal("12.00")},
        {"invoice_no": "INV-X", "po_no": "P1", "line_no": "01",
         "row_ref": "f:L4", "amount_inc_tax": Decimal("13.00")},
    ]
    conflicts, deduped = rules.dup_invoice_conflicts(rows)
    assert deduped == []
    assert [item["row_ref"] for item in conflicts] == ["f:L2", "f:L3", "f:L4"]
    assert all(item["amounts"] == ["10.00", "12.00", "13.00"] for item in conflicts)
