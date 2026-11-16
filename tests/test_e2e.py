"""端到端：样例文件 → 导入 → 匹配 → 四账 → 调整单 → 导出，以及 4xx 失败输入。"""
from __future__ import annotations

import pathlib

from fixtures import expected as X

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_full_pipeline_end_to_end(client):
    """整条链路跑通：6 个页面 200、四账平、调整单守恒、下载可读。"""
    run = client.post("/api/import", json={})
    assert run.status_code == 200
    payload = run.json()
    assert payload["status"] == "done"
    assert payload["reused"] is False
    counters = payload["counters"]
    assert counters["records_read"] == str(X.RECORDS_READ)
    assert counters["accepted"] == str(X.ACCEPTED)
    assert counters["matched"] == str(X.MATCHED)
    assert counters["diff"] == str(X.DIFF)
    assert counters["missing"] == str(X.MISSING)
    assert counters["adjust_total"] == X.ADJUST_TOTAL
    assert payload["reconcile"]["balanced"] is True
    assert payload["adjust"]["balanced"] is True

    for page in ("/", "/matches", "/diffs", "/adjust", "/batches", "/export"):
        response = client.get(page)
        assert response.status_code == 200, page
        assert "三单" in response.text
    matches_page = client.get("/matches").text
    assert "PO202611008" in matches_page
    assert "价差" in matches_page
    diffs_page = client.get("/diffs").text
    assert X.ADJUST_TOTAL in diffs_page or "241.28" in diffs_page
    export_page = client.get("/export").text
    assert "四账" in export_page and "平" in export_page

    adjustment = client.get("/api/export/download?kind=adjustment")
    assert adjustment.status_code == 200
    text = adjustment.content.decode("utf-8-sig")
    assert "PO202611003" in text
    assert X.ADJUST_TOTAL in text
    manifest = client.get("/api/export/download?kind=manifest")
    assert manifest.status_code == 200
    assert payload["run_id"] in manifest.text
    quarantine = client.get("/api/quarantine/export.csv")
    assert quarantine.status_code == 200
    assert "E_DUP_KEY_CONFLICT" in quarantine.content.decode("utf-8-sig")


def test_failure_inputs_return_4xx(client):
    """失败输入必须 4xx 且带原因码，不能 500 也不能静默成功。"""
    unknown = client.post("/api/import", json={"files": ["nope.csv"]})
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "E_UNKNOWN_FILE"
    empty = client.post("/api/import", json={"files": []})
    assert empty.status_code == 400
    first = client.post("/api/import", json={})
    assert first.status_code == 200
    batch_id = first.json()["batch_id"]

    bad_verdict = client.get(f"/api/matches?batch_id={batch_id}&verdict=lucky")
    assert bad_verdict.status_code == 400
    assert bad_verdict.json()["error"] == "E_BAD_VERDICT"
    bad_kind = client.get(f"/api/diffs?batch_id={batch_id}&kind=magic")
    assert bad_kind.status_code == 400
    bad_preview = client.get("/api/contract/preview?kind=money&value=1")
    assert bad_preview.status_code == 400
    missing_batch = client.get("/api/matches?batch_id=NOSUCH")
    assert missing_batch.status_code == 404
    bad_limit = client.get("/api/batches?limit=0")
    assert bad_limit.status_code == 400
    stale = client.post(f"/api/batches/{batch_id}/rerun",
                        json={"rule_version": "invoice3way/0.1.0"})
    assert stale.status_code == 409


def test_contract_and_preview_endpoints(client):
    """契约接口给出列定义、单位表、容差与差异类别优先级。"""
    contract = client.get("/api/contract").json()
    assert [doc["doc_type"] for doc in contract["documents"]] == ["po", "grn", "inv"]
    po_columns = [col["name"] for col in contract["documents"][0]["columns"]]
    assert po_columns[:3] == ["po_no", "line_no", "item_code"]
    assert contract["amount"]["tolerance"] == "0.02"
    assert contract["units"]["万元"] == "10000"
    assert contract["rule_version"].startswith("invoice3way/")
    assert [item["key"] for item in contract["diff_kinds"]][:2] == ["conflict",
                                                                  "missing_po"]
    preview = client.get("/api/contract/preview?kind=amount&value=1,234.50&unit=元").json()
    assert preview["value"] == "1234.50"
    assert preview["ok"] is True
    sentinel = client.get("/api/contract/preview?kind=amount&value=N/A").json()
    assert sentinel["value"] is None
    assert sentinel["code"] == "E_AMOUNT_SENTINEL"
    assert sentinel["ok"] is False
