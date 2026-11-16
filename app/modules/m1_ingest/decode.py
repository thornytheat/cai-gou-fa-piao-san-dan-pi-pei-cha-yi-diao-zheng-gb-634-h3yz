"""编码探测与回退链：先剥 BOM，再按 utf-8 → gb18030 严格解码。

声明编码只作参考，实际用了哪个由字节决定；把两者都记进文件诊断，
页面上能直接看到「声明 utf-8 / 实际 gb18030」这一格。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ... import config, errors

BOM_UTF8 = b"\xef\xbb\xbf"


@dataclass
class Decoded:
    text: str
    encoding: str
    declared: str
    bom_stripped: bool = False
    codes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "encoding": self.encoding,
            "declared_charset": self.declared,
            "bom_stripped": self.bom_stripped,
            "codes": list(self.codes),
        }


def normalize_charset(value: str | None) -> str:
    if not value:
        return config.DEFAULT_CHARSET
    return config.CHARSET_ALIASES.get(value.strip().lower(), value.strip().lower())


def decode_bytes(raw: bytes, declared: str | None = None) -> Decoded:
    """严格解码：任一编码抛 UnicodeDecodeError 就换下一个，全失败才算不可用。"""
    declared_norm = normalize_charset(declared)
    bom = raw.startswith(BOM_UTF8)
    body = raw[len(BOM_UTF8):] if bom else raw
    last_error: Exception | None = None
    for encoding in config.ENCODING_CHAIN:
        if encoding == "utf-8-sig":
            # BOM 已单独剥离，这里只用它做一次严格 utf-8 校验
            encoding = "utf-8"
        try:
            text = body.decode(encoding)
        except UnicodeDecodeError as exc:  # noqa: PERF203
            last_error = exc
            continue
        codes: list[str] = []
        if bom:
            codes.append("E_BOM_STRIPPED")
        if encoding != declared_norm:
            codes.append("E_ENCODING_FALLBACK")
        return Decoded(text=text, encoding=encoding, declared=declared_norm,
                       bom_stripped=bom, codes=codes)
    raise errors.BadRequest(
        f"回退链 {config.ENCODING_CHAIN} 都无法解码：{last_error}",
        code="E_DECODE_FAILED",
        detail={"declared": declared_norm, "bytes": len(raw)},
    )
