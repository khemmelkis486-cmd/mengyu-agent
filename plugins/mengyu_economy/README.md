# 梦雨的钱包

这是独立的 AstrBot 插件，用于模拟梦雨的人民币经济。所有金额以分保存。

## 启用

把本目录的 `config.example.json` 复制为 `data/plugin_data/mengyu_economy/config.json`，填写开户现金、应急留存、五类预算和想启用的周期规则。设置 `enabled: true` 后重载插件。首次启用记录一次开户余额，之后重载读取已有账本。建议启用日期填写当前的虚构生活日期。

| 设置 | 格式 |
|---|---|
| `bill_templates` | 数组，每项包含 `id`、`label`、`day`（1–31）、`amount_cents` |
| `work_income_rules` | 按工单类型命名的对象，每项包含 `amount_cents`、`delay_days`、`policy`（`manual` 或 `simulated_auto`） |
| `savings_goals` | 数组，每项包含 `id`、`label`、`target_cents`、`monthly_cents`，可设 `day`（默认 1） |
| `owner_origin` 和 `owner_id` | 主人的完整私聊来源与发送者 ID。两项都填写才开放模型查询 |
| `enabled_date` | ISO 日期，可留空以首次启用日为准 |

## 使用

- 独立结算任务会处理配置的账单、已经登记的自动应收、储蓄计划与过往月报。逾期账单在资金充足后继续结算。
- 模型工具 `mengyu_economy` 提供 `status`、`budget`、`recent`、`goals`、`wishlist`、`monthly` 查询。
- 内部 `Wallet` 服务提供 `snapshot`、`create_quote`、`preview`、`commit`、`register_receivable`、`update_item`、`evidence` 和 `events_since`。内部写入请求使用稳定的 `action_id`。消费、购买、退款、二手成交、借还、应付及储蓄转账都在账本里模拟。
- 此版的消费、购物、稿费、借款和物品变更来自已确认的内部服务调用。仅启用插件时，配置好的账单和储蓄会自主结算。现有生活、聊天、情绪、主动联系和图片插件的调用由后续接入提供。

