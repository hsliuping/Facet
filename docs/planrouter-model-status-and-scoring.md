# PlanRouter：模型状态标识与渠道×模型稳定性评分

状态：已确认方案（2026-11-09）。本文是实现依据；实现完成后在此追加「实现说明」一节。

## 1. 背景与目标

用户需求原话要点：

1. 「模型要做标识，哪些没有免费额度，哪些可用，哪些是付费的，要根据模型所在的厂家、渠道和套餐综合判断，而不是去写死。」
2. 「要做对应的分数……这个渠道的这个模型不稳定，就自动扣分，服务成功一次就加一分，根据排分的高低来定稳定性。」

即两件事：

- **模型状态标识**：按模型聚合，运行时推导 免费可用 / 免费已耗尽 / 仅付费 / 无渠道 四态，规则来自渠道配置（enabled、tier/tiers）+ 套餐订阅 + freealign 当前映射 + 上游目录，绝无硬编码模型清单。
- **渠道×模型稳定性评分**：评分粒度是「渠道 + 该渠道实际映射出的 wire 模型」，成功 +1、非配额类失败 −5；配额类失败只进冷却、不动分。路由时同一池内按分数降序重排候选（分数相同保持现有顺序），route 链仍决定池子，cooldown 机制照旧。

## 2. 模型状态标识（按模型聚合）

### 2.1 判定链

对每个出现过的模型名（来源：所有渠道的显式映射键，排除 `auto`/`*`；以及 tier=free 渠道的上游目录与厂家 registry 清单），按顺序取第一个命中的状态：

| 状态 | key | 判定 |
|---|---|---|
| 免费可用 | `free_ok` | ∃ enabled 渠道 c：模型 ∈ c 的显式映射 且 `model_tier(c, model) == "free"` |
| 免费已耗尽 | `free_exhausted` | 不满足上条，且 ∃ tier=free 渠道 c：模型 ∈ 其上游目录（store.catalogs）或厂家 registry 模式清单，但不在其当前映射中（freealign 对齐后的映射即「仍有免费额度」的集合） |
| 仅付费 | `paid_only` | ∃ enabled 渠道 c：模型 ∈ c 的显式映射 且 `model_tier(c, model) == "paid"` |
| 可用（档位未知） | `available` | ∃ enabled 渠道 c：模型 ∈ c 的显式映射 且 tier 为空（未标注且无关联套餐可推断） |
| 无渠道 | `no_channel` | 以上皆不满足（例如只出现在已禁用渠道的映射中） |

实现时补充的第五态 `available`：tier 空的渠道是 enabled 的，模型明明可用却标「无渠道」会误导；档位未知就如实展示「可用（档位未知）」，排在 paid_only 之后。

- `model_tier` 复用现有规则：渠道 `tiers` 手工标注 > 渠道 `tier` 字段 > 关联套餐订阅推断。
- 冷却（cooldown）是分钟级渠道健康信号，**不**参与模型状态判定（避免状态闪烁）；渠道冷却仍在渠道卡片与状态表中展示。
- blocked（订阅过期）渠道视为不可用，同 disabled 处理。

### 2.2 输出

`GET /api/state`（`ui_state`）顶层新增 `model_states`：

```json
[{"model": "kimi-k2", "status": "free_ok",
  "channels": [{"name": "魔搭免费", "tier": "free", "cooling": false, "score": 3}]}]
```

`channels` 列出所有提供该模型的渠道及各自的 tier、冷却、分数，供 UI 展开细看。

## 3. 渠道×模型稳定性评分

### 3.1 数据结构

- `StateStore.scores: dict[str, int]`，key 为 `f"{channel}|{wire_model}"`（JSON 友好），缺省 0。
- 不设上下限；+1/−5 的不对称本身就让恢复变慢（失败一次需 5 次成功回本）。

### 3.2 记分点（server.py）

| 事件 | 位置 | 分数 |
|---|---|---|
| 成功（流式/非流式转发完成） | `_relay` 两处 `mark_success` | +1 |
| 客户端断开但上游已完成 | `_relay` 的 BrokenPipe → `mark_success(0)` | +1 |
| 配额类失败（`parse_cooldown` 返回 quota=True：额度耗尽/429/404） | `_forward` HTTPError 分支 | 0（只冷却） |
| 非配额类失败（transient_http_* / transient_network / transient_internal） | `_forward` 其余分支 | −5 |

记分统一走 `StateStore.record_score(channel, wire_model, delta)`，在 store 锁内完成。

### 3.3 路由介入（core.py `pick`）

- sticky 会话绑定渠道保持最前（会话亲和优先，冷却时自然被跳过走 failover）。
- 其余候选（exact 组与 scheduled 组各自内部）按 `-score` 稳定排序：分数相同保持现有顺序（优先级/`$free` 展开序/轮转结果不动）。route 链决定池子、cooldown 决定生死，分数只决定先后。

### 3.4 持久化与清理

- `persistence.capture/restore` 新增 `scores` 字段，随 5 秒周期快照落盘；旧 state.json 缺字段按全 0 起步。
- `_swap`（热重载）与 `restore` 时过滤：只保留渠道名仍存在于当前配置中的 key（渠道被删/改名后旧分自然丢弃；wire 模型改名同理）。
- fingerprint 纳入 scores，无变化不写盘。

## 4. API 变更汇总

- `GET /api/state`：
  - 每个 channel item 增加 `scores: {wire_model: score}`（仅该渠道有记录的键）。
  - 顶层增加 `model_states`（见 2.2）。
- `StateStore.snapshot()`（503 错误响应、maintenance 用）不变。

## 5. 管理页变更

- **渠道卡片**：模型映射 chips 上，分数 ≠ 0 时追加徽标 `+N`（绿）/`−N`（红）；现有 免费/收费 tier 徽标保留。
- **模型总览区**（现有「模型」tab）：每个模型行按 `model_states` 聚合状态加徽标（免费可用=绿、免费已耗尽=黄、仅付费=黄、无渠道=红），并提供状态过滤；行内列出提供渠道。
- 新增小段 CSS，不引外部资源。

## 6. 测试计划

- 新增 `tests/test_planrouter_routes_macro.py`：`$free` 展开——(priority, name) 排序、显式渠道保位去重、协议过滤、tier 空不进宏、disabled 不进宏、展开全空报错、unknown 显式渠道名仍报错、宏在链中间插入。
- 新增评分测试：成功 +1 / 失败 −5 / 配额类不扣分、`pick` 稳定排序（含 sticky 优先、分数相同保持原序）、capture/restore 往返、`_swap` 后保留并过滤失效渠道。
- `model_states` 推导单测：四态各一例 + 手工 tiers 覆盖渠道 tier。
- 全量 `python -m unittest discover tests` 通过。

## 7. 实现说明

已按本文实现，全量测试 541 个通过。涉及文件：

- **core.py**：`StateStore.scores`（key `channel|wire_model`）+ `score` /
  `record_score` / `scores_for`；`pick` 返回前经 `_by_score` 稳定排序
  （`-score`，sticky 会话渠道钉在首位）；`$free` 宏展开「只补显式未列出的
  free 渠道、显式渠道保位」，修复了显式 free 渠道写在宏之后会被前移的缺陷
  （`tests/test_planrouter_routes_macro.py` 覆盖）。
- **server.py**：`_forward` 三类失败分支记 −5（配额类 `quota=True` 不扣分）、
  `_relay` 两个成功分支与 BrokenPipe 分支记 +1；`_swap` 热重载保留仍存在
  渠道的分数；`ui_state` 每个 channel 增加 `scores`，顶层增加
  `model_states`（`App.model_states`，纯运行时推导）。
- **persistence.py**：`capture/restore/fingerprint` 增加 `scores`；restore
  时丢弃渠道不存在、无 `|` 分隔符或值非 int 的键。
- **static/**：渠道卡模型映射追加 `+N/−N` 徽标（`scoreBadge`）；「会精确
  匹配的模型名」区改为按模型聚合视图（状态徽标 + 提供渠道明细）并新增
  状态过滤 chips；`style.css` 新增 `.tag.score-pos/-neg`、`.tag.warn`、
  `.tag.off` 与 `.filter-row`。

与设计的一致性说明：模型明细里 `free_exhausted` 的渠道列表来自 free 渠道
的 catalog（含 registry 渠道的自动目录），score 取该渠道 auto 映射的 wire
模型分（此类模型当前无显式映射，取其内部调度分最接近真实稳定性）。

## 8. 稳定性评分的衰减与重置（优化，2026-11-09）

评分从纯累计 `int` 改为 `(score, last_ts)`，做**惰性指数半衰期衰减**，让
「当前稳定性」压倒「远古历史」，自动填平深坑：

- **半衰期** `H = defaults.score_halflife_hours`（默认 12，`0` 关闭衰减、
  退回纯累计）。读/写时把分数按 `score * 0.5^(Δt / H)` 折算，`Δt` 为距上
  次记录的天数（小时粒度）。
- **两向对称**：正分、负分都随时间向 0 回归。老失败自动被遗忘，不再要求
  「5 次成功硬回正」；老的成功也随时间退场——稳定性反映近期而非终身。
- **读幂等**：`score()` 只做虚拟衰减返回、不落盘、不刷新时间戳；只有
  `record_score()` 才固化衰减并刷新 `last_ts`。避免「读一次就重置计时」。
- **排序**：`_by_score` 的 key 由「原始累计分」改为「衰减后的当前分」，
  稳定排序 / sticky / cooldown 逻辑不变。
- **持久化**：`scores` 序列化为 `[score, last_ts]`；旧 state.json 里的纯
  int 分数视为「升级时刻刚记录」，原值保留（不会因缺时间戳被立即遗忘）。
- **手动重置**：`POST /api/score/reset`，body `{channel?, wire?}`；省略
  channel 清空全部，指定 channel 省略 wire 清空该渠道；管理页渠道卡片
  模型行的分数徽标旁加「×」重置该 `渠道×wire`。

实现涉及：`Defaults.score_halflife_hours`、`StateStore` 的 `score` /
`record_score` / `scores_for` / `reset_scores`、`_by_score`、`_swap`、
`persistence.capture/restore`、`server` 新增 `/api/score/reset`、前端
`scoreBadge` 加重置入口。

