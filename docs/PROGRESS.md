# Facet 项目进度

> 维护约定：每完成一个功能 / 修复 / 发布，及时更新本文件——勾选「接下来」里
> 对应项或移入「已完成」，并在文末「更新记录」追加一行。本文件是唯一的进度
> 基准，功能口径以 [README.md](../README.md) 为准。

**最后更新**：2026-11-15

---

## 已完成

### 事实层（数据表）

- [x] 核心交付物 `registry/model-registry.json`：217 provider / 约 2,400 模型，单文件 < 1 MB，零依赖（`bf1808b`）
- [x] 双数据源同步：models.dev 主源 + OpenRouter 辅源（`29d629f`），模型身份跨拼写变体归一（`9d37b0f`）
- [x] 人工事实标注 `manual-overrides.json`：`quota_tier` 永不自动填写（`3863813`）
- [x] `verify_claims` 实测验证：对声明的能力跑真实请求（`4bcbedb`）
- [x] `facet-models` pip 包 + PyPI 双语元数据（`4c0ba66`、`923d42c`）
- [x] CLI：`facet "模型名"` 解析、`facet --free --tools ...` 筛选（`3d5c9c2`）

### 网关层 · 路由与渠道

- [x] PlanRouter 核心：多渠道故障转移（priority 越小越先）、冷却与配额抢占、审计（`b1cbef9`、`9151747`）
- [x] OpenAI / Anthropic 双协议兼容；模型名策略（branded / auto:链 / 精确匹配 / fallback 链）
- [x] 套餐订阅管理：月费、到期提醒 webhook、过期自动停用关联渠道
- [x] 渠道 free/paid 档位：渠道级标签 + 按模型手动切换（`7859b8d`、`7e9a529`）
- [x] 火山引擎 Agent 套餐模式：注册表 `providers.json` 新增 `agent` 模式（OpenAI 协议 `/api/plan/v3`、Anthropic 协议 `/api/plan` 由 anthropic 渠道承载），专属 Key 不与 Coding 套餐通用；模型清单由每日维护抓取套餐概览写入，控制台直接选「Agent 套餐」即可（`0afe225`）
- [x] 渠道出站代理：转发流量与维护直达调用都走 `proxy`（`0d9086a`）
- [x] 统一 CLI：`facet serve`（`planrouter` 为过渡别名）（`3dba6b1`）
- [x] 渠道厂家强制：渠道必须挂厂家（「没有厂家就没有渠道」）；内置注册表扩至 20 家（openrouter / google / zai），种子渠道与编辑器全量对齐，旧控制台标签页保存不再抹掉 provider/mode（`cd527d1`）
- [x] free-tier 渠道模型映射按每日排程对齐（`ac1a63c`）
- [x] NVIDIA NIM 渠道启用并真实请求验证：`nvapi-` key 到位，auto 链兜底实测 3/3 成功（约 1.8 万 tokens）
- [x] 网关加固（线 A）：冷却、重试与维护上下文（`3406336`）——已由 DSH 真实流量验证故障转移链（502 / 401 冷却后自动落到健康渠道）
- [x] 死渠道诊断与处置：codex-plan / claude-plan 根因是本地桥（8080/8081）未运行且口令未设（密钥手册 6.3 本就要求「先跑桥再启用」）、glm-plan 根因是 `GLM_PLAN_KEY` 未设置（网关发出请求无 Authorization → 401），三渠道临时禁用；魔搭免费 key 与模型映射确认有效（纯上游间歇不稳：120s 挂起 / 400 / 空流），保留启用并加渠道级 `timeout: 30` 封顶挂起成本；重启后实测 auto 链首跳直达 free-tier（`X-Failover-Count: 0`），魔搭非流式 3.4s / 流式 2.6s 均 200
- [x] 修复 `/health`：available 列表此前只滤冷却与订阅过期、不滤 disabled，禁用渠道被误报为可用；现按 `channel.enabled` 过滤（per-channel 明细仍保留诊断状态），e2e 新增用例：禁用渠道不出现在 available、路由零跳直达健康渠道
- [x] 免费额度硬限额：以注册表 `free_quota` 为每模型默认免费额度（火山 500,000 / 百炼 1,000,000 tokens，渠道可覆盖 tokens·requests 与 total·month·day 周期），本地按 (渠道, 模型) 累计用量，任一上限达到即停止路由、绝不静默转按量计费；免费池、维护巡检、直达测验同步跳过，用量 80% 预警，厂家额度命令未同步时向用户告警（`7a5881c`）
- [x] 厂家额度真实数字接入：`facet-ark-quota` 的行补 `total`/`consumed` 且不再丢弃「剩余 0」的模型，对齐时按 (渠道, 模型) 落厂家快照 `vendorquota.json`（重启保留），面板额度列分「厂家侧 / 网关本地」两行并标注对齐时间；厂家 `remaining == 0` 直接纳入硬门禁（账号额度被网关外消耗时也停止路由）

### 网关层 · 自我维护

- [x] 每日自我维护：刷新目录 → 自动应用可确认修复（带备份）→ 建议 / 告警分桶（`4e2aa9a`）
- [x] 厂家注册表两层：内置 `providers.json` + 用户覆盖层；套餐模型与厂家清单每日刷新（`0a00d74`）
- [x] 内置厂家含：百炼、混元、千帆、xAI Grok（`f5a4a5d`）、NVIDIA NIM（`7af04ed`）、魔搭 ModelScope API-Inference 免费档（`948c306`）
- [x] 免费池：仅免费渠道捐候选，测验通过进持久化队列，干活前复验（`9582ebc`、`9e456d4`）
- [x] 维护调用走直达不走路由；维护日志 `maintenance.jsonl` 持久化 + 控制台查看器（`0f8a132`）
- [x] 免费模型总览、测验过滤与串行探测（`aacbf5b`）；维护刷新调用透出上游 429 错误体（`09072ad`）
- [x] 免费池可观测与缓存率：用量记录下沉到 (渠道, 模型) 粒度并采集上游缓存字段（`prompt_tokens_details.cached_tokens` / `cache_read_input_tokens` / `prompt_cache_hit_tokens`），`/api/usage` 新增渠道缓存命中率，`/api/maintenance` 免费模型新增「参与路由 / 失效标记 / 冷却 / 仅目录」状态与近 30 天调用次数 / token 消耗 / 缓存率及池汇总（`18ad705`）；控制台「自维护」面板据此显示可路由数、测验通过数、缓存率
- [x] 上游变更监视（M1 变更中心）：新增 `watch.py`，按三层信息源扫描——厂家注册表各 mode 的 docs_url / models_url、启用渠道自身 models 端点、套餐订阅文档；产出 `new_model` / `removed_model` / `doc_updated` / `catalog_changed` 事件写入 `change_log.jsonl`（列表加 sha256 去重，键保留最近 8 个以防 flip-flop 重复上报），快照原子落盘、先快照后事件保证崩溃安全；默认每 12 小时一轮（`defaults.watch_hours`，0 关闭），与控制台手动扫描共用非阻塞互斥；models 端点跟随 Bailian 风格分页，单源抓取异常只记错误不中断整轮；维护页新增「上游变更」卡片（扫描 / 单条已读 / 全部已读），`/api/changes*` 三个端点接入（`346065a`、`c89cca8`、`87d24b2`、`8c60c0c`）；审查修复 5 项（异常边界、分页、失败基线重建、去重键列表化、已读与落盘双向重读合并），36 条单测全过
- [x] 变更中心扩展：变更事件通知（按 webhook host 自动识别飞书 / 企微 / 通用格式，校验平台回执 errcode 防 200 假成功，一轮扫描合并为一条摘要推送，失败只进扫描报告不影响抓取）、「新增模型」一键映射进渠道（后端按信息源推断目标渠道、前端下拉选择、写入走既有维护映射路径并自动标已读）、按信息源静默（静默源照常抓取推进快照，只跳过事件创建，解除静默不会回灌积压）

### 控制台 UI

- [x] 六个标签页：渠道配置 / 路由链 / 模型接入 / 套餐订阅 / 厂家 / 运行状态 / 维护日志
- [x] 渠道、厂家改全宽列表行布局（`3a69154`）
- [x] 渠道、厂家实时搜索过滤；顶部指标随渠道过滤联动（显示 `X / Y`）（`3a69154`）
- [x] 冷却中渠道黄色高亮 + 「冷却中 · 剩 N 分钟」标签（`3a69154`）
- [x] 渠道行内「测试」按钮（免开编辑器）；测试模型名随渠道保存（`test_model`）；测试通过自动解除冷却（`3a69154`）
- [x] 渠道「使用文档」浮窗：页内渲染本地手册 `docs/api-key-guide.md` 对应厂家小节，不跳厂家官网；浮层独立于编辑器（关闭后草稿完好），可切换完整手册并按锚点跳转；`/api/guide` 按请求读盘（改手册即时生效），`/api/guide/img` 限 docs 目录内防穿越；截图未补显示占位提示；渲染修复：步骤间空行不再打断编号、截图原地水合不跑位
- [x] 定时任务可视化：抽出 `ScheduledTask` 基类统一三个后台循环（目录刷新 / 自维护 / 免费额度对齐）的开关、周期与状态回写，新增 `GET /api/tasks` 与「自维护」面板的「定时任务」卡片（任务 / 状态 / 周期 / 上次运行 / 下次运行 / 上次结果），手动触发也会回写「上次运行」（`819567a`）
- [x] 管理密钥首启简化：默认 `admin`（`DEFAULT_ADMIN_KEY`），登录后可用右上角「管理密钥」重设自定义值或随机生成（`POST /api/admin/key` 写回 `router.json` 并立即生效）；管理接口不再接受网关 key，compose / `.env` 默认 `ROUTER_ADMIN_KEY=admin`（`819567a`）
- [x] 渠道行「复制」：一键整体克隆渠道（端点、真实密钥或 `env:` 引用、模型映射、限流、计费标注），新名自动 `-copy` 去重并紧跟源渠道落盘热生效；克隆不继承冷却状态、不自动加入路由链（`210e2c5`）
- [x] 控制台新增一级「免费模型」页，按用户视角重做：单一扁平表按「能不能用」排序，状态收敛为 9 种枚举并给原因；每行展示免费额度（已用/上限 + 剩余 + 进度条 + 周期与重置口径 + 厂家同步/本地估算）、「厂家免费档限速（参考）」与「本渠道网关限流（实际执行）」双口径、近 30 天用量；顶部三张结论卡（现在能用 / 额度紧张 / 已停止）、筛选与搜索、行内复制与测试、厂家额度未同步一键对齐；仪表板新增「可用免费模型」KPI 与速览卡；「自维护」页移除免费模型卡片，只保留运维；注册表新增 `free_limits`（rpm/rpd/tpm/tpd/note）承载厂家公布值，接口透出厂家名/mode/文档/渠道限流与 `quota_warning`/`quota_unsynced`/`rate_limited` 汇总

### 首启与文档

- [x] 零密钥首启：无 `router.json` 时自动播种（匿名 Pollinations + NVIDIA NIM 等禁用模板）并直接启动（`bee55c8`）
- [x] README 双语：两层定位、网关运维、免费池、非目标（`9a1d36e`、`f62139c`、`0c983d6`）
- [x] `docs/`：竞品分析、DeepSeek superagent 对接分析（含 P0–P3 行动清单）、DSH 接入实操样例 [planrouter-dsh-integration.md](planrouter-dsh-integration.md)（provider patch 配置、profile 搭建、启动验证、踩坑清单，均来自实测）
- [x] 渠道密钥获取手册 `docs/api-key-guide.md`：20 家厂家 key 获取指引 + 截图位（NVIDIA 已补 4 张，其余待补）
- [x] 注册表新增 LLM7 / OVHcloud 免费匿名条目；无候选 503 改为逐渠道状态（disabled/cooling…）+ 启用指引

---

## 进行中

| 事项 | 状态 |
|---|---|
| free-tier 渠道 | 已恢复主力服务（46/55 成功、约 24 万 tokens）；免费额度耗尽时按上游重置头自动冷却 |
| 桥接渠道恢复 | codex-plan / claude-plan / glm-plan 已临时禁用（本地桥未运行 / GLM key 未设）；跑起 8080/8081 桥或提供智谱 key（User 级 `GLM_PLAN_KEY`）后把渠道改回 enabled 即恢复；**glm-plan 需额外排查**：`/health` 显示 `channel header missing authorization` 连续失败，疑似渠道 Key 未配置或协议 Header 映射不对，非桥问题 |
| 火山引擎 Agent 套餐渠道 | 注册表 agent 模式模型清单已恢复到 22 个；LLM 提取提示词已加「禁止改写分隔符」规则+示例，采纳前对已有清单做归一化（`.`/`-`/`_` 等价）比对，变体自动映射回原始拼写；「regex 兜底只增不删」护栏已上线，不会再被回砍到 5 个 |
| 正式试用（容器栈） | 网关已容器化常驻（[planrouter-container-trial.md](planrouter-container-trial.md)）：router/mock 双容器 healthy、宿主 127.0.0.1:8787、状态挂载 ./data、每小时指标监控运行中；试用通过后打 v1.0.0 tag |

## 接下来

- [ ] **桥接渠道恢复**：需要 Claude/Codex coding plan 时跑起对应本地桥（8080/8081，见密钥手册 6.3），渠道改回 enabled；glm-plan 提供智谱 key（User 级 `GLM_PLAN_KEY`）后恢复，并排查 `channel header missing authorization` 是否协议 Header 映射问题（非桥类）
- [ ] **密钥手册补图**：用户按 [api-key-guide.md](api-key-guide.md) 实操截图，放入 `docs/images/api-key-guide/`（手册已预置引用，放对文件名即自动显示；NVIDIA 已补 4 张）；界面与手册不符处回改手册
- [ ] **superagent 对接（往后排）**：2026-11-15 讨论降级——产品转向轻量团队版后受众是小团队管理员，agent 舰队叙事不再是产品需求；P0 后半（Router / Worker 分别指定模型）、P1（Worker 档位）、P2（运维 policy）暂缓，DSH 当模型底座的能力已被实测覆盖无需再验；仅保留 P3（吸收三条设计原则到维护层，见 [行动清单](planrouter-superagent-integration.md)），随维护层迭代渐进
- [ ] **正式试用 → 打 tag 发版**：37 个提交已推送（`5808af4..6c26bbf`，2026-11-09）；**发版前置：正式试用**——把日常真实工具指向网关跑一段实际工作（故障转移、配额冷却、每日维护在真实负载下的表现），试用通过再打 v1.0.0 tag（版本号已定为 1.0）

---

## 产品方向 · 后续发展

> 2026-11-08 讨论结论，仅记录方向，现在不做。个人版核心功能稳定后再展开，
> 届时先出一份最小改动清单。

- **定位**：轻量团队版——给小团队（几人到几十人）做内部共享 AI 网关，
  解决「员工各自充值、各自报销」的痛点；不做那种很重的企业版。
- **市场空位**：LiteLLM、one-api / new-api、Portkey、云厂商网关已占住
  企业级场景；本项目的差异化在三点——零依赖单文件部署、国内外厂商同为
  一等公民、自维护（事实表 + 自我维护任务）。
- **目标形态**：per-user key（每人一把独立 key）+ 按人配额 + 成本分摊
  报表；存储保持 JSON / SQLite，仍是单文件零依赖。不做 SSO / RBAC /
  多实例。（2026-11-15 三步已全部落地：per-user key 早期完成，按人配额 +
  成员报表增强 + 按人限速与成本分摊报表同日完成，见更新记录）
- **合规红线**：个人免费额度聚合与订阅桥只留在个人侧；团队 / 企业场景
  必须用企业账号对接厂家，不绕 ToS。

---

## 更新记录

| 日期 | 内容 |
|---|---|
| 2026-11-15 | 成本分摊报表（轻量团队版第三步，目标形态收官）：`report.py` 新增 `build_member_costs(records, users, days, month)`——把当月非过期订阅的月费等效值按成员用量占比分摊到人，权重取成员在该渠道的 tokens 占比（渠道当月 tokens 全为 0 时退回请求数），同币种各自结算；未关联订阅的渠道不计费、未关联渠道的订阅与无成员用量的渠道月费（含不带 key 的匿名流量份额）计入 `unallocated`，保证「已分摊 + 未分摊 = 月费合计」可对账；过期订阅不参与（与月度报告口径一致）。`server.py` 新增 `member_costs(month)`，`ui.py` 新增 `GET /api/report/members/<YYYY-MM>`（须置于通用 `/api/report` 前缀分支前）；控制台成员用量页顶部新增「成本分摊」卡片：月份选择器 + 月费合计 / 已分摊 / 未分摊 / 有用量成员四项摘要 + 成员×分摊成本×渠道明细表。新增 6 条测试（token 占比分摊与匿名份额、无 tokens 退回请求数、未关联/过期/零用量三口径、默认月与非法月、接口端到端），全量 1018 passed 1 xfailed；容器重建冒烟通过（真实数据 CNY 399 + USD 10 正确聚合，建 key → 推理 → 成员行出现分摊成本与渠道明细 → 删除 key） |
| 2026-11-15 | 成员管理强化（轻量团队版第一步）：`keys.py` 新增按 key 配额块（`normalize_limits` 校验 rpm / 每日请求 / 每日 token / 每月请求 / 每月 token，0 或缺省 = 不限，非法值中文报错），`lookup` 返回完整记录（`match` 改为其薄封装保持兼容）；`usage.py` 成员桶新增按渠道分布 `channels`、`member_totals`（日 / 月窗口聚合供配额判定）、`user_stats` 每成员透出 channels / 今日计数 / 突增标记（今日 ≥10 次且 ≥3× 此前日均）；`server.py` 新增 `_member_limited` 在推理入口认证后执行——rpm 用 60s 内存滑动窗口（拒绝不占槽）+ 日 / 月请求与 token 配额读 usage 聚合，超限 429 中文提示（count_tokens 本地免费不计），`update_key_limits` / `member_usage`（合并 limits 与 enabled）供 UI；`ui.py` 新增 `POST /api/keys/limits`，`/api/usage/users` 改走 member_usage；控制台 Keys 页新增「配额」列与配额弹窗（5 个数字项，留空 = 不限），成员用量页新增今日配额进度、主要渠道（top3+其他）与突增标记；key 仍不区分可用模型，RBAC 不做。新增 11 条测试，全量 1012 passed 1 xfailed；容器重建冒烟通过（设每日 1 次配额 → 首请求 200 → 第二请求 429 → 清理 key） |
| 2026-11-15 | 变更中心扩展：`notifier.py` 新增 `webhook_kind` / `webhook_payload` / `change_digest` / `notify_changes`，按 host 自动识别飞书 / 企微 / 通用 webhook 格式（通用信封保持企微兼容并附 `planrouter` 结构化字段），校验平台回执 errcode 防 200 假成功，一轮扫描合并为一条摘要（最多 10 条事件、每行 6 个模型名）；`watch.py` 新增 `set_muted`（按源静默，快照照常推进只跳过事件创建，落盘前重读防回滚）、`map_targets`（渠道 / 厂家 / 订阅三类信息源推断映射目标）、`notify_events`，`changes_payload` 透出 targets / muted / notify 状态；`/api/changes/map` 一键把「新增模型」事件写入渠道（走 `apply_maintenance_suggestion` 既有路径，成功后自动标已读）、`/api/changes/mute` 静默 / 恢复；维护页上游变更卡片新增静默按钮与写入渠道下拉；新增 22 条测试，全量 1001 passed 1 xfailed；容器重建冒烟通过，浏览器实测静默 / 映射 UI 与事件 targets 正确 |
| 2026-11-15 | M1 变更中心（上游变更监视）上线：`watch.py` 扫描厂家注册表 docs/models、启用渠道 models 端点与订阅文档，产出 new_model / removed_model / doc_updated / catalog_changed 事件落 `change_log.jsonl`；快照先原子落盘再追加事件，去重键保留最近 8 个并兼容迁移旧单键；默认 12 小时一轮（`defaults.watch_hours`，0 关闭），维护页「上游变更」卡片支持扫描 / 已读 / 全部已读；代码审查修复 5 项（异常边界收敛 Exception、models 端点跟随 Bailian 分页、失败快照静默重建基线、flip-flop 去重、已读与落盘双向重读合并），新增 8 条测试，36 条单测全过、全量回归 979 passed 1 xfailed；容器重建冒烟 54 源 41 成功 13 失败，历史事件与已读状态完好 |
| 2026-11-13 | 版本定为 1.0（v1.0.0）：pyproject.toml 版本静态化，「包版本号 = 表日期」日历版本机制废除，版本单一来源收敛到 pyproject.toml（Development Status 升为 Production/Stable）；`tools/build_package.py` 不再派生版本/写 `src/facet/_version.py`（该生成文件删除，.gitignore/.dockerignore 同步清理），`tools/build_windows.py` 改从 pyproject 读版本，gateway `__version__` 0.1.0 → 1.0.0（docstring 顺手修正 PlanRouter 残留）；待正式试用通过后打 v1.0.0 tag |
| 2026-11-11 | 渠道编辑器内嵌 AK/SK 获取指南（用户反馈：「火山引擎和百炼获取ak/sk的过程都要写清楚，还有权限的要求。步骤要细。不然用户不清楚具体过程，我刚才都搞了半天才配好」）：注册表新增厂家级 `credential_help`（steps/permissions/note），厂家只声明一次、加载时自动展开到声明 `credential_envs` 的各模式；`_fill_links` 对早于该字段的 overlay 条目从 builtin 回填（厂家级+模式级 INHERIT_LISTS 双保险），`normalize_vendor` 校验（steps 非空字符串数组、permissions/note 可选字符串）并随控制台保存扩展进模式。内容经官方文档核实——火山：访问控制→用户→新建 IAM 用户（编程访问）→添加权限勾选 `ArkFullAccess`（限制项目时选 default）→用户详情「密钥」Tab 新建密钥，SK 只显示一次，强调填的是 IAM Access Key 而非 ark- 开头的数据面 API Key（个人图快也可直接主账号在 API 访问密钥页建）；百炼：RAM 访问控制创建用户（勾「OpenAPI 调用访问」）+ 创建 AccessKey → RAM 挂 `AliyunBailianFullAccess`（缺则 NoPermission）→ 百炼控制台业务空间管理加成员（缺则 Team.NotAuthorised）→ 空间内模型授权（缺则 Workspace.NotAuthorised），强调填 RAM AccessKey 而非 sk- 开头的 DashScope Key。编辑器凭据区渲染为默认折叠的 `<details>`（summary「不会获取 AK/SK？点开看详细步骤与权限要求」，展开为编号步骤+权限要求+提示行）。补 6 条测试，全量 712 passed；线上验证火山/百炼编辑器帮助块均正确渲染 |
| 2026-11-11 | 渠道绑定凭据（渠道即账号）+ AgentPlan 余量接入（用户指出 agentplan 在另一个账号、且纠正架构理解：「厂家不能绑定帐号，渠道绑定帐号，我们是在渠道的界面里填 AK/SK 的」）：`Channel` 新增 `creds` 字段，凭据归属渠道而非厂家——同一厂家的第二个账号就是第二条渠道各带各的 IAM AK/SK，厂家级 `config.credentials` 降级为未填渠道的共享回退。`freealign._creds_for` 与订阅页 `plan_for` 均按「渠道自带 > 厂家级 > 环境变量」取凭据，免费额度对齐按 (命令， 凭据) 去重执行；`public_config` 对渠道 creds 回显掩码 `***`（env: 引用保留），`merge_secrets` 对其沿用掩码继承规则（漏传整节整体继承、`***`/空值逐键继承、显式值覆盖），渠道「复制」按钮随整体克隆。AgentPlan 接入：`ark_quota.fetch_agent_plan` = `GetPersonalPlan(Plan=AgentPlan)` + `GetAFPUsage`（AFP 绝对 Quota/Used，5小时/日/周/月四窗口，epoch 毫秒时间戳转秒），订阅表按行渲染绝对值「已用 x / y」而非百分比；套餐查询按行缓存 `kind:sha1(ak:sk)`（coding/agent 分槽，成功 5 分钟、失败 60 秒 TTL），行 vendor 非火山系（百炼等）不再挂火山套餐数据。注册表 volcengine `agent`/`coding` 模式补 `credential_envs`（此前仅 payg 有，套餐渠道编辑器不显示凭据区）；编辑器凭据区改为读写渠道自带 creds（厂家级不再有 UI 入口，仅作回退数据）。补 14 条测试，全量 706 passed；线上验证 CodingPlan 行余量正常（会话 98.4%/周 92.5%/月 96.3%），agentplan 行绑定第二条渠道并填入该账号 AK/SK 后即查真实余量 |
| 2026-11-11 | 百炼额度免浏览器授权（用户问「百炼能不能像火山一样填 key」）：核对容器内 `bailian-cli` 源码后确认可以——`bl usage free` 虽走控制台接口（`freeTrial.queryFreeTierQuota`），但 runtime 在没有 `access_token` 时会用 `BAILIAN_ACCESS_KEY_ID/SECRET`（或 `--open-api` 保存的 AK/SK）现场换取 CLI token，并在 `expired-console-session` 时自动重换，无需网页登录；`bl auth login --open-api` 其实也会把生成的 token 一并写进 `config.json`（此前以为只存 AK/SK）。落地：`providers.json` 的 bailian `payg` 模式补 `credential_envs`（AK/SK），控制台渠道编辑器据此渲染输入框、`freealign._creds_for` 注入子进程，与火山一致；补注册表测试断言两家免费额度 CLI 都声明凭据；编辑器提示文案改为区分火山 IAM 与百炼 RAM 的 Key 口径。线上排障：填 key 后依次报 `NoPermission`（RAM 缺 `AliyunBailianFullAccess`）→ `Team.NotAuthorised`（非团队成员）→ `Workspace.NotAuthorised`（无业务空间授权），逐层授权后 `bl usage free` 成功，百炼免费 8 模型对齐并落厂家快照 |
| 2026-11-11 | 订阅页接入火山 Coding Plan 厂家余量（用户指出套餐余量厂家后台可见、面板却是「--」）：调研确认火山官方 OpenAPI `GetPersonalPlan(Plan=CodingPlan)`（档位/状态/起止/自动续费）与 `GetCodingPlanUsage`（session/weekly/monthly 三窗口 Percent + epoch 秒 ResetTimestamp，仅百分比无绝对值）均可 Access Key 鉴权、与 `facet-ark-quota` 同签名体系，`ark_quota.py` 实测线上账号拿到真实数据（Pro、Running、2026-12-09 到期；会话 0.13%/周 7.28%/月 3.64%）；`_post` 从 `fetch_page` 抽出复用，新增 `fetch_coding_plan`；`server.App.vendor_plan()` 5 分钟 TTL 内存缓存 + 持久化到 vendorquota.json 的 `plan` key（重启先显示快照、首页加载即重新拉取；失败返回 error 并在存在旧快照时标 stale 降级显示），`subscriptions_payload` 按 vendor 含 "codingplan" 挂 `vendor_plan`；订阅页剩余量列显示「会话剩 x% · 周剩 y% · 月剩 z% + CodingPlan Pro · 到期 …」；AgentPlan 实测未订阅（GetAFPUsage 全 0、GetPersonalPlan(AgentPlan) 404），暂不接入；百炼 CLI 有 `bl usage coding-plan`/`token-plan` 但需 RAM 加授权（AuthorityPolicies.NoPermission），待用户确认百炼是否有套餐再接；补 2+3 条测试，全量 692 passed |
| 2026-11-11 | 免费模型页一键「重测未通过」（用户反馈逐行点测试太费时间）：新端点 `POST /api/maintenance/retest-failed`——从测验台账取全部 `ok=False` 的 (渠道, 模型) 对（覆盖手动测试与批量探测两种来源），校验渠道仍在且模型仍被该渠道提供（已删除/已改映射的跳过），渠道间并发、同渠道内严格串行（免费键一次只能答一题，并发会 429 被误判为又失败），复用 `_quiz_row`/`_record_quiz_row`（从 `test_single_model` 提取），通过者清失效标签并入队列、失败者按既有定论规则打标；免费模型页在「测验未通过」>0 时显示「重测未通过（N）」按钮，跑完 toast 汇总并刷新；补 2 条测试（只重测失败行+入队、跳过未知渠道/未映射模型），全量 687 passed |
| 2026-11-11 | 免费模型页「测验未通过」状态（用户指出：手动测试失败的模型状态列仍显示可用）：根因是 `routable`/`freeModelState` 都不看最近测验结果，而 `test_single_model` 只有 404 / 400+model 这类"模型真没了"才打失效标签，测验未通过 / 429 / 5xx / 超时按设计不算定论、不影响路由——但 UI 自相矛盾。改为纯展示层修复：`freeModelState` 在 routable 且最近测验失败时返回新状态 `quizfail`（仍可路由，黄色标签），状态格附「最近测验 <时间>：<原因>（仍可路由，测验失败不等于模型失效）」；摘要卡与筛选器新增「测验未通过」，"现在能用"只数真正 ok。后端 payload 本就带 `test` 字段，路由行为不变 |
| 2026-11-11 | OpenRouter 充值档位预设（用户指出「充值 $10 后 1,000 次/天」只改后台参数不够，用户不知道有这个变化）：注册表新增 `free_limit_presets`（`providers.py` 归一化校验 label/rate/note，进 `INHERIT_LISTS` 叠加继承）；渠道编辑器在限流输入框上方渲染档位按钮（OpenRouter：「未充值 50 次/天」「已充值 ≥$10 1,000 次/天」），点击自动填 RPM/RPD 并显示档位说明；OpenRouter 无"今日剩余次数"查询接口（`/api/v1/key` 只报美元余额），本地 rpd 滚动 24h 计数即最准口径；补注册表与归一化共 4 条测试，全量 685 passed |
| 2026-11-11 | 容器时区统一为北京时间（用户报「面板对齐时间比真实时间慢 8 小时」）：镜像默认 UTC，`datetime.now()` / `time.localtime()` / `date.today()` 全部按 UTC 出数，于是 `freealign` 的 `quota_at`、`freealign.jsonl` 与 `maintenance.jsonl` 的 `ts`、以及按天聚合的用量分桶都在北京时间 08:00 才翻篇。修复：`compose.yaml` 给 router 加 `TZ: "Asia/Shanghai"`（基础镜像自带 zoneinfo，无需装 tzdata），容器内时间即 CST；改用量的日/月边界也随之对齐北京时间。`requests.jsonl`（`audit.py` 写带 `Z` 的 UTC ISO）与前端用 epoch 渲染的时间不受影响，浏览器会按本地时区显示；切换前已写入的 `freealign.jsonl` / `maintenance.jsonl` 记录仍是原来的 UTC 时刻，不做回改 |
| 2026-11-11 | 厂家免费额度真实数字接入（用户报「火山 Kimi 已用 19,307 但面板仍显示 0」）：根因不是对齐没跑到（09:31 对齐成功、映射已更新），而是三层断链——① `ark_quota.rows_from_items` 只留 `remaining`，丢掉 `Total/Consumed` 且把「剩余 0」的行直接从映射里剔除，界面看不到「已用完」；② `freealign` 把厂家数字丢在筛模型之后，从不下发前端；③ 面板「免费额度」列只算网关本地计数器 vs 注册表基线，kimi 本地 0 次调用就显示 `0 / 500,000`，却被标成厂家额度（该 19,307 tokens 来自 Ark 控制台等网关外消耗）。修复：行补 `total`/`consumed`、保留 0 剩余行；对齐时按 (渠道, 模型) 落厂家快照并持久化 `vendorquota.json`（重启仍显示真实值，缺失 `consumed` 时按 total−remaining 推导）；额度列改「厂家侧 已用/总量 · 剩」+「网关本地」两行并标对齐时间；硬门禁新增「厂家 remaining ≤ 0 即停」，不再因本地计数为 0 而放行；新增 5 个测试（快照字段、重启读取、本地 0 时展示厂家数字、厂家 0 额度拦截、仅厂家快照的 source），全量 680 通过 |
| 2026-11-11 | 控制台免费模型页重构（用户视角）：原来免费模型散落在「渠道配置 / 自维护 / 用量报表」三处、夹在稳定性评分与配置备份等运维卡片里，且只有网关自身限流、没有厂家免费档口径。现提升为一级「免费模型」页——顶部三张结论卡（现在能用 / 额度紧张 / 已停止），中间单一扁平表（模型 / 免费来源 / 状态 / 免费额度 / 限速 / 近 30 天 / 操作）按可用性排序，支持状态筛选与搜索；额度列给出已用·上限·剩余、进度条、周期与重置口径，并标明「厂家同步」还是「本地估算」；限速列明确区分「厂家免费档（参考，不执行）」与「本渠道网关（实际执行）」；行内可复制模型名、测试、一键对齐厂家额度。注册表新增 `free_limits`（rpm/rpd/tpm/tpd/note，校验正整数）记录厂家公布值（魔搭按「250 魔粒/日」原文说明而非硬套 RPM），接口补厂家名/mode/文档/渠道限流与 `quota_warning`/`quota_unsynced`/`rate_limited` 汇总；仪表板加「可用免费模型」KPI 与速览卡，「自维护」页删除免费模型卡片。全量 675 测试通过 |
| 2026-11-11 | 镜像内置百炼 CLI（`bl 2.8.3`）：免费额度对齐依赖 `bl usage free`，此前镜像只有 Node/DSH、没装 `bailian-cli`，定时任务恒报「命令不存在（bl）」。Dockerfile 在 Node 阶段 `npm install --prefix /opt/bailian` 后只拷运行目录并软链 `/usr/local/bin/bl`（`BAILIAN_CLI_VERSION` 可覆盖）；compose 增加 `BAILIAN_CONFIG_DIR=/data/.bailian`，让一次性 `bl auth login --console` 的令牌落在持久卷里、重建镜像不丢。火山侧失败不是打包问题——`facet-ark-quota` 本就在 wheel 内，缺的是 IAM AK/SK；已在控制台 credentials 填入后对齐成功，`doubao-seed-2-1-lite-260915` 已按厂家真实额度摘除，百炼待控制台授权后即可恢复 |
| 2026-11-11 | 免费额度硬限额（`7a5881c`）：火山 `doubao-seed-2-1-lite-260915` 免费额度早已耗尽（本地 82 次 / 12,406,394 tokens）却仍被路由并静默转按量计费。现按「厂家基础额度（火山 500k / 百炼 1M tokens，渠道 `free_quota` 可覆盖 tokens·requests·total·month·day）+ 本地按 (渠道, 模型) 用量」判定，任一上限达到即从候选剔除、不回退（503 带 `free_quota_skipped`），免费池 / 维护巡检 / 直达测验同样跳过；用量达 80% 预警、面板展示「免费额度已用完」与「额度未同步」告警（厂家额度命令未跑或失败时提示本地估算可能偏差）。线上实测：该模型 503 `free quota exhausted`，上游零调用 |
| 2026-11-11 | 厂家模型清单 LLM 提取降噪：`build_plan_models_prompt` 加「禁止改写分隔符」规则与示例（不得把 `doubao-seed-2.0-mini` 写成 `doubao-seed-2-0-mini`）；`_refresh_provider_mode` 采纳前对已有清单做归一化（`.`/`-`/`_` 等价）比对，LLM 提取的拼写变体自动映射回已有原始拼写，避免变体覆盖正确写法；tab 跳回观察项与「接下来」中对应条目移除（用户确认 tab 不再使用） |
| 2026-11-11 | 控制台渠道行新增「复制」按钮（`210e2c5`）：一键整体克隆渠道（端点、真实密钥或 `env:` 引用、模型映射、限流、计费标注），新名自动 `-copy` 去重并紧跟源渠道落盘热生效；克隆不继承冷却状态、不自动加入路由链；端到端验证通过后清理 |
| 2026-11-11 | 火山引擎 Agent 套餐：注册表 `providers.json` 新增 `agent` 模式（OpenAI `/api/plan/v3`、Anthropic `/api/plan` 由 anthropic 渠道承载），专属 Key；模型清单由每日维护抓取套餐概览写入（`0afe225`）。修复 agent 渠道「只拉到 5 个模型」问题：根因是 regex 兜底只认出 kimi/deepseek 等 5 个第三方模型并把 LLM 提取的 17 个豆包/GLM 删掉；上线「regex 兜底只增不删」护栏，手动触发维护由 LLM 恢复到 22 个模型并清理 2 条 LLM 误写的横杠变体条目；手册 2.1/2.2 讲清密钥粒度与账号×套餐配置、第九章解释免费池与缓存率口径 |
| 2026-11-10 | 免费池可观测与缓存率：用量记录下沉到 (渠道, 模型) 粒度并采集上游缓存字段（`prompt_tokens_details.cached_tokens` / `cache_read_input_tokens` / `prompt_cache_hit_tokens`），`/api/usage` 新增渠道缓存命中率、`/api/maintenance` 的免费模型新增「参与路由 / 失效标记 / 冷却 / 仅目录」状态与近 30 天调用次数、token 消耗、缓存率及池汇总（可路由 / 失效 / 测验通过 / 调用过）；全量 653 测试通过 |
| 2026-11-10 | 控制台两项（`819567a`）：①定时任务可视化——`ScheduledTask` 基类统一目录刷新 / 自维护 / 免费额度对齐三个后台循环，`GET /api/tasks` 暴露状态，自维护面板新增「定时任务」卡片（状态 / 周期 / 上次与下次运行 / 上次结果），手动触发经 `note_run` 同步回写；②管理密钥首启简化——默认 `admin`，登录后「管理密钥」可重设为自定义值或随机生成并写回 `router.json`（重启仍生效），管理接口不再接受网关 key，compose / `.env` 默认 `ROUTER_ADMIN_KEY=admin`；容器重建后实测 admin 登录 200、轮换后旧 key 401 / 新 key 200，全量 634 测试通过 |
| 2026-11-09 | Facet 注册表自维护落地（[R9](planrouter-requirements.md)）：`tools/sync_models_dev.py` 修复悬空标注（上游下架模型不再产生无 provider 僵尸条目）、新增 `tools/weekly_maintenance.py` 一键编排同步→校验→打包（staging 先验后落盘）+ 渠道目录交叉验证 + facet-auto / facet-cheap 建议链生成（仅报告，合入路由仍人工编辑）；产出进 `registry/maintenance-log.jsonl` 与 `route-suggestions.json`；计划任务 `FacetWeeklyMaintenance` 每周日 03:30 自动跑；正本刷新至 2026-11-09（2578 条），测试基线 562→580 |
| 2026-11-09 | 新增需求清单 [planrouter-requirements.md](planrouter-requirements.md)：汇总 PROGRESS.md / superagent 行动清单 / 设计文档的需求项（R1–R8）并补验收标准；进度状态仍以本文件为唯一基准 |
| 2026-11-09 | 新增架构与功能总览 [planrouter-architecture.md](planrouter-architecture.md)：分层架构图、模块职责、入口链、请求处理数据流、HTTP API 面、配置模型、数据资产、自维护子系统、功能清单与非目标 |
| 2026-11-09 | DSH 接入文档补现状速查（第 9 节）、容器化适配说明与验证预期更新（首跳直达 free-tier） |
| 2026-11-09 | 容器化试用运行态确立：修复打包链三问题（providers.json 入 wheel、entrypoint 模板路径、容器 listen 0.0.0.0），采用既有 compose 栈常驻并端到端验证（/health 200 六渠道、facet-auto 首跳直达）；新增设计文档 [planrouter-container-trial.md](planrouter-container-trial.md)；建立每小时指标监控 |
| 2026-11-09 | 修复 `/health`：available 列表补 disabled 过滤（此前禁用渠道被误报为可用），per-channel 明细保留诊断信息；e2e 新增 `test_disabled_channel_hidden_from_health`（禁用渠道不进 available + 路由零跳断言），全量测试套件通过，线上网关重启实测：available 仅剩 6 个真实可用渠道 |
| 2026-11-09 | 死渠道诊断与处置：glm-plan 401 根因是 `GLM_PLAN_KEY` 未设置（网关请求不带 Authorization）；codex/claude 桥未运行；魔搭免费 key/映射有效但上游间歇不稳（120s 挂起、400、空流），加渠道级 `timeout: 30`；前三者临时禁用 + 网关重启，实测 facet-auto 首跳 free-tier（failover 0）、魔搭非流式/流式均 200（3.4s / 2.6s） |
| 2026-11-09 | 免费池与维护迭代：免费模型总览与串行探测（`aacbf5b`）、维护刷新 429 错误体透出（`09072ad`）、free-tier 映射按日对齐（`ac1a63c`）、魔搭 ModelScope 厂家注册（`948c306`）；NVIDIA NIM 渠道启用并实测；**线 A** 网关加固（`3406336`）；**线 B** DSH 接入全链路验证（headless e2e + Web UI，改动在仓库外），新增实操文档 `planrouter-dsh-integration.md`；渠道健康现状刷新：free-tier 恢复主力，codex-plan / glm-plan / 魔搭免费待处置 |
| 2026-11-08 | 渠道厂家强制（`cd527d1`，注册表扩至 20 家）；手册新增 Z.AI 小节（`c3f174d`）；「使用文档」页内浮窗（本地手册渲染 + 列表编号/截图水合渲染修复，含 NVIDIA 截图 4 张）；记录轻量团队版产品方向（见「产品方向 · 后续发展」） |
| 2026-11-08 | 创建 `docs/api-key-guide.md`：19 家厂家 key 获取使用手册，每步骤留截图位（`docs/images/api-key-guide/`），待用户按指引实操补图 |
| 2026-11-08 | 注册表新增 LLM7 / OVHcloud（免费匿名档，已实测 keyless）；无候选 503 错误带逐渠道状态与启用指引，配套 e2e 测试 |
| 2026-11-08 | 控制台 UI 四连改（列表布局、搜索过滤、冷却高亮+指标联动、行内测试+测试模型保存），`3a69154`；NVIDIA NIM 厂家条目 `7af04ed`；种子模板换 NVIDIA `fde1a52` |
| 2026-11-08 | 零密钥首启（种子配置 + 横幅 + 测试），`bee55c8`；README 双语跟进 `0c983d6` |
| 2026-11-07 | 建立本进度文档 |
