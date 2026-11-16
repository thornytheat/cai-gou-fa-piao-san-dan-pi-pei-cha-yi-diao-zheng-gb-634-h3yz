"""表头/分隔符/单据类型识别：列名归一后按契约别名匹配。

列名归一：去空白、去 BOM 残留、全角转半角、小写。别名表在
`app/modules/m2_amount/contract.py`，这里只调用它，不自己维护一份。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ... import config

FULLWIDTH_MAP = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
FULLWIDTH_MAP[0x3000] = 0x20


def to_halfwidth(text: str) -> str:
    return str(text or "").translate(FULLWIDTH_MAP)


def normalize_header(text: str) -> str:
    return to_halfwidth(str(text or "")).replace("\ufeff", "").strip().lower()


def guess_delimiter(sample: str) -> str:
    """按首行候选字符出现次数定分隔符；都没有就当逗号。"""
    line = sample.splitlines()[0] if sample.splitlines() else ""
    best, best_count = ",", 0
    for candidate in (",", ";", "\t", "|"):
        count = line.count(candidate)
        if count > best_count:
            best, best_count = candidate, count
    return best


@dataclass
class Sniffed:
    doc_type: str
    delimiter: str
    headers: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"doc_type": self.doc_type, "delimiter": self.delimiter,
                "headers": list(self.headers)}


def headers_of_line(line: str, delimiter: str) -> list[str]:
    from ...modules.m1_ingest.reader import split_cells
    return split_cells(line, delimiter)


def detect_doc_type(headers: list[str], hint: str | None = None) -> str:
    """按表头命中数挑单据类型；文件名只作同等票数时的加分项。"""
    from ..m2_amount import contract

    normalized = [normalize_header(h) for h in headers]
    scores: dict[str, int] = {}
    for doc_type in config.DOC_TYPES:
        index = contract.match_headers(normalized, doc_type)
        hit = len(index)
        weights = sum(1 for name in index if contract.is_key_column(doc_type, name))
        scores[doc_type] = hit * 10 + weights
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if hint in scores and scores[hint] == ranked[0][1]:
        return hint
    if ranked[0][1] == 0:
        return hint or "po"
    return ranked[0][0]
