"""隔离区落库行构造、原因码分布、CSV 导出。

隔离行的四要素固定：原因码 + 行号 + 字段 + 原值。原值一律按文本保留，
方便财务把 `1,234.50` 和 `1234.50` 的差别看出来。
"""
from __future__ import annotations

import csv
import io

from ... import config, reasons

CSV_HEADERS = ("原因码", "级别", "阶段", "单据", "文件", "行号", "字段", "原值", "说明")
EXPORT_NAME = "quarantine.csv"


def make_row(*, code: str, stage: str, doc_type: str | None = None,
             file_name: str | None = None, row_ref: str | None = None,
             field: str | None = None, raw_value: str | None = None,
             detail: str | None = None) -> dict:
    reason = reasons.get(code)
    return {
        "code": code,
        "level": reason.level,
        "stage": stage,
        "doc_type": doc_type,
        "file_name": file_name,
        "row_ref": row_ref,
        "field": field,
        "raw_value": raw_value,
        "detail": detail or reason.title,
        "title": reason.title,
        "hint": reason.hint,
    }


def from_issue(*, issue: dict, stage: str, doc_type: str, file_name: str,
               row_ref: str) -> dict:
    return make_row(code=issue["code"], stage=stage, doc_type=doc_type,
                    file_name=file_name, row_ref=row_ref,
                    field=issue.get("field"), raw_value=issue.get("raw_value"),
                    detail=issue.get("detail"))


def sort_rows(rows: list[dict]) -> list[dict]:
    """稳定顺序：级别 → 阶段 → 原因码 → 行号；同一批重跑输出逐字相同。"""
    return sorted(rows, key=lambda r: (
        reasons.LEVEL_ORDER.get(r.get("level"), 9), r.get("stage") or "",
        r.get("code") or "", r.get("file_name") or "", r.get("row_ref") or ""))


def to_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADERS)
    for row in sort_rows(rows):
        writer.writerow([
            row.get("code") or "",
            reasons.LEVEL_LABELS.get(row.get("level"), row.get("level") or ""),
            config.STAGE_LABELS.get(row.get("stage"), row.get("stage") or ""),
            config.DOC_LABELS.get(row.get("doc_type"), row.get("doc_type") or ""),
            row.get("file_name") or "",
            row.get("row_ref") or "",
            row.get("field") or "",
            row.get("raw_value") or "",
            row.get("detail") or row.get("title") or "",
        ])
    return buffer.getvalue()


def encode_csv(text: str) -> bytes:
    return ("\ufeff" + text).encode("utf-8")


def distribution(rows: list[dict]) -> list[dict]:
    return reasons.distribution(rows)


def row_level(rows: list[dict]) -> list[dict]:
    """行级隔离（带 :L 行号）与文件级记录（文件 / 表头）分开计数。"""
    return [row for row in rows if _is_row_ref(row.get("row_ref") or "")]


def file_level(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not _is_row_ref(row.get("row_ref") or "")]


def _is_row_ref(ref: str) -> bool:
    return ":L" in ref


def blocking_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("level") == reasons.ERROR]


def field_counts(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        field = row.get("field") or "—"
        counts[field] = counts.get(field, 0) + 1
    return [{"field": field, "count": counts[field]}
            for field in sorted(counts, key=lambda f: (-counts[f], f))]
