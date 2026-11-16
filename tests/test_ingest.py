"""M1 单据接入：编码回退、BOM、列数、截断、重复表头、引号内换行、空态、摘要。"""
from __future__ import annotations

import pathlib

from app import config
from app.modules.m1_ingest import decode, reader, service, sniff
from fixtures import expected as X


def _entry(name: str, doc_type: str, path: pathlib.Path) -> dict:
    return {"name": name, "doc_type": doc_type, "path": str(path), "note": ""}


def test_charset_fallback_decodes_gb18030(sample_dir):
    """TRAP-01：声明 utf-8、字节是 GB18030 时回退解码，中文列名可读。"""
    raw = (sample_dir / X.GB18030_FILE).read_bytes()
    decoded = decode.decode_bytes(raw, declared="utf-8")
    assert decoded.encoding == "gb18030"
    assert decoded.declared == "utf-8"
    assert "E_ENCODING_FALLBACK" in decoded.codes
    header = decoded.text.splitlines()[0]
    assert "grn_no" in header
    assert "一号库" in decoded.text


def test_bom_stripped_before_header_mapping(sample_dir):
    """TRAP-02：BOM 先剥离，首个列名不能带 U+FEFF，否则按列名取值全空。"""
    raw = (sample_dir / X.ORDERS_FILE).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    decoded = decode.decode_bytes(raw)
    assert decoded.bom_stripped is True
    table = reader.parse_table(decoded.text, ",")
    assert table.headers[0] == "po_no"
    assert not table.headers[0].startswith("\ufeff")


def test_column_count_mismatch_quarantined(settings, sample_dir):
    """TRAP-05：多一列的行必须进隔离区，带原因码和原值。"""
    entry = _entry(X.ORDERS_FILE, "po", sample_dir / X.ORDERS_FILE)
    result = service.read_file(settings, entry)
    codes = [q["code"] for q in result["quarantine"]]
    assert "E_COLUMN_COUNT" in codes
    row = next(q for q in result["quarantine"] if q["code"] == "E_COLUMN_COUNT")
    assert row["row_ref"] == "orders_dirty.csv:L16"
    assert "EXTRA" in row["raw_value"]
    assert result["stats"]["record_count"] == 16
    assert result["stats"]["accept_count"] == X.PO_ACCEPTED


def test_truncated_tail_detected(settings, sample_dir):
    """TRAP-16：末行没有换行且列数不足，判截断而不是普通列数不齐。"""
    entry = _entry(X.GB18030_FILE, "grn", sample_dir / X.GB18030_FILE)
    result = service.read_file(settings, entry)
    tail = [q for q in result["quarantine"] if q["code"] == "E_TRUNCATED_TAIL"]
    assert len(tail) == 1
    assert tail[0]["row_ref"].startswith("receipts_gb18030.csv:L")
    assert "GRN-9013" in tail[0]["raw_value"]
    assert tail[0]["raw_value"].endswith("2026-11-0")


def test_repeated_header_counts_segments(settings, sample_dir):
    """分段导出导致表头重复：识别为分段边界，不把表头当数据。"""
    entry = _entry(X.ORDERS_FILE, "po", sample_dir / X.ORDERS_FILE)
    result = service.read_file(settings, entry)
    assert result["counters"]["header_repeat"] == 1
    assert result["stats"]["segment_count"] == 2
    accepted_keys = [row["key"] for row in result["rows"]["po"]]
    assert "po_no|line_no" not in accepted_keys


def test_quoted_newline_keeps_record_count(settings, sample_dir):
    """引号内含换行的仓库名仍算一条记录，逻辑记录数不等于物理行数。"""
    entry = _entry(X.GB18030_FILE, "grn", sample_dir / X.GB18030_FILE)
    result = service.read_file(settings, entry)
    assert result["stats"]["record_count"] == 14
    warehouse = next(row["warehouse"] for row in result["rows"]["grn"]
                     if row["grn_no"] == "GRN-9009")
    assert "\n" in warehouse


def test_empty_and_header_only_states(settings, tmp_path):
    """TRAP-18：0 字节 / 仅表头 / 正常，三种空态给三种不同结论。"""
    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    only_header = tmp_path / "header.csv"
    only_header.write_text("po_no,line_no,item_code,item_name,qty,unit_price,"
                           "amount_unit,tax_rate\n", encoding="utf-8")
    zero = service.read_file(settings, _entry("empty.csv", "po", empty))
    header = service.read_file(settings, _entry("header.csv", "po", only_header))
    assert zero["stats"]["status"] == "empty"
    assert zero["stats"]["code"] == "E_EMPTY_FILE"
    assert header["stats"]["status"] == "header_only"
    assert header["stats"]["code"] == "E_HEADER_ONLY"
    assert zero["stats"]["code"] != header["stats"]["code"]


def test_sample_digests_recorded_in_manifest(run, store, sample_dir):
    """运行清单必须带 run_id 与每个输入文件的 sha256，结果才能复算。"""
    manifest = store.load_manifest(run["batch"]["batch_id"])
    assert manifest["run_id"] == run["batch"]["run_id"]
    names = [item["name"] for item in manifest["inputs"]]
    assert names == [X.ORDERS_FILE, X.GB18030_FILE, X.INVOICES_FILE]
    for item in manifest["inputs"]:
        digest = __import__("hashlib").sha256(
            (sample_dir / item["name"]).read_bytes()).hexdigest()
        assert item["sha256"] == digest
    assert manifest["tolerance"]["amount"] == str(config.AMOUNT_TOLERANCE)


def test_delimiter_and_doc_type_sniffed(sample_dir):
    """分隔符与单据类型都从内容识别，文件名只作同票时的加分项。"""
    text = "发票号;开票日期;采购单号;行号;数量;不含税金额;税率;价税合计;金额单位\n"
    assert sniff.guess_delimiter(text) == ";"
    headers = [sniff.normalize_header(cell) for cell in text.strip().split(";")]
    assert sniff.detect_doc_type(headers, "po") == "inv"
