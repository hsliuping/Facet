# PlanRouter：显式模型名的计费优先路由

状态：已实现并部署（2026-11-14；同日到期比值排序与控制台徽标为同日增强）。本文记录设计动机、判定规则与实现位置，避免后续遗忘。

## 1. 背景与目标

用户需求原话要点：

> 「我们指定某一个模型的时候，比如 deepseek_flash 和 glm5.3-flash，系统是怎么选择上游的渠道和模型的……
> 优先使用包月的模型，优先使用包月快到期的。比如 deepseek 这个模型在多个厂家渠道都有，就要优先按到期时间越少的优先调用包月套餐类型的上级渠道当中模型，按量的模型优先级最低。」

改造前 `StateStore.pick` 只有这些排序依据：route 链序 → priority → 同优先级轮转（`_rotate_tier`）→
粘性会话置顶 → 稳定性评分（坏渠道沉底）→ 冷却 / 限速 / 禁用过滤。
**完全没有「包月还是按量」「套餐何时到期」的概念**，因此同一个模型在多个厂家出现时，
可能先打到按量付费渠道，白花现金，而包月额度到月底作废。

目标：同一个模型名被多个渠道提供时，按「先用会过期的、再用免费的、最后用花钱的」排序候选。

用户确认的两条决策：

1. 免费层排在**包月之后、按量之前**（不是最优先）。
2. **只在显式指定模型名（exact 匹配）时生效**；调用 `facet-auto` / `facet-free` 等内部别名时
   保持原链内调度逻辑。

## 2. 生效范围

`resolve_model()` 的 `decision.mode` 决定走哪条排序路径：

| 请求模型名形态 | mode | 是否计费排序 |
|---|---|---|
| 命中某渠道 `models` 的显式键（如 `deepseek-v4.1-flash`、`glm-5.3-flash`） | `exact` | **是** |
| `facet-auto` / `facet-cheap` / `facet-free` / `facet-agent` / `auto:` / `facet:` 前缀 | `auto` / 内部调度 | 否，保持链序 |
| 未匹配任何显式键，走 `fallback_unknown` | 内部调度 | 否 |

注意：只有**显式键**算 exact；靠通配符（`*`）映射出来的模型不属于 exact，仍按原逻辑。

## 3. 计费画像：每个渠道一个 rank

`App._refresh_billing_ranks()` 遍历 `config.channels`，为每个渠道算出一个排序键：

| rank 元组 | 含义 | 组内顺序 |
|---|---|---|
| `(0, 到期时间戳, -burn)` | 包月套餐，且有明确到期日 | 到期日**升序**，最快到期的先调用；同日到期按 burn 比值**降序**（剩得多的先调用） |
| `(1, 0.0, 0.0)` | 包月套餐，但没有到期日 | 保持链序 |
| `(2, 0.0, 0.0)` | 免费层渠道（`channel.tier == "free"`） | 保持链序 |
| `(3, 0.0, 0.0)` | 按量付费（payg）或未知 | 保持链序，最低 |

### 3.1 数据来源与判定

- **是否包月**：`channel.business_line or channel.mode` ∈ {`coding`, `agent`}；
  或者任一**未过期**的套餐订阅通过 `channels` 字段关联到该渠道（匹配 `channel_ref(channel)`
  或 `channel.name`）——手工在订阅页登记过的渠道即使 `mode` 写的是 payg，也算包月。
- **到期时间**：所有关联且未过期的订阅中，取 `expires_at`（`YYYY-MM-DD`）的**最小值**转 epoch。
  任一关联订阅过期即跳过，不影响「包月」判定（只是失去到期日）。
- **burn 比值**（同日到期 tiebreak，`server._burn_ratio`）：
  `剩余额度比例 / 剩余天数`，其中剩余额度比例 = `(quota_limit - quota_used) / quota_limit`
  （截断到 [0,1]，quota_limit 缺失或非法记 0）。到期日按 0 天算 1。两个包月渠道同一天到期时，
  **剩余越多的越先调用**——它是「不用完就作废」风险最大的那个；没有额度记账的渠道记 0，
  退化为链序。rank 第三位存的是 `-burn`，升序排序即 burn 降序。
- **免费层**：`channel.tier == "free"`。放在 `(2, …)`，即免费额度优先于按量付费，
  但让位于「反正也会作废」的包月套餐。
- `expires_at` 解析失败/为空时，渠道降为 `(1, 0.0, 0.0)`（仍是包月，只是没有截止时间）。

### 3.2 为什么这样排

- 包月是沉没成本：不用也照扣月费，快到期就应先耗尽，边际成本为 0。
- 免费层虽有额度但可续/可换，作废损失小于包月套餐的现金损失。
- 按量是唯一按 token 产生现金流的形式，排最后。

## 4. 实现结构

| 位置 | 内容 |
|---|---|
| [core.py](../src/facet/gateway/core.py) `StateStore.__init__` | 新增 `billing_ranks: dict[str, tuple[int, float, float]]`，键为渠道 name，初值空 |
| [server.py](../src/facet/gateway/server.py) `_burn_ratio()` | 单条订阅的「剩余额度比例 / 剩余天数」，同日到期 tiebreak 用 |
| [server.py](../src/facet/gateway/server.py) `App._refresh_billing_ranks()` | 读订阅与渠道配置，**整体替换** `store.billing_ranks`（快照式，无锁撕裂） |
| [server.py](../src/facet/gateway/server.py) 调用点 | `App.__init__`（启动）、`_swap()`（配置热重载）、`save_subscriptions()`、`renew_subscription()` |
| [server.py](../src/facet/gateway/server.py) `ui_state()` | 每渠道附 `billing: {group, expiry, burn}` 供控制台渲染徽标 |
| [app.js](../src/facet/gateway/static/app.js) `billingBadge()` | 渠道卡片徽标：`包月·YYYY-MM-DD（剩约N%）` / `包月·未登记到期` / `免费档` / `按量`，tooltip 解释排序规则 |
| [core.py](../src/facet/gateway/core.py) `StateStore.pick()` | `decision.mode == "exact" and exact` → `_billing_order()`；其余路径沿用 `_by_score` / `_weighted_order` |
| [core.py](../src/facet/gateway/core.py) `_billing_order()` | 实际排序：粘性置顶 → 健康/坏对分离 → 按 rank 稳定排序 |

### 4.1 `_billing_order` 细节

1. 候选对 `< 2` 直接返回，不做无谓排序。
2. sticky 会话渠道（若已在首位）保持钉在第一位，其余参与排序。
3. 按稳定性分数 `<= DEMOTE_SCORE` 拆成健康组与坏组：坏组永远排在健康组之后
   （已知坏的渠道不该因为包月就优先踩坑），坏组内部按分数降序、再按 rank。
4. 健康组按 `billing_ranks` 升序（到期日 → burn 比值）；Python `sort` 稳定，
   因此同 rank 的渠道保持控制台链序。
5. 未登记在 map 里的渠道默认 `(3, 0.0, 0.0)`，与新渠道/临时渠道的安全默认一致。

排序**只重排候选**，不改变池子：route 链、冷却、限速、禁用、订阅过期停用、
目录缺模型软跳过等前置过滤全部照旧生效。

### 4.2 与既有策略的关系

- `lb: weighted` 只影响非 exact 路径；exact 请求统一走计费排序（避免两套排序叠加语义不清）。
- `exact` 候选被冷却全部过滤为空时，`pick` 仍走原有的「回退内部调度链」分支，不受影响。
- 计费画像在**请求路径之外**刷新，转发请求时只读内存 dict，不产生文件 IO。

## 5. 边界与已知取舍

- **到期时间只到天粒度**（订阅记录是 `YYYY-MM-DD`）；同一天到期的渠道用 burn 比值分先后，
  比值也相同（都没记账）时保持链序。
- **多订阅关联**取最早到期日；一个渠道挂多条订阅时不累加额度。同日到期的多条订阅取 burn 比值更高的一条。
- **包月无到期日**的渠道（如 `opencodego免费`，`business_line=coding` 但未在订阅页登记）排
  `(1, 0.0, 0.0)`，位于有日期包月之后、免费层之前——符合「它是包月」的事实，只是不知道该何时催续费。
- **剩余额度仅来自订阅页手工记账**（`quota_limit` / `quota_used`）：不实时调厂家接口，
  不参与跨到期日的排序（跨日仍纯按到期时间）；没记账的渠道在同日 tiebreak 中退化为链序。
  额度真正耗尽的渠道靠既有的 429 / 配额冷却机制绕开。
- **不跨链**：只在一个 route 的候选集合内排序；模型在另一条链上更便宜也不会被拉进来。

## 6. 验证

单元测试 [tests/test_planrouter_billing.py](../tests/test_planrouter_billing.py)：

| 用例 | 断言 |
|---|---|
| `test_exact_request_drains_soonest_plan_first` | 到期升序：快到期的包月 → 晚到期包月 → 无日期包月 → 免费 → 按量 |
| `test_expired_subscription_leaves_plan_undated` | 订阅过期后渠道降为 `(1, 0.0, 0.0)`，不再参与到期排序 |
| `test_linked_subscription_marks_channel_monthly` | payg 渠道挂上订阅后 rank 变为 0 且排到最前 |
| `test_auto_alias_keeps_chain_order` | `auto` 请求保持控制台链序，计费排序不介入 |
| `test_free_tier_beats_payg_but_not_plans` | 免费层在按量之前、包月之后 |
| `test_same_expiry_fuller_plan_drains_first` | 同日到期的两个包月，记账剩余多者（burn 比值高）排前 |
| `test_ui_state_exposes_billing` | `ui_state()` 每渠道带 `billing {group, expiry, burn}` |

容器实测（2026-11-14，`facet-router-1`）：

```
glm-5.3-flash      → 火山引擎codingplan0241(12-10) → 火山引擎agentplan6198(12-13) → opencodego(12-14)
deepseek-v4.1-flash→ 火山引擎codingplan0241 → 火山引擎agentplan6198 → opencodego → 百炼免费
facet-auto         → 保持原链序（mode=auto，未受计费排序影响）
```

## 7. 已落地的演进与后续可选方向

已落地（2026-11-14 增强）：

- 同日到期 tiebreak：`_burn_ratio`（剩余额度比例 / 剩余天数），剩得多的先调用。
- 控制台渠道卡片计费徽标（`billingBadge`）+ `ui_state` 暴露 `billing` 字段。

后续可选：

- 剩余额度参与**跨**到期日排序（如「剩 90% 只剩 5 天」提到「剩 10% 还有 20 天」之前），
  需要厂家额度自动同步才能可靠。
- 订阅页 UI 增加「计费优先级」列，让用户直接看到排序依据。
- 支持渠道级手工覆盖 rank（例如强制某渠道最优先）。
