"""供应商归集与汇总：采购单 → 供应商、分批入库拆分、跨税率重算、按供应商汇总。

三条硬规则：
1. 采购单号只在主数据里查；查不到记「未登记」，不猜、也不并到相邻供应商。
2. 同一供应商编码出现多个名称时按名称排序取第一个当代表名，与读入顺序无关。
3. 分批入库一行一批：合计必须等于各批之和，未入库 = 订单数量 − 各批之和。
   朴素实现只取最后一批或最大一批，未入库数量立刻算错（TRAP-28）。

金额一律 Decimal 元，量化 0.01；数量 Decimal 4 位。本文件不碰 store 与文件读取，
入参都是已归一的 dict 列表，便于单测。
"""
from __future__ import annotations

from decimal import Decimal

from ... import config
from ..m1_ingest import sniff
from ..m2_amount import coerce, taxmath

ZERO = Decimal("0")
UNKNOWN_CODE = "—"
UNKNOWN_NAME = "未登记"
UNKNOWN_STATUS = "未登记"
STATUS_OFF = "停用"

MASTER_COLUMNS = {
    "supplier_code": ("供应商编码", "supplier_code", "code", "sap_code"),
    "supplier_name": ("供应商名称", "supplier_name", "name", "vendor_name"),
    "po_no": ("采购单号", "po_no", "po", "采购订单号"),
    "status": ("状态", "status", "启用状态"),
}
MASTER_REQUIRED = ("po_no", "supplier_code", "supplier_name")


def header_map(headers: list[str]) -> dict:
    """列名归一后按别名表定位；同一规范列只认第一次出现。"""
    index: dict[str, str] = {}
    for canonical, aliases in MASTER_COLUMNS.items():
        for alias in aliases:
            index[sniff.normalize_header(alias)] = canonical
    mapping: dict[str, int] = {}
    for position, header in enumerate(headers):
        canonical = index.get(sniff.normalize_header(header))
        if canonical and canonical not in mapping:
            mapping[canonical] = position
    return mapping


def normalize_code(text) -> str:
    """供应商编码归一：全角转半角、去空白、转大写；空值给哨兵。"""
    body = coerce.clean(text).replace(" ", "").upper()
    return body or UNKNOWN_CODE


def normalize_name(text) -> str:
    return coerce.clean(text)


def unknown_supplier() -> dict:
    return {"supplier_code": UNKNOWN_CODE, "supplier_name": UNKNOWN_NAME,
            "status": UNKNOWN_STATUS}


def parse_master(cells_rows: list[list[str]], mapping: dict) -> tuple[dict, list[dict]]:
    """主数据 → {采购单号: 供应商}。重复登记的采购单按编码/名称排序取第一条。

    返回 (映射, 问题清单)。问题只做提示，不阻断汇总。
    """
    issues: list[dict] = []
    entries: dict[str, list[dict]] = {}
    for cells in cells_rows:
        def cell(name: str) -> str:
            position = mapping.get(name)
            if position is None or position >= len(cells):
                return ""
            return cells[position]

        po_no = coerce.clean(cell("po_no"))
        if not po_no:
            issues.append({"code": "E_MASTER_INCOMPLETE", "po_no": "",
                           "detail": "主数据行没有采购单号"})
            continue
        entries.setdefault(po_no, []).append({
            "supplier_code": normalize_code(cell("supplier_code")),
            "supplier_name": normalize_name(cell("supplier_name")) or UNKNOWN_NAME,
            "status": normalize_name(cell("status")) or "正常",
        })

    master: dict[str, dict] = {}
    names_by_code: dict[str, set] = {}
    for po_no in sorted(entries):
        candidates = sorted(entries[po_no],
                            key=lambda item: (item["supplier_code"], item["supplier_name"]))
        if len(candidates) > 1:
            issues.append({"code": "E_MASTER_DUP", "po_no": po_no,
                           "detail": f"采购单在主数据里登记 {len(candidates)} 次"})
        chosen = candidates[0]
        master[po_no] = chosen
        names_by_code.setdefault(chosen["supplier_code"], set()).add(
            chosen["supplier_name"])

    for code in sorted(names_by_code):
        names = sorted(names_by_code[code])
        chosen_name = names[0]
        for po_no, entry in master.items():
            if entry["supplier_code"] == code:
                entry["supplier_name"] = chosen_name
    return master, issues


def supplier_of(master: dict, po_no: str) -> dict:
    entry = master.get(coerce.clean(po_no))
    if entry is None:
        return unknown_supplier()
    return {"supplier_code": entry["supplier_code"],
            "supplier_name": entry["supplier_name"],
            "status": entry["status"]}


def split_batches(po_qty, grn_rows: list[dict]) -> dict:
    """分批入库拆分：每张入库单一批，顺序按入库日期 + 单号定，与读入顺序无关。"""
    ordered = po_qty if po_qty is not None else ZERO
    batches = []
    for row in sorted(grn_rows,
                      key=lambda r: (r.get("received_at") or "", r.get("grn_no") or "",
                                     r.get("row_ref") or "")):
        batches.append({
            "grn_no": row.get("grn_no") or "",
            "qty": row.get("qty_received") or ZERO,
            "received_at": row.get("received_at") or "",
            "warehouse": (row.get("warehouse") or "").replace("\n", " "),
            "row_ref": row.get("row_ref") or "",
        })
    received = sum((item["qty"] for item in batches), ZERO)
    remaining = ordered - received
    over = bool(ordered) and received > ordered * config.OVER_RECEIPT_RATIO
    return {
        "batches": batches,
        "batch_count": len(batches),
        "ordered": ordered,
        "received": received,
        "remaining": remaining,
        "over": over,
        "balanced": received == sum((item["qty"] for item in batches), ZERO),
    }


def rate_shifts(inv_rows: list[dict], po_rows: list[dict]) -> list[dict]:
    """同一采购单跨税率变更：发票税率 ≠ 采购单税率时按发票税率重算应付。

    税额差有两种来源：算错，或税率变更。变更行的实际税差应该按发票税率推算，
    用采购单税率推出来的那部分差额属于税率变更带来的迁移量，不是差错。
    """
    po_by_key = {row["key"]: row for row in po_rows}
    events = []
    for row in sorted(inv_rows, key=lambda r: (r["key"], r["row_ref"])):
        po = po_by_key.get(row["key"])
        if po is None:
            continue
        po_rate = po.get("tax_rate")
        inv_rate = row.get("tax_rate")
        if po_rate is None or inv_rate is None or inv_rate == po_rate:
            continue
        ex = row.get("amount_ex_tax")
        by_invoice = taxmath.inc_from_ex(ex, inv_rate) if ex is not None else None
        by_order = taxmath.inc_from_ex(ex, po_rate) if ex is not None else None
        declared = row.get("amount_inc_tax")
        moved = None
        if declared is not None and by_order is not None:
            moved = declared - by_order
        events.append({
            "po_no": row["po_no"],
            "line_no": row["line_no"],
            "invoice_no": row.get("invoice_no") or "",
            "po_rate": po_rate,
            "inv_rate": inv_rate,
            "amount_ex": ex,
            "inc_by_invoice_rate": by_invoice,
            "inc_by_order_rate": by_order,
            "moved_amount": moved,
            "row_ref": row["row_ref"],
        })
    return events


def _bucket(rows: dict, supplier: dict) -> dict:
    code = supplier["supplier_code"]
    bucket = rows.get(code)
    if bucket is None:
        bucket = {
            "supplier_code": code,
            "supplier_name": supplier["supplier_name"],
            "status": supplier["status"],
            "po_nos": set(),
            "po_lines": 0,
            "grn_lines": 0,
            "inv_lines": 0,
            "ordered": ZERO,
            "received": ZERO,
            "orphan": ZERO,
            "orphan_lines": 0,
            "inv_inc": ZERO,
            "red_total": ZERO,
            "red_pending": ZERO,
            "red_over": 0,
            "diff_total": ZERO,
            "adjust_total": ZERO,
            "rate_shift": 0,
            "rate_moved": ZERO,
            "matched": 0,
            "diff": 0,
            "missing": 0,
        }
        rows[code] = bucket
    return bucket


def rollup(*, po_rows: list[dict], grn_rows: list[dict], inv_rows: list[dict],
           matches: list[dict], adjustments: list[dict], reds: list[dict],
           master: dict) -> dict:
    """按供应商汇总三方数量与金额、红冲冲减、差异与调整额。

    reds 只认「已冲抵」的行进净额；未冲抵的挂在待确认，不静默冲减。
    """
    buckets: dict[str, dict] = {}
    grn_by_key: dict[str, list[dict]] = {}
    for row in grn_rows:
        grn_by_key.setdefault(row["key"], []).append(row)

    for row in po_rows:
        supplier = supplier_of(master, row["po_no"])
        bucket = _bucket(buckets, supplier)
        bucket["po_nos"].add(row["po_no"])
        bucket["po_lines"] += 1
        bucket["ordered"] += row.get("qty") or ZERO

    po_keys = {row["key"] for row in po_rows}
    for row in grn_rows:
        supplier = supplier_of(master, row["po_no"])
        bucket = _bucket(buckets, supplier)
        bucket["po_nos"].add(row["po_no"])
        bucket["grn_lines"] += 1
        qty = row.get("qty_received") or ZERO
        if row["key"] in po_keys:
            bucket["received"] += qty
        else:
            # 采购订单行被隔离或缺失：这批入库不能算到某个采购行的实收上
            bucket["orphan"] += qty
            bucket["orphan_lines"] += 1

    for row in inv_rows:
        supplier = supplier_of(master, row["po_no"])
        bucket = _bucket(buckets, supplier)
        bucket["po_nos"].add(row["po_no"])
        bucket["inv_lines"] += 1
        bucket["inv_inc"] += row.get("amount_inc_tax") or ZERO

    for row in matches:
        supplier = supplier_of(master, row["po_no"])
        bucket = _bucket(buckets, supplier)
        if row["verdict"] == "diff":
            bucket["diff"] += 1
            bucket["diff_total"] += row.get("diff_amount") or ZERO
        elif row["verdict"] == "matched":
            bucket["matched"] += 1
        else:
            bucket["missing"] += 1

    for row in adjustments:
        supplier = supplier_of(master, row["po_no"])
        bucket = _bucket(buckets, supplier)
        bucket["adjust_total"] += row.get("alloc_amount") or ZERO

    for red in reds:
        supplier = supplier_of(master, red.get("po_no") or "")
        bucket = _bucket(buckets, supplier)
        if red.get("offset"):
            bucket["red_total"] += red.get("amount_inc") or ZERO
            if red.get("over"):
                bucket["red_over"] += 1
        else:
            bucket["red_pending"] += red.get("amount_inc") or ZERO

    shifts = rate_shifts(inv_rows, po_rows)
    for event in shifts:
        supplier = supplier_of(master, event["po_no"])
        bucket = _bucket(buckets, supplier)
        bucket["rate_shift"] += 1
        bucket["rate_moved"] += event.get("moved_amount") or ZERO

    rows = []
    for code in sorted(buckets):
        bucket = buckets[code]
        net = bucket["inv_inc"] + bucket["red_total"]
        rows.append({
            "supplier_code": bucket["supplier_code"],
            "supplier_name": bucket["supplier_name"],
            "status": bucket["status"],
            "po_nos": " ".join(sorted(bucket["po_nos"])) or "—",
            "po_lines": bucket["po_lines"],
            "grn_lines": bucket["grn_lines"],
            "inv_lines": bucket["inv_lines"],
            "ordered": bucket["ordered"],
            "received": bucket["received"],
            "orphan": bucket["orphan"],
            "remaining": bucket["ordered"] - bucket["received"],
            "inv_inc": bucket["inv_inc"],
            "red_total": bucket["red_total"],
            "red_pending": bucket["red_pending"],
            "red_over": bucket["red_over"],
            "net": net,
            "diff_total": bucket["diff_total"],
            "adjust_total": bucket["adjust_total"],
            "rate_shift": bucket["rate_shift"],
            "rate_moved": bucket["rate_moved"],
            "matched": bucket["matched"],
            "diff": bucket["diff"],
            "missing": bucket["missing"],
            "suspended": bucket["status"] == STATUS_OFF,
        })

    totals = {
        "suppliers": len(rows),
        "po_lines": sum(row["po_lines"] for row in rows),
        "grn_lines": sum(row["grn_lines"] for row in rows),
        "inv_lines": sum(row["inv_lines"] for row in rows),
        "ordered": sum((row["ordered"] for row in rows), ZERO),
        "received": sum((row["received"] for row in rows), ZERO),
        "orphan": sum((row["orphan"] for row in rows), ZERO),
        "remaining": (sum((row["ordered"] for row in rows), ZERO)
                      - sum((row["received"] for row in rows), ZERO)),
        "inv_inc": sum((row["inv_inc"] for row in rows), ZERO),
        "red_total": sum((row["red_total"] for row in rows), ZERO),
        "red_pending": sum((row["red_pending"] for row in rows), ZERO),
        "net": sum((row["net"] for row in rows), ZERO),
        "diff_total": sum((row["diff_total"] for row in rows), ZERO),
        "adjust_total": sum((row["adjust_total"] for row in rows), ZERO),
        "rate_shift": sum(row["rate_shift"] for row in rows),
        "rate_moved": sum((row["rate_moved"] for row in rows), ZERO),
        "matched": sum(row["matched"] for row in rows),
        "diff": sum(row["diff"] for row in rows),
        "missing": sum(row["missing"] for row in rows),
        "unknown": sum(1 for row in rows if row["supplier_name"] == UNKNOWN_NAME),
        "suspended": sum(1 for row in rows if row["suspended"]),
    }
    return {"rows": rows, "totals": totals, "shifts": shifts,
            "grn_splits": grn_splits(po_rows, grn_by_key)}


def grn_splits(po_rows: list[dict], grn_by_key: dict) -> list[dict]:
    """每个采购行的分批入库明细；没有入库行也要出现，写「未入库」。"""
    out = []
    for row in sorted(po_rows, key=lambda r: (r["po_no"], r["line_no"])):
        parts = split_batches(row.get("qty"), grn_by_key.get(row["key"], []))
        out.append({
            "po_no": row["po_no"],
            "line_no": row["line_no"],
            "item_name": row.get("item_name") or "",
            "ordered": parts["ordered"],
            "received": parts["received"],
            "remaining": parts["remaining"],
            "batch_count": parts["batch_count"],
            "over": parts["over"],
            "balanced": parts["balanced"],
            "batches": parts["batches"],
        })
    return out
