"""SQLite 存储：批次、文件诊断、三单行、隔离区、匹配结论、调整单、阶段、合计、清单。

约定：
- 金额/数量/税率一律 TEXT（Decimal 字符串），不落 float；
- 一次运行的所有行在同一事务里「先删后插」，失败整体回滚，不留半批数据；
- 读取永远按 batch_id 过滤，页面只展示一个批次。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL,
    resume_of TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batch_key ON batches(batch_key, status);
CREATE TABLE IF NOT EXISTS files (
    batch_id TEXT NOT NULL, name TEXT NOT NULL, doc_type TEXT NOT NULL,
    status TEXT NOT NULL, encoding TEXT, declared_charset TEXT, bom_stripped INTEGER,
    bytes INTEGER, sha256 TEXT, record_count INTEGER, segment_count INTEGER,
    accept_count INTEGER, quarantine_count INTEGER, code TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS po_lines (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, po_no TEXT NOT NULL, line_no TEXT NOT NULL,
    item_code TEXT, item_name TEXT, qty TEXT, unit_price TEXT, unit TEXT, tax_rate TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}', row_ref TEXT NOT NULL, flags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS grn_lines (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, grn_no TEXT NOT NULL, po_no TEXT NOT NULL,
    line_no TEXT NOT NULL, qty_received TEXT, received_at TEXT, warehouse TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}', row_ref TEXT NOT NULL, flags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS inv_lines (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, invoice_no TEXT NOT NULL, invoice_date TEXT,
    po_no TEXT NOT NULL, line_no TEXT NOT NULL, qty TEXT, amount_ex_tax TEXT,
    tax_rate TEXT, amount_inc_tax TEXT, unit TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}', row_ref TEXT NOT NULL, flags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS quarantine (
    batch_id TEXT NOT NULL, code TEXT NOT NULL, level TEXT NOT NULL, stage TEXT NOT NULL,
    doc_type TEXT, file_name TEXT, row_ref TEXT, field TEXT, raw_value TEXT,
    detail TEXT, title TEXT, hint TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, po_no TEXT NOT NULL, line_no TEXT NOT NULL,
    verdict TEXT NOT NULL, diff_kind TEXT, reasons_json TEXT NOT NULL DEFAULT '[]',
    po_qty TEXT, grn_qty TEXT, inv_qty TEXT, unit_price TEXT,
    inv_amount_ex TEXT, inv_amount_inc TEXT, expect_amount_ex TEXT,
    expect_amount_inc TEXT, diff_amount TEXT, qty_diff TEXT, price_diff TEXT,
    tax_diff TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS adjustments (
    batch_id TEXT NOT NULL, adj_no TEXT NOT NULL, target_no TEXT NOT NULL,
    po_no TEXT NOT NULL, weight TEXT NOT NULL, alloc_amount TEXT NOT NULL,
    diff_kind TEXT, evidence_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stages (
    batch_id TEXT NOT NULL, stage TEXT NOT NULL, in_count INTEGER NOT NULL,
    out_count INTEGER NOT NULL, quarantine_count INTEGER NOT NULL,
    status TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totals (batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manifests (batch_id TEXT PRIMARY KEY, json TEXT NOT NULL);
"""


def _batch_dict(row) -> dict:
    batch = dict(row)
    batch["params"] = json.loads(batch.get("params_json") or "{}")
    return batch


def _s(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------- 批次 ----------------
    def upsert_batch(self, batch: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id, batch_key, run_id, source, status,
                        rule_version, created_at, finished_at, input_digest, resume_of,
                        params_json, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     input_digest=excluded.input_digest, note=excluded.note,
                     params_json=excluded.params_json""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["rule_version"], batch["created_at"],
                 batch.get("finished_at"), batch["input_digest"], batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", "")))

    def find_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (batch_key,)).fetchone()
        return _batch_dict(row) if row else None

    def latest_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1", (batch_key,)).fetchone()
        return _batch_dict(row) if row else None

    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return _batch_dict(row) if row else None

    def latest_batch(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE status=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 40) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 一次运行的原子写入 ----------------
    def save_run(self, *, batch: dict, files: list[dict], po_lines: list[dict],
                 grn_lines: list[dict], inv_lines: list[dict], quarantine: list[dict],
                 matches: list[dict], adjustments: list[dict], stages: list[dict],
                 totals: dict, manifest: dict) -> None:
        batch_id = batch["batch_id"]
        with self.connect() as conn:
            for table in ("files", "po_lines", "grn_lines", "inv_lines", "quarantine",
                          "matches", "adjustments", "stages", "totals"):
                conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (batch_id,))
            for f in files:
                conn.execute(
                    """INSERT INTO files (batch_id,name,doc_type,status,encoding,
                        declared_charset,bom_stripped,bytes,sha256,record_count,
                        segment_count,accept_count,quarantine_count,code,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, f["name"], f["doc_type"], f["status"], f.get("encoding"),
                     f.get("declared_charset"), 1 if f.get("bom_stripped") else 0,
                     f.get("bytes"), f.get("sha256"), f.get("record_count", 0),
                     f.get("segment_count", 1), f.get("accept_count", 0),
                     f.get("quarantine_count", 0), f.get("code"), f.get("note", "")))
            for row in po_lines:
                conn.execute(
                    """INSERT INTO po_lines (batch_id,key,po_no,line_no,item_code,item_name,
                        qty,unit_price,unit,tax_rate,raw_json,row_ref,flags_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["key"], row["po_no"], row["line_no"], row.get("item_code"),
                     row.get("item_name"), _s(row.get("qty")), _s(row.get("unit_price")),
                     row.get("unit"), _s(row.get("tax_rate")),
                     json.dumps(row.get("raw", {}), ensure_ascii=False),
                     row["row_ref"], json.dumps(row.get("flags", []), ensure_ascii=False)))
            for row in grn_lines:
                conn.execute(
                    """INSERT INTO grn_lines (batch_id,key,grn_no,po_no,line_no,qty_received,
                        received_at,warehouse,raw_json,row_ref,flags_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["key"], row["grn_no"], row["po_no"], row["line_no"],
                     _s(row.get("qty_received")), row.get("received_at"),
                     row.get("warehouse"),
                     json.dumps(row.get("raw", {}), ensure_ascii=False),
                     row["row_ref"], json.dumps(row.get("flags", []), ensure_ascii=False)))
            for row in inv_lines:
                conn.execute(
                    """INSERT INTO inv_lines (batch_id,key,invoice_no,invoice_date,po_no,
                        line_no,qty,amount_ex_tax,tax_rate,amount_inc_tax,unit,raw_json,
                        row_ref,flags_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, row["key"], row["invoice_no"], row.get("invoice_date"),
                     row["po_no"], row["line_no"], _s(row.get("qty")),
                     _s(row.get("amount_ex_tax")), _s(row.get("tax_rate")),
                     _s(row.get("amount_inc_tax")), row.get("unit"),
                     json.dumps(row.get("raw", {}), ensure_ascii=False),
                     row["row_ref"], json.dumps(row.get("flags", []), ensure_ascii=False)))
            for q in quarantine:
                conn.execute(
                    """INSERT INTO quarantine (batch_id,code,level,stage,doc_type,file_name,
                        row_ref,field,raw_value,detail,title,hint)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, q["code"], q["level"], q["stage"], q.get("doc_type"),
                     q.get("file_name"), q.get("row_ref"), q.get("field"),
                     q.get("raw_value"), q.get("detail"), q.get("title", ""),
                     q.get("hint", "")))
            for m in matches:
                conn.execute(
                    """INSERT INTO matches (batch_id,key,po_no,line_no,verdict,diff_kind,
                        reasons_json,po_qty,grn_qty,inv_qty,unit_price,inv_amount_ex,
                        inv_amount_inc,expect_amount_ex,expect_amount_inc,diff_amount,
                        qty_diff,price_diff,tax_diff,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, m["key"], m["po_no"], m["line_no"], m["verdict"],
                     m.get("diff_kind"),
                     json.dumps(m.get("reasons", []), ensure_ascii=False),
                     _s(m.get("po_qty")), _s(m.get("grn_qty")), _s(m.get("inv_qty")),
                     _s(m.get("unit_price")), _s(m.get("inv_amount_ex")),
                     _s(m.get("inv_amount_inc")), _s(m.get("expect_amount_ex")),
                     _s(m.get("expect_amount_inc")), _s(m.get("diff_amount")),
                     _s(m.get("qty_diff")), _s(m.get("price_diff")),
                     _s(m.get("tax_diff")), m.get("note", "")))
            for a in adjustments:
                conn.execute(
                    """INSERT INTO adjustments (batch_id,adj_no,target_no,po_no,weight,
                        alloc_amount,diff_kind,evidence_json,note)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (batch_id, a["adj_no"], a["target_no"], a["po_no"], _s(a["weight"]),
                     _s(a["alloc_amount"]), a.get("diff_kind"),
                     json.dumps(a.get("evidence", []), ensure_ascii=False),
                     a.get("note", "")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (batch_id, s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"], s.get("note", "")))
            for key, value in totals.items():
                conn.execute("INSERT INTO totals (batch_id,key,value) VALUES (?,?,?)",
                             (batch_id, key, _s(value)))
            conn.execute(
                "INSERT INTO manifests (batch_id,json) VALUES (?,?) "
                "ON CONFLICT(batch_id) DO UPDATE SET json=excluded.json",
                (batch_id, json.dumps(manifest, ensure_ascii=False)))
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,source,status,rule_version,
                        created_at,finished_at,input_digest,resume_of,params_json,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     input_digest=excluded.input_digest, note=excluded.note""",
                (batch_id, batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["rule_version"], batch["created_at"],
                 batch.get("finished_at"), batch["input_digest"], batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", "")))

    def save_attempt(self, *, batch: dict, files: list[dict], stages: list[dict],
                     note: str = "") -> None:
        """失败尝试：只写批次、文件与阶段，不动任何已完成的批次数据。"""
        batch_id = batch["batch_id"]
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,source,status,rule_version,
                        created_at,finished_at,input_digest,resume_of,params_json,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET status=excluded.status,
                     finished_at=excluded.finished_at, note=excluded.note""",
                (batch_id, batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["rule_version"], batch["created_at"],
                 batch.get("finished_at"), batch["input_digest"], batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 note or batch.get("note", "")))
            conn.execute("DELETE FROM files WHERE batch_id=?", (batch_id,))
            conn.execute("DELETE FROM stages WHERE batch_id=?", (batch_id,))
            for f in files:
                conn.execute(
                    """INSERT INTO files (batch_id,name,doc_type,status,encoding,
                        declared_charset,bom_stripped,bytes,sha256,record_count,
                        segment_count,accept_count,quarantine_count,code,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, f["name"], f["doc_type"], f["status"], f.get("encoding"),
                     f.get("declared_charset"), 1 if f.get("bom_stripped") else 0,
                     f.get("bytes"), f.get("sha256"), f.get("record_count", 0),
                     f.get("segment_count", 1), f.get("accept_count", 0),
                     f.get("quarantine_count", 0), f.get("code"), f.get("note", "")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,
                        quarantine_count,status,elapsed_ms,note)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (batch_id, s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"], s.get("note", "")))

    # ---------------- 读取 ----------------
    def load_files(self, batch_id: str) -> list[dict]:
        return self._all("SELECT * FROM files WHERE batch_id=? ORDER BY doc_type, name",
                         (batch_id,))

    def load_po_lines(self, batch_id: str) -> list[dict]:
        return self._dec_rows(
            self._all("SELECT * FROM po_lines WHERE batch_id=? ORDER BY po_no, line_no",
                      (batch_id,)),
            ("qty", "unit_price", "tax_rate"), ("raw_json", "flags_json"))

    def load_grn_lines(self, batch_id: str) -> list[dict]:
        return self._dec_rows(
            self._all("SELECT * FROM grn_lines WHERE batch_id=? ORDER BY po_no, line_no, grn_no",
                      (batch_id,)),
            ("qty_received",), ("raw_json", "flags_json"))

    def load_inv_lines(self, batch_id: str) -> list[dict]:
        return self._dec_rows(
            self._all("SELECT * FROM inv_lines WHERE batch_id=? "
                      "ORDER BY po_no, line_no, invoice_no", (batch_id,)),
            ("qty", "amount_ex_tax", "tax_rate", "amount_inc_tax"),
            ("raw_json", "flags_json"))

    def load_quarantine(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM quarantine WHERE batch_id=? ORDER BY stage, code, row_ref",
            (batch_id,))

    def load_matches(self, batch_id: str) -> list[dict]:
        rows = self._dec_rows(
            self._all("SELECT * FROM matches WHERE batch_id=? ORDER BY po_no, line_no",
                      (batch_id,)),
            ("po_qty", "grn_qty", "inv_qty", "unit_price", "inv_amount_ex",
             "inv_amount_inc", "expect_amount_ex", "expect_amount_inc", "diff_amount",
             "qty_diff", "price_diff", "tax_diff"),
            ("reasons_json",))
        return rows

    def load_adjustments(self, batch_id: str) -> list[dict]:
        rows = self._dec_rows(
            self._all("SELECT * FROM adjustments WHERE batch_id=? ORDER BY adj_no",
                      (batch_id,)),
            ("weight", "alloc_amount"), ("evidence_json",))
        return rows

    def load_stages(self, batch_id: str) -> list[dict]:
        from . import config
        rows = self._all("SELECT * FROM stages WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for row in rows:
            row["label"] = config.STAGE_LABELS.get(row["stage"], row["stage"])
        return rows

    def load_totals(self, batch_id: str) -> dict:
        rows = self._all("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def load_manifest(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT json FROM manifests WHERE batch_id=?",
                               (batch_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_rows(self, batch_id: str) -> dict:
        out = {}
        with self.connect() as conn:
            for table in ("files", "po_lines", "grn_lines", "inv_lines", "quarantine",
                          "matches", "adjustments"):
                out[table] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE batch_id=?",
                    (batch_id,)).fetchone()["n"]
        return out

    def _all(self, sql: str, args: tuple) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    @staticmethod
    def _dec_rows(rows: list[dict], dec_fields: tuple, json_fields: tuple = ()) -> list[dict]:
        for row in rows:
            for field in dec_fields:
                if field in row and row[field] is not None and row[field] != "":
                    row[field] = Decimal(row[field])
            for field in json_fields:
                if field in row:
                    key = field.replace("_json", "")
                    row[key] = json.loads(row.pop(field) or "null")
        return rows
