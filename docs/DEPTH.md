# 深度登记 invoice3way（采购发票三单匹配与差异调整）

- 规模：中型（7 个功能模块 / 7 个页面 / 109 条测试函数，pytest 112 条用例）
- 核心规则一：**三单匹配**——按 `(采购单号, 行号)` 把采购订单、入库单、发票分组，命中规则全部收集后按固定优先级（缺单 > 税额差 > 价差 > 量差）取结论，判定结果不随规则书写顺序变化。
- 核心规则二：**差异归因与尾差**——差异拆成量差 / 价差 / 税额差三类金额，调整单按采购单的发票不含税金额加权，用最大余额法分摊到分，分摊合计严格等于差异总额。
- 核心规则三：**复核台账**——跨批次重复报销按「同号不同额 / 同号同额 / 同额不同号」三类报；红字行不参与匹配但进供应商汇总做冲减；分批入库按批相加；发票税率与采购单不同按发票税率重算应付。
- 使用者与工作流：财务共享中心 9 人，月均 1200 张发票，原先把三张表贴进同一份表格逐行对数。现在一次导入三份脏 CSV，得到「匹配 / 差异 / 缺单」三类结论、可复核的证据行号和一张可导出的调整单，再在复核台账里看重复报销、红字冲减与供应商汇总。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 单据接入 | 选样例文件导入，看到逐行诊断与分段 | POST /api/import、页面 / | `app/modules/m1_ingest/handler.py` `app/modules/m1_ingest/service.py` `app/modules/m1_ingest/decode.py` `app/modules/m1_ingest/reader.py` `app/modules/m1_ingest/sniff.py` | `tests/test_ingest.py` |
| M2 金额税率 | 查列契约，单格试算归一结果 | GET /api/contract、GET /api/contract/preview | `app/modules/m2_amount/handler.py` `app/modules/m2_amount/service.py` `app/modules/m2_amount/contract.py` `app/modules/m2_amount/coerce.py` `app/modules/m2_amount/taxmath.py` | `tests/test_amount.py` |
| M3 质量隔离 | 看原因码分布、逐行证据、导出隔离 CSV | GET /api/quarantine、页面 / | `app/modules/m3_quality/handler.py` `app/modules/m3_quality/service.py` `app/modules/m3_quality/rules.py` `app/modules/m3_quality/quarantine.py` `app/modules/m3_quality/metrics.py` | `tests/test_quality.py` |
| M4 三单匹配 | 左右分栏看三单对照、差异明细与四账 | GET /api/trios、GET /api/matches、GET /api/diffs、GET /api/reconcile | `app/modules/m4_match/handler.py` `app/modules/m4_match/service.py` `app/modules/m4_match/compare.py` `app/modules/m4_match/diffattr.py` `app/modules/m4_match/reconcile.py` | `tests/test_match.py` |
| M5 批次重跑 | 查历史、核对输入依据、按留存原件重算、失败重试 | GET /api/batches、POST /api/batches/{id}/rerun、POST /api/batches/{id}/retry | `app/modules/m5_batches/handler.py` `app/modules/m5_batches/service.py` `app/modules/m5_batches/state.py` `app/modules/m5_batches/resume.py` `app/modules/m5_batches/history.py` | `tests/test_batches.py` `tests/test_batch_inputs.py` |
| M6 调整单导出 | 看调整单与守恒数，下载 CSV / 清单 | GET /api/export、GET /api/export/download | `app/modules/m6_export/handler.py` `app/modules/m6_export/service.py` `app/modules/m6_export/adj.py` `app/modules/m6_export/csv_out.py` `app/modules/m6_export/manifest_out.py` | `tests/test_export.py` |
| M7 复核台账 | 看跨批次重复组、红字冲减与供应商汇总 | GET /api/audit、GET /api/audit/dups、GET /api/audit/vendors、页面 /audit | `app/modules/m7_audit/handler.py` `app/modules/m7_audit/service.py` `app/modules/m7_audit/dups.py` `app/modules/m7_audit/redline.py` `app/modules/m7_audit/rollup.py` | `tests/test_audit_dups.py` `tests/test_audit_redline.py` `tests/test_audit_vendors.py` |

端到端：`tests/test_e2e.py`（样例文件 → 导入 → 匹配 → 四账 → 调整单 → 导出，含 4xx 失败输入与契约接口）。

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 入库单声明 utf-8，字节其实是 GB18030 | `open()` 崩或整份乱码，列名对不上 | 严格解码失败按 utf-8 → gb18030 回退，声明与实际都记进文件诊断 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_charset_fallback_decodes_gb18030` |
| TRAP-02 | 采购订单文件头带 UTF-8 BOM | 首列名变 `\ufeffpo_no`，按列名取值全空 | 解码前剥 BOM 并记 `E_BOM_STRIPPED` | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom_stripped_before_header_mapping` |
| TRAP-05 | 某行多一列（导出带了备注列） | 静默补空或错列错位 | 严格列数校验，整行进隔离并留原值 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_column_count_mismatch_quarantined` |
| TRAP-06 | `1,234.50`、`１２３４．５０` | `float()` 抛错或把全角数字当字符串 | 全角转半角 + 去千分位 + 单位后缀归一后再 Decimal 化 | `app/modules/m2_amount/coerce.py` | `tests/test_amount.py::test_thousands_and_fullwidth_amount` |
| TRAP-07 | 单价写 `-`、金额写 `N/A` | 被当成 0 参与合计，合计虚低 | 哨兵集中定义，返回 None 三态，与真值 0 分开 | `app/reasons.py`、`app/modules/m2_amount/coerce.py` | `tests/test_amount.py::test_sentinel_amount_three_state` |
| TRAP-09 | 发票号被导出成 `4.500123E+9` | `int()` 化后丢位，两个不同发票号撞成一个 | 单号全列按字符串保形，科学计数法原值保留并报 `E_KEY_SCIENTIFIC` | `app/modules/m2_amount/coerce.py`、`app/modules/m6_export/csv_out.py` | `tests/test_amount.py::test_scientific_notation_key_preserved` |
| TRAP-10 | 上千行金额累加 | float 累加后合计差几分 | 一律 Decimal，只在最后一步量化 0.01 | `app/modules/m2_amount/taxmath.py` | `tests/test_amount.py::test_decimal_sum_has_no_float_error` |
| TRAP-14 | 同一发票号两行金额不同（1841.00 / 1900.00） | 后写覆盖，或整批丢弃 | 冲突归因，两行都进隔离区并列出原值，不猜哪条为准 | `app/modules/m3_quality/rules.py` | `tests/test_quality.py::test_duplicate_invoice_no_conflict_isolated` |
| TRAP-15 | 一张单用元、一张单用万元 | 合计差 10000 倍 | 单位表集中在配置，先折算成元再算，单位随数字展示 | `app/config.py`、`app/modules/m2_amount/coerce.py` | `tests/test_amount.py::test_amount_unit_yuan_and_wan_normalized` |
| TRAP-16 | 入库单末行只有 5 列且没有换行 | 半行被当有效入库，凭空多一条 | 完整性校验：末行无换行且列数不足判 `E_TRUNCATED_TAIL` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_truncated_tail_detected` |
| TRAP-19 | 一行同时命中量差与价差 | 取代码里先写的那条规则，调整顺序结论就变 | 命中规则全收集，按 `DIFF_RANK` 取优先；打乱输入顺序结论不变 | `app/modules/m4_match/compare.py` | `tests/test_match.py::test_rule_priority_order_independent` |
| TRAP-24 | 241.28 元按三个采购单加权分摊 | 各自四舍五入后合计多一分或少一分 | 最大余额法：先向下取整到分，尾差按小数部分降序补，合计守恒 | `app/modules/m6_export/adj.py` | `tests/test_export.py::test_largest_remainder_conserves_total` |
| TRAP-26 | 旧规则版本提交覆盖新批次 | 无条件覆盖，历史数据被改写 | 批次键只认输入摘要 + 评估日；版本更旧的提交 409 拒绝 | `app/modules/m5_batches/resume.py` | `tests/test_batches.py::test_stale_commit_rejected` |
| TRAP-27 | 红字发票（负数行）只在汇总里出现 | 汇总只加正数发票，净额虚高；或把红字取绝对值当增加 | 红字不进匹配键；按「已冲抵 / 未冲抵」分流，已冲抵进净额、未冲抵挂待确认，超额单独计数 | `app/modules/m7_audit/redline.py`、`app/modules/m7_audit/rollup.py` | `tests/test_audit_redline.py::test_red_total_reduces_supplier_net` |
| TRAP-28 | 一个采购行分三批入库（40 + 18 + 2） | 只取最后一批或最大一批，未入库数量与超收判断都错 | 按入库日期 + 单号排序逐批列出，实收 = 各批相加，未入库 = 订单数量 − 实收 | `app/modules/m7_audit/rollup.py` | `tests/test_audit_vendors.py::test_grn_multi_batch_sum_conserves` |
| TRAP-29 | 源表每天更新，历史批次重跑却按文件名重新读 | 重算读到当天更正件，把更正件冒充原始凭证，历史结果被悄悄改写 | 采信时原件字节随批次落库（`batch_inputs`）；重算、重试只读留存副本与原评估日 | `app/store.py`、`app/modules/m1_ingest/service.py`、`app/modules/m5_batches/service.py` | `tests/test_batch_inputs.py::test_rerun_uses_retained_original_after_source_replaced` |
| TRAP-30 | 源表采信后被替换或移走 | 重算报文件不存在，或按新文件算出另一个结果 | 原件的字节、sha256、来源路径都留存，源表与重算解耦 | 同上 | `tests/test_batch_inputs.py::test_rerun_survives_source_removed` |
| TRAP-31 | 两个并发请求同时导入同一份文件 | 各建一个批次、各落一次账，同一输入出现两份结果 | 事务内原子领取：先查 done、再查 running 租约；只有一方拿到 claim_token 能写库 | `app/store.py::claim_run` | `tests/test_batch_inputs.py::test_parallel_imports_write_one_batch`、`test_concurrent_claim_writes_once` |
| TRAP-32 | 输入已留存、结果写到一半进程中断 | 半批数据可见，或重跑重新读源表拿到更正件 | 结果与 done 状态同一事务提交；失败/中断批次只留原件，结果表为空；租约过期可接续 | `app/store.py::save_run`、`app/modules/m5_batches/resume.py` | `tests/test_batch_inputs.py::test_interrupted_run_is_invisible_and_recoverable` |
| TRAP-33 | 输入留存上线前的旧批次点重算 | 按当前文件补造一份「原件」，历史凭证被伪造 | 缺留存原件的批次 422 `E_INPUT_MISSING`，明确拒绝，不读现有文件 | `app/modules/m5_batches/resume.py::assert_inputs_saved` | `tests/test_batch_inputs.py::test_missing_inputs_rejected_without_backfill` |

## 对账规则

- 行账：数据记录数（48）= 采纳（41）+ 行级隔离（7）；空行 1 行、重复表头 2 行单独列出，不并进隔离
- 单账：三单键数（16）= 匹配（8）+ 差异（3）+ 缺单（5）
- 金额账：发票价税合计（402213.25 元）= 匹配合计（391489.07）+ 差异合计（8269.82）+ 缺单合计（2454.36），缺单也有发票金额，必须计入
- 隔离账：隔离行数（11）= 原因码分布合计（11）
- 四账在 `GET /api/reconcile`、`/export` 页面与 `reconcile.json` 导出中显示，差额非 0 会显式为「四账不平」
- 调整账：调整单合计（241.28）= 已分摊（241.28）+ 尾差（0.00）

## 输入留存与批次绑定（M5）

- 留存：采信时把每份文件的原始字节、`sha256`、来源路径、留存时间与次序写进 `batch_inputs`，
  与批次行同一事务提交；批次页「输入依据」展示这些字段，并核对留存摘要与 `input_digest` 是否一致。
- 分层：**新导入**按文件名读当天文件；**重算**只读指定批次的留存原件与原评估日；
  **失败重试**只读失败批次自己的留存原件。三者共用同一流水线，区别只在输入来源。
- 更正件：新导入读到更正件时摘要变化 → 批次键变化 → 新批次；旧批次的原件、评估日与结果都不动，
  所以更正件不会冒充原始凭证。
- 评估日：批次创建时写入 `eval_date`；重算沿用原值，换评估日只有新导入才会产生另一个批次键。
- 并发：`claim_run` 在 `BEGIN IMMEDIATE` 里先查同键同版本 done（有则复用）、再查 running；
  租约内（默认 900s）的 running 一律拒绝（`E_BATCH_RUNNING`），落账只发生一次；
  写库时校验 `claim_token`，执行权被接管则 `E_CLAIM_LOST`。
- 中断恢复：结果表与 `done` 状态同一事务提交，未完成批次任何页面/接口都取不到结果；
  租约过期的 running 视为进程中断，可按留存原件接续同一批次号，`attempts` 递增。
- 缺原件：`batch_inputs` 为空的旧批次重算返回 422 `E_INPUT_MISSING`，不读当前文件补造。

## 复核规则（M7）

- 净额账：发票价税合计（402213.25）+ 已冲抵红字（-98913.34）= 供应商净额合计（303299.91），差额非 0 显式写出来
- 行账：本批发票行数（14）= Σ 供应商发票行（14），每张发票行必须落到一个供应商，含「未登记」
- 入库账：入库行实收合计（764）= 对上订单的实收（724）+ 无订单入库（40）；订单 780，未入库 56（PO202611003 差 2、PO202611009 差 25、PO202611014 差 35，PO202611002 超收 6）
- 红字：清单 9 行数据 = 已冲抵 6 行（4 部分冲红 + 2 超额冲抵）+ 未冲抵 1 行 + 拒绝 2 行；未冲抵挂待确认（-500.59），不进净额
- 红字写法：`(250.00)`、`－76.00`、`-3,487.46`、`-0.678万元` 都是负数写法，先归符号与单位再算
- 供应商归集：采购单号在主数据里查不到记「未登记」；同一编码多名称取名称排序第一个当代表名
- 税率变更：发票税率 ≠ 采购单税率时，应付按发票税率重算，用采购单税率推出来的差额记成迁移额，不算差错
- 分批入库：一采购行多批时逐批列出，实收 = 各批相加；实收大于订单数量 × 1.05 判超收
- 无订单入库：入库单有行、采购订单行被隔离或缺失时，这批只记「无订单」，不算到任何采购行的实收（样例里 GRN-9008 的 40 件属此列）

## 反例与不变量

- 反例（朴素实现会挂）：`tests/test_amount.py::test_decimal_sum_has_no_float_error`（float 累加 1000 行 0.07 得不到 70.00）、`tests/test_match.py::test_naive_first_hit_wins_would_flip`（先命中即结论会被规则顺序左右）、`tests/test_audit_redline.py::test_red_total_reduces_supplier_net`（只加正数会得 265550.00，冲减后是 252272.50）、`tests/test_audit_vendors.py::test_grn_multi_batch_sum_conserves`（只取最后一批得 2，按批相加得 60）。
- 不变量：`tests/test_batches.py::test_idempotent_rerun_adds_no_rows`（幂等，重跑行数不变）、`tests/test_batch_inputs.py::test_parallel_imports_write_one_batch`（并发只落一次账）、`tests/test_batch_inputs.py::test_rerun_uses_retained_original_after_source_replaced`（重算只认原件）、`tests/test_export.py::test_largest_remainder_conserves_total`（守恒，分摊合计等于总额）、`tests/test_match.py::test_four_accounts_balanced`（对账守恒）、`tests/test_audit_vendors.py::test_net_conserves_invoice_total`（Σ 供应商净额 = 发票合计 + 已冲抵红字）、`tests/test_audit_vendors.py::test_grn_multi_batch_sum_conserves`（分批入库各批相加守恒）。
- 边界：`tests/test_amount.py::test_rate_upper_bound_inclusive`（税率上界 0.5 含端点）、`tests/test_match.py::test_qty_tolerance_zero_boundary`（数量容差 0，差 0.0001 也算量差）、`tests/test_quality.py::test_three_rows_same_number_all_conflict`（同号三行全部进冲突）、`tests/test_batches.py::test_different_eval_date_creates_new_batch`（换评估日建新批次）。
- 端到端：`tests/test_e2e.py::test_full_pipeline_end_to_end`。

## 已知不覆盖

- 发票表内的负数行仍由 M3 按 `E_NEGATIVE_AMOUNT` 整行隔离，既不进匹配也不进汇总；红冲走独立红字清单（`sample_data/credit_notes.csv`），两边不互相替代。
- 分批入库规则支持一个采购行多批入库，但样例里每个采购行只有一批；多批场景由构造行测试 `tests/test_audit_vendors.py::test_grn_multi_batch_sum_conserves` 覆盖，没有对应的真实样例文件。
- 同一采购单跨税率变更在样例里没有实例（发票税率与采购单都是 13%），规则由构造行测试 `tests/test_audit_vendors.py::test_rate_shift_recomputes_by_invoice_rate` 覆盖。
- 供应商主数据只按采购单号登记，没有按供应商编码前缀或组织维度继承；同一编码多名称只取排序最小的一个，不做人工别名表。
- 重复报销只比已完成批次（最多 20 个）里的发票行；跨库、外部报销系统、纸质单据不在范围内。
- 三单匹配只支持「一采购行对多入库行、多发票行」的求和规则；一张发票跨多个采购单的分摊（票面拆分）不支持，遇到时按行号单独匹配，拆不开就缺单。
- 同一发票号金额一致时只保留首行，不做去重后的数量取大取小裁定，理由写在 `docs/DATA-DICTIONARY.md` 的去重键一节。
- 只做单机 SQLite 与串行导入；并发保护是「同键同版本只落一次账」与租约接管，不是通用任务队列；
  没有跨机分布式锁。9 人内部使用场景下足够。
- 留存原件存在同一个 SQLite 文件里（base64）；样例文件很小，月均 1200 张发票量级够用。
  若将来单批附件达到百 MB 级，需要改成外部对象存储 + 只存引用。
- 中断恢复以租约（默认 900 秒）判定：进程若在租约内崩溃，需要等租约过期才能接续；这是有意的保守选择。
- 样例文件固定在 `sample_data/`，不支持浏览器上传任意文件；导入入口只接受样例清单内的文件名。
