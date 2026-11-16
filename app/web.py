"""模板渲染与页面路由：过滤器、导航、当前批次上下文。

页面只读 store 与各模块 service，不在这里算业务；数字格式化统一在本文件，
避免每页各写一套。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import config, reasons

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

NAV = (
    ("/", "导入", "ingest"),
    ("/matches", "三单对照", "matches"),
    ("/diffs", "差异明细", "diffs"),
    ("/adjust", "调整单", "adjust"),
    ("/batches", "批次", "batches"),
    ("/export", "导出", "export"),
    ("/audit", "复核台账", "audit"),
)

STATUS_LABELS = {
    "done": "完成",
    "done_with_quarantine": "完成·有隔离",
    "empty": "空文件",
    "header_only": "仅表头",
    "failed": "失败",
    "running": "运行中",
    "ready": "就绪",
}

router = APIRouter()


@dataclass
class Ctx:
    settings: config.Settings
    store: object

    def current_batch(self, batch_id: str | None = None) -> dict | None:
        if batch_id:
            return self.store.get_batch(batch_id)
        return self.store.latest_batch()


def get_ctx(request: Request) -> Ctx:
    return request.app.state.ctx


def setup(app) -> None:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    env.filters["money"] = fmt_money
    env.filters["num"] = fmt_num
    env.filters["qty"] = fmt_qty
    env.filters["pct"] = fmt_pct
    env.filters["stamp"] = fmt_stamp
    env.filters["localdt"] = fmt_local_dt
    env.filters["verdict"] = lambda v: config.VERDICT_LABELS.get(v, v or "—")
    env.filters["diff_label"] = lambda v: config.DIFF_LABELS.get(v, v or "—")
    env.filters["doc"] = lambda v: config.DOC_LABELS.get(v, v or "—")
    env.filters["stage"] = lambda v: config.STAGE_LABELS.get(v, v or "—")
    env.filters["level"] = lambda v: reasons.LEVEL_LABELS.get(v, v or "—")
    env.filters["status"] = lambda v: STATUS_LABELS.get(v, v or "—")
    env.globals.update(
        nav=NAV,
        stage_flow=config.STAGE_FLOW,
        stage_labels=config.STAGE_LABELS,
        stages=config.STAGES,
        verdict_labels=config.VERDICT_LABELS,
        diff_labels=config.DIFF_LABELS,
        diff_ranks=config.DIFF_RANK,
        doc_labels=config.DOC_LABELS,
        amount_tolerance=str(config.AMOUNT_TOLERANCE),
        money_unit=config.MONEY_UNIT,
        reason_catalog=reasons.catalog(),
        reasons=reasons.REASONS,
        generated_at=now_local().strftime("%Y-%m-%d %H:%M:%S"),
        timezone_label="Asia/Shanghai (+08:00)",
    )
    app.state.templates = templates


def render(request: Request, name: str, **context):
    ctx: Ctx = request.app.state.ctx
    batch = context.pop("batch", None)
    if batch is None:
        batch = ctx.current_batch(context.get("batch_id"))
    base = {
        "request": request,
        "ctx": ctx,
        "settings": ctx.settings,
        "batch": batch,
        "rule_version": ctx.settings.rule_version,
        "eval_date": ctx.settings.eval_date.isoformat(),
        "generated_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone_label": "Asia/Shanghai (+08:00)",
        "active": context.pop("active", ""),
    }
    base.update(context)
    return request.app.state.templates.TemplateResponse(
        request=request, name=name, context=base)


def now_local() -> dt.datetime:
    return dt.datetime.now(config.CHINA_TZ)


def to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def fmt_money(value) -> str:
    dec = to_decimal(value)
    if dec is None:
        return "—"
    return f"{dec.quantize(config.AMOUNT_QUANT):,}"


def fmt_num(value) -> str:
    dec = to_decimal(value)
    if dec is None:
        return "—"
    return f"{dec:,}"


def fmt_qty(value) -> str:
    dec = to_decimal(value)
    if dec is None:
        return "—"
    text = format(dec.normalize(), "f")
    return text


def fmt_pct(value) -> str:
    dec = to_decimal(value)
    if dec is None:
        return "—"
    return f"{(dec * Decimal('100')).quantize(Decimal('0.01'))}%"


def fmt_local_dt(value) -> str:
    """清单里的 UTC 时间按 +08:00 展示，库里仍存 UTC。"""
    if not value:
        return "—"
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.UTC)
    return parsed.astimezone(config.CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def fmt_stamp(value) -> str:
    if not value:
        return "—"
    text = str(value).replace("T", " ")
    return text[:19]


def page_context(store, batch: dict | None) -> dict:
    """所有页面共用的批次抬头数字：记录数、差异数、调整额。"""
    if batch is None:
        return {"totals": {}, "head": {"records": 0, "diff": 0, "adjust": "0.00",
                                       "quarantined": 0}}
    totals = store.load_totals(batch["batch_id"])
    head = {
        "records": int(totals.get("records_read") or 0),
        "diff": int(totals.get("diff") or 0),
        "adjust": totals.get("adjust_total") or "0.00",
        "quarantined": int(totals.get("quarantine_rows") or 0),
        "matched": int(totals.get("matched") or 0),
        "missing": int(totals.get("missing") or 0),
        "balanced": totals.get("balanced") == "1",
    }
    return {"totals": totals, "head": head}


def _guard(store, batch):
    return {"batch": batch, **page_context(store, batch)}


@router.get("/", response_class=HTMLResponse)
def page_index(request: Request, batch_id: str | None = None):
    from .modules.m1_ingest import service as ingest_service
    from .modules.m3_quality import service as quality_service

    ctx = get_ctx(request)
    batch = ctx.current_batch(batch_id)
    data = {"active": "ingest", "batch_id": batch_id,
            "samples": ingest_service.sample_list(ctx.settings),
            "defaults": ingest_service.default_names()}
    if batch is not None:
        detail = ingest_service.batch_detail(ctx.store, batch["batch_id"])
        quality = quality_service.payload(ctx.store, batch["batch_id"])
        data.update({
            "files": detail["files"],
            "stages": detail["stages"],
            "quarantine_rows": detail["quarantine"][:24],
            "distribution": detail["quarantine_distribution"],
            "quality": detail["quality"],
            "manifest": detail["manifest"],
        })
    else:
        data.update({"files": [], "stages": [], "quarantine_rows": [],
                     "distribution": [], "quality": None, "manifest": None})
    data.update(_guard(ctx.store, batch))
    return render(request, "index.html", **data)


@router.get("/matches", response_class=HTMLResponse)
def page_matches(request: Request, batch_id: str | None = None,
                 verdict: str | None = None):
    from .modules.m4_match import service as match_service

    ctx = get_ctx(request)
    batch = ctx.current_batch(batch_id)
    data = {"active": "matches", "batch_id": batch_id, "verdict": verdict or "",
            "rows": [], "counts": {key: 0 for key in config.VERDICTS},
            "totals": {}, "po_total": 0, "grn_total": 0, "inv_total": 0}
    if batch is not None:
        payload = match_service.trios(ctx.store, batch["batch_id"])
        rows = payload["rows"]
        if verdict in config.VERDICTS:
            rows = [row for row in rows if row["verdict"] == verdict]
        payload["rows"] = rows
        data.update(payload)
    data.update(_guard(ctx.store, batch))
    return render(request, "matches.html", **data)


@router.get("/diffs", response_class=HTMLResponse)
def page_diffs(request: Request, batch_id: str | None = None, kind: str | None = None):
    from .modules.m4_match import service as match_service

    ctx = get_ctx(request)
    batch = ctx.current_batch(batch_id)
    data = {"active": "diffs", "batch_id": batch_id, "kind": kind or "",
            "rows": [], "missing": [], "by_kind": [], "totals": {}, "labels": {}}
    if batch is not None:
        payload = match_service.diffs_payload(ctx.store, batch["batch_id"], kind=kind)
        payload.update({"kind": kind or ""})
        data.update(payload)
    data.update(_guard(ctx.store, batch))
    return render(request, "diffs.html", **data)


@router.get("/adjust", response_class=HTMLResponse)
def page_adjust(request: Request, batch_id: str | None = None):
    from .modules.m6_export import service as export_service

    ctx = get_ctx(request)
    batch = ctx.current_batch(batch_id)
    data = {"active": "adjust", "batch_id": batch_id, "adjustments": [],
            "adjust_summary": {}, "diff_lines": [], "by_kind": [], "totals": {},
            "manifest_rows": [], "manifest_inputs": [], "run_id": "", "kinds": []}
    if batch is not None:
        data.update(export_service.payload(ctx.store, batch["batch_id"]))
    data.update(_guard(ctx.store, batch))
    return render(request, "adjust.html", **data)


@router.get("/batches", response_class=HTMLResponse)
def page_batches(request: Request, batch_id: str | None = None):
    from .modules.m5_batches import service as batch_service

    ctx = get_ctx(request)
    batch = ctx.current_batch(batch_id)
    payload = batch_service.list_payload(ctx.store, ctx.settings, limit=30)
    selected = None
    if batch_id:
        selected = batch_service.detail(ctx.store, batch_id)
    elif batch is not None:
        selected = batch_service.detail(ctx.store, batch["batch_id"])
    data = {"active": "batches", "batch_id": batch_id,
            "rows": payload["rows"], "summary": payload["summary"],
            "selected": selected,
            "selected_recovery": (batch_service.recovery(ctx.store, ctx.settings,
                                                         batch_id)
                                  if batch_id else None)}
    data.update(_guard(ctx.store, batch))
    return render(request, "batches.html", **data)


@router.get("/audit", response_class=HTMLResponse)
def page_audit(request: Request, batch_id: str | None = None,
               kind: str | None = None):
    from .modules.m7_audit import service as audit_service

    ctx = get_ctx(request)
    batch = ctx.current_batch(batch_id)
    data = {"active": "audit", "batch_id": batch_id, "kind": kind or "",
            "suppliers": [], "supplier_totals": {}, "master": {},
            "credits": {"rows": [], "rejected": [], "totals": {}, "file_issues": [],
                        "reason_counts": [], "file": "", "exists": False,
                        "encoding": None},
            "dup_groups": [], "dup_counts": {}, "dup_kinds": [], "dup_kind": "",
            "grn_splits": [], "rate_shifts": [], "checks": [], "balanced": False}
    if batch is not None:
        data.update(audit_service.payload(ctx.settings, ctx.store,
                                          batch["batch_id"], dup_kind=kind or None))
    data.update(_guard(ctx.store, batch))
    return render(request, "audit.html", **data)


@router.get("/export", response_class=HTMLResponse)
def page_export(request: Request, batch_id: str | None = None):
    from .modules.m6_export import service as export_service
    from .modules.m4_match import service as match_service

    ctx = get_ctx(request)
    batch = ctx.current_batch(batch_id)
    data = {"active": "export", "batch_id": batch_id, "kinds": [],
            "adjust_summary": {}, "totals": {}, "manifest_rows": [],
            "manifest_inputs": [], "reconcile": None, "run_id": "", "watermark": {}}
    if batch is not None:
        data.update(export_service.payload(ctx.store, batch["batch_id"]))
        data["reconcile"] = match_service.reconcile_payload(ctx.store, batch["batch_id"])
    data.update(_guard(ctx.store, batch))
    return render(request, "export.html", **data)
