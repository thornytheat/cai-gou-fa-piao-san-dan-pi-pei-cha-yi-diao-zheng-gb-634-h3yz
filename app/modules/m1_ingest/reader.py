"""流式 CSV 读取：按记录（不是按物理行）编号，保留原始行号与分段号。

用 csv 模块逐条 yield，不整表读入内存；引号内含换行由 csv 处理成一条记录，
`record_no` 是逻辑记录序号，`line_no` 是这条记录起始的物理行号。
重复表头当分段边界（分段导出），空行只计数不产生记录。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from typing import Iterator

MAX_RECORDS = 20000


@dataclass
class Record:
    record_no: int
    line_no: int
    cells: list


@dataclass
class ParsedTable:
    headers: list
    records: list = field(default_factory=list)
    segment_count: int = 1
    blank_count: int = 0
    header_repeat_count: int = 0
    truncated_tail: bool = False
    width: int = 0

    @property
    def record_count(self) -> int:
        return len(self.records)


def split_cells(line: str, delimiter: str) -> list[str]:
    reader = csv.reader([line], delimiter=delimiter)
    for row in reader:
        return [cell.strip() for cell in row]
    return []


def iter_records(handle, delimiter: str) -> Iterator[Record]:
    """按记录流式读取；handle 可以是任意按行迭代的文本流。"""
    reader = csv.reader(handle, delimiter=delimiter)
    record_no = 0
    physical = 0
    for row in reader:
        physical += 1 + _embedded_newlines(row)
        record_no += 1
        yield Record(record_no=record_no, line_no=physical - _embedded_newlines(row),
                     cells=[cell.strip() for cell in row])


def _embedded_newlines(row: list[str]) -> int:
    return sum(cell.count("\n") for cell in row)


def is_blank(cells: list[str]) -> bool:
    return not any(str(cell).strip() for cell in cells)


def parse_table(text: str, delimiter: str = ",", *, max_records: int = MAX_RECORDS) -> ParsedTable:
    """解析整段文本；headers 之前允许出现空行。"""
    table = ParsedTable(headers=[])
    if not text:
        return table
    trailing_newline = text.endswith("\n") or text.endswith("\r")
    records: list[Record] = []
    pending_header = True
    for record in iter_records(text.splitlines(keepends=True), delimiter):
        if pending_header:
            if is_blank(record.cells):
                table.blank_count += 1
                continue
            table.headers = record.cells
            table.width = len(record.cells)
            pending_header = False
            continue
        if is_blank(record.cells):
            table.blank_count += 1
            continue
        if _same_header(record.cells, table.headers):
            table.header_repeat_count += 1
            table.segment_count += 1
            continue
        records.append(record)
        if len(records) >= max_records:
            break
    table.records = records
    if records and not trailing_newline:
        last = records[-1]
        if len([cell for cell in last.cells if cell != ""]) < table.width:
            table.truncated_tail = True
    return table


def _same_header(cells: list[str], headers: list[str]) -> bool:
    if len(cells) != len(headers):
        return False
    return all(str(a).strip() == str(b).strip() for a, b in zip(cells, headers))
