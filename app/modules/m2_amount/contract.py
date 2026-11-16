"""列契约：每种单据的列名 / 类型 / 必填 / 历史别名，以及表头映射。

别名表就是 schema 演进的处理位置：旧导出把 `po_no` 写成 `po`/`purchase_no`，
新契约只需在别名里加一项，映射函数自动认出来，不用改下游任何代码。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config


def columns(doc_type: str) -> tuple:
    return config.COLUMNS[doc_type]


def column_names(doc_type: str) -> list[str]:
    return [name for name, _label, _kind, _req, _alias in columns(doc_type)]


def label_of(doc_type: str, name: str) -> str:
    for col, label, _kind, _req, _alias in columns(doc_type):
        if col == name:
            return label
    return name


def kind_of(doc_type: str, name: str) -> str:
    for col, _label, kind, _req, _alias in columns(doc_type):
        if col == name:
            return kind
    return "text"


def required_columns(doc_type: str) -> list[str]:
    return [name for name, _l, _k, req, _a in columns(doc_type) if req]


def is_key_column(doc_type: str, name: str) -> bool:
    return name in ("po_no", "line_no", "invoice_no", "grn_no")


def alias_index(doc_type: str) -> dict:
    """规范列名 + 全部别名 → 规范列名。"""
    index: dict[str, str] = {}
    for name, label, _kind, _req, aliases in columns(doc_type):
        index[name] = name
        if label:
            index[label] = name
        for alias in aliases:
            index[alias] = name
    return index


def match_headers(normalized_headers: list[str], doc_type: str) -> dict:
    """归一化表头 → {规范列名: 列下标}；命中顺序按表头顺序，稳定可复现。"""
    index = alias_index(doc_type)
    mapping: dict[str, int] = {}
    for position, header in enumerate(normalized_headers):
        canonical = index.get(header) or index.get(header.replace(" ", ""))
        if canonical and canonical not in mapping:
            mapping[canonical] = position
    return mapping


def missing_required(doc_type: str, mapping: dict) -> list[str]:
    return [name for name in required_columns(doc_type) if name not in mapping]


def unknown_headers(normalized_headers: list[str], doc_type: str) -> list[str]:
    index = alias_index(doc_type)
    return [h for h in normalized_headers if h and h not in index]


def contract_payload(settings) -> dict:
    docs = []
    for doc_type in config.DOC_TYPES:
        docs.append({
            "doc_type": doc_type,
            "label": config.DOC_LABELS[doc_type],
            "short": config.DOC_SHORT[doc_type],
            "columns": [
                {"name": name, "label": label, "kind": kind, "required": req,
                 "aliases": list(aliases)}
                for name, label, kind, req, aliases in columns(doc_type)
            ],
        })
    return {
        "rule_version": settings.rule_version,
        "eval_date": settings.eval_date.isoformat(),
        "documents": docs,
        "amount": {"unit": config.MONEY_UNIT,
                   "quant": str(config.AMOUNT_QUANT),
                   "tolerance": str(config.AMOUNT_TOLERANCE)},
        "qty": {"quant": str(config.QTY_QUANT), "tolerance": str(config.QTY_TOLERANCE)},
        "rate": {"quant": str(config.RATE_QUANT), "max": str(config.MAX_TAX_RATE)},
        "units": {name: str(factor) for name, factor in config.UNIT_TO_YUAN.items()},
        "sentinels": sorted(config.SENTINELS),
        "verdicts": [{"key": v, "label": config.VERDICT_LABELS[v]} for v in config.VERDICTS],
        "diff_kinds": [{"key": k, "label": config.DIFF_LABELS[k],
                        "rank": config.DIFF_RANK[k]} for k in config.DIFF_KINDS],
        "key": ["po_no", "line_no"],
    }


def quant_amount(value: Decimal) -> Decimal:
    return value.quantize(config.AMOUNT_QUANT)
