"""invoice3way 全局配置：单据列契约、金额/税率规则、容差、单位表、阶段名。

规则唯一来源，其他模块只读不写：
- 金额一律 Decimal，单位「元」，最小刻度 0.01；数量 4 位小数；
- 金额容差 0.02 元，数量容差 0，税率区间 (0, 0.5]；
- 三单匹配键 = (采购单号, 行号)，两者都按字符串保形，不做 int() 化。
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHINA_TZ = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
UTC = dt.timezone.utc

RULE_VERSION = "invoice3way/1.2.0"
AMOUNT_QUANT = Decimal("0.01")
QTY_QUANT = Decimal("0.0001")
RATE_QUANT = Decimal("0.000001")
MONEY_UNIT = "元"
AMOUNT_TOLERANCE = Decimal("0.02")
QTY_TOLERANCE = Decimal("0")
MAX_TAX_RATE = Decimal("0.5")
OVER_RECEIPT_RATIO = Decimal("1.05")

# 阶段：采信 → 金额 → 质量 → 匹配 → 对账 → 导出
STAGES = ("ingest", "amount", "quality", "match", "reconcile", "export")
STAGE_LABELS = {
    "ingest": "单据接入",
    "amount": "金额税率",
    "quality": "质量隔离",
    "match": "三单匹配",
    "reconcile": "对账",
    "export": "调整单",
    "audit": "复核",
}
STAGE_FLOW = "采信 → 金额 → 质量 → 匹配 → 对账 → 调整单"

# 单据类型
DOC_TYPES = ("po", "grn", "inv")
DOC_LABELS = {"po": "采购订单", "grn": "入库单", "inv": "发票"}
DOC_SHORT = {"po": "PO", "grn": "GRN", "inv": "INV"}

# 编码回退链：gb18030 是 gbk/gb2312 的超集，放最后兜底
ENCODING_CHAIN = ("utf-8-sig", "utf-8", "gb18030")
DEFAULT_CHARSET = "utf-8"
CHARSET_ALIASES = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb18030": "gb18030",
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "utf8sig": "utf-8-sig",
}

# 空值哨兵：集中定义；哨兵 → None（缺失），与 0（真值）、空串（空字段）三态分开
SENTINELS = frozenset(
    {"", "-", "--", "—", "——", "/", "n/a", "na", "null", "none", "nil",
     "未知", "无", "暂无", "待定", "不适用", "未开票"}
)

# 金额单位 → 元换算系数
UNIT_TO_YUAN = {
    "元": Decimal("1"),
    "块": Decimal("1"),
    "千元": Decimal("1000"),
    "千": Decimal("1000"),
    "万元": Decimal("10000"),
    "万": Decimal("10000"),
}

# 列契约：列名 / 中文标签 / 类型 / 必填 / 历史别名（schema 演进用）
COLUMNS = {
    "po": (
        ("po_no", "采购单号", "text", True, ("po", "purchase_no", "采购订单号", "单号")),
        ("line_no", "行号", "text", True, ("line", "行", "行项目")),
        ("item_code", "物料编码", "text", False, ("item", "material", "物料号")),
        ("item_name", "物料名称", "text", False, ("name", "品名")),
        ("qty", "订单数量", "qty", True, ("quantity", "数量", "订购数量")),
        ("unit_price", "不含税单价", "amount", True, ("price", "单价")),
        ("amount_unit", "金额单位", "unit", True, ("unit", "单位")),
        ("tax_rate", "税率", "rate", True, ("rate", "增值税率")),
    ),
    "grn": (
        ("grn_no", "入库单号", "text", True, ("grn", "receipt_no", "入库号")),
        ("po_no", "采购单号", "text", True, ("po", "purchase_no", "采购订单号")),
        ("line_no", "行号", "text", True, ("line", "行")),
        ("qty_received", "实收数量", "qty", True, ("qty", "quantity", "收货数量")),
        ("received_at", "入库日期", "date", True, ("date", "in_date", "入库时间")),
        ("warehouse", "仓库", "text", False, ("wh", "库位")),
    ),
    "inv": (
        ("invoice_no", "发票号", "text", True, ("inv_no", "invoice", "发票号码")),
        ("invoice_date", "开票日期", "date", True, ("date", "bill_date", "开票时间")),
        ("po_no", "采购单号", "text", True, ("po", "purchase_no")),
        ("line_no", "行号", "text", True, ("line", "行")),
        ("qty", "开票数量", "qty", True, ("quantity", "数量")),
        ("amount_ex_tax", "不含税金额", "amount", True, ("amount_ex", "net_amount", "未税金额")),
        ("tax_rate", "税率", "rate", True, ("rate", "增值税率")),
        ("amount_inc_tax", "价税合计", "amount", True, ("amount_inc", "gross_amount", "含税金额")),
        ("amount_unit", "金额单位", "unit", True, ("unit", "单位")),
    ),
}

# 匹配结论与差异类别；DIFF_RANK 越小越优先，判定不依赖规则书写顺序
VERDICTS = ("matched", "diff", "missing")
VERDICT_LABELS = {"matched": "匹配", "diff": "差异", "missing": "缺单"}
DIFF_KINDS = ("conflict", "missing_po", "missing_grn", "missing_inv", "tax", "price", "qty")
DIFF_LABELS = {
    "conflict": "单据冲突",
    "missing_po": "缺采购订单",
    "missing_grn": "缺入库单",
    "missing_inv": "缺发票",
    "tax": "税额差",
    "price": "价差",
    "qty": "量差",
}
DIFF_RANK = {kind: rank for rank, kind in enumerate(DIFF_KINDS)}

# 样例文件（脏样例 3 类以上：编码、BOM、千分位、全角、哨兵、万元、重复表头、
# 列数不齐、截断、重复发票号、税率混写、科学计数法）
SAMPLE_FILES = (
    {"name": "orders_dirty.csv", "doc_type": "po",
     "note": "BOM + 千分位 + 全角数字 + 哨兵 + 万元 + 重复表头 + 多一列 + 空行"},
    {"name": "receipts_gb18030.csv", "doc_type": "grn",
     "note": "GB18030 编码 + CRLF + 重复表头 + 引号内换行 + 末行截断"},
    {"name": "invoices_dirty.csv", "doc_type": "inv",
     "note": "重复发票号不同金额 + 税率 13%/0.13 混写 + 三位小数 + 哨兵 + 科学计数法"},
    {"name": "orders_clean.csv", "doc_type": "po",
     "note": "干净对照样例，与脏样例走同一条代码路径"},
)


@dataclass(frozen=True)
class Settings:
    """运行参数；测试用 replace() 覆盖，不读环境变量的分支。"""

    db_path: str = str(PROJECT_ROOT / "var" / "invoice3way.sqlite")
    sample_dir: str = str(PROJECT_ROOT / "sample_data")
    export_dir: str = str(PROJECT_ROOT / "var" / "export")
    rule_version: str = RULE_VERSION
    eval_date: dt.date = dt.date(2026, 11, 15)

    @classmethod
    def from_env(cls) -> "Settings":
        base = cls()
        db_path = os.getenv("INVOICE3WAY_DB") or base.db_path
        sample_dir = os.getenv("INVOICE3WAY_SAMPLES") or base.sample_dir
        export_dir = os.getenv("INVOICE3WAY_EXPORT") or base.export_dir
        eval_raw = os.getenv("EVAL_DATE") or ""
        eval_date = base.eval_date
        if eval_raw:
            eval_date = dt.date.fromisoformat(eval_raw)
        return replace(cls(), db_path=db_path, sample_dir=sample_dir,
                       export_dir=export_dir, eval_date=eval_date)

    @property
    def data_dir(self) -> Path:
        return Path(self.db_path).resolve().parent
