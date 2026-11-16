"""运行清单输出：JSON 与一页可核对的摘要行。

JSON 里 Decimal 一律已经转成字符串，不存在浮点往返丢精度的问题；
`input_digest` 与每个文件的 sha256 让结果可以复算。
"""
from __future__ import annotations

import json

from ... import config


def to_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def summary_rows(manifest: dict) -> list[dict]:
    rows = [
        {"label": "运行号", "value": manifest.get("run_id", "")},
        {"label": "批次号", "value": manifest.get("batch_id", "")},
        {"label": "批次键", "value": manifest.get("batch_key", "")},
        {"label": "规则版本", "value": manifest.get("rule_version", "")},
        {"label": "评估日", "value": manifest.get("eval_date", "")},
        {"label": "输入摘要", "value": manifest.get("input_digest", "")},
        {"label": "输入留存", "value": "是" if manifest.get("inputs") else "否"},
        {"label": "重放", "value": "是" if manifest.get("replayed") else "否"},
        {"label": "开始(UTC)", "value": manifest.get("started_at", "")},
        {"label": "结束(UTC)", "value": manifest.get("finished_at", "")},
        {"label": "耗时(ms)", "value": str(manifest.get("elapsed_ms", ""))},
    ]
    return rows


def input_rows(manifest: dict) -> list[dict]:
    rows = []
    for item in manifest.get("inputs") or []:
        rows.append({
            "name": item.get("name", ""),
            "doc_label": config.DOC_LABELS.get(item.get("doc_type"), item.get("doc_type", "")),
            "bytes": item.get("bytes", 0),
            "records": item.get("records", 0),
            "encoding": item.get("encoding", ""),
            "sha256": item.get("sha256", ""),
        })
    return rows


def filename(batch_id: str) -> str:
    return f"{batch_id}-manifest.json"
