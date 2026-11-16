## Facet API Key Guide

**本手册怎么用**：按厂家分节，每节给出「注册 → 拿 Key → 填进 Facet」的完整步骤。
文中所有 📷 是**截图位**——按说明操作到对应页面时截图，保存到
`docs/images/api-key-guide/` 目录并使用标注的文件名，图片会自动显示在本文档中。

**为什么值得照着注册（不花您的钱）**：国内主流厂家（百炼、火山、魔搭、智谱、千帆、DeepSeek、Kimi、MiniMax）
都给实名个人账号**免费额度**——新模型 90 天窗口、每日登录领魔粒、新人 100 万 token……
这些额度**不用就作废，用了不扣您的钱**。用户不知道有免费、也不知道去哪领，是普遍痛点；
本手册把注册入口、Key 位置、免费额度规则聚合在一起，照着点一遍即可。Facet 还会**每日自动对齐**
哪些模型还有免费额度（见 [4.3 百炼](#43-阿里云百炼) 的 `usage free` 定时任务），帮您把白嫖额度一直用满。

真正「匿名免注册」现在几乎绝迹（多数平台都以注册送免费额度为前提），
其余各家**注册是拿免费额度的前提**，不是收费门槛。

密钥属于敏感信息：截图前请**遮住或截掉完整 Key**，只保留开头几个字符（如 `nvapi-…`）。

平台界面会随时间变化，本手册按 2026-11 时点整理；若界面不同，以实际为准并顺手更新本文。

---

## 目录

- [一、密钥怎么填进 Facet](#一密钥怎么填进-facet)
- [二、厂家总览](#二厂家总览)
- [三、免费渠道（零成本起步）](#三免费渠道零成本起步)
- [四、国内主流厂家](#四国内主流厂家)
- [五、海外按量付费厂家](#五海外按量付费厂家)
- [六、订阅聚合类](#六订阅聚合类)
- [七、在 Trae / IDE 里使用 Facet](#七在-trae-ide-里使用-facet)
- [八、常见问题](#八常见问题)

---

## 一、密钥怎么填进 Facet

渠道配置时的关键字段：

| 字段 | 说明 | 示例 |
|---|---|---|
| **协议** | 决定请求格式（OpenAI/Anthropic/共享/自定义） | `openai`、`anthropic` |
| **端点** | API 服务地址 | `https://api.openai.com/v1` |
| **模式** | `payg`（按量）/`agent`（套餐/`token_plan`）/`coding`（编程套餐） | 参见各厂家章节 |
| **免费额度** | `free_quota`（tokens + period）或 `free_limits`（rpm/rpd/note） | 参见各厂家章节 |
| **Key** | 平台颁发的调用凭据 | `sk-proj-...`、`sk-ant-...` 等 |

**模式对照**：

| 模式键 | 对应计费类型 | 典型厂家 |
|---|---|---|
| `payg` | 按量付费 | 大部分厂家 |
| `agent` / `token_plan` | Token 包月/套餐 | xiaomi、bailian、hunyuan、qianfan、minimax |
| `coding` / `coding_plan` | 编程套餐 | zhipu、volcengine、zai、commandcode |

---

## 二、厂家总览

| # | 厂家 | 免费 | 付费形态 | 备注 |
|---|------|------|----------|------|
| 1 | 火山引擎方舟 | ✅ 各模型约50万tokens/模型 | payg + coding_plan + agent_plan | 一致；遗漏企业版Coding Plan |
| 2 | 智谱 GLM | ✅ 注册送2000万tokens + Flash系列免费 | payg + coding_plan | payg应加免费标注 |
| 3 | 阿里云百炼 | ✅ 新人每模型100万tokens(90天) | payg + token_plan | 一致；遗漏旧版Coding Plan |
| 4 | 魔搭 ModelScope | ✅ 魔粒免费（登录 200/日） | 无 | 免费靠魔粒抵扣（0.5/1/2 粒/次，随模型）；完善邮箱即可用，无需阿里云实名 |
| 5 | 小米 MiMo | ✅ 新人¥10体验金(40天) | payg + token_plan | payg应加免费标注 |
| 6 | Kimi (月之暗面) | ✅ 新人15元代金券(3个月) | payg（API无订阅） | payg应加免费标注 |
| 7 | DeepSeek | ❌ 无明确免费额度 | payg（峰谷计价） | 一致 |
| 8 | 腾讯混元 | ✅ 开通送100万tokens(1年) | payg + token_plan(TokenHub) | **已补 token_plan 模式**：Token Plan 28/78/238/468 元/月；payg 加免费额度 100 万 tokens/1 年 |
| 9 | 百度千帆 | ⚠️ 新人免费额度以控制台领取页为准 | payg + token_plan + 算力包 | **已补 token_plan 模式**；Token Plan 个人版五档 Mini/Lite/Standard/Pro/Max（Mini ¥4.9 为首购限时价，Standard ¥49.9）；免费额度口径以官方活动页为准 |
| 10 | Z.AI 智谱海外 | ✅ GLM-4.7-Flash等完全免费 | payg + coding_plan($18起) | **已补 coding_plan 模式**；Coding Plan Lite $18/Pro $80/Max $168/月；payg 加免费模型标注（GLM-4.7-Flash/4.5-Flash/4.6V-Flash 完全免费，其余按量计费） |
| 11 | Grok xAI | ❌ 无标准免费层（活动:数据共享换$150/月） | payg | 基本一致 |
| 12 | OpenAI | ❌ 新账号无赠送credits | payg（订阅不含API） | 一致 |
| 13 | Anthropic | ⚠️ 一次性$5试用 | payg（Max/Ultra订阅送API credits） | 一致；可加免费note |
| 14 | OpenCode | ✅ 12+免费模型 | payg + agent_plan(Go订阅) | Go $10/月、Go Plus $40/月、Go Teams $20/人/月；v2 文档含 console.opencode.ai/inference 域名变体 |
| 15 | Command Code | ✅ 免费模型1000请求/天 | 套餐订阅（Provider $15/月，含 $15 额度按量扣） | 免费模型随 agent 套餐 models 清单下发（tier: free） |
| 16 | LLM7 | ✅ 60次/分·250/时·10万tokens/24h | Pro $12/月订阅 | Pro $12/月 为官网订阅档，注册表仅登记免费 payg 模式（见 6.3） |
| 17 | NVIDIA NIM | ✅ 免费层=速率限制（约 40 RPM，控制台右上角显示） | ❌ 无按量付费通道；提额需付费部署/自托管 | 免费层是速率限制非 credits；10,000 请求/日为页面嵌入值、非官方承诺 |
| 18 | OpenRouter | ✅ :free模型50→1000次/天 | payg（预付credits，5.5%手续费） | 一致 |
| 19 | MiniMax | ⚠️ 新人赠金（一次性，以账户页为准） | payg + Token Plan 包月 | **已接入**：余额/Token Plan 余量查询；双 Key 体系（`sk-api-` 按量 / 订阅 Key Token Plan） |

---

## 三、免费渠道（零成本起步）

### 3.1 NVIDIA NIM——免费层

**官方入口**：[https://build.nvidia.com/](https://build.nvidia.com/)

**使用方式**：
- **速率限制而非余额**：登录 [build.nvidia.com](https://build.nvidia.com)，导航栏右上角显示账号的最大速率限制（例如 "Up to 40 RPM"），不是余额。官方 FAQ 原话："Your personal rate limit is shown in the top-right of the dashboard"。
- **请求速率**：多数模型最高 40 RPM，无 per-token billing（官方 FAQ 公开口径）。
- **每日上限**：10,000 requests/day——存在于页面嵌入 JSON，**非官方承诺值**，dashboard 未必显示。
- **每模型具体值**：不公布，随模型、用途、当前总流量浮动；版主明确 "The rate limits vary for each model, and we do not publish those"。
- **试用期限**：无时间限制（"The trial period is not limited by a time period"）。
- **提额**：免费档无官方通道——想提高只能付费部署或自托管 NIM。

**步骤**

1. 打开 [build.nvidia.com](https://build.nvidia.com)，点「创建账户」用邮箱注册；
   ![NVIDIA 创建账户页（邮箱已打码）](images/api-key-guide/nvidia-01-signup.png)
2. 首次生成 Key 前需验证手机号（一次性 OTP），官方提示验证通过后账号才能生成 API Key；
   ![NVIDIA 手机号验证弹窗](images/api-key-guide/nvidia-02-signup.png)
3. 登录后展开右上角账号菜单——这里就是官方 FAQ 说的「速率限制显示位置」（示例账号显示 "Up to 40 rpm"）；菜单内点 **API Keys** 进入密钥页；
   ![NVIDIA 账号菜单：右上角速率限制与 API Keys 入口](images/api-key-guide/nvidia-02-keys-page.png)
4. 点 **Generate API Key**，填 Key 名称与有效期（默认 12 months），生成后立即复制保存。
   ![NVIDIA Generate API Key 弹窗](images/api-key-guide/nvidia-03-key-created.png)

**渠道参数**（在 Facet 中配置时）：

| 字段 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://integrate.api.nvidia.com/v1` |
| 模式 | `free`（免费层按速率限制运行，无 per-token 计费；限额随模型浮动，免费档无提额通道） |
| free_limits | `rpm: 40（官方口径上限）+ note：每日 10,000 请求为页面嵌入值、非官方承诺；试用无时间限制` |

**常见模型**：`nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3-super-120b-a12b`, `nvidia/mistral` 等。

**适用场景**：个人开发试用、快速原型验证、不需要大规模生产的场景。

---

### 3.2 OpenRouter——免费池

**官方入口**：[https://openrouter.ai](https://openrouter.ai)

**使用方式**：
- **免费模型**：25+ `:free` 模型，每日配额根据账号余额动态调整：
  - 余额 **< $10**：50 次/天
  - 累计充值 **≥ $10**：1,000 次/天
  - 同时共享率限制：20 RPM（每分钟请求数）
- **付费模型**：预付 credits 按量（pass-through 原价），Standard 档购值收 5.5% 手续费（Business 8%），BYOK 每月 $25,000 推理额免平台费。

**步骤**

1. 打开 [openrouter.ai](https://openrouter.ai)，点 **Get API Key** 注册登录；
   ![OpenRouter 官网首页：Get API Key 入口](images/api-key-guide/openrouter-01.png)
2. 进入 **Activity** 仪表盘，可查看消费（Total spend）、请求量、token 用量与缓存命中率等汇总；
   ![OpenRouter Activity 仪表盘](images/api-key-guide/openrouter-01-dashboard.png)
3. **Models** 页搜「free」筛出当前所有 `:free` 免费模型（输入输出均 $0/M tokens），示例：NVIDIA Nemotron 3 Ultra (free)、Nemotron 3.5 Super VL (free)、Baidu ERNIE-5.5-Flash-Preview (free)；
   ![OpenRouter Models 页搜索 free 模型](images/api-key-guide/openrouter-02-free.png)
4. 侧栏 **Keys** → **New key** 创建 API Key（可为单 Key 设 Key limit 消费上限），创建后立即复制。
   ![OpenRouter API keys 页：侧栏 Keys → New key](images/api-key-guide/openrouter-03-keys.png)

**渠道参数**（在 Facet 中配置时）：

| 字段 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://openrouter.ai/api/v1` |
| 模式 | `payg`（预付 credits） |
| free_limits | `note: :free 模型 50→1000次/天（余额<$10→50/≥$10累充1000），20 RPM` |

**常见免费模型**：`nvidia/nemotron-3.5-lightning:free`, `google/gemma-4-31b-it:free`, `nvidia/nemotron-3-ultra-550b-a55b` 等（随平台更新而变化）。

**适用场景**：希望同时使用多家厂家模型、免费额度有需求的开发者、跨平台兼容性要求。

---

## 四、国内主流厂家

### 4.1 火山引擎方舟

**官方入口**：[https://console.volcengine.com/ark](https://console.volcengine.com/ark)

**第一步：开通服务，并开启「免费额度用完即停」**

- 实名账号登录后按提示开通方舟模型服务。只有开通后，各模型才会发放免费推理额度（约 **50 万 tokens/模型**，一次性，仅抵扣按量后付费）。没开通时拿不到额度，调用会直接计费。
- **按量付费是后付费结算**：免费额度用完后，如果没有停服保护，请求会继续走付费通道并产生扣款。开通页 / 模型广场里的「安心体验模式 / 免费额度用完即停」务必打开——这是避免后付费扣款的关键，不要只依赖余额告警。
- 开通后再去模型广场逐个开通要用到的模型，免费额度是按模型分别发放的。

![火山引擎注册 / 登录页（手机号登录，支持豆包账号）](images/api-key-guide/volcengine-01.png)

![方舟「开通管理」：领取免费资源包，各模型免费推理额度（剩 499,398 / 共 500,000 tokens）与用量上限开关、关闭服务入口](images/api-key-guide/volcengine-01-free-quota.png)

**第二步：建只读 IAM 子账号，用来查免费 token 剩余额度**

- 免费 token 余量不在数据面 API Key 里，要通过管理面 OpenAPI（`GetPersonalPlan` 等，AK/SK 签名）查询，所以**必须用 IAM 的 AK/SK**，不能用 `ark-` 开头的推理 Key。
- 到 [访问控制 → 用户](https://console.volcengine.com/iam/identitymanage/user) 新建一个**子账号**，访问方式选「编程访问 / API 调用」（无需控制台登录密码）。
- 权限设为**只读**：勾选系统策略 `ArkReadOnlyAccess` 即可，不要给主账号、也不要挂写权限（如 `ArkFullAccess`）。查询场景只读足够，能降低密钥泄露后的风险。
- 在该子账号详情页「密钥」Tab 新建密钥，得到 **Access Key ID（AK）** 与 **Secret Access Key（SK）**——SK 只在创建时显示一次，立刻复制保存。把 AK/SK 填到 Facet 火山渠道的「管理凭据 / credential_envs（`VOLC_ACCESSKEY`、`VOLC_SECRETKEY`）」里；它们只用于查额度与套餐余量，不是推理调用 Key。

![访问控制「API 访问密钥」：子账号创建 Access Key 成功弹窗，AccessKeyID 与 SecretAccessKey 均已打码](images/api-key-guide/volcengine-02-AKSK.png)

**第三步：按量付费、Agent Plan、Coding Plan 的 Key 各不相同，要分别获取**

三套凭据相互独立，**不能互换**，每个都要单独获取：

| 形态 | Key 类型 | 获取位置 |
|---|---|---|
| 按量付费 `payg` | `ark-` 开头的 API Key（数据面推理 Key） | [方舟 API Key 管理](https://console.volcengine.com/ark/region:cn-beijing/apiKey) |
| Coding Plan `coding` | Coding 套餐专用 Key | 同上页面，开通 Coding 套餐后才显示 |
| Agent Plan `agent` | Agent 套餐专用 Key | [Agent Plan 订阅页](https://console.volcengine.com/ark/region:cn-beijing/subscription/agent-plan?projectName=default) 开通后获取 |

注意区分：第二步建的是 **IAM AK/SK**（管理面，查额度用），这里三种是 **`ark-` 推理 Key**（数据面，调模型用），两套东西别填混；同厂家三种模式也各填各的 Key。

![方舟「API Key 管理」：左侧菜单 API Key 管理 →「创建 API Key」，得到 ark- 开头的推理 Key（按量付费用）](images/api-key-guide/volcengine-01-keys.png)

**第四步：三种形态的调用 URL 也不一样**

| 形态 | 协议 | `base_url` |
|---|---|---|
| 按量付费 `payg` | `openai` | `https://ark.cn-beijing.volces.com/api/v3` |
| Coding Plan `coding` | `openai` | `https://ark.cn-beijing.volces.com/api/coding/v3` |
| Agent Plan `agent` | `openai` | `https://ark.cn-beijing.volces.com/api/plan/v3` |

在 Facet 里选对模式后 URL 会自动带出；手工填写时务必按上表选，否则会出现 404 或「模型不存在 / 无权限」。路径只需填 `/chat/completions`（或留空自动补），**不要**在已含 `/v3` 的 `base_url` 后再拼 `/v1/chat/completions`，否则会变成 `/v3/v1/...` 而 404。

![订阅 → Agent Plan「我的订阅」：套餐档位、起止时间与 Agent 燃料值（AFP）用量](images/api-key-guide/volcengine-03-Agentplan.png)

![Agent Plan 订阅页「开始使用」区块：① 获取专属 API Key（套餐 Key 独立于按量 Key）② 配置 Base URL 与模型](images/api-key-guide/volcengine-03-Agentplan-key.png)

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量付费** | 计费：doubao-seed-1.6 输入 0.8~2.4 元/百万 tokens；免费额度用完即停 | - |
| **Coding Plan 个人版** | Lite 40 元/月、Pro 200 元/月（新用户首购特惠已于 2026-03-17 暂停） | - |
| **Coding Plan 企业版** | Team Lite 120 / Team Pro 600 元/月 | - |
| **Agent Plan 个人版** | Small 40 / Medium 200 / Large 500 / Max 1000 元 | - |

**模式说明**：

- `payg`：按量付费，免费额度 `free_quota: {tokens: 500000, period: "total"}`（约 50 万 tokens，一次性，**务必配合用完即停**）
- `coding`：Coding Plan 套餐，`tier: "paid"`，月付制，`base_url` 为 `/api/coding/v3`
- `agent`：Agent Plan 套餐，`tier: "paid"`，月付制，含 `billing: "agent_plan"` 标识，`base_url` 为 `/api/plan/v3`

**常见模型**：`doubao-seed-1.6`, `doubao-seed-32b`, `ernie-turbo`, `ernie-speed` 等。

**适用场景**：企业级应用开发、代码辅助、对话式 AI 等。

#### 套餐使用说明（Coding Plan / Agent Plan）

Facet 已接入火山套餐用量查询，在「套餐订阅」页和仪表板「订阅用量」卡里可直接看余量。

**Coding Plan**（编程包月）：

| 项目 | 说明 |
|---|---|
| 档位 | Lite / Pro（个人版），Team Lite / Team Pro（企业版） |
| 用量窗口 | **会话窗口**（session）、**周窗口**（weekly）、**月窗口**（monthly） |
| 余量口径 | 百分比（已用百分比，上限 100%）；不显示绝对 token 数 |
| 重置时间 | 厂家返回 `ResetTimestamp`（epoch 秒，`-1` 表示无重置计划） |
| 可用模型 | 以控制台套餐页实际显示为准；Facet 订阅行 `models` 字段记录当前映射（如 `doubao-seed-evolving`、`doubao-seed-2.1-pro`、`glm-5.3`、`deepseek-v4-pro`、`kimi-k3.1-pro` 等） |
| 用量查询 | 管理面 OpenAPI `GetCodingPlanUsage`（AK/SK 签名）；Facet 用渠道绑定的 AK/SK 自动拉取，TTL 5 分钟 |

**Agent Plan**（Agent 包月，AFP）：

| 项目 | 说明 |
|---|---|
| 档位 | Small / Medium / Large / Max |
| 用量窗口 | **5 小时窗口**（five_hour）、**日窗口**（daily）、**周窗口**（weekly）、**月窗口**（monthly） |
| 余量口径 | **绝对值**（Quota / Used，可显示已用 X / Y tokens），比 Coding Plan 的百分比更直观 |
| 重置时间 | 滚动窗口，厂家返回 `ResetTime`（毫秒时间戳自动转秒） |
| 可用模型 | 以控制台 Agent Plan 页为准（待官网核实：完整模型清单） |
| 用量查询 | 管理面 OpenAPI `GetAFPUsage`（AK/SK 签名）；Facet 自动拉取，TTL 5 分钟 |

**冷却联动**：套餐窗口的 `reset_ts` 会驱动渠道 429 冷却——某窗口到 100% 时，Facet 自动把该渠道冷却到窗口重置时间（最长 45 天），避免无意义重试。

---

### 4.2 智谱 GLM

**官方入口**：[https://www.bigmodel.cn](https://www.bigmodel.cn)

![BigModel 登录 / 注册弹窗：完成登录 / 注册开启开发之旅，支持手机号、账号、微信扫码与 CCF 会员授权登录](images/api-key-guide/zhipu-01.png)

**免费额度**：
- **注册送 2000 万 tokens 体验包**（注册页横幅实测文案，https://www.bigmodel.cn/invite）；
- **GLM-4-Flash 长期免费**（官方文档：https://docs.bigmodel.cn/cn/guide/start/pricing.md）；
- **GLM-4.7-Flash / FlashX 等限时免费**（以官网公告为准）；
- **邀好友领资源包**：控制台右上角活动入口，最高 **2 亿 Tokens**，是体验包用完后的补充免费来源。

![BigModel 登录 / 注册页：手机号、账号、微信扫码登录，横幅「新用户免费赠送专享 2000 万 tokens 体验包」](images/api-key-guide/zhipu-06.png)

![BigModel 官网首页：GLM-5.3 / GLM-5.3-FlashX / GLM Coding Plan 三大入口](images/api-key-guide/zhipu-02.png)

![控制台首页「立即体验」：GLM-5.3 对话入口与「API 接入」，右上角邀好友领取最高 2 亿 Tokens 资源包](images/api-key-guide/zhipu-03.png)

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量付费** | GLM-5.3 输入 8 元/输出 28 元/百万 tokens | - |
| **GLM Coding Plan**（积分制） | Lite 118 / Pro 538 / Max 1078 元/月（老用户保留 49/149 元） | - |
| **Coding 团队版** | 2 席位起购 | - |

**渠道参数**：协议 `openai`，端点 `https://open.bigmodel.cn/api/paas/v4`。

**Key 获取注意：标准 Key 与套餐 Key 分开管理**

- 「模型标准 API（按量计费/资源包）」与「套餐的 API Key」是两套凭据，自 10 月 16 日起在「API Key」页新建的 Key **仅可用于标准 API**（页面提示原文）；
- Coding Plan 的 Key 要在 Coding Plan 订阅流程里单独获取，不能拿标准 Key 调套餐额度，反之亦然——和火山引擎三种 Key 各自独立是同一个道理。
- 标准 Key 入口：控制台左侧「API Key」→「标准 API key」→「+ 新建API Key」。

![「标准 API key」管理页：标准 API（按量计费/资源包）与套餐 API Key 分开管理，新建 Key 仅用于标准 API（Key 已打码）](images/api-key-guide/zhipu-04.png)

![Coding Plan 套餐概览：未订阅时点「立即订阅」，左侧「用量统计」可查套餐用量](images/api-key-guide/zhipu-05.png)

**模式说明**：

- `payg`：按量付费，实际有免费层（注册赠金 + Flash 系列），建议在渠道配置中添加 note 标注
- `coding`：Coding Plan 订阅，`tier: "paid"`，月付制，含 `billing: "coding_plan"` 标识

**常见模型**：`glm-5.3`, `glm-5.3-flash`, `glm-5.3-flashx` 等。

**适用场景**：自然语言理解、代码生成、多模态交互等。

#### 套餐使用说明

- **按量付费（payg）**：Facet 已接入账户余额查询，渠道配置页渠道卡「厂家用量」行显示「余额 / 充值 / 累计消费」（接口 `GET /api/biz/account/query-customer-account-report`，用渠道 key 直查）。
- **GLM Coding Plan（积分制）**：待接入套餐用量查询。控制台「Coding Plan → 用量统计」页可直接人工查看套餐消耗（见上方 zhipu-05 截图）；需调研项：Coding Plan 的积分消耗窗口（日/周/月）、积分余额接口、可用模型清单（待官网核实）。

---

### 4.3 阿里云百炼

**官方入口**：[https://consoles.aliyun.com/model-studio](https://consoles.aliyun.com/model-studio)

**免费额度**：
- **新人每款模型约 100 万 tokens**（全平台约 7000 万），90 天有效；
- 实名认证后额外赠送少量 tokens。

📷 **截图 4-5** ｜ 拍取新人免费额度页（打码） ｜ 存为：`bailian-01-free-quota.png`

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量付费** | 所有模型均支持按量计费 | - |
| **Token Plan 个人版** | Lite 39 / Essential 79 / Standard 139 / Pro 312 元/月 | - |
| **Token Plan 团队版** | 150 元/坐席/月 | - |
| **Coding Plan（旧版）** | 仅剩 Pro 200 元/月，停售过渡中 | - |

**渠道参数**：协议 `openai`，端点 `https://dashscope.aliyuncs.com/compatible-mode/v1`。

📷 **截图 4-6** ｜ 拍取 Token Plan 页（打码） ｜ 存为：`bailian-02-token-plan.png`

**模式说明**：

- `payg`：按量付费
- `token_plan`：Token Plan 套餐，`tier: "paid"`，月付制，`billing: "token_plan"` 标识，`plan_key: "agent"`（对应 agent 键名）
- `coding`：旧版 Coding Plan（可选 note）

**常见模型**：`qwen-turbo`, `qwen-plus`, `qwen-max`, `embedding` 等。

**适用场景**：企业级应用、大规模并发、长文本处理等。

#### 套餐使用说明

- **Token Plan（个人/团队版）**：待接入套餐用量查询。需调研项：Token Plan 的额度窗口（日/月）、用量查询接口（阿里云百炼控制台）、各档位可用模型清单（待官网核实）。
- **按量付费**：余额/欠费查询待接入（需确认百炼是否提供 API key 可查的余额接口）。

---

### 4.4 魔搭 ModelScope

**官方入口**：[https://modelscope.cn](https://modelscope.cn)

**免费额度**：
- **每日登录领 200 魔粒**（24h 有效，约 200–400 次调用）；
- **API-Inference 按魔粒计费**（0.5 / 1 / 2 粒/次，随模型浮动），**完善邮箱即可使用**，无需绑定阿里云实名；
- 魔粒不可购买，官方明示商用请用商业平台 API。

**步骤**

1. 打开[魔搭官网](https://modelscope.cn)，注册/登录账号（手机号或邮箱，也可用 CSDN / GitHub / 淘宝第三方登录，新用户限时赠免费 GPU 算力）；
   ![ModelScope 魔搭官网首页](images/api-key-guide/modelscope-01.png)
   ![ModelScope 魔搭登录注册弹窗](images/api-key-guide/modelscope-02.png)
2. 进「我的魔粒」页领取免费魔粒：「注册并登录」+200、「绑定阿里云账号」+50（每日各一次，有效期 1 天），开源贡献任务（模型 / 数据集 / 创空间影响力）每项 +50；
   ![ModelScope 我的魔粒·赚魔粒页](images/api-key-guide/modelscope-03.png)
   「发放记录」页可见每日发放明细（示例累计 600 魔粒）：
   ![ModelScope 我的魔粒·发放记录页](images/api-key-guide/modelscope-02-moli.png)
3. 右上角头像 →「设置」→ 左侧「访问令牌」菜单，进入访问令牌页；
   ![ModelScope 头像菜单 → 设置 → 访问令牌](images/api-key-guide/modelscope-05.png)
4. 点「新建访问令牌」，输入令牌名称（如 `facet`），有效期选「长期使用」；令牌由系统自动生成，单账号最多 10 个，**创建后只显示一次**，立即复制保存。
   ![ModelScope 新建访问令牌弹窗](images/api-key-guide/modelscope-04.png)

**付费形态**：**无任何付费套餐**，魔粒不可购买，官方明示商用请用商业平台 API。

**渠道参数**：协议 `openai`，端点 `https://api.modelscope.cn/v1`。

**模式说明**：

- 免费层：`tier: "free"`，`free_limits: {rpd: 250, note: 每日登录领 200 魔粒（约 200–400 次调用），当日有效}`（与注册表一致）；
- 无付费套餐，仅供非商用免费研究。

**常见模型**：`qwen-2-57b-a3b`, `chatglm3-6b`, `stable-diffusion` 等。

**适用场景**：学术研究、个人项目、非商业实验等。

---

### 4.5 小米 MiMo

**官方入口**：[https://mimo.mi.com](https://mimo.mi.com)（活动页）；控制台 [https://platform.xiaomimimo.com](https://platform.xiaomimimo.com)（API Keys 与余额页）

**免费额度**：
- **大陆新用户 ¥10 体验金**（40 天内，仅抵扣按量）；
- 邀请好友注册**双方各得 ¥10**（限前 30 人），好友首单 9 折、之后每单返实付 10% 赠金。

**步骤**

1. 打开官网，首页弹出「邀创造者体验 MiMo V2.6」促销活动（注册各得 ¥10 体验金，40 天有效）；
   ![小米 MiMo 注册促销活动](images/api-key-guide/mimo-02-promo.png)
2. 用手机号注册小米账号（选国家/地区、填验证码、勾选协议后提交）；
   ![小米 MiMo 账号注册页](images/api-key-guide/mimo-03-promo.png)
3. 注册后登录控制台，¥10 体验金在「账户余额」页可见：现金余额与赠送余额分列，可设预算阈值、在线充值（小米支付 / 支付宝 / 微信）；
   ![小米 MiMo 账户余额页](images/api-key-guide/mimo-04-promo.png)
4. 控制台 → **API Keys** → 点「新建 API Key」，填一个名称（如 `facet`）；
   ![小米 MiMo 新建 API Key](images/api-key-guide/mimo-05-promo.png)
5. 复制生成的 Key（`sk-` 开头，**只显示一次**，关闭弹窗后无法再查看）。
   ![小米 MiMo API Key 创建成功](images/api-key-guide/mimo-06-promo.png)

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量计费** | mimo-v2.6-pro 输入¥3/输出¥6 每百万 | - |
| **Token Plan 个人版** | Lite 39 / Standard 99 / Pro 329 / Max 659 元/月（按年计费立减后折合 34 / 87 / 289 / 579 元/月） | - |
| **Token Plan 团队版** | 详见平台官网 | - |

个人版支持 V2.6 全旗舰模型（文本 / 视觉 / 语音），套餐 Credits 40 天内有效；连续包年 88 折、首购优惠限时限量，支持 OpenClaw / Codex / MiMo Claw 等主流编程框架：

![小米 MiMo Token Plan 订阅页](images/api-key-guide/mimo-01-promo.png)

**渠道参数**：协议 `openai`；按量付费端点 `https://api.xiaomimimo.com/v1`，Token Plan 套餐端点 `https://token-plan-cn.xiaomimimo.com/v1`（与注册表一致，`/v1/models` 支撑模型自动同步）。

**模式说明**：

- `payg`：按量付费，`free_quota` 中建议标注 `¥10 体验金`（免费额度说明）
- `token_plan`：Token Plan 套餐，`tier: "paid"`，月付制，`billing: "token_plan"` 标识

**常见模型**：`mimo-v2.6-flash`, `mimo-v2.6-pro` 等（还有 `mimo-v2.5-asr` 等语音 / 嵌入模型；向导与渠道配置页「拉取模型清单」会自动挑可对话模型试通）。

**适用场景**：移动端 AI、个人开发、轻量级应用等。

**⚠️ 备注**：payg 建议在免费额度处标注“¥10 体验金（40天内仅抵扣按量）”，帮助用户快速识别免费入口。

---

### 4.6 Kimi（月之暗面）

**官方入口**：[https://platform.kimi.com](https://platform.kimi.com)

**免费额度**：
- **国内新用户注册实名后赠 15 元代金券**（3 个月有效），见官方帮助页：https://www.kimi.com/zh-cn/help/kimi-api/api-free-trial

📷 **截图 4-9** ｜ 拍取免费试用领取页（打码） ｜ 存为：`kimi-01-free-trial.png`

**付费形态**：**仅按量付费**（官方明确「无订阅制方案」）：

| 形态 | 说明 | 价格 |
|---|---|---|
| **kimi-k3.1-pro** | 输入¥13/输出¥68 每百万 tokens | - |

**渠道参数**：协议 `openai`，端点 `https://api.moonshot.cn/v1`（与注册表一致，`/v1/models` 支撑模型自动同步）。

📷 **截图 4-10** ｜ 拍取按量计费页（打码） ｜ 存为：`kimi-02-pricing.png`

**模式说明**：

- `payg`：按量付费，`free_limits` 或 `free_quota` 中建议标注“新人 15 元代金券（3个月有效）”，其余模型按量计费
- 官方明确：**API 无订阅制方案**，Kimi Code 订阅（Plus/Pro/Max/Ultra）与开放平台 API **不互通**，不计入 API 形态

**适用场景**：长文本对话、知识库检索、代码辅助等。

**⚠️ 备注**：payg 建议加免费标注（15 元券），帮助用户理解实际可用额度。

#### 套餐使用说明

- **无套餐，仅余额展示**：Kimi 开放平台没有包月/Token Plan 类 API 套餐（Kimi Code 订阅与 API 不互通），Facet 只注册了 `payg` 模式，渠道配置页渠道卡「厂家用量」行显示账户余额。
- **余额查询**：Facet 已接入 `GET https://api.moonshot.cn/v1/users/me/balance`（用渠道 Key 直查，TTL 5 分钟），显示「可用 / 现金 / 代金券」三项（CNY）：
  - 现金余额可为负（欠费状态），摘要会标注「欠费」；
  - 可用余额 ≤ 0 时推理会报 `exceeded_current_quota_error`，Facet 在摘要追加「余额不足，调用将失败」并按账户级失败冻结渠道。
- **可用模型**：以 `GET https://api.moonshot.cn/v1/models` 实时返回为准，由每日自动维护任务同步写入注册表。

---

### 4.7 DeepSeek

**官方入口**：[https://api-docs.deepseek.com](https://api-docs.deepseek.com)

**免费额度**：
- **无明确官方免费额度**（仅活动性「赠送余额」机制，非常规福利）。

📷 **截图 4-11** ｜ 拍取定价页（打码） ｜ 存为：`deepseek-01-pricing.png`

**付费形态**：**仅按量付费**（峰谷计价，峰时 = 谷时 ×2）：

| 形态 | 说明 | 价格 |
|---|---|---|
| **deepseek-flash** | 谷输入 $0.15/输出 $0.6，峰输入 $0.3/输出 $1.2 每百万 | - |
| **deepseek-v4-pro** | 同等峰谷计价（具体以官网定价页为准） | - |

**渠道参数**：协议 `openai`，端点 `https://api.deepseek.com/v1`。

📷 **截图 4-12** ｜ 拍取峰谷计价说明（打码） ｜ 存为：`deepseek-02-pricing-note.png`

**模式说明**：

- `payg`：按量付费，`tier: "paid"`，无免费额度（活动性赠送除外）
- 与注册表一致：无 Token Plan / Agent Plan 等套餐

**适用场景**：代码生成、长文本处理、多模态对话等。

**⚠️ 备注**：注册表无需修改，与官网保持一致。

---

### 4.8 腾讯混元

**官方入口**：[https://cloud.tencent.com/product/hunyuan](https://cloud.tencent.com/product/hunyuan)

**免费额度**：
- **首次开通送一次性资源包**（8 款模型共享 100 万 tokens，1 年有效）；
- **TokenHub 新用户免费体验包**（活动至 2026-12-31）。

📷 **截图 4-13** ｜ 拍取开通送资源包页（打码） ｜ 存为：`hunyuan-01-free-resource.png`

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **token 后付费** | Hunyuan-a13b 输入¥0.5/输出¥2 每百万 tokens | - |
| **预付费资源包** | 如 1 万点 100 元 | - |
| **TokenHub Token Plan 包月** | Hy Token Plan 28/78/238/468 元/月；通用 Token Plan 39/99/299/599 元/月 | - |

**渠道参数**（与注册表一致）：
- `agent`（Token Plan）：`openai` 协议，端点 `https://tokenhub.tencentmaas.com/plan/v3`
- `payg`（按量付费）：`openai` 协议，端点 `https://api.hunyuan.cloud.tencent.com/v1`

📷 **截图 4-14** ｜ 拍取 Token Plan 与 Agent 套餐页（打码） ｜ 存为：`hunyuan-02-plans.png`

**模式说明**：

- `payg`：按量付费，`free_quota: {tokens: 1000000, period: "total"}`（开通送 100 万 tokens/1 年）
- `agent` / `token_plan`：Token Plan 套餐，新增入口（此前缺失，现已补全）

  - **Hy Token Plan**：28/78/238/468 元/月（8 模型共享）
  - **通用 Token Plan**：39/99/299/599 元/月（含 DeepSeek/GLM/Kimi/MiMo 等）
  - `billing: "token_plan"` 标识，`plan_key: "agent"`（对应 agent 键名）

- `coding`：Coding Plan（可选，视官方是否保留旧版）

**常见模型**：`hunyuan-a13b`, `hunyuan-lite`, `hunyuan-large` 等。

**适用场景**：企业级大模型应用、8模型混合调度、需要按月预算的场景。

#### 套餐使用说明

- **Token Plan（Hy Token Plan / 通用 Token Plan）**：待接入套餐用量查询。需调研项：套餐额度窗口、用量查询接口、各档位可用模型清单（待官网核实）。
- **按量付费**：开通送 100 万 tokens（1 年有效），余量查询待接入。

**⚠️ 备注**：注册表已补全 token_plan mode + payg 免费额度 100万 tokens/1年（见前文更新记录）。

---

### 4.9 MiniMax

**官方入口**：[https://platform.minimaxi.com](https://platform.minimaxi.com)

**免费额度**：

- 无固定长期免费层；新注册账号有一次性新人赠金（以账户页实际到账为准，有效期短），适合先跑通接入验证。

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量付费（PAYG）** | 普通 API Key（`sk-api-` 开头），按实际 token 计费 | 以官网定价页为准 |
| **Token Plan** | 订阅 Key（非 `sk-api-` 前缀，如 `sk-cp-`），按「5 小时窗口 + 每周窗口」限流 | Plus ¥49/月 · Max ¥119/月 · Ultra ¥469/月（老用户保留档 Starter ¥29 / Plus-极速 ¥98） |

**渠道参数**（与注册表一致）：

| 模式 | 协议 | 端点 |
|---|---|---|
| `payg` | `openai` | `https://api.minimax.cn/v1`（`/v1/models` 支撑模型自动同步） |
| `agent`（Token Plan） | `openai` | `https://api.minimax.cn/v1`，Key 填**订阅 Key** |
| （可选）Anthropic 协议 | `anthropic` | `https://api.minimax.cn/anthropic` |

📷 **截图 4-15** ｜ 拍取 Token Plan 订阅页（打码） ｜ 存为：`minimax-01-token-plan.png`

**模式说明**：

- `payg`：`tier: "paid"`，无长期免费额度标注（新人赠金可在渠道备注里自行记录）
- `agent`：Token Plan 包月，`tier: "paid"`；**Key 必须是订阅 Key**，普通按量 Key 查 Token Plan 余量会报登录失败
- **双 Key 体系**：`sk-api-` 开头是按量付费 Key，其余前缀是 Token Plan 订阅 Key；Facet 按前缀自动选择对应用量接口，无需手工指定

**常见模型**：`MiniMax-M3`、`MiniMax-M2.1`、`MiniMax-Text-01` 等（以 `/v1/models` 同步结果为准）。

**适用场景**：高频编程 Agent、多模态调用、包月预算可控的日常使用等。

**⚠️ 备注**：同一厂家两种 Key 各建一个渠道即可；订阅 Key 渠道选「Token Plan」模式，按量 Key 渠道选「按量付费」模式。

#### 套餐使用说明

Facet 已接入 MiniMax 用量查询，显示在渠道配置页渠道卡「厂家用量」行（TTL 5 分钟）：

| 模式 | 查询接口 | 展示内容 |
|---|---|---|
| `payg`（`sk-api-` Key） | `GET https://api.minimax.cn/account/query_balance`（注意**不在 `/v1` 下**） | 可用 / 现金 / 代金券 / 信用 / 欠费（CNY，非零项才显示） |
| `agent`（Token Plan Key） | `GET https://api.minimax.cn/v1/token_plan/remains` | 5 小时窗口与每周窗口的已用百分比、已用量、重置时间；「不限额」「已耗尽」窗口各有状态标注 |

- 上游 `current_interval_remaining_percent` / `current_weekly_remaining_percent` 是**剩余**百分比，Facet 统一换算为**已用**百分比渲染，与其它厂家口径一致。
- 窗口重置时间取自 `end_time` / `weekly_end_time`（毫秒时间戳自动转秒）；`weekly_boost_permille`（如 1500 = 显示余量的 150%）已参与换算。
- 计数字段（`*_usage_count`）上游语义存在「已用/剩余」二义性，Facet 用百分比仲裁，保证绝对值与百分比不矛盾。
- 查询失败时渠道卡保留上一次成功快照并标注 `stale`，不会抛错打断页面。

---

### 4.10 百度千帆

**官方入口**：[https://cloud.baidu.com/product/qianfan](https://cloud.baidu.com/product/qianfan)（控制台 [console.bce.baidu.com](https://console.bce.baidu.com)）

**免费**：⚠️ 新人免费额度以控制台「领取页」为准——2026-11 复核时官网未公开固定口径（注册表历史 note 中「17 个指定模型各 100 万 tokens + 实名送 ¥20 券」为宣传语，已同步软化）。

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量付费（PAYG）** | IAM API Key，按实际 token 计费 | 以官网定价页为准 |
| **Token Plan** | 订阅制，按窗口限流 | 个人版五档 Mini / Lite / Standard / Pro / Max（Mini ¥4.9 为首购限时价，Standard ¥49.9） |
| **算力包** | 专属部署算力单元 | 4500~7500 元/月起 |

**渠道参数**（与注册表一致）：

| 模式 | 协议 | 端点 | tier |
|---|---|---|---|
| `payg` | `openai` | `https://qianfan.baidubce.com/v2`（`/v2/models` 支撑模型自动同步） | `free`（新人免费额度以领取页为准） |
| `agent`（Token Plan） | `openai` | `https://qianfan.baidubce.com/v2/tokenplan/personal` | `paid` |

📷 **截图 4-16** ｜ 拍什么：IAM API Key 列表页（Key 打码） ｜ 存为：`qianfan-01-apikey.png`

**模式说明**：

- `payg`：`tier: "free"`，登记 `free_quota` 100 万 tokens；实际到账额度以控制台领取页为准
- `agent`：Token Plan 套餐，`tier: "paid"`；Key 用 Token Plan 订阅页颁发的 Key

**常见模型**（Token Plan 登记）：`deepseek-v4.1-flash`、`deepseek-v4-pro-0813`、`glm-5.3`；按量端点模型以 `/v2/models` 同步结果为准。

**适用场景**：需要 DeepSeek / GLM 等第三方模型的国内合规接入，或已实名的百度智能云主体。

**⚠️ 备注**：`agent` 端点为 Token Plan 个人版路径（`tokenplan/personal`），订阅档位以控制台为准。

---

## 五、海外按量付费厂家

### 5.1 Anthropic 直连

**官方入口**：[https://console.anthropic.com](https://console.anthropic.com)

**步骤**

1. 打开 [console.anthropic.com](https://console.anthropic.com) 注册（邮箱验证，部分情况要求手机验证）；
2. **预充值**：Billing 页买 credit（最低 $5）；
3. **一次性 $5 试用金**：完成手机验证后，Console 余额会自动发放 $5 试用金（有效期以官方页面为准），可用于按量调用；
4. **API Keys**（直链：[console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)）→ **Create Key**（`sk-ant-` 开头）。

   📷 **截图 5-1** ｜ 拍什么：API Keys 页（Key 打码） ｜ 存为：`anthropic-01-apikeys.png`

**渠道参数**：协议 `anthropic`，端点 `https://api.anthropic.com`。国内直连不通时配 `proxy`。

💡 **提示**：Max/Ultra/Team 订阅后，每月会向 Console 附赠 API credits（$100/$200/至$500），若需持续使用建议订阅对应方案。

---

### 5.2 OpenAI 直连

**官方入口**：[https://platform.openai.com](https://platform.openai.com)

**步骤**

1. 打开 [platform.openai.com](https://platform.openai.com) 注册；
2. Billing 页预充值（最低 $5）；
3. **API keys**（直链：[platform.openai.com/api-keys](https://platform.openai.com/api-keys)）→ **Create new secret key**（`sk-proj-` 开头）。

   📷 **截图 5-2** ｜ 拍什么：API keys 页（Key 打码） ｜ 存为：`openai-01-apikeys.png`

**渠道参数**：协议 `openai`，端点 `https://api.openai.com/v1`。国内直连不通时配 `proxy`。

---

### 5.3 xAI Grok

**官方入口**：[https://console.x.ai](https://console.x.ai)

**步骤**

1. 打开 [console.x.ai](https://console.x.ai)，用 X（Twitter）账号登录；
2. 团队/计费初始化后 → **API Keys** → **Create API Key**（`xai-` 开头）；新账号常有少量赠送额度。

   📷 **截图 5-3** ｜ 拍什么：API Keys 页（Key 打码） ｜ 存为：`xai-01-apikeys.png`

**渠道参数**：协议 `openai`，端点 `https://api.x.ai/v1`。

---

### 5.4 Z.AI（智谱海外）

**官方入口**：[https://z.ai](https://z.ai)

**步骤**

1. 打开 [z.ai](https://z.ai) 注册登录；
2. 进入 **API Keys**（直链：[z.ai/manage-api-key/apikey-keys](https://z.ai/manage-api-key/apikey-keys)）→ 创建并复制 API Key。

   📷 **截图 5-4** ｜ 拍什么：z.ai API Keys 页（Key 打码） ｜ 存为：`zai-01-apikeys.png`

**渠道参数**：协议 `openai`，端点 `https://api.z.ai/api/paas/v4`。

**模型套餐信息**（同一账号可同时使用）：

| 形态 | 说明 | 端点/备注 |
|---|---|---|
| 按量计费 | GLM-5.3 等模型，按输入输出 tokens 计费 | 同一 Key 双模式切换 |
| **Coding Plan 订阅**（新增） | **Lite $18/月、Pro $80/月、Max $168/月**（季/年付有折扣）<br>基于 OpenAI Chat Completions 协议，兼容 Facet coding 模式 | 访问 [z.ai/subscribe](https://z.ai/subscribe) 完成订阅 |

---

## 六、订阅聚合类

### 6.1 OpenCode

**官方入口**：[https://opencode.ai/console](https://opencode.ai/console)

**步骤**

1. 打开 [opencode.ai](https://opencode.ai) 注册登录；
2. **Console**（[opencode.ai/console](https://opencode.ai/console)）→ 订阅 **Go**（$10/月；另有 Go Plus $40/月、Go Teams $20/人/月）并绑定支付方式；
3. Console 内直接**复制 API Key**。

   📷 **截图 6-1** ｜ 拍什么：Console 概览页显示订阅状态与 Key（打码） ｜ 存为：`opencode-01-console.png`

**渠道参数**：

| 模式 | 端点 |
|---|---|
| Go 订阅套餐 | `https://opencode.ai/inference/go/openai/v1` |
| 按量（Models） | `https://opencode.ai/inference/openai/v1` |

#### 套餐使用说明（Go 订阅）

Facet 已接入 Go 套餐用量查询，在「套餐订阅」页和仪表板「订阅用量」卡里可直接看余量。

| 项目 | 说明 |
|---|---|
| 档位 | Go（$10/月）、Go Plus（$40/月）、Go Teams（$20/人/月） |
| 用量窗口 | **滚动 5 小时窗口**（rolling）、**周窗口**（weekly）、**月窗口**（monthly） |
| 余量口径 | 百分比（已用百分比） |
| 重置时间 | ISO-8601 时间戳（厂家返回 `resetsAt`，Facet 自动转 epoch 秒） |
| 可用模型 | Go 套餐可用模型以 Console 套餐页为准（待官网核实：完整模型清单） |
| 用量查询 | 推理网关 `GET https://opencode.ai/inference/go/v1/usage`，用渠道的 `oc_sk_` Go key 直查（无需额外 AK/SK）；Facet 自动拉取，TTL 5 分钟 |
| 注意 | Console 管理面（`/console/api/...`）对 Bearer key 返回 403（仅登录态），所以走推理网关路由 |

---

### 6.2 Command Code

**官方入口**：[https://commandcode.ai](https://commandcode.ai)

**步骤**

1. 打开 [commandcode.ai](https://commandcode.ai) 注册；
2. Studio → **Billing**（[commandcode.ai/billing](https://commandcode.ai/billing)）选套餐：**Provider** $15/月 + $1.01 手续费，含 $15 永不过期额度，按量扣；Go 档（$1/月）**没有 API 权限**，勿选；
3. **Settings → Keys**（[commandcode.ai/settings/keys](https://commandcode.ai/settings/keys)）创建并复制。

   📷 **截图 6-2** ｜ 拍什么：Keys 页（Key 打码） ｜ 存为：`commandcode-01-keys.png`

**渠道参数**：协议 `openai`，端点 `https://api.commandcode.ai/provider/v1`。

📷 **截图 6-3** ｜ 拍什么：桥启动后的终端 + 控制台渠道行测试通过 ｜ 存为：`bridge-01-running.png`

**模式说明**：

- `agent`：套餐订阅（注册表登记的唯一模式，`tier: "free"`），Provider $15/月 + $1.01 手续费，含 $15 永不过期额度，按量扣；免费模型随套餐 `models` 清单下发（如 glyph-cluster:free 1000 requests/天、crouton-alpha 1000 requests/天、inkling-2-small:free 200 requests/天），无需额外配置即可使用
- `coding` / `coding_plan`：可选，视用户需求

**⚠️ 备注**：注册表仅登记 `agent` 模式（免费模型在 agent 的 models 清单中），无独立 `payg` mode。

---

### 6.3 LLM7

**官方入口**：[https://docs.llm7.io](https://docs.llm7.io)（控制台 [dash.llm7.io](https://dash.llm7.io)）

**免费层**（注册表 `payg` 模式，label「免费匿名」，`tier: "free"`）：

| 限额 | 值 |
|---|---|
| 请求频率 | 60 次/分（turbo 模型约 1 次/秒） |
| 每小时 | 250 次/时 |
| 24 小时 | 10 万 tokens/24h |

**付费**：Pro $12/月——25 次/秒、1500 次/分、15000 次/时，解锁 pro + turbo 模型；超出部分按账户余额扣费。

**步骤**

1. 打开 [dash.llm7.io](https://dash.llm7.io) 注册登录；
2. 控制台创建 API Key 并复制；
3. 填入 Facet：协议 `openai`，端点 `https://api.llm7.io/v1`，模式选「按量付费（免费匿名）」。

   📷 **截图 6-4** ｜ 拍什么：LLM7 控制台 Key 页（打码） ｜ 存为：`llm7-01-keys.png`

**渠道参数**（与注册表一致）：协议 `openai`，端点 `https://api.llm7.io/v1`（`/v1/models` 支撑模型自动同步）。

**常见模型**：`DeepSeek-V4-Flash-0731`、`deepseek-v4-pro`、`glm-5.3`、`kimi-k3.1-pro`、`claude-sonnet-5`、`grok-4.7` 等聚合清单，以 `/v1/models` 同步结果为准。

**适用场景**：零成本起步、聚合多厂家模型试跑、给免费档当兜底渠道。

**⚠️ 备注**：注册表仅登记 `payg` 模式（免费匿名层）；官网 Pro $12/月 订阅未在注册表登记，需要更高限流时按官网 Pro 档自建渠道（同一端点 + Pro Key）。

---

## 七、在 Trae / IDE 里使用 Facet

**前提**：你的机器上已经成功启动 Facet 网关（`facet serve`），并且用下面的网关 Key 能通
（`curl -H "Authorization: Bearer <gateway_key>" http://127.0.0.1:8787/v1/models` → 200）。

**快速开始**：

1. 打开 Trae / IDE，调出 Facet 侧边栏（或命令面板：`PlanRouter: Open`）；
2. 如果是首次使用，会弹出**新手向导**（8 步）——引导您设置管理密码、选择厂家、填入 Key，并自动测试连通性；
3. 向导完成后，配置会写入 `router.json`，此后可以直接在聊天中使用 `/scheduler`、`/route` 等指令；
4. 如需修改配置，可通过右上角「快速开始」重新打开向导，或直接编辑 `~/.facet/router.json`。

**常用指令**：

| 指令 | 说明 |
|---|---|
| `/scheduler` | 根据模型特性和当前负载自动调度至最合适的厂家/渠道 |
| `/route <模型名>` | 手动指定模型路由至指定厂家 |
| `/free-quota` | 查询当前可用的免费额度模型列表（Facet 每日自动对齐） |
| `/billing` | 查看当前渠道的计费模式（payg / token_plan / coding_plan / agent_plan） |

**密钥管理**：

- Key 存储在本地配置文件 `~/.facet/keys.json`（或 Trae 本地储存）
- 建议设置管理密码（默认为 `admin`），防止他人在共享环境下修改配置
- 每把 Key 都建议设置平台侧的额度上限或限速，避免意外大额消费
- 不再用的 Key 已在平台上删除或轮换，避免产生无效开销

**每日自动对齐**：

Facet 会每日后台检查各厂家免费额度状态，并在 `/free-quota` 指令中返回当前仍有免费额度的模型。这包括但不限于：

- 魔搭 ModelScope（每日 200 魔粒）
- 火山引擎方舟（剩余免费 tokens）
- 百度千帆（新人免费额度，以领取页为准）
- 智谱 GLM（Flash 系列免费）
- 以及其他已标注 free_quota/free_limits 的厂家

---

## 八、常见问题

### 8.1 套餐的 5 小时 / 周 / 月窗口用完了怎么办？

不用人工干预，网关自动处理：上游返回「用量已达上限」时，网关把该渠道的这一个模型冷却到厂家窗口的**精确重置时刻**（从订阅页关联的厂家凭据读取窗口快照），当次请求自动切换到下一个可用渠道；窗口重置后该模型自动恢复。同渠道其他模型的额度相互独立，不受影响。

注意：需要在控制台**订阅页**为套餐关联厂家凭据（AK/SK），网关才能读到「用到多少、何时重置」，冷却时间才算得准。

### 8.2 在 Trae / IDE 里指定的模型，后端所有渠道都用完了怎么办？

两层兜底，尽量不让请求失败：

1. 持有该模型的渠道全部冷却时，请求自动落到链上其他渠道的调度模型——响应里的**实际模型可能不是你指定的那个**；
2. 整条链都不可用时才返回 503，错误信息带每个渠道的失败原因（`failures`）。

冷却到期后自动恢复，指定的模型会重新回到候选首位。排查时看容器日志的 `[route]` 行：`skipped=` 列出因冷却被跳过的渠道，`picked=` 是最终实际选中的渠道/模型。

### 8.3 显式指定模型时，先调哪个渠道？

按计费优先级排序：**包月（有到期日的最先，快到期的先用；同日到期的剩额度比例高的先用）→ 包月未登记到期日 → 免费档 → 按量渠道**。`facet-auto` / `facet-free` 等聚合模型名不走此排序，保持控制台链序。详见 `docs/planrouter-billing-priority.md`。

---

## 附：安全清单

- [ ] 每张截图都检查过：完整 Key 不可见（至少打码到只剩前 6 位）
- [ ] 密钥只存进 Facet 渠道配置或 `env:` 环境变量，不进聊天记录 / 截图 / 仓库
- [ ] 给每把 Key 设了平台侧的额度上限或限速（能设的都设上）
- [ ] 不再用的 Key 已在平台上删除或轮换

---

## 修订记录

| 日期 | 修订人 | 说明 |
|---|---|---|
| 2026-11-13 | - | 初版发布，基于 19 家厂家官网核实 |
| 2026-11-13 | - | 更新魔粒每日领取数：250 → 200 |
| 2026-11-13 | - | 计划新增 LLM7 Pro $12/月 订阅说明（当时未写入正文，2026-11-16 补入 6.3 章节并订正本行） |
| 2026-11-13 | - | 新增 CommandCode 免费模型 note |
| 2026-11-13 | - | 新增 Z.AI Coding Plan 订阅信息 |
| 2026-11-13 | - | 新增 Anthropic $5 试用金说明 |
| 2026-11-13 | - | 更新千魔免费额度：¥10 体验金、¥20 代金券等 |
| 2026-11-14 | - | 新增 4.9 MiniMax 章节（按量 + Token Plan，双 Key 体系，用量查询已接入） |
| 2026-11-14 | - | 4.6 Kimi 补「套餐使用说明」：余额接口已接入，端点对齐注册表 api.moonshot.cn/v1 |
| 2026-11-14 | - | 新增「八、常见问题」：窗口用尽的精确冷却与自动恢复、显式模型全量用尽的两层兜底、显式模型的计费优先顺序 |
| 2026-11-15 | - | 修正 NVIDIA NIM 口径：免费层是速率限制（多数模型约 40 RPM）而非 credits；10,000 请求/日非官方承诺；端点对齐注册表 integrate.api.nvidia.com/v1 |
| 2026-11-16 | - | 全文口径核对：修正混元/智谱/百炼端点（混元此前误写火山方舟域名）；DeepSeek 峰谷价互换与在售模型、火山 doubao-seed-1.6 输入价与新用户特惠（已暂停）、魔搭免费口径（魔粒计费、完善邮箱即可，无需阿里云实名）、千帆 Token Plan 五档与免费额度软化、OpenCode 三档价、Anthropic 试用金有效期；CommandCode 模式说明与注册表对齐（仅 agent 模式）；新增 4.10 百度千帆、6.3 LLM7 章节；订正 2026-11-13 LLM7 修订记录（原文声称已新增说明但当时未落文）；3.1 NVIDIA、3.2 OpenRouter 截图占位替换为真实截图（各 4 张），5.4 Z.AI 补截图占位 |