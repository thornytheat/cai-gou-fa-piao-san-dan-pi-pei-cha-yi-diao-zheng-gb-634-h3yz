"""运行清单：run_id、输入文件摘要、规则版本、参数、耗时，可复算。

run_id = 前缀 + UTC 时间戳 + 随机后缀；台账里的每个数字都能回溯到输入 sha256。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid

from . import config


def new_run_id(prefix: str = "3WM") -> str:
    stamp = dt.datetime.now(config.UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:4]}"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_parts(parts: list[str]) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def build(*, run_id: str, batch_id: str, batch_key: str, settings, files: list[dict],
          input_digest: str, params: dict, started_at: str, finished_at: str,
          elapsed_ms: int, stages: list[dict], counters: dict, totals: dict) -> dict:
    return {
        "run_id": run_id,
        "batch_id": batch_id,
        "batch_key": batch_key,
        "rule_version": settings.rule_version,
        "eval_date": settings.eval_date.isoformat(),
        "decimals": "金额 Decimal 元 2 位；数量 4 位；税率 6 位",
        "timezone": "入库 UTC，展示 Asia/Shanghai(+08:00)",
        "tolerance": {"amount": str(config.AMOUNT_TOLERANCE),
                      "qty": str(config.QTY_TOLERANCE)},
        "input_digest": input_digest,
        "inputs": [
            {"name": f["name"], "doc_type": f["doc_type"], "bytes": f["bytes"],
             "sha256": f["sha256"], "encoding": f["encoding"],
             "records": f["record_count"]}
            for f in files
        ],
        "params": params,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_ms": elapsed_ms,
        "stages": stages,
        "counters": counters,
        "totals": totals,
        "replayable": "同 batch_key 重跑返回同一批次，不新增行",
    }
