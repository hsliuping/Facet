# DeepSeek Super-agent 调研与 PlanRouter 对接分析

> **分析对象**：dsh-superagent-prototype（DeepSeek Harness 团队，2026-10-31 开源，MIT 协议，约 2,800 星）
> **我方产品**：PlanRouter（定位：自维护的免费额度聚合器，网关是其产品形态）
> **数据截至**：2026-11-08 · **方法**：公开资料桌面研究 + 架构比对

---

## 1. 摘要与核心结论

DeepSeek 这次开源的不是一个模型，也不是一个产品，而是**一份架构设计 + 一个 MIT 研究原型**（dsh-superagent-prototype）。它瞄准的痛点是「**人的注意力瓶颈**」：当前一个操作者最多同时盯 3–5 个活跃 agent 会话，再多就管不过来了。

它的答案是一套分层调度架构：Router 只管「人和任务的关系」（用轻模型即可），Worker 隔离满配工具去干细节活，事件 Trigger 驱动而非人肉唤醒，再配上外部 Watchdog 兜底。

**对 PlanRouter 的结论分三层**：

1. **立即可做（零代码）**：PlanRouter 当它的模型底座——DSH 原生支持 Custom model API 接任意 OpenAI 兼容网关，填一个 URL 就能让 Router 走 `facet-cheap` 免费链、Worker 走 default 强模型链；
2. **中期方向**：让 super-agent 当我们维护层的**外部大脑**——盯 PlanRouter 代码仓库做开发维护，运行时运维走管理 API，验证闸门留在网关侧；
3. **借理念不接代码**：它的三条设计原则（能力边界靠移除工具、外部 Watchdog 巡检、Fast/Slow Path 两级过滤）与 PlanRouter 维护层高度同构，值得直接吸收。

**明确不做**：把 dsh-superagent 嵌进 PlanRouter。Node 技术栈与「纯 stdlib 零依赖」的产品身份直接冲突。正确的关系是——**它跑在我们的产品上，成为第一批超级客户**。

---

## 2. 它是什么：dsh-superagent-prototype 全貌

### 2.1 基本盘

| 项目 | 内容 |
|---|---|
| 发布方 | DeepSeek Harness 团队 |
| 发布时间 | 2026-10-31 |
| 形态 | 架构设计文档 + 可运行研究原型（明确标注 research prototype） |
| 协议 | MIT |
| 热度 | 约 2,800 星（发布一周内） |
| 技术栈 | Node（运行要求 Node 22.19+）、依赖 gh CLI 与专用 GitHub 账号 |

### 2.2 要解决的问题

不是「让 agent 更聪明」，而是「**让一个人能带一支 agent 舰队**」。瓶颈在人：会话多了盯不过来，会话少了吞吐浪费。Super-agent 的解法是把「人盯每个会话」改成「Router 盯所有会话、人只盯 Router」。

### 2.3 核心架构五件套

1. **Router / Worker 双层**：Router 负责分派与管理关系，**可以不配 Bash**（干不了活反而干不了坏事），轻量模型即可胜任；Worker 每个会话独立隔离、满配工具，用强模型干活。
2. **Trigger 事件驱动**：任务不靠人唤醒，靠事件触发。两级过滤——**Fast Path** 用程序规则先筛掉大多数噪音，**Slow Path** 才动用 LLM 判断，控制成本。
3. **订阅直通**：用户订阅的事件直达对应 Worker，不经 Router 中转。实测去掉它 Router 决定量 +30%、缓存 token +37%。
4. **存算分离 + 外部 Watchdog**：会话状态外置存储，Watchdog 独立于主循环之外巡检，防止 agent 循环跑飞。
5. **异步 ReAct Loop**：Worker 内部异步执行，多会话并行。

### 2.4 实验数据与原型局限

实验显示端到端 p95 缩短 47%——但注意这是**合成负载**上的结果，仅供参考。

原型当前的明确局限：

- 只盯**一个 GitHub 仓库**（本质是「仓库运维 agent」）；
- 蓝图（blueprint）等规划功能尚未实现；
- 默认 `danger-full-access` 权限，仅适合隔离环境；
- 默认 observe 模式（只观察不动手），动手需显式开启。

---

## 3. 与 PlanRouter 的关系判断

### 3.1 为什么不内嵌

| 维度 | dsh-superagent | PlanRouter |
|---|---|---|
| 技术栈 | Node 22.19+ | 纯 Python stdlib，零第三方依赖 |
| 运行形态 | 常驻 agent 舰队 | 本地网关 + 定时维护 |
| 依赖 | gh CLI、专用 GitHub 账号 | 无 |

把一个 Node 常驻系统塞进以「零依赖」为身份认同的产品里，等于亲手拆掉自己的卖点。竞品分析已经确认我们的差异化三角是「自维护 × 中文生态 × 事实表闭环」——底座外置不损害任何一角，反而强化「自维护」的可信度（连维护我们的 agent 都是外挂的、可替换的）。

### 3.2 为什么必须接

自维护产品的终局问题是：**维护层自己的大脑从哪来**。当前 PlanRouter 的 L3（LLM 分析建议）用的是维护池里轮询出来的免费小模型，天花板明显。Super-agent 提供了一个现成的、开源的、带完整权限与观察模式的「外部大脑」候选——这正是它和我们的接口。

---

## 4. 三层对接方案

### 层面 1（立即可做，零代码）：PlanRouter 当它的模型底座

DSH 原生支持 **Custom model API**：在 DSH Settings → Models 里填入任意 OpenAI 兼容端点即可。

```
Base URL:  http://127.0.0.1:8787/v1
API Key:   listen.api_key（PlanRouter 监听密钥）
协议:       OpenAI 兼容
```

理想的模型分配：

| DSH 角色 | 走 PlanRouter | 理由 |
|---|---|---|
| Router | `facet-cheap`（cheap 路由链） | Router 只做分派，免费小模型足够 |
| Worker | default 路由链（强模型优先） | 干活需要真能力 |

**待实测**：原型是否支持 Router 与 Worker 分别指定模型。若支持，这是最理想形态；若不支持（全舰队共用一个模型），则退化为 Worker 档位选型问题。

这一层做完，我们得到一个真实场景的验证：**免费额度聚合网关能不能撑起一支 agent 舰队的日常开销**——这本身就是产品叙事的一部分。

### 层面 2（中期）：Super-agent 当维护层的外部大脑

让 super-agent 盯 PlanRouter 自己的代码仓库，走 issue → PR 做开发维护；运行时运维不碰文件，走管理 API：

- policy 中写明：运维操作通过 `curl 127.0.0.1:8787/api/*` + admin key 完成，**禁止直接改配置文件**；
- 所有验证闸门（探测、quiz 式测验、淘汰）留在网关侧，agent 只发起、不裁决。

这对应 L3/L4 的升级路径：现在的 LLM 建议由维护池小模型生成，升级为由外部 super-agent 生成——能力边界不变，大脑升级。

### 层面 3（借理念不接代码）：三条可直接吸收的设计原则

1. **能力边界靠移除工具，不靠提示词**：Router 不配 Bash 是「物理防呆」。对应到 PlanRouter 维护层——想限制 agent 能做什么，从它的工具面里删掉，而不是在提示词里求它。
2. **外部 Watchdog 巡检维护层**：用一个独立进程检查维护层自身是否在做无用功（重复探测、无效修复、报告写不落盘）。这与我们已有的「目录确认 → LLM 分析」两级结构同构。
3. **Fast/Slow Path 两级过滤**：便宜规则先过滤（L2 目录 diff 自动修复），贵的 LLM 判断只处理漏网之鱼（L3）。我们的 L1–L4 阶梯天然就是这个形状，可以在文档与实现上更明确地对齐这个术语。

---

## 5. 明确不做

| 不做 | 理由 |
|---|---|
| 把 dsh-superagent 嵌入 PlanRouter | Node 栈与零依赖身份冲突；原型本身标注 research prototype，未达到可分发成熟度 |
| 依赖它的蓝图等未实现功能 | 原型明确标注未实现 |
| 让它在生产配置上 `danger-full-access` | 信任红线：绝不烧错付费额度；隔离环境 + observe 起步 |

定位一句话：**dsh-superagent 是跑在 PlanRouter 上的第一批超级客户，不是我们的组件。**

---

## 6. 行动清单

| 优先级 | 动作 | 前置条件 |
|---|---|---|
| P0 | DSH 接 PlanRouter 实测：Custom model API 填网关端点 | Node 22.19+、gh CLI、专用 GitHub 账号 |
| P0 | 验证 Router / Worker 能否分别指定模型 | 同上 |
| P1 | 观察模式跑通后评估 Worker 模型档位（facet-cheap 够不够） | P0 完成 |
| P2 | policy 草案：运维走 `/api/*` + admin key，禁改配置文件 | P0 完成 |
| P3 | 吸收层面 3 三条原则到维护层文档与实现 | 无硬依赖，可渐进 |

---

## 7. 资料来源

1. DeepSeek Harness 团队，dsh-superagent-prototype 仓库（GitHub，MIT）——架构文档、README、原型局限说明
2. dsh-superagent-prototype 发布公告与社区讨论（2026-10-31 前后）
3. PlanRouter 竞品分析报告（本仓库 docs/planrouter-competitive-analysis.md）——差异化三角与信任红线结论
4. PlanRouter 代码仓库自评（App 类 / 维护层 L1–L4 阶梯 / 管理 API 面现状）
