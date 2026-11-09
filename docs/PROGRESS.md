# Facet 项目进度

> 维护约定：每完成一个功能 / 修复 / 发布，及时更新本文件——勾选「接下来」里
> 对应项或移入「已完成」，并在文末「更新记录」追加一行。本文件是唯一的进度
> 基准，功能口径以 [README.zh-CN.md](../README.zh-CN.md) 为准。

**最后更新**：2026-11-09

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
- [x] 渠道出站代理：转发流量与维护直达调用都走 `proxy`（`0d9086a`）
- [x] 统一 CLI：`facet serve`（`planrouter` 为过渡别名）（`3dba6b1`）
- [x] 渠道厂家强制：渠道必须挂厂家（「没有厂家就没有渠道」）；内置注册表扩至 20 家（openrouter / google / zai），种子渠道与编辑器全量对齐，旧控制台标签页保存不再抹掉 provider/mode（`cd527d1`）
- [x] free-tier 渠道模型映射按每日排程对齐（`ac1a63c`）
- [x] NVIDIA NIM 渠道启用并真实请求验证：`nvapi-` key 到位，auto 链兜底实测 3/3 成功（约 1.8 万 tokens）
- [x] 网关加固（线 A）：冷却、重试与维护上下文（`3406336`）——已由 DSH 真实流量验证故障转移链（502 / 401 冷却后自动落到健康渠道）
- [x] 死渠道诊断与处置：codex-plan / claude-plan 根因是本地桥（8080/8081）未运行且口令未设（密钥手册 6.3 本就要求「先跑桥再启用」）、glm-plan 根因是 `GLM_PLAN_KEY` 未设置（网关发出请求无 Authorization → 401），三渠道临时禁用；魔搭免费 key 与模型映射确认有效（纯上游间歇不稳：120s 挂起 / 400 / 空流），保留启用并加渠道级 `timeout: 30` 封顶挂起成本；重启后实测 auto 链首跳直达 free-tier（`X-Failover-Count: 0`），魔搭非流式 3.4s / 流式 2.6s 均 200
- [x] 修复 `/health`：available 列表此前只滤冷却与订阅过期、不滤 disabled，禁用渠道被误报为可用；现按 `channel.enabled` 过滤（per-channel 明细仍保留诊断状态），e2e 新增用例：禁用渠道不出现在 available、路由零跳直达健康渠道

### 网关层 · 自我维护

- [x] 每日自我维护：刷新目录 → 自动应用可确认修复（带备份）→ 建议 / 告警分桶（`4e2aa9a`）
- [x] 厂家注册表两层：内置 `providers.json` + 用户覆盖层；套餐模型与厂家清单每日刷新（`0a00d74`）
- [x] 内置厂家含：百炼、混元、千帆、xAI Grok（`f5a4a5d`）、NVIDIA NIM（`7af04ed`）、魔搭 ModelScope API-Inference 免费档（`948c306`）
- [x] 免费池：仅免费渠道捐候选，测验通过进持久化队列，干活前复验（`9582ebc`、`9e456d4`）
- [x] 维护调用走直达不走路由；维护日志 `maintenance.jsonl` 持久化 + 控制台查看器（`0f8a132`）
- [x] 免费模型总览、测验过滤与串行探测（`aacbf5b`）；维护刷新调用透出上游 429 错误体（`09072ad`）

### 控制台 UI

- [x] 六个标签页：渠道配置 / 路由链 / 模型接入 / 套餐订阅 / 厂家 / 运行状态 / 维护日志
- [x] 渠道、厂家改全宽列表行布局（`3a69154`）
- [x] 渠道、厂家实时搜索过滤；顶部指标随渠道过滤联动（显示 `X / Y`）（`3a69154`）
- [x] 冷却中渠道黄色高亮 + 「冷却中 · 剩 N 分钟」标签（`3a69154`）
- [x] 渠道行内「测试」按钮（免开编辑器）；测试模型名随渠道保存（`test_model`）；测试通过自动解除冷却（`3a69154`）
- [x] 渠道「使用文档」浮窗：页内渲染本地手册 `docs/api-key-guide.md` 对应厂家小节，不跳厂家官网；浮层独立于编辑器（关闭后草稿完好），可切换完整手册并按锚点跳转；`/api/guide` 按请求读盘（改手册即时生效），`/api/guide/img` 限 docs 目录内防穿越；截图未补显示占位提示；渲染修复：步骤间空行不再打断编号、截图原地水合不跑位

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
| 桥接渠道恢复 | codex-plan / claude-plan / glm-plan 已临时禁用（本地桥未运行 / GLM key 未设）；跑起 8080/8081 桥或提供智谱 key（User 级 `GLM_PLAN_KEY`）后把渠道改回 enabled 即恢复 |
| 观察项：控制台自动刷新偶发跳回渠道配置 tab | 等用户确认日常复现，复现则修 |

## 接下来

- [ ] **桥接渠道恢复**：需要 Claude/Codex coding plan 时跑起对应本地桥（8080/8081，见密钥手册 6.3），渠道改回 enabled；glm-plan 提供智谱 key（User 级 `GLM_PLAN_KEY`）后恢复
- [ ] **密钥手册补图**：用户按 [api-key-guide.md](api-key-guide.md) 实操截图，放入 `docs/images/api-key-guide/`（手册已预置引用，放对文件名即自动显示；NVIDIA 已补 4 张）；界面与手册不符处回改手册
- [ ] **superagent 对接 P0 后半**（见 [行动清单](planrouter-superagent-integration.md)）：DSH 底座已接通并实测（样例见 [planrouter-dsh-integration.md](planrouter-dsh-integration.md)）；剩验证 super_agent 原型 Router / Worker 能否分别指定模型
- [ ] **tab 跳回 bug**：复现即修
- [ ] **发布节奏**：本地 main 领先 origin 35 个提交（用户暂缓 push）；攒到合适节点一起推并考虑打 tag 发版
- [ ] **P1–P3**（superagent）：Worker 模型档位评估（facet-cheap 够不够）→ 运维走 `/api/*` + admin key 的 policy 草案 → 吸收三条设计原则到维护层

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
  多实例。
- **合规红线**：个人免费额度聚合与订阅桥只留在个人侧；团队 / 企业场景
  必须用企业账号对接厂家，不绕 ToS。

---

## 更新记录

| 日期 | 内容 |
|---|---|
| 2026-11-09 | 修复 `/health`：available 列表补 disabled 过滤（此前禁用渠道被误报为可用），per-channel 明细保留诊断信息；e2e 新增 `test_disabled_channel_hidden_from_health`（禁用渠道不进 available + 路由零跳断言），全量测试套件通过，线上网关重启实测：available 仅剩 6 个真实可用渠道 |
| 2026-11-09 | 死渠道诊断与处置：glm-plan 401 根因是 `GLM_PLAN_KEY` 未设置（网关请求不带 Authorization）；codex/claude 桥未运行；魔搭免费 key/映射有效但上游间歇不稳（120s 挂起、400、空流），加渠道级 `timeout: 30`；前三者临时禁用 + 网关重启，实测 facet-auto 首跳 free-tier（failover 0）、魔搭非流式/流式均 200（3.4s / 2.6s） |
| 2026-11-09 | 免费池与维护迭代：免费模型总览与串行探测（`aacbf5b`）、维护刷新 429 错误体透出（`09072ad`）、free-tier 映射按日对齐（`ac1a63c`）、魔搭 ModelScope 厂家注册（`948c306`）；NVIDIA NIM 渠道启用并实测；**线 A** 网关加固（`3406336`）；**线 B** DSH 接入全链路验证（headless e2e + Web UI，改动在仓库外），新增实操文档 `planrouter-dsh-integration.md`；渠道健康现状刷新：free-tier 恢复主力，codex-plan / glm-plan / 魔搭免费待处置 |
| 2026-11-08 | 渠道厂家强制（`cd527d1`，注册表扩至 20 家）；手册新增 Z.AI 小节（`c3f174d`）；「使用文档」页内浮窗（本地手册渲染 + 列表编号/截图水合渲染修复，含 NVIDIA 截图 4 张）；记录轻量团队版产品方向（见「产品方向 · 后续发展」） |
| 2026-11-08 | 创建 `docs/api-key-guide.md`：19 家厂家 key 获取使用手册，每步骤留截图位（`docs/images/api-key-guide/`），待用户按指引实操补图 |
| 2026-11-08 | 注册表新增 LLM7 / OVHcloud（免费匿名档，已实测 keyless）；无候选 503 错误带逐渠道状态与启用指引，配套 e2e 测试 |
| 2026-11-08 | 控制台 UI 四连改（列表布局、搜索过滤、冷却高亮+指标联动、行内测试+测试模型保存），`3a69154`；NVIDIA NIM 厂家条目 `7af04ed`；种子模板换 NVIDIA `fde1a52` |
| 2026-11-08 | 零密钥首启（种子配置 + 横幅 + 测试），`bee55c8`；README 双语跟进 `0c983d6` |
| 2026-11-07 | 建立本进度文档 |
