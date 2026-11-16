"""跨批次重复报销检测：同号不同额、同号同额、同额不同号。

三条硬规则：
1. 只比「不同输入摘要」的批次。同一份输入换规则版本重跑不算再次报销，
   否则一次重跑就把整批发票标成重复。
2. 发票号先归一（全角转半角、去空白、转大写）再比；金额按分量化后比，
   差一分就是两个金额，不能靠浮点近似。
3. 同批次内同号不同额由 M3 判 `E_DUP_KEY_CONFLICT` 并整行隔离，这里不再重复报，
   重复组只列跨批次的行，每条都带批次号与证据行。
"""
from __future__ import annotations

from decimal import Decimal

from ..m1_ingest import sniff

KINDS = ("same_no_diff_amount", "same_no_same_amount", "same_amount_diff_no")
KIND_LABELS = {
    "same_no_diff_amount": "同号不同额",
    "same_no_same_amount": "同号同额",
    "same_amount_diff_no": "同额不同号",
}
KIND_RANK = {kind: rank for rank, kind in enumerate(KINDS)}
CENT = Decimal("0.01")


def normalize_no(text) -> str:
    """发票号归一：全角转半角、去空白、转大写；展示仍用原值。"""
    body = sniff.to_halfwidth(str(text or ""))
    return "".join(body.split()).upper()


def amount_key(value) -> str:
    """金额按分量化成字符串当比较键；None 给空串（不参与同额比对）。"""
    if value is None or value == "":
        return ""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value.quantize(CENT), "f")


def _digest_of(row: dict) -> str:
    return row.get("input_digest") or row.get("batch_id") or ""


def _row_view(row: dict) -> dict:
    return {
        "batch_id": row.get("batch_id") or "",
        "invoice_no": row.get("invoice_no") or "",
        "amount_inc": (row.get("amount_inc_tax") if row.get("amount_inc_tax") is not None
                       else Decimal("0")),
        "po_no": row.get("po_no") or "",
        "line_no": row.get("line_no") or "",
        "row_ref": row.get("row_ref") or "",
    }


def _group(kind: str, key: str, rows: list[dict], detail: str) -> dict:
    views = sorted((_row_view(row) for row in rows),
                   key=lambda item: (item["batch_id"], item["row_ref"]))
    return {
        "kind": kind,
        "label": KIND_LABELS[kind],
        "key": key,
        "count": len(views),
        "detail": detail,
        "batches": len({item["batch_id"] for item in views}),
        "rows": views,
        "evidence": " / ".join(item["row_ref"] for item in views),
    }


def detect(rows: list[dict]) -> list[dict]:
    """rows 是各批次已采纳的发票行，必须带 batch_id 与 input_digest。"""
    groups: list[dict] = []

    by_no: dict[str, list[dict]] = {}
    for row in rows:
        key = normalize_no(row.get("invoice_no"))
        if not key:
            continue
        by_no.setdefault(key, []).append(row)

    for key in sorted(by_no):
        bucket = by_no[key]
        if len({_digest_of(row) for row in bucket}) < 2:
            continue
        amounts = sorted({amount_key(row.get("amount_inc_tax")) for row in bucket})
        kind = "same_no_diff_amount" if len(amounts) > 1 else "same_no_same_amount"
        detail = (f"同一发票号出现在 {len({_digest_of(row) for row in bucket})} 次报送，"
                  f"金额 {len(amounts)} 种")
        groups.append(_group(kind, key, bucket, detail))

    by_amount: dict[str, list[dict]] = {}
    for row in rows:
        key = amount_key(row.get("amount_inc_tax"))
        if not key:
            continue
        by_amount.setdefault(key, []).append(row)

    for amount in sorted(by_amount):
        bucket = by_amount[amount]
        numbers = {normalize_no(row.get("invoice_no")) for row in bucket}
        if len(numbers) < 2 or len({_digest_of(row) for row in bucket}) < 2:
            continue
        detail = f"同一价税合计对应 {len(numbers)} 个发票号"
        groups.append(_group("same_amount_diff_no", amount, bucket, detail))

    return sorted(groups, key=lambda item: (KIND_RANK[item["kind"]], item["key"]))


def filter_kind(groups: list[dict], kind: str | None) -> list[dict]:
    if not kind:
        return groups
    return [group for group in groups if group["kind"] == kind]


def counts(groups: list[dict]) -> dict:
    out = {kind: 0 for kind in KINDS}
    for group in groups:
        out[group["kind"]] += 1
    out["groups"] = len(groups)
    return out


def kind_options(groups: list[dict]) -> list[dict]:
    tally = counts(groups)
    return [{"key": kind, "label": KIND_LABELS[kind], "count": tally[kind]}
            for kind in KINDS]
