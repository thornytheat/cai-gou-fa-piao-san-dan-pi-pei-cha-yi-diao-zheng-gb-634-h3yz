"""质量指标：按单据类型算采纳/隔离/通过率，以及原因码覆盖率。

通过率 = 采纳记录 / 记录总数，分母为 0 时给 None，不返回 0% 冒充结论。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from ... import config, reasons


def _rate(numerator: int, denominator: int) -> Decimal | None:
    if not denominator:
        return None
    return (Decimal(numerator) / Decimal(denominator) * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def by_doc(files: list[dict], quarantine_rows: list[dict]) -> list[dict]:
    rows = []
    for doc_type in config.DOC_TYPES:
        doc_files = [f for f in files if f.get("doc_type") == doc_type]
        accepted = sum(int(f.get("accept_count") or 0) for f in doc_files)
        quarantined = sum(1 for q in quarantine_rows
                          if q.get("doc_type") == doc_type
                          and q.get("level") == reasons.ERROR)
        records = accepted + quarantined
        rows.append({
            "doc_type": doc_type,
            "label": config.DOC_LABELS[doc_type],
            "files": len(doc_files),
            "records": records,
            "accepted": accepted,
            "quarantined": quarantined,
            "pass_rate": _rate(accepted, records),
        })
    return rows


def summary(files: list[dict], quarantine_rows: list[dict]) -> dict:
    docs = by_doc(files, quarantine_rows)
    records = sum(row["records"] for row in docs)
    accepted = sum(row["accepted"] for row in docs)
    quarantined = sum(row["quarantined"] for row in docs)
    errors = sum(1 for q in quarantine_rows if q.get("level") == reasons.ERROR)
    warns = sum(1 for q in quarantine_rows if q.get("level") == reasons.WARN)
    infos = sum(1 for q in quarantine_rows if q.get("level") == reasons.INFO)
    return {
        "records": records,
        "accepted": accepted,
        "quarantined": quarantined,
        "errors": errors,
        "warns": warns,
        "infos": infos,
        "pass_rate": _rate(accepted, records),
        "by_doc": docs,
        "distinct_codes": sorted({q.get("code") for q in quarantine_rows if q.get("code")}),
    }


def gate(summary_payload: dict) -> dict:
    """是否可进入匹配：有隔离行不算阻断，未识别到任何单据才算阻断。"""
    blocking = summary_payload["accepted"] == 0
    return {
        "blocking": blocking,
        "reason": "无有效单据行" if blocking else "",
        "hint": "检查编码与列名后重导" if blocking else "",
    }
