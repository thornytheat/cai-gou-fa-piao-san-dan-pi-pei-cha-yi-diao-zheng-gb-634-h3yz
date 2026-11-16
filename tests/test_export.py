"""M6 调整单与导出：最大余额法守恒、分摊确定性、CSV 保形、清单序列化。"""
from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

import pytest

from app import errors
from app.modules.m6_export import adj, csv_out, manifest_out, service as export_service
from fixtures import expected as X


def test_largest_remainder_conserves_total():
    """TRAP-24：100.00 按 1:1:1 分，每份 33.33 + 1 分尾差补出去，合计仍 100.00。"""
    shares = adj.allocate(Decimal("100.00"),
                          {"A": Decimal("1"), "B": Decimal("1"), "C": Decimal("1")})
    assert sum(shares.values()) == Decimal("100.00")
    assert sorted(shares.values()) == [Decimal("33.33"), Decimal("33.33"),
                                       Decimal("33.34")]
    assert set(shares) == {"A", "B", "C"}


def test_allocation_is_deterministic_and_floors():
    """权重不等时先向下取整到分，尾差按小数部分降序、单号次序补。"""
    weights = {"PO202611003": Decimal("5328.00"), "PO202611008": Decimal("486.00"),
               "PO202611011": Decimal("1498.50")}
    total = Decimal(X.ADJUST_TOTAL)
    first = adj.allocate(total, weights)
    second = adj.allocate(total, dict(reversed(list(weights.items()))))
    assert first == second
    assert {key: format(value, "f") for key, value in first.items()} == X.ALLOC
    assert sum(first.values()) == total


def test_adjustment_total_equals_diff_total(run, store):
    """调整单合计必须等于差异金额合计，且每行都有证据行。"""
    rows = store.load_adjustments(run["batch"]["batch_id"])
    total = sum((row["alloc_amount"] for row in rows), Decimal("0"))
    assert format(total, "f") == X.ADJUST_TOTAL
    assert [row["po_no"] for row in rows] == ["PO202611003", "PO202611008",
                                              "PO202611011"]
    assert all(row["evidence"] for row in rows)
    assert rows[1]["alloc_amount"] == Decimal(X.ALLOC["PO202611008"])


def test_adjustment_csv_keeps_big_integer_key():
    """TRAP-09：科学计数法单号导出后与输入逐字相同，不被转成整数。"""
    rows = [{"adj_no": "ADJ-01", "target_no": "PO1", "po_no": X.SCIENTIFIC_KEY,
             "weight": Decimal("1.00"), "alloc_amount": Decimal("0.50"),
             "diff_kind": "price", "evidence": ["inv:L2"]}]
    text = csv_out.adjustment_csv(rows, {"total": "0.50", "allocated": "0.50",
                                        "remainder": "0.00"})
    reader = csv.reader(io.StringIO(text))
    body = list(reader)
    assert body[1][1] == X.SCIENTIFIC_KEY
    assert body[1][1] == "4.500123E+9"
    assert body[-1][0] == "尾差"


def test_manifest_json_decimal_as_string(run, store):
    """清单里金额是字符串，JSON 往返后与库内值逐字相同。"""
    manifest = store.load_manifest(run["batch"]["batch_id"])
    text = manifest_out.to_json(manifest)
    reloaded = json.loads(text)
    assert reloaded["counters"]["adjust_total"] == X.ADJUST_TOTAL
    assert isinstance(reloaded["counters"]["matched_total"], str)
    rows = manifest_out.summary_rows(manifest)
    assert rows[0]["label"] == "运行号"
    assert rows[0]["value"] == run["batch"]["run_id"]
    inputs = manifest_out.input_rows(manifest)
    assert len(inputs) == 3
    assert inputs[0]["doc_label"] == "采购订单"


def test_zero_total_allocation_returns_zero_rows(settings, store, run):
    """没有差异时调整单为空，不产生 0 元行。"""
    rows, summary = adj.build_sheet([], store.load_inv_lines(run["batch"]["batch_id"]),
                                    batch_id=run["batch"]["batch_id"])
    assert rows == []
    assert summary["total"] == "0.00"
    assert summary["balanced"] is True
    assert adj.allocate(Decimal("0.00"), {"A": Decimal("1")}) == {"A": Decimal("0.00")}


def test_export_kinds_reject_unknown(client_with_run):
    """未知导出类别返回 4xx，不返回空文件。"""
    bad = client_with_run.get("/api/export/download?kind=everything")
    assert bad.status_code == 400
    assert bad.json()["error"] == "E_BAD_EXPORT_KIND"
    ok = client_with_run.get("/api/export/download?kind=matches")
    assert ok.status_code == 200
    assert ok.content.startswith(b"\xef\xbb\xbf")


def test_csv_has_bom_for_excel(run, store):
    """导出的 CSV 带 BOM，Excel 直接打开不乱码；导回时 M1 会先剥掉。"""
    body, name, media = export_service.export_bytes(store, "matches",
                                                    run["batch"]["batch_id"])
    assert body.startswith(b"\xef\xbb\xbf")
    assert name.endswith("-matches.csv")
    assert media.startswith("text/csv")
    text = body.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("采购单号,行号,结论")
    quarantine_body, quarantine_name, _ = export_service.export_bytes(
        store, "quarantine", run["batch"]["batch_id"])
    assert quarantine_body.startswith(b"\xef\xbb\xbf")
    assert "quarantine.csv" in quarantine_name


def test_export_payload_carries_reconcile_and_watermark(client_with_run):
    """导出页载荷同时给出调整汇总、四账与水位，页面与文件同源。"""
    payload = client_with_run.get("/api/export").json()
    assert payload["adjust_summary"]["total"] == X.ADJUST_TOTAL
    assert payload["adjust_summary"]["remainder"] == "0.00"
    assert len(payload["adjustments"]) == 3
    assert payload["watermark"]["batch_id"] == payload["batch_id"]
    reconcile = client_with_run.get("/api/reconcile").json()
    assert reconcile["balanced"] is True
    assert reconcile["counts"]["judged"] == X.JUDGED
    unknown = client_with_run.get("/api/diffs?kind=nonsense")
    assert unknown.status_code == 400
    with pytest.raises(errors.BadRequest):
        export_service.export_bytes(client_with_run.app.state.store, "nope")
