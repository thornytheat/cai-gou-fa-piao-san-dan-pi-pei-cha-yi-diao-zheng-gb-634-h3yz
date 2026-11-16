"""M5 批次：幂等重跑、陈旧拒绝、批次键、清单、历史、续跑、水位。"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from app import config, errors, manifest
from app.modules.m1_ingest import service as ingest
from app.modules.m5_batches import history, resume, service as batch_service, state


def test_idempotent_rerun_adds_no_rows(settings, store):
    """不变量：同一输入重跑两次，第二次直接复用批次，行数一个不增。"""
    first = ingest.run_import(settings=settings, store=store)
    batch_id = first["batch"]["batch_id"]
    before = store.count_rows(batch_id)
    second = ingest.run_import(settings=settings, store=store)
    after = store.count_rows(batch_id)
    assert second["reused"] is True
    assert second["batch"]["batch_id"] == batch_id
    assert after == before
    assert after["matches"] == 16
    assert after["adjustments"] == 3


def test_stale_commit_rejected(settings, store):
    """TRAP-26：已有更新版本的批次后，旧版本提交必须 409，不能覆盖。"""
    new_run = ingest.run_import(settings=settings, store=store,
                               rule_version="invoice3way/1.3.0")
    assert new_run["batch"]["rule_version"] == "invoice3way/1.3.0"
    with pytest.raises(errors.Conflict) as excinfo:
        ingest.run_import(settings=settings, store=store,
                          rule_version="invoice3way/1.0.0")
    assert excinfo.value.code == "E_STALE_BATCH"
    assert excinfo.value.status_code == 409
    stayed = store.get_batch(new_run["batch"]["batch_id"])
    assert stayed["rule_version"] == "invoice3way/1.3.0"


def test_batch_key_stable_for_same_inputs(settings, store, run, sample_dir):
    """批次键只由输入摘要与评估日决定，换规则版本会落到同一个键上。"""
    wanted = {"orders_dirty.csv", "receipts_gb18030.csv", "invoices_dirty.csv"}
    digests = sorted((entry["name"], manifest.digest_file(sample_dir / entry["name"]))
                     for entry in config.SAMPLE_FILES if entry["name"] in wanted)
    digest = manifest.digest_parts([f"{n}:{d}" for n, d in digests])
    expected = state.batch_key(digest, settings.eval_date)
    assert run["batch"]["batch_key"] == expected
    assert state.batch_key(digest, settings.eval_date) == expected


def test_manifest_records_run_id_and_sha256(run, store):
    """运行清单可复算：run_id、输入 sha256、参数、阶段计数都在。"""
    manifest = store.load_manifest(run["batch"]["batch_id"])
    assert manifest["batch_key"] == run["batch"]["batch_key"]
    assert manifest["counters"]["records_read"] == "48"
    assert len(manifest["inputs"]) == 3
    assert all(len(item["sha256"]) == 64 for item in manifest["inputs"])
    assert manifest["params"]["rule_version"] == "invoice3way/1.2.0"
    assert [stage["stage"] for stage in manifest["stages"]][0] == "ingest"
    assert manifest["stages"][-1]["stage"] == "export"


def test_history_lists_latest_first(settings, store, run):
    """历史按创建时间倒序，最新批次在第一行。"""
    ingest.run_import(settings=settings, store=store, rule_version="invoice3way/1.3.0")
    rows = history.build(store)
    assert len(rows) == 2
    assert rows[0]["rule_version"] == "invoice3way/1.3.0"
    assert rows[0]["records"] == 48
    assert rows[0]["adjust_total"] == "241.28"
    assert rows[0]["balanced"] is True


def test_resume_plan_reports_done_stages(settings, store, run):
    """已完成批次的续跑计划应说明全部阶段已做完，不再重跑。"""
    plan = resume.resume_plan(store, run["batch"]["batch_id"])
    assert plan["status"] == "done"
    assert plan["remaining_stages"] == []
    assert plan["resumable"] is False
    assert plan["done_stages"][0] == "ingest"


def test_new_rule_version_creates_separate_batch(settings, store, run):
    """同输入换更新版本 → 新批次，旧批次数据保留不动。"""
    newer = ingest.run_import(settings=settings, store=store,
                             rule_version="invoice3way/1.4.0")
    assert newer["reused"] is False
    assert newer["batch"]["batch_id"] != run["batch"]["batch_id"]
    assert len(store.list_batches()) == 2
    old = store.load_totals(run["batch"]["batch_id"])
    assert old["records_read"] == "48"


def test_watermark_and_rerun_api(client_with_run):
    """水位指向最新批次；重跑接口返回同一个 batch_id（幂等）。"""
    payload = client_with_run.get("/api/batches").json()
    watermark = payload["summary"]["watermark"]
    assert watermark["batch_id"]
    assert watermark["records"] == 48
    batch_id = watermark["batch_id"]
    rerun = client_with_run.post(f"/api/batches/{batch_id}/rerun", json={})
    assert rerun.status_code == 200
    body = rerun.json()
    assert body["same_batch"] is True
    assert body["reused"] is True
    stale = client_with_run.post(f"/api/batches/{batch_id}/rerun",
                                 json={"rule_version": "invoice3way/0.9.0"})
    assert stale.status_code == 409
    assert stale.json()["error"] == "E_STALE_BATCH"
    missing = client_with_run.get("/api/batches/NOSUCHBATCH")
    assert missing.status_code == 404


def test_history_summary_accumulates(settings, store, run):
    """历史汇总的调整额是各批次相加（Decimal）。"""
    ingest.run_import(settings=settings, store=store, rule_version="invoice3way/1.3.0")
    rows = history.build(store)
    summary = history.summary(rows)
    assert summary["batches"] == 2
    assert str(summary["adjust_total"]) == "482.56"
    assert summary["versions"] == ["invoice3way/1.2.0", "invoice3way/1.3.0"]
    assert batch_service.stage_flow()[0]["stage"] == "ingest"


def test_different_eval_date_creates_new_batch(settings, store):
    """边界：输入不变、评估日不同 → 新批次；原评估日再跑仍复用旧批次。"""
    first = ingest.run_import(settings=settings, store=store)
    other = dataclasses.replace(settings, eval_date=dt.date(2026, 11, 16))
    second = ingest.run_import(settings=other, store=store)
    assert second["reused"] is False
    assert second["batch"]["batch_key"] != first["batch"]["batch_key"]
    assert len(store.list_batches()) == 2
    again = ingest.run_import(settings=settings, store=store)
    assert again["reused"] is True
    assert again["batch"]["batch_id"] == first["batch"]["batch_id"]
