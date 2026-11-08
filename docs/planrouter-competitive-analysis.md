# 免费 Token 聚合网关 · 竞品分析报告

> **分析对象**：PlanRouter（定位：自维护的免费额度聚合器，网关是其产品形态）
> **竞品范围**：6 家 / 4 类 · **数据截至**：2026-11-08
> **方法**：公开资料桌面研究 + 产品定性评估

---

## 1. 摘要与核心结论

「把散落在 N 家厂商的零碎免费额度，整合成一个用户不用管的池子」——**这已经不是空白市场**：托管侧有 OpenRouter 免费层和 API Keychain，自托管侧有 freellmpool、freelm 等 pip 包，灰色地带还有 66k 星标的 g4f。池化、故障转移、配额冷却正在**快速商品化**。

但调研同时验证了两处**结构性空白**：

1. **没有任何一家产品把「网关自己修自己」（自维护）当作品牌承诺**——最接近的竞品 freellmpool 在文档里明写目录漂移「靠一行 PR 修」；
2. **中文厂商的免费额度（火山方舟、智谱、百炼、千帆、混元、魔搭、硅基流动）在所有竞品目录里几乎是零覆盖**。

这两处空白恰好是 PlanRouter 的两块长板，叠加事实表（217 家厂商 / 约 2,400 模型的 quota_tier 侦察数据）构成的独家弹药，形成「**自维护 × 中文生态 × 事实表闭环**」的差异化三角。

**一句话结论**：池化免费额度是入场券而非护城河；「自维护」定位全球无人占位，中文生态是结构性空白——差异化三角成立，但获客体验（零密钥首启、知名模型名自动映射）必须向 freellmpool 抄作业。

---

## 2. 产品定义与市场背景

### 2.1 什么是「免费 Token 聚合网关」

它回答普通用户的一个具体痛点：手里有若干家厂商的免费额度（注册送的、套餐送的、永久免费层），但每家都有自己的端点、密钥、模型清单和限额窗口；模型三天两头下架、额度随时重置，追着 N 个控制台跑的精力成本远超额度本身的价值。

聚合网关把这一切收拢成**一个本地或云端 URL**：自动故障转移、配额冷却、请求照常走 OpenAI / Anthropic 兼容协议。

### 2.2 「免费」的三种含义

按 OpenRouter 2026 年横评划分，市面上的"免费 LLM API"分三类，聚合对象主要是前两类：

| 类型 | 特征 | 代表 |
|---|---|---|
| **永久免费层** | 不限期，只需管理限速 | OpenRouter 免费层、Google AI Studio、Groq、Cerebras、Mistral Experiment |
| **试用额度** | $1–$30 或 10M token 一次性预算 | Baseten $30、DeepSeek 10M token |
| **本地推理** | 无 token 费但吃硬件与运维 | Ollama / vLLM |

### 2.3 原料层：免费额度本身在变大变散

聚合网关的「原料」是各家免费层。2026 年代表性限额：

| 厂商 | 免费内容 | 限额（免费层） | 备注 |
|---|---|---|---|
| OpenRouter | 20+ 免费模型（多厂商） | 20 RPM · 50 请求/天（充 $10 解锁 1000/天） | 失败请求也计数 |
| Groq | Llama 3.3 70B 等，LPU ~320 tok/s | 30 RPM · 1,000 请求/天 | 延迟敏感场景首选 |
| Cerebras | Llama / Qwen / DeepSeek 蒸馏版 | 30 RPM · ~1M token/天 | 吞吐型批量任务 |
| Mistral | Codestral、Small/Large（Experiment 档） | ~1B token/月 | 需同意数据训练 |
| Google AI Studio | 8 个 Gemini/Gemma 变体 | 5–15 RPM · 20–1,500 请求/天 | 1M 上下文；EU 外默认训练 |
| GitHub Models | GPT-4o、Claude 3.5 Sonnet、Llama、Phi | 15 RPM · 150–1,000 请求/天 | GitHub 账号即可 |
| Cohere | Command R+ | ~100 请求/天 | 仅限非商用 |
| NVIDIA NIM | Nemotron、Llama 系 | ~1,000 请求/天 | — |
| SiliconFlow 硅基流动 | Qwen2.5-7B 等开源模型 API | 多模型永久免费 + 新用户赠 ¥14 | 国内最大开源算力平台 |
| 魔搭 ModelScope | Qwen / DeepSeek 等共享推理层 | 共享免费层 | 阿里生态 |
| 火山方舟 / 智谱 / 百炼 等 | doubao、GLM、Qwen 系免费/赠送额度 | 各家口径不一、随活动变动 | PlanRouter 注册表内置覆盖 |

要点：**单一家的免费层是个玩具，十六家叠起来才是每天数万级请求的产能**——这正是聚合网关存在的理由；但限额口径五花八门（RPM / RPD / token / neuron），且「免费层的隐藏成本」（数据训练、缩水上下文、低量化）全部要由聚合层替用户把关。

中国市场另有 143 家以上 API 中转站构成的灰色生态，免费额度信息高度碎片化，社区靠手工清单（cheahjs/free-llm-api-resources）和「白嫖指南」型内容补位。

---

## 3. 竞品地图

按「部署形态 × 目录维护方式」两个正交维度，市场玩家分为四类，外加一层原料供应商：

| 类别 | 形态 | 代表 | Keys 归属 | 目录谁维护 |
|---|---|---|---|---|
| **A · 托管式聚合** | 云服务，零安装 | OpenRouter 免费层、API Keychain、InferAll | 厂商侧（或 BYO 加密托管） | 厂商（闭集，用户无感） |
| **B · 自托管池客户端** | pip 包，本地进程 | freellmpool、freelm | 用户本机 | **用户（人肉 PR / 手改配置）** |
| **C · 灰色逆向** | 逆向网页接口 | g4f（GPT4Free） | 无 key（逆向） | 社区追着修 |
| **D · 通用网关** | 渠道聚合，无免费聚焦 | one-api / new-api、LiteLLM | 用户本机 | **用户（手动运维）** |
| 原料层 | 各家官方免费层 | Groq、Cerebras、Gemini、SiliconFlow、魔搭、火山… | — | 厂商 |
| **★ PlanRouter** | **自托管服务 + 控制台** | 本报告分析对象 | 用户本机 | **系统自动（L1–L4 自治）** |

四象限视角：

```
                自动维护
                   │
   【自托管+自动】  │  【托管+自动】
    PlanRouter ★   │  OpenRouter / API Keychain / InferAll
                   │  （厂商在自己服务端修，对你不可见）
人工维护 ──────────┼────────── 托管服务
   【自托管+人工】  │  【托管+人工】
   one-api/LiteLLM │  （基本无人）
   freellmpool/freelm
   g4f             │
```

**左上角象限整块无人**——「数据不出门 + 系统自己活着」的组合没有任何竞品提供。

---

## 4. 逐家深度分析

### 4.1 OpenRouter 免费层（托管式聚合 · 市场教育者）

一个 key 覆盖 70+ 厂商的路由层，月处理 token 百万亿级，免费层只是获客漏斗的最上层。

- **关键数据**：20+ 免费模型；免费账户 20 RPM / 50 请求/天；充 $10 解锁 1,000/天；**失败请求同样计数**，负余额直接禁用免费模型
- **优势**：模型广度与协议兼容性第一；教育入口（13 家免费 API 横评长文）被它占据；$10 解锁设计把免费用户平滑转成付费
- **弱点**：keys 与流量经过第三方，隐私/合规敏感场景不可用；50 请求/天对重度用户形同虚设；目录闭集、厂商维护，用户无法干预路由策略

### 4.2 API Keychain（托管式聚合 · BYOK）

定位「one API key for every free」：用户把自己的免费层密钥粘贴进去（认证加密存储），换来一个 `ak-` 统一密钥。

- **关键数据**：12 家 provider、69 个免费模型；effort 三档级联路由（`keychain-low/medium/high`）；原生 Anthropic Messages（Claude Code 直接用）；配额冷却 + 用量分析
- **优势**：对「普通用户」体验最顺——不用装任何东西，一个 URL 即用；effort 三档路由是优秀的产品抽象
- **弱点**：**密钥托管在第三方服务器**（免费聚合换来交出钥匙）；目录闭集 69 个模型，**12 家 provider 全是西方厂商，中文生态零覆盖**；托管形态从产品结构上锁死了本地化与自主权需求

### 4.3 freellmpool（自托管池客户端 · 最直接竞品）

16 家免费层池化成一个 `/v1` 端点的 pip 包。

- **关键数据**：16 provider / 56 模型；3 家免密钥端点（Pollinations、OVHcloud、LLM7）实现**零配置首启**；真流式 + 工具调用；按天配额跟踪（UTC 午夜重置）；知名模型名自动别名（`gpt-4o-mini` 静默落到免费模型）；Anthropic shim + Codex Responses shim；免费 embeddings；MCP server；仅 1 个 httpx 依赖
- **关键原话**：「Free tiers change without notice. Endpoints, model ids, and limits drift — **that's what the one-line providers.toml PRs are for**.」——**目录漂移靠用户提 PR 人肉修**。这句话就是「自维护」差异化的直接竞品证据
- **优势**：零密钥首启 + 模型名自动别名，获客钩子做得最滑；正确性设计到位；keys 不出门
- **弱点**：无自治层（目录/限额/端点漂移全靠人工 PR）；无控制台、无订阅台账、无成本层；中国厂商仅 Z.ai 与 LongCat 两家

### 4.4 freelm（自托管池客户端）

Python 客户端/网关二合一：聚合 OpenRouter、Gemini、NVIDIA NIM、Groq、Cerebras、Mistral 六家免费层，主打多账号 API-key 轮换、跨厂商 failover、熔断与限速感知路由，以及「live free-model discovery」——最接近自动发现的竞品实现，但停留在客户端级，没有维护闭环与自愈动作。

### 4.5 g4f / GPT4Free（灰色逆向 · 规模巨人）

通过逆向各厂商网页接口与浏览器自动化取用模型，66,664 stars，v8.4.5（2026-09）仍在高频发版，GPLv3。

模型池含各家旗舰，但 **ToS 违规、随时封禁、接口随版本漂移**，只服务灰色需求，与合规产品不构成正面竞争——却常年占据「免费」心智入口。

### 4.6 one-api / new-api / LiteLLM（通用网关 · 无免费聚焦）

自托管渠道聚合的事实标准：任意兼容端点收进一个面板，手动维护渠道、密钥、模型清单。中国「中转站」生态（143 家以上）大量以 one-api 系为底座。

它们证明了「聚合」本身没有护城河——**把聚合做成自维护，才是这片红海里没人做的蓝海**。

---

## 5. 多维对比矩阵

| 维度 | OpenRouter | API Keychain | freellmpool | g4f | one-api/LiteLLM | **PlanRouter** |
|---|---|---|---|---|---|---|
| 形态 | 托管 | 托管 | 自托管 pip | 自托管 pip | 自托管服务 | **自托管服务** |
| keys 归属 | 用厂商的 | 交给厂商加密托管 | 本机 | 无（逆向） | 本机 | **本机** |
| 免费来源 | 厂商池 | 你的免费 key | 你的免费 key | 逆向接口 | 自配渠道 | **免费 key + 订阅额度** |
| 目录维护 | 厂商做 | 厂商做（闭集 69 模型） | **人肉 PR** | 社区追修 | **纯手动** | **自动（L1–L4）** |
| failover/冷却 | 有 | 有 | 有 | 不稳定 | 部分 | 有（含半开探测、配额预判） |
| 中文厂商覆盖 | 少量 | **0** | 2（Z.ai/LongCat） | — | 3（自配） | **13 家内置注册表** |
| 订阅/成本层 | 无 | 用量分析 | 无 | 无 | 无 | **订阅台账 + 月度报表** |
| 客户端协议 | OpenAI | OpenAI + Anthropic | OpenAI + Anthropic shim | OpenAI 系 | OpenAI/Anthropic | **双协议原生 + Responses 桥接** |
| 品牌承诺 | 一个 key | 一个 key | 零配置 | 白嫖 | 聚合 | **零维护** |
| 开源 | 否 | 否 | 是 | 是（GPLv3） | 是 | 是 |

**六维能力雷达（1–5 定性评分）**：

| 维度 | OpenRouter | API Keychain | freellmpool | one-api | PlanRouter |
|---|---|---|---|---|---|
| 目录自治 | 3 | 4 | 2 | 1 | **5** |
| 中文生态 | 2 | 1 | 2 | 3 | **5** |
| 隐私与控制 | 2 | 2 | 5 | 5 | **5** |
| 上手便利 | 5 | 5 | 4 | 2 | 3 |
| 免费额度厚度 | 2 | 4 | 4 | 1 | 4 |
| 协议兼容 | 5 | 4 | 5 | 4 | **5** |

聚合广度对比：freellmpool 16 厂商/56 免费模型、API Keychain 12/69、PlanRouter 13 厂家内置注册表（免费模型数取决于用户渠道配置）、OpenRouter 免费层 20+ 模型（未披露厂商拆分）、freelm 6 厂商。

值得注意的是**「广度」并不构成壁垒**：真正拉开身位的是「目录自治」与「中文生态」两维——前者决定维护成本，后者决定人群规模。

---

## 6. 关键发现

### 发现 1：「池化免费层」已经商品化

pip 侧至少三个包在做同一件事，托管侧两家，灰色侧一家 66k 星。聚合、failover、配额冷却正在成为**所有网关的标配**——这一层会继续卷，卷到没利润。产品定位必须落在标配之上。

### 发现 2：「自维护」定位全球无人占位（最重要）

检索 "self-maintaining LLM gateway / auto-remediation" 得到的全是**学术论文**（自进化技能库的 Library Drift、REPAIRGATE 修复门）和企业 LLM 运维手册，**没有一家商业网关产品把「自己修自己」当卖点**。freellmpool 甚至在文档里明写目录漂移「靠一行 PR 修」。左上象限空着，先到先得。

学术侧的呼应：Library Drift 论文指出自进化系统的失效模式是「无结果驱动的生命周期管理导致积累漂移」——PlanRouter 维护池的 **quiz 闸门 + 运行时淘汰**恰好就是这个缺失件的基础设施级实现；「验证在先、自主在后」的 REPAIRGATE 思路与 quiz 门设计同构。

### 发现 3：中文生态是最大的结构性空白

API Keychain 目录 12 家 provider 清一色西方厂商；freellmpool 16 家里只有 Z.ai 和 LongCat；火山方舟、智谱、百炼、千帆、混元、魔搭、硅基流动的免费/赠送额度**没有任何一个聚合器系统覆盖**——对中文用户这不是长尾，是主粮。事实表 217 家厂商的数据底座在此场景直接变现。

### 发现 4：用户分层真相——「普通用户」住在托管那边

API Keychain / OpenRouter 的用户不装任何东西。自托管形态天然服务「要隐私、要控制权、key 不出门」的人群（个人开发者 / 小团队）。获客叙事应当是「freellmpool 的零配置便利 + 托管商给不了的自主权」，而不是与托管商比赛省事。

### 发现 5：事实表的真正对手不是网关，是社区手工清单

社区正典是 cheahjs/free-llm-api-resources 那份 README，连 OpenRouter 的横评都拿它当校验源。事实表要做的是「比 README 更结构化、带 schema 承诺、机器可消费」——而它最好的下场是被网关自己消费掉（quota_tier 侦察 → 免费池收割 → 可用性回写），正好闭合产品飞轮。

---

## 7. PlanRouter 定位与行动建议

### 7.1 差异化三角

| 顶点 | 内容 | 竞品现状 |
|---|---|---|
| **自维护（全球唯一）** | L1 冷却 → L2 目录修复 → L3 LLM 建议 → L4 系统自选大脑（quiz 选拔 + 运行时淘汰） | 要么让用户人肉修，要么把维护藏进厂商后端 |
| **中文生态（结构性空白）** | 13 家内置注册表覆盖火山/智谱/百炼/千帆/混元/小米/DeepSeek 等 | 竞品对中国免费额度覆盖趋近于零 |
| **事实表闭环（独家弹药）** | 217 厂商 / ~2,400 模型的 quota_tier 侦察层：表找免费额度在哪 → 网关收进池子 → 真实可用性回写 | 竞品目录是静态清单，这是越用越准的数据飞轮 |

三角缺一不可：freellmpool 有便利没自治，API Keychain 有托管没中文，OpenRouter 有流量没自托管。**三个顶点都占的，市场上只有 PlanRouter。**

产品叙事定型：**「给 LLM 基础设施做 AI SRE」——免费额度是获客钩子，自维护是留存本体，订阅/成本层是变现扩展。**

### 7.2 行动建议

**要抄的作业（把获客钩子做滑）：**

1. **零密钥首启**：内置 1–2 个免密钥端点（如 Pollinations / OVHcloud），让用户 `pip install` 后第一分钟就出第一个字，再引导补 key 扩池；
2. **知名模型名自动映射**：用户带着 `gpt-4o-mini` / `claude-sonnet` 进来不报错、静默落到池内免费模型；
3. **控制台即正门**：不会写 JSON 的用户从厂家预设 + 填 key 的流程进入（能力已具备，需成为默认叙事而非后门）。

**要守的红线（信任即产品）：**

1. **绝不烧错付费额度**：自治层的全部价值押在「从不出错地花钱」上，烧错一次信任清零——渠道 tier 语义必须顺着用户直觉（标 free 即全 free），任何 L4 动作都要过 quiz 式闸门；
2. **唯一健康指标前置**：池内模型本周实际服务成功率（免费 token 被真实用掉的比例）放控制台最显眼处，其余运维细节保持沉默；
3. **文档同步**：把「为什么不用手动维护」写成 FAQ——每次用户问「为什么还要手动做 X」，都是自治层的 bug 报告。

**要防的威胁：**

1. **教育入口被占**：OpenRouter 的横评长文是「免费 API」心智的 SEO 阵地，需要一篇对位的中文深度内容（「免费额度聚合的正确姿势」）占住中文搜索位；
2. **竞品跟进自治**：freellmpool 的 providers.toml 加一个定时校验器就能宣称「半自动」——护城河不在概念而在 L4 深度 + 中文注册表 + 事实表数据的叠加，趁窗口期把纵深做出来。

---

## 8. 资料来源

1. [OpenRouter — Free LLM APIs Compared: Rate Limits, Models, and Real Costs (2026)](https://openrouter.ai/blog/tutorials/free-llm-apis-compared/) — 13 家免费 API 横评、限额与隐藏成本数据
2. [OpenRouter Pricing](https://openrouter.ai/pricing) — 免费模型限额（20 RPM / 50 天），$10 充值解锁 1000/天
3. [OpenRouter Support — Rate Limits](https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know) — 失败请求计数与余额规则
4. [freellmpool 0.8.0 · PyPI](https://pypi.org/project/freellmpool/0.8.0/) — 16 家免费层池化、零密钥首启、目录靠 PR 维护的原话
5. [freelm 0.2.0 · PyPI](https://pypi.org/project/freelm/0.2.0/) — key 轮换、熔断、live free-model discovery
6. [API Keychain 官网](https://www.apikeychain.dev/) — 12 provider / 69 免费模型、effort 三档路由、BYOK 加密托管
7. [InferAll 官网](https://inferall.ai/) — Anthropic 兼容网关，NVIDIA 托管开源模型免费层
8. [AnyGen — gpt4free 档案](https://www.anygen.io/showcase/gpt4free/index.html) — v8.4.5（2026-09）、66,664 stars、GPLv3
9. [SiliconFlow 产品简介](https://docs.siliconflow.cn/cn/userguide/introduction) — Qwen2.5-7B 等开源模型免费、新用户赠 ¥14
10. [EggStriker.AI — 中转站测评榜单 2026](https://www.eggstriker.com/ai-api) — 143 家 AI API 中转站全景对比，硅基流动 / 魔搭免费层
11. [iGetoken — 免费 Token 白嫖指南](https://igetoken.com/) — 免费额度信息碎片化的社区补位样本
12. [arXiv 2605.19576 — Library Drift](https://arxiv.org/pdf/2605.19576) — 自进化系统缺结果驱动生命周期管理的失效模式
13. [ACL Findings EMNLP 2026 — REPAIRGATE](https://aclanthology.org/2026.findings-emnlp.648.pdf) — 「验证在先、自主在后」的修复门机制

---

*本报告基于公开资料桌面研究编制；对 PlanRouter 的评估基于其当前实现与内部定位讨论。竞品数据以各来源发布时点为准，免费层限额变动频繁，落地决策前请复核最新数值。*
