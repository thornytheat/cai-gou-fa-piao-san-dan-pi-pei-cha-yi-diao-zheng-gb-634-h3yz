"""批次输入留存与重算语义：原件绑定、源表替换/移走、失败重试、并发、中断恢复。

覆盖的用户承诺：
- 新导入采信当天更正件；
- 历史批次重算保住原件与原评估日，不用更正件冒充原始凭证；
- 采信后源表被替换或移走仍可重算；
- 失败重试仍用原件；并发重跑只落一次账；
- 结果入库前中断可恢复，未完成结果不可见；
- 缺原件的旧批次明确拒绝重算，不从现有文件补造。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import pathlib
import shutil
import threading

import pytest
from fastapi.testclient import TestClient

from app import config, errors
from app.main import create_app
from app.modules.m1_ingest import service as ingest
from app.modules.m5_batches import history, resume, service as batch_service
from app.store import Store

ORIGINAL = "orig"
CORRECTED = "corrected"


@pytest.fixture()
def live_settings(settings, tmp_path) -> config.Settings:
    """每个用例一份独立样例副本，绝不改仓库里的 sample_data。"""
    base = tmp_path / "samples"
    shutil.copytree(settings.sample_dir, base)
    return dataclasses.replace(settings, sample_dir=str(base))


@pytest.fixture()
def live_client(live_settings) -> TestClient:
    client = TestClient(create_app(live_settings))
    response = client.post("/api/import", json={})
    assert response.status_code == 200, response.text
    return client


def _file(settings, name: str) -> pathlib.Path:
    return pathlib.Path(settings.sample_dir) / name


def _swap(settings, name: str, text: str) -> None:
    _file(settings, name).write_bytes(text.encode("utf-8"))


def _pristine(settings, tmp_path) -> dict:
    """把样例目录复制一份，测试可以随意替换源表。"""
    base = tmp_path / "samples"
    if not base.exists():
        shutil.copytree(settings.sample_dir, base)
    return dataclasses.replace(settings, sample_dir=str(base))


def _stored_bytes(store, batch_id: str) -> dict:
    return {item["name"]: item["bytes"] for item in store.load_input_bytes(batch_id)}


def test_import_retains_inputs_with_sha256(settings, store):
    """采信即留存：每份输入的字节、摘要、来源路径都随批次落库。"""
    result = ingest.run_import(settings=settings, store=store)
    batch_id = result["batch"]["batch_id"]
    saved = store.load_input_bytes(batch_id)
    assert [item["name"] for item in saved] == [
        "orders_dirty.csv", "receipts_gb18030.csv", "invoices_dirty.csv"]
    for item in saved:
        assert item["bytes"] == _file(settings, item["name"]).read_bytes()
        assert len(item["sha256"]) == 64
        assert item["source_path"].endswith(item["name"])
    assert store.get_batch(batch_id)["eval_date"] == settings.eval_date.isoformat()


def test_rerun_uses_retained_original_after_source_replaced(settings, store, tmp_path):
    """源表被当天更正件替换后，历史批次重算仍按原件，不把更正件当原始凭证。"""
    live = _pristine(settings, tmp_path)
    first = ingest.run_import(settings=live, store=store)
    batch_id = first["batch"]["batch_id"]

    _swap(live, "invoices_dirty.csv",
          "invoice_no,invoice_date,po_no,line_no,qty,amount_ex_tax,tax_rate,"
          "amount_inc_tax,amount_unit\nINV-9999,2026-11-15,PO202611001,01,1,1.00,13%,1.13,元\n")

    rerun = batch_service.rerun(store, live, batch_id)
    assert rerun["reused"] is True
    assert rerun["batch_id"] == batch_id
    assert str(store.count_rows(batch_id)["inv_lines"]) == first["counters"]["inv_accepted"]
    assert "INV-9999" not in str(store.load_inv_lines(batch_id))

    # 换新规则版本重算，仍只认原件
    newer = batch_service.rerun(store, live, batch_id, rule_version="invoice3way/1.9.0")
    assert newer["reused"] is False
    assert newer["source_batch_id"] == batch_id
    assert newer["batch_id"] != batch_id
    assert "INV-9999" not in str(store.load_inv_lines(newer["batch_id"]))
    assert store.load_totals(newer["batch_id"])["records_read"] == \
        first["counters"]["records_read"]


def test_rerun_survives_source_removed(settings, store, tmp_path):
    """采信后源表被移走，仍可按留存原件重算。"""
    live = _pristine(settings, tmp_path)
    first = ingest.run_import(settings=live, store=store)
    batch_id = first["batch"]["batch_id"]
    shutil.rmtree(live.sample_dir)
    rerun = batch_service.rerun(store, live, batch_id)
    assert rerun["reused"] is True
    assert rerun["same_batch"] is True
    assert store.load_totals(batch_id)["adjust_total"] == "241.28"


def test_new_import_uses_corrected_file_without_touching_history(settings, store, tmp_path):
    """新导入用当天更正件生成新批次；旧批次的原件、评估日与结果都不动。"""
    live = _pristine(settings, tmp_path)
    first = ingest.run_import(settings=live, store=store)
    old_id = first["batch"]["batch_id"]
    old_inputs = _stored_bytes(store, old_id)
    old_totals = store.load_totals(old_id)

    text = _file(live, "invoices_dirty.csv").read_text(encoding="utf-8")
    _swap(live, "invoices_dirty.csv", text + "\n")
    second = ingest.run_import(settings=live, store=store)
    new_id = second["batch"]["batch_id"]

    assert second["reused"] is False
    assert new_id != old_id
    assert _stored_bytes(store, old_id) == old_inputs
    assert store.load_totals(old_id) == old_totals
    assert _stored_bytes(store, new_id) != old_inputs
    assert store.get_batch(old_id)["eval_date"] == store.get_batch(new_id)["eval_date"]


def test_rerun_keeps_original_eval_date(settings, store, tmp_path):
    """重算沿用批次原评估日；换评估日的新导入是另一个批次键。"""
    live = _pristine(settings, tmp_path)
    first = ingest.run_import(settings=live, store=store)
    batch_id = first["batch"]["batch_id"]

    later = dataclasses.replace(live, eval_date=dt.date(2026, 11, 30))
    rerun = batch_service.rerun(store, later, batch_id, rule_version="invoice3way/1.9.0")
    new_batch = store.get_batch(rerun["batch_id"])
    assert new_batch["eval_date"] == settings.eval_date.isoformat()

    fresh = ingest.run_import(settings=later, store=store)
    assert fresh["batch"]["batch_key"] != first["batch"]["batch_key"]
    assert store.get_batch(fresh["batch"]["batch_id"])["eval_date"] == "2026-11-30"


def test_failed_retry_reuses_retained_original(settings, store, tmp_path, monkeypatch):
    """失败后重试仍用原件：重试期间把源表换成更正件也不影响。"""
    live = _pristine(settings, tmp_path)
    first = ingest.run_import(settings=live, store=store)
    batch_id = first["batch"]["batch_id"]
    original = _stored_bytes(store, batch_id)

    # 制造一次在算完匹配、写库前失败的运行
    def boom(*args, **kwargs):
        raise RuntimeError("模拟入库前中断")

    from app.modules.m6_export import adj as adj_mod
    monkeypatch.setattr(adj_mod, "build_sheet", boom)
    with pytest.raises(RuntimeError):
        ingest.run_import(settings=live, store=store,
                          rule_version="invoice3way/1.5.0")
    failed = store.find_failed_by_key(first["batch"]["batch_key"],
                                      "invoice3way/1.5.0")
    assert failed is not None
    assert store.count_rows(failed["batch_id"])["matches"] == 0
    assert _stored_bytes(store, failed["batch_id"]) == original

    _swap(live, "invoices_dirty.csv", "garbage,not,a,valid,header\n")
    monkeypatch.undo()
    retried = batch_service.retry(store, live, failed["batch_id"])
    assert retried["same_batch"] is True
    assert retried["reused"] is False
    assert store.get_batch(failed["batch_id"])["status"] == "done"
    assert store.load_totals(failed["batch_id"])["records_read"] == "48"
    assert _stored_bytes(store, failed["batch_id"]) == original


def test_retry_rejects_done_batch(settings, store, run):
    """只有失败/中断的批次才谈得上重试；已完成批次走重算。"""
    with pytest.raises(errors.Conflict) as excinfo:
        batch_service.retry(store, settings, run["batch"]["batch_id"])
    assert excinfo.value.code == "E_BATCH_NOT_RETRYABLE"


def test_missing_inputs_rejected_without_backfill(settings, store, tmp_path):
    """缺原件的旧批次明确 422 拒绝重算，不从现有文件补造。"""
    live = _pristine(settings, tmp_path)
    first = ingest.run_import(settings=live, store=store)
    batch_id = first["batch"]["batch_id"]
    with store.connect() as conn:
        conn.execute("DELETE FROM batch_inputs WHERE batch_id=?", (batch_id,))

    with pytest.raises(errors.Unsupported) as excinfo:
        batch_service.rerun(store, live, batch_id)
    assert excinfo.value.code == "E_INPUT_MISSING"
    assert excinfo.value.status_code == 422
    assert store.count_rows(batch_id)["matches"] == 16  # 原结果没被动过

    plan = resume.recovery_plan(store, live, batch_id)
    assert plan["has_inputs"] is False
    assert "拒绝重算" in plan["note"]


def test_concurrent_claim_writes_once(settings, store):
    """并发领取同一批次键：只有一方拿到执行权，落账只发生一次。"""
    key = "K" * 64
    token_a, token_b = "token-a", "token-b"
    now = "2026-11-15T00:00:00+00:00"
    inputs = [{"name": "orders_dirty.csv", "doc_type": "po",
               "bytes": b"a,b\n", "sha256": "0" * 64}]
    first = store.claim_run(batch_key=key, rule_version="v1", eval_date="2026-11-15",
                            candidate_id="B-A", preferred_id=None, run_id="R-A",
                            source="orders_dirty.csv", created_at=now,
                            input_digest="d", inputs=inputs, claim_token=token_a,
                            now=now, stale_before="2026-11-14T23:45:00+00:00")
    second = store.claim_run(batch_key=key, rule_version="v1", eval_date="2026-11-15",
                             candidate_id="B-B", preferred_id=None, run_id="R-B",
                             source="orders_dirty.csv", created_at=now,
                             input_digest="d", inputs=inputs, claim_token=token_b,
                             now=now, stale_before="2026-11-14T23:45:00+00:00")
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert second["reason"] == "running"
    assert len([b for b in store.list_batches() if b["batch_key"] == key]) == 1

    # 第二次写入被拒：执行权已失效
    with pytest.raises(errors.Conflict) as excinfo:
        store.save_run(batch={"batch_id": "B-A", "batch_key": key, "run_id": "R-A",
                              "source": "x", "status": "done",
                              "rule_version": "v1", "created_at": now,
                              "finished_at": now, "input_digest": "d",
                              "params": {}, "note": ""},
                       files=[], po_lines=[], grn_lines=[], inv_lines=[],
                       quarantine=[], matches=[], adjustments=[], stages=[],
                       totals={}, manifest={}, claim_token=token_b)
    assert excinfo.value.code == "E_CLAIM_LOST"


def test_parallel_imports_write_one_batch(live_settings, tmp_path):
    """两个线程同时导入同一份输入：只落一次账，另一方要么复用要么被 409。"""
    store = Store(live_settings.db_path)
    results: list[dict] = []
    errors_seen: list[str] = []

    def worker():
        try:
            results.append(ingest.run_import(settings=live_settings, store=store))
        except errors.Conflict as exc:
            errors_seen.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(store.list_batches()) == 1
    batch_id = store.list_batches()[0]["batch_id"]
    assert store.count_rows(batch_id)["matches"] == 16
    for item in results:
        assert item["batch"]["batch_id"] == batch_id
    for code in errors_seen:
        assert code == "E_BATCH_RUNNING"


def test_claim_after_done_returns_reused(settings, store, run):
    """已完成同键：再次领取直接返回复用，不新建批次。"""
    saved = store.find_done_by_key(run["batch"]["batch_key"])
    assert saved is not None
    again = store.claim_run(
        batch_key=run["batch"]["batch_key"], rule_version=run["batch"]["rule_version"],
        eval_date=settings.eval_date.isoformat(), candidate_id="B-X",
        preferred_id=None, run_id="R-X", source="x", created_at="2026-11-15T00:00:00+00:00",
        input_digest=run["batch"]["input_digest"], inputs=[], claim_token="t",
        now="2026-11-15T00:00:00+00:00", stale_before="2026-11-14T23:45:00+00:00")
    assert again["claimed"] is False
    assert again["reason"] == "done"
    assert again["batch"]["batch_id"] == run["batch"]["batch_id"]


def test_interrupted_run_is_invisible_and_recoverable(settings, store, tmp_path,
                                                      monkeypatch):
    """入库前中断：结果不可见；租约过期后可按留存原件接续，批次号不变。"""
    live = _pristine(settings, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("写库前中断")

    from app.modules.m1_ingest import service as ingest_mod
    monkeypatch.setattr(ingest_mod, "parse_bytes", boom)
    with pytest.raises(RuntimeError):
        ingest.run_import(settings=live, store=store)
    failed = store.list_batches()[0]
    assert failed["status"] == "failed"
    assert store.latest_batch() is None
    monkeypatch.undo()

    # 模拟进程中断：状态 running、租约早已过期
    with store.connect() as conn:
        conn.execute("UPDATE batches SET status='running', claimed_at=?, claim_token=? "
                     "WHERE batch_id=?",
                     ("2026-01-01T00:00:00+00:00", "dead-token", failed["batch_id"]))
    plan = resume.recovery_plan(store, live, failed["batch_id"])
    assert plan["resumable"] is True
    assert store.latest_batch() is None

    recovered = ingest.run_import(settings=live, store=store)
    assert recovered["reused"] is False
    assert recovered["batch"]["batch_id"] == failed["batch_id"]
    assert store.get_batch(failed["batch_id"])["status"] == "done"
    assert store.count_rows(failed["batch_id"])["matches"] == 16


def test_history_flags_replayability(settings, store, run):
    """批次列表标出原件留存与评估日，缺原件的批次不可重算。"""
    batch_id = run["batch"]["batch_id"]
    with store.connect() as conn:
        conn.execute("DELETE FROM batch_inputs WHERE batch_id=?", (batch_id,))
    rows = history.build(store)
    assert rows[0]["replayable"] is False
    assert rows[0]["input_count"] == 0
    assert rows[0]["eval_date"] == settings.eval_date.isoformat()


def test_batch_page_shows_input_basis(live_client):
    """批次页面能核对输入依据：文件、摘要、评估日与可重算标记。"""
    page = live_client.get("/batches")
    assert page.status_code == 200
    text = page.text
    assert "输入依据" in text
    assert "orders_dirty.csv" in text
    assert "2026-11-15" in text
    assert "可按住原件重算" in text
    detail = live_client.get(f"/api/batches/{_only_batch(live_client)}").json()
    assert detail["input_check"]["retained"] is True
    assert detail["input_check"]["digest_match"] is True
    assert len(detail["inputs"]) == 3


def test_rerun_api_after_source_replaced(live_client, live_settings):
    """接口层：源表被替换后重算仍按原件复用同一批次。"""
    batch_id = _only_batch(live_client)
    _swap(live_settings, "orders_dirty.csv", "po_no\n")
    response = live_client.post(f"/api/batches/{batch_id}/rerun", json={})
    assert response.status_code == 200
    assert response.json()["same_batch"] is True


def _only_batch(client) -> str:
    rows = client.get("/api/batches").json()["rows"]
    return rows[0]["batch_id"]
