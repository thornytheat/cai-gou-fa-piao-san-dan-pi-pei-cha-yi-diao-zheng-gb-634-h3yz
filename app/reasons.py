"""原因码唯一来源：跨模块共享，页面 / 导出 / 测试都从这里取文案。

一个原因码 = 级别 + 所属阶段 + 中文结论 + 复核动作。级别只有三档：
info（记录，不影响结论）、warn（提示，需人工看一眼）、error（已隔离，不进匹配）。
"""
from __future__ import annotations

from typing import NamedTuple

INFO = "info"
WARN = "warn"
ERROR = "error"
LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}
LEVEL_LABELS = {ERROR: "隔离", WARN: "提示", INFO: "记录"}


class Reason(NamedTuple):
    code: str
    level: str
    stage: str
    title: str
    hint: str


def _r(code, level, stage, title, hint):
    return Reason(code, level, stage, title, hint)


REASONS = {
    # ---- M1 单据接入 ----
    "E_ENCODING_FALLBACK": _r(
        "E_ENCODING_FALLBACK", WARN, "ingest",
        "按声明编码解码失败，已回退到备用编码", "确认来源系统导出编码"),
    "E_BOM_STRIPPED": _r(
        "E_BOM_STRIPPED", INFO, "ingest",
        "文件头带 BOM，已剥离", "无需处理"),
    "E_DECODE_FAILED": _r(
        "E_DECODE_FAILED", ERROR, "ingest",
        "回退链内所有编码都无法解码", "换编码重导该文件"),
    "E_HEADER_UNKNOWN": _r(
        "E_HEADER_UNKNOWN", ERROR, "ingest",
        "必需列在表头中找不到", "补列或改列名后重导"),
    "E_COLUMN_COUNT": _r(
        "E_COLUMN_COUNT", ERROR, "ingest",
        "该行列数与表头不一致", "检查分隔符或多余列"),
    "E_TRUNCATED_TAIL": _r(
        "E_TRUNCATED_TAIL", ERROR, "ingest",
        "末行不完整，疑似导出被截断", "重新导出完整文件"),
    "E_BLANK_ROW": _r(
        "E_BLANK_ROW", INFO, "ingest",
        "空行，已跳过", "无需处理"),
    "E_HEADER_REPEAT": _r(
        "E_HEADER_REPEAT", INFO, "ingest",
        "重复表头，按分段导出处理", "无需处理"),
    "E_EMPTY_FILE": _r(
        "E_EMPTY_FILE", WARN, "ingest",
        "文件 0 字节", "确认导出是否成功"),
    "E_HEADER_ONLY": _r(
        "E_HEADER_ONLY", WARN, "ingest",
        "只有表头没有数据行", "确认导出范围"),
    # ---- M2 金额与税率 ----
    "E_AMOUNT_SENTINEL": _r(
        "E_AMOUNT_SENTINEL", ERROR, "amount",
        "金额/数量为占位符，视为缺失", "向业务确认实际值"),
    "E_AMOUNT_UNPARSABLE": _r(
        "E_AMOUNT_UNPARSABLE", ERROR, "amount",
        "金额无法解析为数字", "检查千分位与全角字符"),
    "E_UNIT_UNKNOWN": _r(
        "E_UNIT_UNKNOWN", ERROR, "amount",
        "金额单位不在单位表内", "改成元/千元/万元"),
    "E_RATE_UNPARSABLE": _r(
        "E_RATE_UNPARSABLE", ERROR, "amount",
        "税率无法解析", "改成 13% 或 0.13 写法"),
    "E_RATE_RANGE": _r(
        "E_RATE_RANGE", ERROR, "amount",
        "税率超出 (0, 0.5] 区间", "核对税率"),
    "E_DATE_UNPARSABLE": _r(
        "E_DATE_UNPARSABLE", ERROR, "amount",
        "日期无法解析", "改成 2026-11-03 写法"),
    "E_QTY_NONPOSITIVE": _r(
        "E_QTY_NONPOSITIVE", ERROR, "amount",
        "数量小于等于 0", "核对订单/入库数量"),
    "E_KEY_SCIENTIFIC": _r(
        "E_KEY_SCIENTIFIC", ERROR, "amount",
        "单号为科学计数法，原值不可靠", "按文本格式重新导出该列"),
    # ---- M3 质量与隔离 ----
    "E_DUP_KEY_CONFLICT": _r(
        "E_DUP_KEY_CONFLICT", ERROR, "quality",
        "同一发票号出现金额不同的多行", "确认作废或补开，保留哪条需人工裁定"),
    "E_DUP_KEY_SAME": _r(
        "E_DUP_KEY_SAME", INFO, "quality",
        "同一发票号重复且金额一致，保留首行", "无需处理"),
    "E_OVER_RECEIPT": _r(
        "E_OVER_RECEIPT", WARN, "quality",
        "实收数量超过订单数量 5%", "确认是否超收"),
    "E_MISSING_FIELD": _r(
        "E_MISSING_FIELD", ERROR, "quality",
        "必填列为空", "补齐后重导"),
    "E_NEGATIVE_AMOUNT": _r(
        "E_NEGATIVE_AMOUNT", ERROR, "quality",
        "金额为负", "红字发票需走单独路径"),
    # ---- M7 复核台账 ----
    "E_RED_SENTINEL": _r(
        "E_RED_SENTINEL", ERROR, "audit",
        "红字发票金额为占位符", "向对方索取实际红字金额"),
    "E_RED_UNPARSABLE": _r(
        "E_RED_UNPARSABLE", ERROR, "audit",
        "红字金额无法解析", "核对括号负数、全角负号与单位"),
    "E_RED_NOT_NEGATIVE": _r(
        "E_RED_NOT_NEGATIVE", ERROR, "audit",
        "红字清单出现非负数", "红字金额必须为负，确认是否贴错行"),
    "E_RED_UNMATCHED": _r(
        "E_RED_UNMATCHED", WARN, "audit",
        "原发票号在本批发票里找不到", "确认原发票是否已导入或号码录错"),
    "E_RED_OVER": _r(
        "E_RED_OVER", WARN, "audit",
        "冲减金额超过原发票金额", "核对是否重复开红字"),
    "E_RED_PARTIAL": _r(
        "E_RED_PARTIAL", INFO, "audit",
        "部分冲红，已记剩余可冲金额", "无需处理"),
    "E_MASTER_INCOMPLETE": _r(
        "E_MASTER_INCOMPLETE", WARN, "audit",
        "供应商主数据行缺采购单号，已跳过", "补齐采购单号后重导主数据"),
    "E_MASTER_DUP": _r(
        "E_MASTER_DUP", WARN, "audit",
        "同一采购单在主数据里登记多次", "按编码最小的记录取用，确认后清理主数据"),
    "E_MASTER_HEADER": _r(
        "E_MASTER_HEADER", WARN, "audit",
        "供应商主数据缺列", "补列后重导主数据"),
    "E_CREDIT_HEADER": _r(
        "E_CREDIT_HEADER", WARN, "audit",
        "红字清单缺列", "补列后重导红字清单"),
    "E_FILE_MISSING": _r(
        "E_FILE_MISSING", WARN, "audit",
        "复核台账文件不存在", "把主数据与红字清单放回样例目录"),
    "E_BAD_DUP_KIND": _r(
        "E_BAD_DUP_KIND", ERROR, "audit",
        "重复组类别不在清单内", "用同号不同额/同号同额/同额不同号"),
    # ---- M5 批次 ----
    "E_STALE_BATCH": _r(
        "E_STALE_BATCH", ERROR, "batches",
        "提交的规则版本早于已完成的批次，拒绝覆盖", "用当前版本重跑"),
    "E_BATCH_NOT_FOUND": _r(
        "E_BATCH_NOT_FOUND", ERROR, "batches",
        "批次号不存在", "在批次历史里选一个批次"),
    "E_UNKNOWN_FILE": _r(
        "E_UNKNOWN_FILE", ERROR, "ingest",
        "文件不在样例清单内", "从样例清单选择文件"),
}


def get(code: str) -> Reason:
    return REASONS.get(code) or _r(code, WARN, "ingest", code, "")


def level_of(code: str) -> str:
    return get(code).level


def is_blocking(code: str) -> bool:
    return get(code).level == ERROR


def catalog() -> list[dict]:
    rows = []
    for code in sorted(REASONS):
        reason = REASONS[code]
        rows.append({
            "code": reason.code,
            "level": reason.level,
            "level_label": LEVEL_LABELS.get(reason.level, reason.level),
            "stage": reason.stage,
            "title": reason.title,
            "hint": reason.hint,
        })
    return rows


def distribution(rows: list[dict]) -> list[dict]:
    """按原因码计数，用于隔离区的分布条；顺序稳定，不随输入顺序变化。"""
    counts: dict[str, int] = {}
    for row in rows:
        code = row.get("code") or "E_UNKNOWN"
        counts[code] = counts.get(code, 0) + 1
    out = []
    for code in sorted(counts, key=lambda c: (LEVEL_ORDER[level_of(c)], c)):
        reason = get(code)
        out.append({
            "code": code,
            "count": counts[code],
            "level": reason.level,
            "level_label": LEVEL_LABELS.get(reason.level, reason.level),
            "title": reason.title,
            "hint": reason.hint,
        })
    return out
