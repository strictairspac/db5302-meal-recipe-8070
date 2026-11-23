# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 采购领单追加结构

领单功能只新增表，不改动既有表：`review_ledger`（复核落账顺序）、`workflow_counters`（序号与领单锁）、`purchase_claims` 与 `purchase_claim_items`（批次与条目）。启动时 `backfill_ledger()` 会把已复核但未进入 `review_ledger` 的历史证据按 `reviewed_at`、`id` 补录；同一秒内的历史复核无法还原真实提交先后，会以 `id` 顺序兜底。上线前建议在无并发写入的窗口执行一次。
