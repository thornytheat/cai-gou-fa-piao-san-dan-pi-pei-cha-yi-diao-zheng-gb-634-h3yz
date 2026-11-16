"""CSV 输出：调整单、匹配证据行。原值按文本写出，大整数与科学计数法逐字保留。

导出文件带 UTF-8 BOM（Excel 直接打开不乱码），因此导入端必须先剥 BOM，
这一点由 M1 的解码模块负责。
"""
from __future__ import annotations

import csv
import io

from ... import config

ADJUST_HEADERS = ("调整单号", "采购单号", "差异类别", "权重金额(元)", "调整金额(元)", "证据行")
MATCH_HEADERS = ("采购单号", "行号", "结论", "差异类别", "发票价税合计(元)",
                 "应付价税合计(元)", "差异金额(元)", "证据行")


def encode(text: str) -> bytes:
    return ("\ufeff" + text).encode("utf-8")


def _writer(buffer: io.StringIO):
    return csv.writer(buffer, lineterminator="\n")


def adjustment_csv(rows: list[dict], summary: dict) -> str:
    buffer = io.StringIO()
    writer = _writer(buffer)
    writer.writerow(ADJUST_HEADERS)
    for row in rows:
        writer.writerow([
            row["adj_no"], row["po_no"],
            config.DIFF_LABELS.get(row.get("diff_kind") or "", ""),
            _num(row["weight"]), _num(row["alloc_amount"]),
            " ".join(row.get("evidence") or []),
        ])
    writer.writerow([])
    writer.writerow(["合计", "", "", "", summary["total"], ""])
    writer.writerow(["已分", "", "", "", summary["allocated"], ""])
    writer.writerow(["尾差", "", "", "", summary["remainder"], ""])
    return buffer.getvalue()


def matches_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = _writer(buffer)
    writer.writerow(MATCH_HEADERS)
    for row in rows:
        writer.writerow([
            row["po_no"], row["line_no"],
            config.VERDICT_LABELS.get(row["verdict"], row["verdict"]),
            config.DIFF_LABELS.get(row.get("diff_kind") or "", ""),
            _num(row.get("inv_amount_inc")), _num(row.get("expect_amount_inc")),
            _num(row.get("diff_amount")),
            " ".join(row.get("row_refs") or []),
        ])
    return buffer.getvalue()


def _num(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    return format(value, "f")


def headers_of(kind: str) -> tuple:
    return ADJUST_HEADERS if kind == "adjustment" else MATCH_HEADERS
