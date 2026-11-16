"""M1 编排：采信原件 → 留存 → 解码 → 解析 → 列映射 → 逐行归一并诊断 → 跑完整条流水线。

一份文件里的一行只有两种结局：采纳，或带着稳定原因码进隔离区。
文件级问题（编码回退、BOM、重复表头、末行截断）单独记在文件诊断上。
流水线每阶段都产出 in/out/隔离三个数，页面上按阶段看得到行去了哪里。

输入与批次绑定：
- 新导入读当天文件字节，先算出摘要再领取批次，原件随批次落库；
- 重算只读批次留存的原件与原评估日，源表被替换或移走都不影响结果；
- 同一批次键并发领取只有一方拿到执行权，落账只发生一次。
"""
from __future__ import annotations

import datetime as dt
import time
import uuid
from pathlib import Path

from ... import config, errors, manifest, reasons
from ..m2_amount import contract as amount_contract
from ..m2_amount import service as amount_service
from ..m3_quality import metrics as quality_metrics
from ..m3_quality import quarantine as quarantine_mod
from ..m3_quality import rules as quality_rules
from ..m4_match import compare as match_compare
from ..m4_match import diffattr, reconcile
from ..m5_batches import resume as batch_resume
from ..m5_batches import state as batch_state
from ..m6_export import adj as adjust_mod
from . import decode, reader, sniff

ROW_FIELDS = {
    "po": ("po_no", "line_no"),
    "grn": ("po_no", "line_no"),
    "inv": ("po_no", "line_no"),
}


def sample_list(settings) -> list[dict]:
    rows = []
    base = Path(settings.sample_dir)
    for entry in config.SAMPLE_FILES:
        path = base / entry["name"]
        exists = path.exists()
        rows.append({
            "name": entry["name"],
            "doc_type": entry["doc_type"],
            "doc_label": config.DOC_LABELS[entry["doc_type"]],
            "note": entry["note"],
            "exists": exists,
            "bytes": path.stat().st_size if exists else 0,
            "sha256": manifest.digest_file(path)[:16] if exists else "",
        })
    return rows


def default_names() -> list[str]:
    return [entry["name"] for entry in config.SAMPLE_FILES if "dirty" in entry["name"]
            or "gb18030" in entry["name"]]


def resolve_files(settings, names: list[str] | None) -> list[dict]:
    base = Path(settings.sample_dir)
    known = {entry["name"]: entry for entry in config.SAMPLE_FILES}
    chosen = names or default_names()
    if not chosen:
        raise errors.BadRequest("没有可导入的文件", code="E_UNKNOWN_FILE")
    out = []
    for name in chosen:
        entry = known.get(name)
        path = base / name
        if entry is None or not path.exists():
            raise errors.BadRequest(f"文件不在样例清单内：{name}", code="E_UNKNOWN_FILE",
                                    detail={"known": sorted(known)})
        out.append({"name": name, "doc_type": entry["doc_type"], "path": str(path),
                    "note": entry["note"]})
    return out


def _enrich(issue: dict) -> dict:
    """把原因码的级别/结论/建议补到格级问题上，隔离判定只认 level。"""
    reason = reasons.get(issue["code"])
    enriched = dict(issue)
    enriched.setdefault("level", reason.level)
    enriched.setdefault("title", reason.title)
    enriched.setdefault("hint", reason.hint)
    return enriched


def _row_ref(name: str, record) -> str:
    return f"{name}:L{record.line_no}"


def read_bytes(entry: dict) -> bytes:
    """读采信字节：entry 带 `bytes` 时用留存副本，否则读源文件。"""
    if entry.get("bytes") is not None:
        return entry["bytes"]
    return Path(entry["path"]).read_bytes()


def parse_bytes(entry: dict, raw: bytes) -> dict:
    """对一段字节做解码、解析、归一并给出逐行诊断；不决定结果落到哪个批次。"""
    stats = {
        "name": entry["name"],
        "doc_type": entry["doc_type"],
        "status": "done",
        "encoding": None,
        "declared_charset": config.DEFAULT_CHARSET,
        "bom_stripped": False,
        "bytes": len(raw),
        "sha256": manifest.digest_bytes(raw),
        "record_count": 0,
        "segment_count": 1,
        "accept_count": 0,
        "quarantine_count": 0,
        "code": None,
        "note": entry.get("note", ""),
    }
    rows = {"po": [], "grn": [], "inv": []}
    quarantine: list[dict] = []
    counters = {"records": 0, "blank": 0, "header_repeat": 0}

    if not raw:
        stats.update(status="empty", code="E_EMPTY_FILE")
        quarantine.append(quarantine_mod.make_row(
            code="E_EMPTY_FILE", stage="ingest", doc_type=entry["doc_type"],
            file_name=entry["name"], row_ref="文件", detail="0 字节"))
        return {"stats": stats, "rows": rows, "quarantine": quarantine,
                "counters": counters}

    try:
        decoded = decode.decode_bytes(raw)
    except errors.InvoiceError as exc:
        stats.update(status="failed", code=exc.code, note=str(exc))
        quarantine.append(quarantine_mod.make_row(
            code="E_DECODE_FAILED", stage="ingest", doc_type=entry["doc_type"],
            file_name=entry["name"], row_ref="文件", detail=str(exc)))
        return {"stats": stats, "rows": rows, "quarantine": quarantine,
                "counters": counters}

    stats["encoding"] = decoded.encoding
    stats["declared_charset"] = decoded.declared
    stats["bom_stripped"] = decoded.bom_stripped
    for code in decoded.codes:
        quarantine.append(quarantine_mod.make_row(
            code=code, stage="ingest", doc_type=entry["doc_type"],
            file_name=entry["name"], row_ref="文件",
            detail=f"声明 {decoded.declared} / 实际 {decoded.encoding}"))

    delimiter = sniff.guess_delimiter(decoded.text)
    table = reader.parse_table(decoded.text, delimiter)
    counters["blank"] = table.blank_count
    counters["header_repeat"] = table.header_repeat_count
    stats["segment_count"] = table.segment_count
    stats["record_count"] = table.record_count

    if not table.headers:
        stats.update(status="header_only", code="E_HEADER_ONLY")
        quarantine.append(quarantine_mod.make_row(
            code="E_HEADER_ONLY", stage="ingest", doc_type=entry["doc_type"],
            file_name=entry["name"], row_ref="文件", detail="没有数据行"))
        return {"stats": stats, "rows": rows, "quarantine": quarantine,
                "counters": counters}

    if table.record_count == 0:
        stats.update(status="header_only", code="E_HEADER_ONLY")
        quarantine.append(quarantine_mod.make_row(
            code="E_HEADER_ONLY", stage="ingest", doc_type=entry["doc_type"],
            file_name=entry["name"], row_ref="文件", detail="只有表头，没有数据行"))
        return {"stats": stats, "rows": rows, "quarantine": quarantine,
                "counters": counters}

    normalized = [sniff.normalize_header(cell) for cell in table.headers]
    doc_type = sniff.detect_doc_type(normalized, entry["doc_type"])
    stats["doc_type"] = doc_type
    mapping = amount_contract.match_headers(normalized, doc_type)
    missing = amount_contract.missing_required(doc_type, mapping)
    if missing:
        stats.update(status="failed", code="E_HEADER_UNKNOWN")
        quarantine.append(quarantine_mod.make_row(
            code="E_HEADER_UNKNOWN", stage="ingest", doc_type=doc_type,
            file_name=entry["name"], row_ref="表头",
            field=",".join(missing), detail="缺必需列"))
        return {"stats": stats, "rows": rows, "quarantine": quarantine,
                "counters": counters}

    if table.header_repeat_count:
        quarantine.append(quarantine_mod.make_row(
            code="E_HEADER_REPEAT", stage="ingest", doc_type=doc_type,
            file_name=entry["name"], row_ref="文件",
            detail=f"重复表头 {table.header_repeat_count} 次，共 {table.segment_count} 段"))

    stats["code"] = decoded.codes[0] if decoded.codes else None
    accepted = 0
    rejected = 0
    for index, record in enumerate(table.records):
        is_last = index == len(table.records) - 1
        if table.truncated_tail and is_last:
            rejected += 1
            quarantine.append(quarantine_mod.make_row(
                code="E_TRUNCATED_TAIL", stage="ingest", doc_type=doc_type,
                file_name=entry["name"], row_ref=_row_ref(entry["name"], record),
                field="末行", raw_value="|".join(record.cells),
                detail=f"末行 {len(record.cells)} 列 / 表头 {table.width} 列"))
            continue
        if len(record.cells) != table.width:
            rejected += 1
            quarantine.append(quarantine_mod.make_row(
                code="E_COLUMN_COUNT", stage="ingest", doc_type=doc_type,
                file_name=entry["name"], row_ref=_row_ref(entry["name"], record),
                field="列数", raw_value="|".join(record.cells),
                detail=f"该行 {len(record.cells)} 列 / 表头 {table.width} 列"))
            continue
        row, raw_issues = amount_service.coerce_row(doc_type, mapping, record.cells)
        issues = [_enrich(issue) for issue in raw_issues]
        issues += quality_rules.row_issues(doc_type, row)
        row_ref = _row_ref(entry["name"], record)
        if quality_rules.blocking(issues):
            rejected += 1
            for issue in issues:
                if issue.get("level") != reasons.ERROR:
                    continue
                quarantine.append(quarantine_mod.from_issue(
                    issue=issue, stage="amount", doc_type=doc_type,
                    file_name=entry["name"], row_ref=row_ref))
            continue
        for issue in issues:
            quarantine.append(quarantine_mod.from_issue(
                issue=issue, stage="quality", doc_type=doc_type,
                file_name=entry["name"], row_ref=row_ref))
        row["row_ref"] = row_ref
        row["key"] = match_compare.key_of(row["po_no"], row["line_no"])
        row["doc_type"] = doc_type
        row["file_name"] = entry["name"]
        row["line_no"] = row["line_no"]
        rows[doc_type].append(row)
        accepted += 1

    stats["accept_count"] = accepted
    stats["quarantine_count"] = rejected
    if rejected and stats["status"] == "done":
        stats["status"] = "done_with_quarantine"
    return {"stats": stats, "rows": rows, "quarantine": quarantine,
            "counters": counters}


def read_file(settings, entry: dict) -> dict:
    """对单个文件做采信：源文件或留存副本都走同一条解析路径。"""
    return parse_bytes(entry, read_bytes(entry))


def _entry(name: str, doc_type: str, note: str = "", *, path: Path | None = None,
           raw: bytes | None = None, source_path: str | None = None) -> dict:
    entry = {"name": name, "doc_type": doc_type, "note": note}
    if raw is None:
        entry["path"] = str(path)
        entry["bytes"] = Path(path).read_bytes()
    else:
        entry["bytes"] = raw
        if path is not None:
            entry["path"] = str(path)
    entry["sha256"] = manifest.digest_bytes(entry["bytes"])
    entry["source_path"] = source_path or entry.get("path")
    return entry


def resolve_files(settings, names: list[str] | None) -> list[dict]:
    """从样例目录采信当天文件；读一次字节，摘要就以此为准。"""
    base = Path(settings.sample_dir)
    known = {entry["name"]: entry for entry in config.SAMPLE_FILES}
    chosen = names or default_names()
    if not chosen:
        raise errors.BadRequest("没有可导入的文件", code="E_UNKNOWN_FILE")
    out = []
    for name in chosen:
        entry = known.get(name)
        path = base / name
        if entry is None or not path.exists():
            raise errors.BadRequest(f"文件不在样例清单内：{name}", code="E_UNKNOWN_FILE",
                                    detail={"known": sorted(known)})
        out.append(_entry(name, entry["doc_type"], entry["note"], path=path))
    return out


def resolve_saved(store, batch_id: str, *, require: bool = True) -> list[dict]:
    """重算只认留存原件：源表被替换或移走也不影响。缺原件时明确拒绝。"""
    saved = store.load_input_bytes(batch_id)
    if not saved:
        if not require:
            return []
        raise errors.Unsupported(
            f"批次 {batch_id} 没有留存输入原件，拒绝重算；不从现有文件补造",
            code="E_INPUT_MISSING",
            detail={"batch_id": batch_id, "hint": "旧批次只保留结果，不能按当前文件重读"})
    out = []
    for item in saved:
        out.append({"name": item["name"], "doc_type": item["doc_type"],
                    "note": "", "bytes": item["bytes"], "sha256": item["sha256"],
                    "source_path": item.get("source_path"),
                    "stored_at": item.get("stored_at")})
    return out


def _claim_inputs(entries: list[dict]) -> list[dict]:
    """领取时随批次落库的输入：文件名、单据类型、字节与来源指纹。"""
    return [{"name": entry["name"], "doc_type": entry["doc_type"],
             "bytes": entry["bytes"], "sha256": entry["sha256"],
             "source_path": entry.get("source_path")} for entry in entries]


def _prepare(*, entries: list[dict], store, settings, version: str, eval_date,
             source: str, started_at: str, resume_of: str | None,
             preferred_id: str | None, claim_token: str) -> dict:
    """算批次键 → 查复用/陈旧 → 原子领取执行权。"""
    # 指纹与文件顺序无关：同一组文件换顺序仍是同一批
    input_digest = manifest.digest_parts(
        sorted(f"{entry['name']}:{entry['sha256']}" for entry in entries))
    batch_key = batch_state.batch_key(input_digest, eval_date)

    existing = store.find_done_by_key(batch_key, version)
    if existing is not None:
        return {"reused": True, "batch": existing, "batch_key": batch_key,
                "input_digest": input_digest}
    batch_resume.assert_not_stale(store, batch_key=batch_key, rule_version=version)

    claim = store.claim_run(
        batch_key=batch_key, rule_version=version, eval_date=eval_date.isoformat(),
        candidate_id=batch_state.new_batch_id(), preferred_id=preferred_id,
        run_id=manifest.new_run_id(), source=source, created_at=started_at,
        input_digest=input_digest, inputs=_claim_inputs(entries),
        claim_token=claim_token, now=started_at,
        stale_before=batch_state.lease_cutoff(settings), resume_of=resume_of,
        params={"files": [entry["name"] for entry in entries],
                "rule_version": version, "eval_date": eval_date.isoformat()},
        note=f"采信 {len(entries)} 个文件")
    if not claim["claimed"]:
        if claim.get("reason") == "done" and claim.get("batch") is not None:
            # 并发窗口内别人先完成了同一次运行：复用它的结果，不再落账
            return {"reused": True, "batch": claim["batch"], "batch_key": batch_key,
                    "input_digest": input_digest}
        active = claim.get("batch") or {}
        raise errors.Conflict(
            f"批次键 {batch_key[:12]} 正在运行（批次 {active.get('batch_id')}），"
            f"同一输入不重复落账",
            code="E_BATCH_RUNNING",
            detail={"batch_id": active.get("batch_id"), "status": active.get("status")})
    return {"reused": False, "batch_key": batch_key, "input_digest": input_digest,
            "batch_id": claim["batch_id"], "resumed": claim.get("resumed", False),
            "attempt": claim.get("attempt", 1)}


def run_import(*, settings, store, names: list[str] | None = None,
               rule_version: str | None = None, resume_of: str | None = None,
               input_batch_id: str | None = None) -> dict:
    """导入/重跑统一入口。

    - `input_batch_id` 指定时只读该批次留存的原件与评估日（历史重算）；
    - 否则从样例目录采信当天文件（新导入）；
    - `resume_of` 是失败重试：沿用来源批次的原件，不重新读源表。
    """
    started = time.monotonic()
    started_at = _now()
    version = rule_version or settings.rule_version

    if input_batch_id:
        source_batch = store.get_batch(input_batch_id)
        if source_batch is None:
            raise errors.NotFound(f"批次不存在：{input_batch_id}", code="E_BATCH_NOT_FOUND")
        entries = resolve_saved(store, input_batch_id)
        eval_date = batch_state.parse_eval_date(source_batch.get("eval_date")) \
            or settings.eval_date
        source = source_batch.get("source") or ",".join(e["name"] for e in entries)
        preferred_id = None
        resume_of = input_batch_id
    elif resume_of:
        source_batch = store.get_batch(resume_of)
        if source_batch is None:
            raise errors.NotFound(f"批次不存在：{resume_of}", code="E_BATCH_NOT_FOUND")
        entries = resolve_saved(store, resume_of)
        eval_date = batch_state.parse_eval_date(source_batch.get("eval_date")) \
            or settings.eval_date
        source = source_batch.get("source") or ",".join(e["name"] for e in entries)
        preferred_id = resume_of
    else:
        entries = resolve_files(settings, names)
        eval_date = settings.eval_date
        source = ",".join(entry["name"] for entry in entries)
        preferred_id = None

    claim_token = uuid.uuid4().hex
    prepared = _prepare(entries=entries, store=store, settings=settings, version=version,
                        eval_date=eval_date, source=source, started_at=started_at,
                        resume_of=resume_of, preferred_id=preferred_id,
                        claim_token=claim_token)
    if prepared["reused"]:
        return {"batch": prepared["batch"], "reused": True,
                "stages": store.load_stages(prepared["batch"]["batch_id"]),
                "counters": store.load_totals(prepared["batch"]["batch_id"])}

    batch_id = prepared["batch_id"]
    run_id = manifest.new_run_id()
    input_digest = prepared["input_digest"]
    batch_key = prepared["batch_key"]
    try:
        return _execute(settings=settings, store=store, entries=entries,
                        batch_id=batch_id, run_id=run_id, batch_key=batch_key,
                        input_digest=input_digest, version=version,
                        eval_date=eval_date, source=source, started_at=started_at,
                        resume_of=resume_of, claim_token=claim_token,
                        started=started, replayed=bool(prepared.get("resumed")))
    except Exception as exc:
        store.fail_run(batch_id, claim_token, _error_text(exc), _now())
        raise


def _execute(*, settings, store, entries, batch_id, run_id, batch_key, input_digest,
             version, eval_date, source, started_at, resume_of, claim_token,
             started, replayed) -> dict:
    stage_rows = []
    files = []
    all_rows = {"po": [], "grn": [], "inv": []}
    quarantine_rows: list[dict] = []
    counters = {"records_read": 0, "accepted": 0, "blank": 0, "header_repeat": 0}
    ingest_in = 0
    ingest_out = 0
    ingest_bad = 0
    for entry in entries:
        result = parse_bytes(entry, entry["bytes"])
        result["stats"]["source_path"] = entry.get("source_path")
        files.append(result["stats"])
        for doc_type in config.DOC_TYPES:
            all_rows[doc_type].extend(result["rows"][doc_type])
        quarantine_rows.extend(result["quarantine"])
        counters["records_read"] += result["stats"]["record_count"]
        counters["blank"] += result["counters"]["blank"]
        counters["header_repeat"] += result["counters"]["header_repeat"]
        ingest_in += result["stats"]["record_count"]
        ingest_out += result["stats"]["accept_count"]
        ingest_bad += result["stats"]["quarantine_count"]
    counters["accepted"] = ingest_out
    stage_rows.append(_stage("ingest", ingest_in, ingest_out, ingest_bad,
                             f"{len(files)} 个文件"))

    amount_rows = sum(len(all_rows[k]) for k in config.DOC_TYPES)
    stage_rows.append(_stage("amount", amount_rows, amount_rows, 0,
                             "金额归元、税率归一、数量 4 位"))

    conflicts, deduped = quality_rules.dup_invoice_conflicts(all_rows["inv"])
    blocked_refs = {item["row_ref"] for item in conflicts} | {item["row_ref"]
                                                             for item in deduped}
    if blocked_refs:
        all_rows["inv"] = [row for row in all_rows["inv"]
                           if row["row_ref"] not in blocked_refs]
    for item in conflicts:
        quarantine_rows.append(quarantine_mod.make_row(
            code="E_DUP_KEY_CONFLICT", stage="quality", doc_type="inv",
            file_name="invoices_dirty.csv", row_ref=item["row_ref"],
            field="invoice_no", raw_value=item["invoice_no"], detail=item["detail"]))
    for item in deduped:
        quarantine_rows.append(quarantine_mod.make_row(
            code="E_DUP_KEY_SAME", stage="quality", doc_type="inv",
            file_name="invoices_dirty.csv", row_ref=item["row_ref"],
            field="invoice_no", raw_value=item["invoice_no"],
            detail="同一发票号重复且金额一致，保留首行"))
    by_name = {item["name"]: item for item in files}
    for item in conflicts:
        _drop(by_name, item["row_ref"])
    for item in deduped:
        _drop(by_name, item["row_ref"], level=reasons.INFO)
    counters["accepted"] = sum(len(all_rows[k]) for k in config.DOC_TYPES)
    counters["po_accepted"] = len(all_rows["po"])
    counters["grn_accepted"] = len(all_rows["grn"])
    counters["inv_accepted"] = len(all_rows["inv"])
    stage_rows.append(_stage("quality", amount_rows, counters["accepted"],
                             len(blocked_refs) + len(deduped),
                             f"发票号冲突 {len(conflicts)} 行 / PO {counters['po_accepted']}"
                             f" GRN {counters['grn_accepted']}"
                             f" INV {counters['inv_accepted']}"))

    groups = match_compare.build_groups(all_rows["po"], all_rows["grn"], all_rows["inv"])
    matches = match_compare.decide_all(groups)
    verdict_counts = match_compare.counts(matches)
    stage_rows.append(_stage("match", len(groups), len(groups), 0,
                             f"匹配 {verdict_counts['matched']} / 差异 "
                             f"{verdict_counts['diff']} / 缺单 {verdict_counts['missing']}"))

    accounts = reconcile.accounts(files=files, counters=counters, po_rows=all_rows["po"],
                                  inv_rows=all_rows["inv"], matches=matches,
                                  quarantine_rows=quarantine_rows)
    stage_rows.append(_stage("reconcile", 4, sum(1 for a in accounts["accounts"]
                                                 if a["balanced"]), 0,
                             "四账" + ("平" if accounts["balanced"] else "不平")))

    adjustments, adj_summary = adjust_mod.build_sheet(matches, all_rows["inv"],
                                                      batch_id=batch_id)
    stage_rows.append(_stage("export", len(diffattr.diff_lines(matches)),
                             len(adjustments), 0,
                             f"调整额 {adj_summary['total']} {config.MONEY_UNIT}"))

    totals = {
        "records_read": str(counters["records_read"]),
        "accepted": str(counters["accepted"]),
        "blank": str(counters["blank"]),
        "header_repeat": str(counters["header_repeat"]),
        "grn_accepted": str(counters["grn_accepted"]),
        "po_accepted": str(counters["po_accepted"]),
        "inv_accepted": str(counters["inv_accepted"]),
        "quarantine_rows": str(len(quarantine_rows)),
        "matched": str(verdict_counts["matched"]),
        "diff": str(verdict_counts["diff"]),
        "missing": str(verdict_counts["missing"]),
        "judged": str(len(matches)),
        "invoice_total": accounts["invoice_total"],
        "matched_total": accounts["totals"]["matched_total"],
        "diff_total": accounts["totals"]["diff_total"],
        "missing_total": accounts["totals"]["missing_total"],
        "adjust_total": adj_summary["total"],
        "allocated_total": adj_summary["allocated"],
        "balanced": "1" if accounts["balanced"] else "0",
    }

    elapsed_ms = int((time.monotonic() - started) * 1000)
    finished_at = _now()
    names = [entry["name"] for entry in entries]
    manifest_payload = manifest.build(
        run_id=run_id, batch_id=batch_id, batch_key=batch_key, settings=settings,
        files=files, input_digest=input_digest,
        params={"files": names, "rule_version": version,
                "eval_date": eval_date.isoformat()},
        started_at=started_at, finished_at=finished_at, elapsed_ms=elapsed_ms,
        stages=stage_rows, counters=totals, totals=totals)
    manifest_payload["inputs"] = [
        {**item, "source_path": entry.get("source_path")}
        for item, entry in zip(manifest_payload["inputs"], entries)]
    manifest_payload["eval_date"] = eval_date.isoformat()
    manifest_payload["rule_version"] = version
    manifest_payload["replayed"] = replayed
    batch = {
        "batch_id": batch_id,
        "batch_key": batch_key,
        "run_id": run_id,
        "source": source,
        "status": "done",
        "rule_version": version,
        "created_at": started_at,
        "finished_at": finished_at,
        "input_digest": input_digest,
        "resume_of": resume_of,
        "eval_date": eval_date.isoformat(),
        "params": {"files": names, "rule_version": version,
                   "eval_date": eval_date.isoformat()},
        "note": f"{counters['accepted']} 采纳 / {len(quarantine_rows)} 隔离",
    }
    store.save_run(batch=batch, files=files, po_lines=all_rows["po"],
                   grn_lines=all_rows["grn"], inv_lines=all_rows["inv"],
                   quarantine=quarantine_rows, matches=matches,
                   adjustments=adjustments, stages=stage_rows, totals=totals,
                   manifest=manifest_payload, claim_token=claim_token)
    return {"batch": batch, "reused": False, "stages": stage_rows,
            "counters": totals, "quality": quality_metrics.summary(files, quarantine_rows),
            "reconcile": accounts, "adjust": adj_summary, "replayed": replayed}


def _error_text(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    text = getattr(exc, "message", None) or str(exc)
    return f"{code}: {text}" if code else text


def _drop(by_name: dict, row_ref: str, level: str = reasons.ERROR) -> None:
    """行被后续阶段剔除时，把文件诊断上的采纳/隔离数同步减一。"""
    name = row_ref.split(":")[0]
    stats = by_name.get(name)
    if stats is None:
        return
    stats["accept_count"] = max(0, int(stats.get("accept_count") or 0) - 1)
    if level == reasons.ERROR:
        stats["quarantine_count"] = int(stats.get("quarantine_count") or 0) + 1


def _stage(stage: str, in_count: int, out_count: int, quarantined: int,
           note: str) -> dict:
    return {"stage": stage, "in_count": in_count, "out_count": out_count,
            "quarantine_count": quarantined,
            "status": "ok" if out_count else "empty",
            "elapsed_ms": 0, "note": note}


def _now() -> str:
    from ... import config as cfg
    import datetime as dt
    return dt.datetime.now(cfg.UTC).isoformat(timespec="seconds")


def batch_detail(store, batch_id: str) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise errors.NotFound(f"批次不存在：{batch_id}", code="E_BATCH_NOT_FOUND")
    files = store.load_files(batch_id)
    quarantine_rows = store.load_quarantine(batch_id)
    inputs = store.list_inputs(batch_id)
    return {
        "batch": batch,
        "files": files,
        "stages": store.load_stages(batch_id),
        "quarantine": quarantine_rows,
        "quarantine_distribution": quarantine_mod.distribution(quarantine_rows),
        "quality": quality_metrics.summary(files, quarantine_rows),
        "totals": store.load_totals(batch_id),
        "manifest": store.load_manifest(batch_id),
        "inputs": inputs,
        "input_check": input_check(batch, inputs),
    }


def input_check(batch: dict, inputs: list[dict]) -> dict:
    """批次页核对输入依据：留存有原件、摘要与清单一致、评估日锁定。"""
    digest = manifest.digest_parts(
        sorted(f"{item['name']}:{item['sha256']}" for item in inputs))
    declared = (batch.get("input_digest") or "")
    return {
        "count": len(inputs),
        "total_bytes": sum(int(item.get("bytes") or 0) for item in inputs),
        "retained": bool(inputs),
        "digest": digest,
        "stored_digest": declared,
        "digest_match": bool(inputs) and digest == declared,
        "eval_date": batch.get("eval_date") or "",
        "source": batch.get("source") or "",
        "replayable": bool(inputs),
    }
