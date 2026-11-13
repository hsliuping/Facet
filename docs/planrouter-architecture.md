# Facet / PlanRouter 架构与功能总览

日期：2026-11-09。性质：架构与功能基准文档——口径与
[README.md](../README.md) 对齐，实现细节以代码为准。

## 1. 一句话定位

**自维护的 LLM 免费额度聚合网关，附带模型事实表。** 一个包两层：

- **事实层（`facet`）**：模型事实表（能力 / 价格 / quota_tier），每周变化，纯数据 + 工具；
- **网关层（`planrouter`）**：运行时网关（渠道 / 路由 / 免费池 / 每日自维护），每天变化。

两层之间的桥是 [facts.py](../src/planrouter/facts.py)：网关做名字解析与 quota_tier 查询，
只读不写。

## 2. 总体架构

```
客户端（DSH / coding 工具 / 任意 OpenAI、Anthropic 兼容客户端）
   │  POST /v1/chat/completions · /v1/messages · /v1/responses（桥）
   ▼
┌─ 网关层 planrouter（纯 stdlib，单进程 ThreadingHTTPServer，约 6300 行）─────────┐
│  鉴权 → 模型名解析（branded / auto 链 / 精确匹配 / fallback）                     │
│  → 渠道选择（priority 轮换 · 评分 · 粘性会话）→ 逐候选转发                        │
│  → 429/配额冷却 · failover → 审计（requests.jsonl）· 用量（usage.json）· 响应头   │
│  控制台 UI + /api/* 管理 · 订阅与到期提醒 · 维护 / 免费池 / freealign 后台线程     │
└──────────────┬──────────────────────────────────────────────────────────┘
               │ facts.py 只读桥（名字归一解析 + quota_tier）
               ▼
┌─ 事实层 facet（约 380 行 + 数据表）───────────────────────────────────────────┐
│  registry/model-registry.json（217 厂家 / 约 2400 模型，双源周同步）           │
│  manual-overrides.json（人工标注）· schema/（字段契约）                        │
│  tools/：sync_models_dev（同步）· validate_registry（质量门）                  │
│          build_package（打 wheel）· verify_claims（实测验证声明）              │
└──────────────────────────────────────────────────────────────────────────┘
```

部署形态：宿主常驻容器栈（router + mock 冒烟上游，compose 编排，状态挂载 `./data`），
见 [planrouter-container-trial.md](planrouter-container-trial.md)。

## 3. 目录结构与模块职责

### 3.1 事实层 `src/facet`（约 380 行）

| 模块 | 职责 |
|---|---|
| [__init__.py](../src/facet/__init__.py) | 包门面：导出 load / find / resolve / check |
| [_table.py](../src/facet/_table.py) | 表加载：捆绑快照 / 本地路径 / HTTP refresh |
| [_identity.py](../src/facet/_identity.py) | 名字规范化与别名归一 |
| [_find.py](../src/facet/_find.py) | 硬条件过滤 + 排名 |
| [cli.py](../src/facet/cli.py) | `facet "模型名"` 查询 CLI；`serve` 子命令转交网关 |
| data/model-registry.json | 构建时拷入的表快照 |

### 3.2 网关层 `src/planrouter`（约 6300 行）

| 模块 | 职责 |
|---|---|
| [core.py](../src/planrouter/core.py)（842 行） | 核心：配置解析（`parse_config`）、模型名解析（`resolve_model`）、`StateStore`（冷却 / 评分 / 粘性 / 轮换）、配额冷却判定、`env:` 密钥解析、出站代理 |
| [server.py](../src/planrouter/server.py)（1736 行） | HTTP 网关本体：App / Handler / ThreadingHTTPServer、转发与 failover、后台线程编排（目录刷新 / 维护 / freealign / 用量 / 提醒） |
| [maintenance.py](../src/planrouter/maintenance.py)（981 行） | 每日自维护：目录分析、建议三桶、LLM 顾问、免费池测验；`maintenance_models.json` 为其持久化队列 |
| [ui.py](../src/planrouter/ui.py)（432 行） | `/api/*` 管理 API + `/` 控制台壳 |
| [subscriptions.py](../src/planrouter/subscriptions.py)（272 行） | 套餐订阅：月费换算、到期状态、续期 |
| [notifier.py](../src/planrouter/notifier.py)（158 行） | 到期 webhook 提醒（带去重） |
| [report.py](../src/planrouter/report.py)（98 行） | 月度订阅 × 用量报表 |
| [quota.py](../src/planrouter/quota.py)（130 行） | 读 x-ratelimit 头做配额抢占冷却 |
| [persistence.py](../src/planrouter/persistence.py)（146 行） | router.state.json 健康快照持久化 |
| [audit.py](../src/planrouter/audit.py)（80 行） | requests.jsonl 路由审计（仅元数据） |
| [usage.py](../src/planrouter/usage.py)（84 行） | 每日每渠道用量桶 |
| [probe.py](../src/planrouter/probe.py)（308 行） | 控制台一次性探测（不动渠道状态） |
| [responses.py](../src/planrouter/responses.py)（351 行） | Responses ↔ Chat 协议边缘适配（桥） |
| [facts.py](../src/planrouter/facts.py)（84 行） | 事实表只读桥：名字解析 + quota_tier |
| [freealign.py](../src/planrouter/freealign.py)（141 行） | 免费额度对齐 sweep（vendor CLI） |
| [providers.py](../src/planrouter/providers.py)（240 行） | 厂家注册表：内置 + 用户覆盖层 |
| [seed.py](../src/planrouter/seed.py)（100 行） | 零密钥首启种子配置 |
| [cli.py](../src/planrouter/cli.py)（149 行） | `serve` 入口 |
| [ark_quota.py](../src/planrouter/ark_quota.py)（190 行） | 火山 Ark 免费额度 CLI |
| [_alias.py](../src/planrouter/_alias.py) | 弃用的 `planrouter` 命令别名（转 `facet serve` 语义） |
| [providers.json](../src/planrouter/providers.json) | 内置厂家注册表（20 家） |
| static/（index.html 448 / app.js 2869 / style.css 423 行） | 零构建中文控制台 |

### 3.3 其他目录

| 目录 | 角色 |
|---|---|
| registry/ | 事实表本体 + manual-overrides.json 人工标注 |
| schema/ | 表字段契约（JSON Schema） |
| tools/ | 表同步、质量门、打包、实测验证四件套 |
| docker/ | 容器 entrypoint / mock 上游 / 冒烟脚本 |
| tests/ | 28 个 unittest 文件（含 e2e） |

## 4. 入口与启动链

| 命令 | 指向 | 用途 |
|---|---|---|
| `facet` | facet.cli:main | 事实表查询；`facet serve` 启动网关 |
| `planrouter` | planrouter._alias:main | 过渡别名，打印弃用提示后转 serve |
| `facet-ark-quota` | planrouter.ark_quota:main | Ark 免费额度表 |

`facet serve` 启动链：无 `router.json` 时 [seed.py](../src/planrouter/seed.py) 零密钥播种 →
`core.parse_config` → `server.make_server()` 构建 App + ThreadingHTTPServer →
`App.start_background()` 拉起后台线程（目录刷新 / 每日维护 / freealign / 用量 / 到期提醒）→
`serve_forever()`；退出时逐一停止。容器部署时由
[entrypoint.py](../docker/entrypoint.py) 播种 `/data/router.json` 后 exec 同一入口。

## 5. 请求处理数据流（POST /v1/chat/completions）

1. **鉴权**：`App._authorized`——`listen.api_key`，接受 `Authorization: Bearer` /
   `x-api-key`，hmac 比对（加载 key 后全端点强制）。
2. **模型名解析**（`core.resolve_model`）→ ModelDecision：
   - branded 映射（如 `facet-auto` → default 链）；
   - `auto:` / `facet:` 前缀 → 指定链内部调度；
   - 其余在各链渠道 `models` 键**精确匹配**（exact）；不匹配 → `fallback_unknown`
     决定走 fallback 内部调度还是拒绝。
3. **渠道选择**（`StateStore.pick`）：粘性会话优先 → free 档优先轮换
   （`_rotate_tier`）→ 过滤冷却 / 限速 / 禁用渠道 → exact 全冷却时回退调度链 →
   组内按时间衰减分数稳定排序；目录确证缺该模型则软跳过该渠道。
4. **逐候选转发**（`_forward`，≤ `max_failover` + 1 次；瞬时网络错误原地重试）：
   渠道 `proxy` 出站 + `defaults.timeout`；429 / 配额耗尽 → `parse_cooldown`
   （Retry-After / reset 头优先，日配额冷却到 UTC 零点）→ 标记冷却（half-open 探活）；
   上一响应 remaining=0 时 [quota.py](../src/planrouter/quota.py) **提前抢占冷却**。
5. **中继与记账**：成功 `_relay`（流式 SSE 从 usage 块汇总 token）→ 绑定粘性会话 →
   每次尝试写 requests.jsonl + usage.json → 全败返回 503（带逐渠道失败明细）。

响应头：`X-Routed-Via: 渠道/线模型`、`X-Failover-Count`、
`X-Routed-Model-Mode/Reason/Requested-Model`、`X-Route`。

## 6. HTTP API 面

| 分组 | 端点 | 用途 |
|---|---|---|
| 推理 | `POST /v1/chat/completions` | OpenAI 协议（主力） |
| | `POST /v1/messages`(+`/count_tokens`) | Anthropic 协议；count_tokens 本地启发式 |
| | `POST /v1/responses` | Responses 协议桥 |
| | `GET /v1/models` | 模型清单（`{"data": [...]}`） |
| 状态 | `GET /` + `/static/*` | 控制台（免认证页壳） |
| | `GET /health` | 健康 + available 渠道（需 Bearer） |
| | `GET /stats` | 每渠道用量 |
| 管理 | `/api/config`(GET/POST)、`/api/reload` | 配置读写与热重载 |
| | `/api/state`、`/api/requests[/N]`、`/api/usage`、`/api/report[/月]` | 运行状态 / 审计 / 用量 / 报表 |
| | `/api/facts`、`/api/facts/free`、`/api/facts/resolve/<name>` | 事实表查询 |
| | `/api/providers`(GET/POST/DELETE)+`/maintain` | 厂家注册表管理 |
| | `/api/subscriptions`(GET/POST)+`/<id>/renew`、`/api/notifications`(GET/POST/check) | 订阅与提醒 |
| | `/api/test/channels`、`/api/probe/models` | 渠道草稿探测 / 一次性探测 |
| | `/api/maintenance`(logs/run/test-models/apply)、`/api/freealign/run|logs` | 自维护操作 |
| | `/api/guide`(+`/img/…`)、`/api/backups`、`/api/channels/<n>/reset`、`/api/score/reset` | 手册浮窗 / 备份 / 冷却与评分重置 |

## 7. 配置模型（router.json 顶层键）

| 键 | 内容 |
|---|---|
| `listen` | host / port / api_key / admin_key（支持 `env:VAR` 引用） |
| `defaults` | cooldown_minutes（默认 300）、max_failover、sticky_minutes、timeout、track_stream_usage、preempt_quota、catalog_refresh_hours、maintenance_hours、free_align_hours、network_retries、score_halflife_hours 等 |
| `models` | branded 映射、fallback_unknown、fallback_route |
| `channels[]` | name / protocol / base_url / upstream_path / api_key / models / priority / cooldown_minutes / rate / proxy / tier / enabled |
| `routes[]` | 命名路由链（protocol + channels，支持 `$free` 宏展开全部 free 渠道） |

`env:` 密钥机制在 `core.resolve_secret`：`env:VAR` 前缀查环境变量，缺失解析为 None；
listen key / 渠道 key / 代理 / webhook 均经此解析，密钥不落配置文件。

## 8. 数据资产

| 文件 | 内容 | 谁写 |
|---|---|---|
| registry/model-registry.json | 事实表本体（217 厂家 / 约 2400 模型） | tools/ 周同步 |
| src/planrouter/providers.json | 内置厂家注册表 | 随包发布 |
| data/router.json | 网关配置（容器挂载 `/data`） | 用户 / 控制台 |
| data/router.state.json | 冷却 / 计数 / 评分 / 粘性会话快照 | persistence.py |
| data/usage.json | 每日每渠道 requests / success / failed / tokens | usage.py |
| data/requests.jsonl | 路由审计（每次尝试的元数据） | audit.py |
| data/subscriptions.json | 套餐订阅 | subscriptions.py |
| data/maintenance.jsonl / maintenance_models.json | 维护报告 / 免费池持久化队列 | maintenance.py |
| data/freealign.jsonl / probe.jsonl / notifications.json | 对齐历史 / 探测记录 / 提醒去重 | freealign / probe / notifier |

## 9. 自维护子系统

- **每日维护**（MaintenanceRunner）：刷新模型目录 → 可确认修复自动应用（先备份）→
  刷新套餐与厂家模型清单 → LLM 顾问意见 → **applied / suggestions / alerts 三桶**，
  落内存 + maintenance.jsonl。
- **免费池**：仅 tier:free 渠道捐候选 → 合成文档页测验（全对才入持久化队列）→
  干活前复验，失败即除名。
- **维护调用直达不走路由**：优先第一个暴露目标模型的启用渠道（free 优先、单轮非流式、
  走渠道 proxy），无渠道暴露才回退网关自身路由——维护流量不污染业务路由统计。
- **套餐订阅**：月费换算、到期 webhook 提醒（去重）、全链接订阅过期自动停用渠道。

## 10. 功能清单（用户视角）

1. **模型事实表 + CLI**：`facet "模型名"` 解析、`facet --free --tools …` 筛选、
   `verify_claims` 对声明能力跑真实请求；表每周双源同步 + 质量门 + 人工标注保留。
2. **双协议路由网关**：OpenAI / Anthropic / Responses 三端点；auto 链、branded 别名
   （facet-auto / facet-cheap）、精确匹配、fallback 链；priority 故障转移、429 / 配额
   冷却与抢占、粘性会话、评分轮换、渠道出站代理、全链路审计与响应头核对。
3. **套餐订阅管理**：月费 / 到期 / 提醒 / 过期停用 / 月度报表。
4. **自我维护**：每日三桶维护、免费池测验队列、免费额度对齐 sweep、维护日志查看器。
5. **控制台**：零构建中文 UI（仪表板 / 渠道 / 路由链 / 模型接入 / 套餐订阅 / 厂家 /
   自维护 / 用量 / 请求日志）；搜索过滤、冷却高亮、行内测试、厂家手册页内浮窗。
6. **首启与部署**：零密钥首启种子；多阶段 Docker 镜像 + compose 栈（mock 冒烟上游）、
   `.env` 密钥注入、`./data` 状态持久化、`restart: unless-stopped`、liveness 探针、
   每小时指标监控（见 [planrouter-container-trial.md](planrouter-container-trial.md)）。
7. **客户端接入**：任意 OpenAI / Anthropic 兼容客户端零改造接入；DSH 一份 patch 全量
   接入（见 [planrouter-dsh-integration.md](planrouter-dsh-integration.md)）。

## 11. 非目标（设计边界）

- 事实表**不做**路由决策、不管 key、不遥测、不逐请求排序、不聚合跑分；
- 网关路由只依据维护层观测到的渠道状态，**绝不依赖表的新鲜度**；
- 不做编排 / 多实例 / per-user 隔离（团队版方向见 [PROGRESS.md](PROGRESS.md)）；
- 不把 Node 生态组件（如 super_agent）嵌入包内——它们跑在网关之上，不是本项目的组件。

## 12. 关联文档

| 文档 | 内容 |
|---|---|
| [planrouter-requirements.md](planrouter-requirements.md) | 需求清单（编号、验收标准、状态视图） |
| [planrouter-container-trial.md](planrouter-container-trial.md) | 容器化部署设计与试用运行态 |
| [planrouter-dsh-integration.md](planrouter-dsh-integration.md) | DSH 接入实操样例 |
| [planrouter-superagent-integration.md](planrouter-superagent-integration.md) | super_agent 调研与三层对接方案 |
| [planrouter-vendor-binding-and-docker-seed.md](planrouter-vendor-binding-and-docker-seed.md) | 厂家强关联校验 + 容器种子设计 |
| [api-key-guide.md](api-key-guide.md) | 20 家厂家 key 获取手册 |
| [planrouter-competitive-analysis.md](planrouter-competitive-analysis.md) | 竞品分析 |
