# 接口清单 invoice3way

## 页面

| 路径 | 内容 |
|---|---|
| `/` | 样例文件、阶段诊断、文件诊断、原因码分布、逐行诊断 |
| `/matches` | 三单对照（左：订单 + 入库，右：发票，中间差异标记） |
| `/diffs` | 差异明细、缺单、按类别合计 |
| `/adjust` | 调整单、分摊依据、运行清单、输入摘要 |
| `/batches` | 批次历史、水位、重跑 |
| `/export` | 四账、下载、金额合计、输入摘要 |
| `/audit` | 复核台账：守恒检查、供应商汇总、重复组、红字冲减、分批入库、税率变更 |

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/samples` | 样例文件清单（含字节数与摘要前缀） |
| POST | `/api/import` | 触发导入，body `{"files": [...], "rule_version": "..."}`；采信即留存原件 |
| GET | `/api/import/files` | 文件诊断、留存原件与阶段计数；可选 `batch_id` |
| GET | `/api/contract` | 列契约、单位表、容差、结论与差异类别 |
| GET | `/api/contract/preview` | 单格试算，参数 `kind`、`value`、`unit` |
| GET | `/api/quarantine` | 隔离行、原因码分布、字段分布、质量指标 |
| GET | `/api/quarantine/export.csv` | 隔离区 CSV |
| GET | `/api/trios` | 三单对照数据（左列 / 标记列 / 右列） |
| GET | `/api/matches` | 匹配明细，可选 `verdict` 过滤 |
| GET | `/api/diffs` | 差异明细，可选 `kind` 过滤 |
| GET | `/api/reconcile` | 四账 |
| GET | `/api/batches` | 批次历史与水印（含原件留存与可重算标记） |
| GET | `/api/batches/{batch_id}` | 批次详情，含留存原件与输入核对 |
| POST | `/api/batches/{batch_id}/rerun` | 按留存原件重算，body 可带 `rule_version`；缺原件 422 |
| POST | `/api/batches/{batch_id}/retry` | 失败/中断批次按留存原件重试 |
| GET | `/api/batches/{batch_id}/recovery` | 中断恢复判定（租约、原件、可接续） |
| GET | `/api/export` | 导出页载荷（调整单 / 差异 / 清单 / 水位） |
| GET | `/api/export/download` | 下载，`kind` = adjustment / matches / quarantine / manifest / reconcile |
| GET | `/api/audit` | 复核台账全量载荷，可选 `batch_id`、`kind` |
| GET | `/api/audit/dups` | 跨批次重复组，`kind` = same_no_diff_amount / same_no_same_amount / same_amount_diff_no |
| GET | `/api/audit/vendors` | 供应商汇总、分批入库、税率变更与守恒检查 |
| GET | `/healthz` | 健康检查 |

## 错误码

| HTTP | 错误码 | 触发 |
|---|---|---|
| 400 | `E_UNKNOWN_FILE` | 文件名不在样例清单内，或 `files` 为空数组 |
| 400 | `E_BAD_VERDICT` | `verdict` 不在 matched / diff / missing 内 |
| 400 | `E_BAD_DIFF_KIND` | `kind` 不在差异类别表内 |
| 400 | `E_BAD_KIND` | 试算 `kind` 不在 text / amount / qty / rate / date / unit 内 |
| 400 | `E_BAD_EXPORT_KIND` | 导出 `kind` 未知 |
| 400 | `E_BAD_LIMIT` | 批次列表 `limit` 不在 1..200 |
| 400 | `E_BAD_DUP_KIND` | 重复组 `kind` 不在三类里 |
| 404 | `E_BATCH_NOT_FOUND` | 批次号不存在，或还没有任何批次 |
| 409 | `E_STALE_BATCH` | 提交的规则版本早于已有批次 |
| 409 | `E_BATCH_RUNNING` | 同键同版本已有活跃运行，同一输入不重复落账 |
| 409 | `E_CLAIM_LOST` | 写入时执行权已失效（被并发运行或中断恢复接管） |
| 409 | `E_BATCH_NOT_RETRYABLE` | 对非失败/中断批次调用重试 |
| 422 | `E_INPUT_MISSING` | 批次没有留存输入原件，拒绝重算；不从现有文件补造 |
