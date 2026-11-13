## Facet API Key Guide

**本手册怎么用**：按厂家分节，每节给出「注册 → 拿 Key → 填进 Facet」的完整步骤。
文中所有 📷 是**截图位**——按说明操作到对应页面时截图，保存到
`docs/images/api-key-guide/` 目录并使用标注的文件名，图片会自动显示在本文档中。

**为什么值得照着注册（不花您的钱）**：国内主流厂家（百炼、火山、魔搭、智谱、千帆、DeepSeek、Kimi）
都给实名个人账号**免费额度**——新模型 90 天窗口、每日登录领魔粒、新人 100 万 token……
这些额度**不用就作废，用了不扣您的钱**。用户不知道有免费、也不知道去哪领，是普遍痛点；
本手册把注册入口、Key 位置、免费额度规则聚合在一起，照着点一遍即可。Facet 还会**每日自动对齐**
哪些模型还有免费额度（见 [4.3 百炼](#43-阿里云百炼) 的 `usage free` 定时任务），帮您把白嫖额度一直用满。

真正「匿名免注册」现在只剩极少数端点（且限速极低，见 [3.1](#31-pollinations免注册免-key)），
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
| `agent` / `token_plan` | Token 包月/套餐 | xiaomi、bailian、hunyuan、qianfan |
| `coding` / `coding_plan` | 编程套餐 | zhipu、volcengine、zai、commandcode |

---

## 二、厂家总览

| # | 厂家 | 免费 | 付费形态 | 备注 |
|---|------|------|----------|------|
| 1 | 火山引擎方舟 | ✅ 各模型约50万tokens/模型 | payg + coding_plan + agent_plan | 一致；遗漏企业版Coding Plan |
| 2 | 智谱 GLM | ✅ 注册送2000万tokens + Flash系列免费 | payg + coding_plan | payg应加免费标注 |
| 3 | 阿里云百炼 | ✅ 新人每模型100万tokens(90天) | payg + token_plan | 一致；遗漏旧版Coding Plan |
| 4 | 魔搭 ModelScope | ✅ 纯免费(魔粒200/日) | 无 | 免费额度应为200魔粒/日非250 |
| 5 | 小米 MiMo | ✅ 新人¥10体验金(40天) | payg + token_plan | payg应加免费标注 |
| 6 | Kimi (月之暗面) | ✅ 新人15元代金券(3个月) | payg（API无订阅） | payg应加免费标注 |
| 7 | DeepSeek | ❌ 无明确免费额度 | payg（峰谷计价） | 一致 |
| 8 | 腾讯混元 | ✅ 开通送100万tokens(1年) | payg + token_plan(TokenHub) | **已补 token_plan 模式**：Token Plan 28/78/238/468 元/月；payg 加免费额度 100 万 tokens/1 年 |
| 9 | 百度千帆 | ✅ 17模型各100万tokens(3个月)+¥20券 | payg + token_plan + 算力包 | **已补 token_plan 模式**；Token Plan 个人版 Mini/Lite/Pro/Max（首购 4.9 元起）；payg 加免费额度 17×100 万 tokens |
| 10 | Z.AI 智谱海外 | ✅ GLM-4.7-Flash等完全免费 | payg + coding_plan($18起) | **已补 coding_plan 模式**；Coding Plan Lite $18/Pro $80/Max $168/月；payg 加免费模型标注（GLM-4.7-Flash/4.5-Flash/4.6V-Flash 完全免费，其余按量计费） |
| 11 | Grok xAI | ❌ 无标准免费层（活动:数据共享换$150/月） | payg | 基本一致 |
| 12 | OpenAI | ❌ 新账号无赠送credits | payg（订阅不含API） | 一致 |
| 13 | Anthropic | ⚠️ 一次性$5试用 | payg（Max/Ultra订阅送API credits） | 一致；可加免费note |
| 14 | OpenCode | ✅ 12+免费模型 | payg + agent_plan(Go订阅) | 一致 |
| 15 | Command Code | ✅ 免费模型1000请求/天 | 套餐订阅 + 按量充值 | **已补按量充值形态 + 免费模型标注** |
| 16 | Pollinations | ✅ 匿名1次/15秒，Seed 1次/5秒 | 付费档Flower/Nectar | 一致 |
| 17 | LLM7 | ✅ 60次/分·250/时·10万tokens/24h | Pro $12/月订阅 | **已补Pro订阅形态** |
| 18 | OVHcloud AI Endpoints | ⚠️ 非真免费层（匿名2rpm限速试用） | payg（按token后付） | tier标注需修正 |
| 19 | NVIDIA NIM | ✅ 1000 credits(个人)/5000(企业) | ❌ 官方无按量付费通道 | free准确；无付费选项 |
| 20 | OpenRouter | ✅ :free模型50→1000次/天 | payg（预付credits，5.5%手续费） | 一致 |

---

## 三、免费渠道（零成本起步）

### 3.1 Pollinations——免注册免 Key

**官方入口**：[https://pollinations.ai](https://pollinations.ai)

**使用方式**：
- **匿名调用**：无需注册，直接通过 HTTP 请求访问基础模型。
  - 基础模型：1 次/15 秒（速率限制较低，适合轻量级试用）。
  - Seed 模型：免费注册 1 次/5 秒，获取后可持续使用。
- **注册/登录**：可选，登录后可获得 slightly higher rate limits 和 额外 Seed 配额。
- **关键点**：完全不需要 API Key，直接在代理或网关中使用对应的 `model` 名称即可。

📷 **截图 3-1** ｜ 拍什么：Pollinations 官网首页或 API 文档页（打码） ｜ 存为：`pollinations-01-home.png`

**渠道参数**（在 Facet 中配置时）：

| 字段 | 值 |
|---|---|
| 协议 | `openai`（兼容 OpenAI 格式的请求） |
| 端点 | `https://api.pollinations.ai/v1`（或根据实际部署地址） |
| 模式 | `payg`（按量），但**免费额度由平台直接提供**，不消耗用户 Key 余额 |
| free_limits | `rpm: 12`（约 1 次/5秒），`rpd: 约 1700/日`，`note: 匿名 1次/15秒，Seed 1次/5秒` |

**常见模型**：`pollinations/base-model`、`pollinations/v3` 等（以官方文档为准）。

**适用场景**：极速原型验证、不想暴露 Key 的快速测试、教育演示等。

---

### 3.2 NVIDIA NIM——免费层

**官方入口**：[https://build.nvidia.com/](https://build.nvidia.com/)

**使用方式**：
- **个人邮箱注册**：1,000 credits（1 credit ≈ 1 次请求），约 40 RPM，无需信用卡。
- **企业邮箱注册**：5,000 credits，同等使用量。
- ** credits 使用**：每次请求扣除 1 credit，用于访问 NIM 管理的模型（如 Llama 3.1, Nemotron 等）。
- **官方声明**：**无按量付费、无包月通道**。生产环境需购买 NIM 许可 ($4,500/GPU/年) 或使用伙伴端点。

📷 **截图 3-2** ｜ 拍什么：build.nvidia.com 注册页显示 credit 数量（打码） ｜ 存为：`nvidia-nim-01-signup.png`

**渠道参数**（在 Facet 中配置时）：

| 字段 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://api.nvidia.com/v1`（实际使用需通过 NIM 网关） |
| 模式 | `free`（免费层， credits 耗尽后不可继续，需重新注册或升级） |
| free_quota | `tokens: 1000（个人）/5000（企业），period: "total"` |

**常见模型**：`nvidia/llama-3.1-8b`, `nvidia/nemotron`, `nvidia/mistral` 等。

**适用场景**：个人开发试用、快速原型验证、不需要大规模生产的场景。

---

### 3.3 OpenRouter——免费池

**官方入口**：[https://openrouter.ai](https://openrouter.ai)

**使用方式**：
- **免费模型**：25+ `:free` 模型，每日配额根据账号余额动态调整：
  - 余额 **< $10**：50 次/天
  - 累计充值 **≥ $10**：1,000 次/天
  - 同时共享率限制：20 RPM（每分钟请求数）
- **付费模型**：预付 credits 按量（pass-through 原价），Standard 档购值收 5.5% 手续费（Business 8%），BYOK 每月 $25,000 推理额免平台费。

📷 **截图 3-3** ｜ 拍什么：openrouter.ai 仪表盘显示余额与每日配额（打码） ｜ 存为：`openrouter-01-dashboard.png`

**渠道参数**（在 Facet 中配置时）：

| 字段 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://openrouter.ai/api/v1` |
| 模式 | `payg`（预付 credits） |
| free_limits | `note: :free 模型 50→1000次/天（余额<$10→50/≥$10累充1000），20 RPM` |

**常见免费模型**：`openrouter/anthropic/claude-3-haiku`, `openrouter/mistral/mistral-large`, `openrouter/google/gemini-flash` 等（随平台更新而变化）。

**适用场景**：希望同时使用多家厂家模型、免费额度有需求的开发者、跨平台兼容性要求。

---

## 四、国内主流厂家

### 4.1 火山引擎方舟

**官方入口**：[https://console.volcengine.com/ark](https://console.volcengine.com/ark)

**免费额度**：
- 注册开通模型自动领取免费推理额度（**约 50 万 tokens/模型**，仅抵扣按量后付费）；
- 另有「安心体验模式」用完即停。

📷 **截图 4-1** ｜ 拍取免费额度展示页（打码） ｜ 存为：`volcengine-01-free-quota.png`

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量付费** | 计费：doubao-seed-1.6 输入 0.6~2.4 元/百万 tokens | - |
| **Coding Plan 个人版** | Lite 40 元/月、Pro 200 元/月（新用户 Lite 首两月 9.9 元） | - |
| **Coding Plan 企业版** | Team Lite 120 / Team Pro 600 元/月 | - |
| **Agent Plan 个人版** | Small 40 / Medium 200 / Large 500 / Max 1000 元 | - |

**渠道参数**：协议 `openai`，端点 `https://ark.cn-beijing.volces.com/api/v3`。

📷 **截图 4-2** ｜ 拍取 Coding/Agent 套餐页（打码） ｜ 存为：`volcengine-02-plans.png`

**模式说明**：

- `payg`：按量付费，免费额度 `free_quota: {tokens: 500000, period: "total"}`（约 50 万 tokens，一次性）
- `coding`：Coding Plan 套餐，`tier: "paid"`，月付制
- `agent`：Agent Plan 套餐，`tier: "paid"`，月付制，含 `billing: "agent_plan"` 标识

**常见模型**：`doubao-seed-1.6`, `doubao-seed-32b`, `ernie-turbo`, `ernie-speed` 等。

**适用场景**：企业级应用开发、代码辅助、对话式 AI 等。

---

### 4.2 智谱 GLM

**官方入口**：[https://www.bigmodel.cn](https://www.bigmodel.cn)

**免费额度**：
- **注册送 2000 万 tokens 体验包**（https://www.bigmodel.cn/invite）；
- **GLM-4-Flash 长期免费**（官方文档：https://docs.bigmodel.cn/cn/guide/start/pricing.md）；
- **GLM-4.7-Flash / FlashX 等限时免费**（以官网公告为准）。

📷 **截图 4-3** ｜ 拍取免费额度页（打码） ｜ 存为：`zhipu-01-free-quota.png`

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量付费** | GLM-5.3 输入 8 元/输出 28 元/百万 tokens | - |
| **GLM Coding Plan**（积分制） | Lite 118 / Pro 538 / Max 1078 元/月（老用户保留 49/149 元） | - |
| **Coding 团队版** | 2 席位起购 | - |

**渠道参数**：协议 `openai`，端点 `https://bigmodel.cn/api/v1`。

📷 **截图 4-4** ｜ 拍取 Coding Plan 页（打码） ｜ 存为：`zhipu-02-coding-plan.png`

**模式说明**：

- `payg`：按量付费，实际有免费层（注册赠金 + Flash 系列），建议在渠道配置中添加 note 标注
- `coding`：Coding Plan 订阅，`tier: "paid"`，月付制，含 `billing: "coding_plan"` 标识

**常见模型**：`glm-4`, `glm-4-flash`, `glm-4.5-flash`, `glm-4.7-flash` 等。

**适用场景**：自然语言理解、代码生成、多模态交互等。

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

**渠道参数**：协议 `openai`，端点 `https://api.bailian.aliyun.com/api/v1`。

📷 **截图 4-6** ｜ 拍取 Token Plan 页（打码） ｜ 存为：`bailian-02-token-plan.png`

**模式说明**：

- `payg`：按量付费
- `token_plan`：Token Plan 套餐，`tier: "paid"`，月付制，`billing: "token_plan"` 标识，`plan_key: "agent"`（对应 agent 键名）
- `coding`：旧版 Coding Plan（可选 note）

**常见模型**：`qwen-turbo`, `qwen-plus`, `qwen-max`, `embedding` 等。

**适用场景**：企业级应用、大规模并发、长文本处理等。

---

### 4.4 魔搭 ModelScope

**官方入口**：[https://modelscope.cn](https://modelscope.cn)

**免费额度**：
- **每日登录领 200 魔粒**（24h 有效，约 200–500 次调用，非 250/日）；
- **API-Inference 免费开放**（非商用，需绑定阿里云实名）；
- 魔粒不可购买，官方明示商用请用商业平台 API。

📷 **截图 4-7** ｜ 拍取魔粒领取页（打码） ｜ 存为：`modelscope-01-magic-grains.png`

**付费形态**：**无任何付费套餐**，魔粒不可购买，官方明示商用请用商业平台 API。

**渠道参数**：协议 `openai`，端点 `https://api.modelscope.cn/v1`。

📷 **截图 4-8** ｜ 拍取 API 文档免费说明（打码） ｜ 存为：`modelscope-02-api.png`

**模式说明**：

- 免费层：`tier: "free"`，`free_limits: {rpm: 约 12, rpd: 约 1700/日, note: 每日登录领 200 魔粒（24h 有效）}`；
- 无付费套餐，仅供非商用免费研究。

**常见模型**：`qwen-2-57b-a3b`, `chatglm3-6b`, `stable-diffusion` 等。

**适用场景**：学术研究、个人项目、非商业实验等。

**⚠️ 备注**：手册第 3.7 章已更新为“每日登录领 200 魔粒（约 200–500 次调用）”，原写 250 需更正。

---

### 4.5 小米 MiMo

**官方入口**：[https://mimo.mi.com](https://mimo.mi.com)

**免费额度**：
- **大陆新用户 ¥10 体验金**（40 天内，仅抵扣按量）；
- 邀请双方各 ¥10。

📷 **截图 4-9** ｜ 拍取促销活动页（打码） ｜ 存为：`mimo-01-promo.png`

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **按量计费** | mimo-v2.6-pro 输入¥3/输出¥6 每百万 | - |
| **Token Plan 个人版** | Lite 39 / Standard 99 / Pro 329 / Max 659 元/月 | - |
| **Token Plan 团队版** | 详见平台官网 | - |

**渠道参数**：协议 `openai`，端点 `https://api.mimomidi.com/v1`（实际端点以平台为准）。

📷 **截图 4-10** ｜ 拍取 Token Plan 页（打码） ｜ 存为：`mimo-02-token-plan.png`

**模式说明**：

- `payg`：按量付费，`free_quota` 中建议标注 `¥10 体验金`（免费额度说明）
- `token_plan`：Token Plan 套餐，`tier: "paid"`，月付制，`billing: "token_plan"` 标识

**常见模型**：`mimo-v2.6-pro`, `mimo-lite`, `mimo-embedding` 等。

**适用场景**：移动端 AI、个人开发、轻量级应用等。

**⚠️ 备注**：payg 建议在免费额度处标注“¥10 体验金（40天内仅抵扣按量）”，帮助用户快速识别免费入口。

---

### 4.6 Kimi（月之暗面）

**官方入口**：[https://platform.kimi.com](https://platform.kimi.com)

**免费额度**：
- **国内新用户注册实名后赠 15 元代金券**（3 个月有效），见官方帮助页：https://www.kimi.com/zh-cn/help/kimi-api/api-free-trial

📷 **截图 4-11** ｜ 拍取免费试用领取页（打码） ｜ 存为：`kimi-01-free-trial.png`

**付费形态**：**仅按量付费**（官方明确「无订阅制方案」）：

| 形态 | 说明 | 价格 |
|---|---|---|
| **kimi-k3.1-pro** | 输入¥13/输出¥68 每百万 tokens | - |

**渠道参数**：协议 `openai`，端点 `https://platform.kimi.com/api/v1`。

📷 **截图 4-12** ｜ 拍取按量计费页（打码） ｜ 存为：`kimi-02-pricing.png`

**模式说明**：

- `payg`：按量付费，`free_limits` 或 `free_quota` 中建议标注“新人 15 元代金券（3个月有效）”，其余模型按量计费
- 官方明确：**API 无订阅制方案**，Kimi Code 订阅（Plus/Pro/Max/Ultra）与开放平台 API **不互通**，不计入 API 形态

**适用场景**：长文本对话、知识库检索、代码辅助等。

**⚠️ 备注**：payg 建议加免费标注（15 元券），帮助用户理解实际可用额度。

---

### 4.7 DeepSeek

**官方入口**：[https://api-docs.deepseek.com](https://api-docs.deepseek.com)

**免费额度**：
- **无明确官方免费额度**（仅活动性「赠送余额」机制，非常规福利）。

📷 **截图 4-13** ｜ 拍取定价页（打码） ｜ 存为：`deepseek-01-pricing.png`

**付费形态**：**仅按量付费**（峰谷计价，谷半价）：

| 形态 | 说明 | 价格 |
|---|---|---|
| **deepseek-flash** | 谷输入 $0.15/输出 $0.3，峰输入 $0.6/输出 $1.2 每百万 | - |
| **deepseek-coder** | 同等峰谷计价 | - |

**渠道参数**：协议 `openai`，端点 `https://api.deepseek.com/v1`。

📷 **截图 4-14** ｜ 拍取峰谷计价说明（打码） ｜ 存为：`deepseek-02-pricing-note.png`

**模式说明**：

- `payg`：按量付费，`tier: "paid"`，无免费额度（活动性赠送除外）
- 与注册表一致：无 Token Plan / Agent Plan 等套餐

**适用场景**：代码生成、长文本处理、多模odal 对话等。

**⚠️ 备注**：注册表无需修改，与官网保持一致。

---

### 4.8 腾讯混元

**官方入口**：[https://cloud.tencent.com/product/hunyuan](https://cloud.tencent.com/product/hunyuan)

**免费额度**：
- **首次开通送一次性资源包**（8 款模型共享 100 万 tokens，1 年有效）；
- **TokenHub 新用户免费体验包**（活动至 2026-12-31）。

📷 **截图 4-15** ｜ 拍取开通送资源包页（打码） ｜ 存为：`hunyuan-01-free-resource.png`

**付费形态**：

| 形态 | 说明 | 价格 |
|---|---|---|
| **token 后付费** | Hunyuan-a13b 输入¥0.5/输出¥2 每百万 tokens | - |
| **预付费资源包** | 如 1 万点 100 元 | - |
| **TokenHub Token Plan 包月** | Hy Token Plan 28/78/238/468 元/月；通用 Token Plan 39/99/299/599 元/月 | - |

**渠道参数**：
- `openai` 协议，端点 `https://ark.cn-beijing.volces.com/api/plan/v3`（TokenPlan 入口）
- `anthropic` 协议，端点 `https://ark.cn-beijing.volces.com/api/plan`（Anthropic 协议渠道）

📷 **截图 4-16** ｜ 拍取 Token Plan 与 Agent 套餐页（打码） ｜ 存为：`hunyuan-02-plans.png`

**模式说明**：

- `payg`：按量付费，`free_quota: {tokens: 1000000, period: "total"}`（开通送 100 万 tokens/1 年）
- `agent` / `token_plan`：Token Plan 套餐，新增入口（此前缺失，现已补全）

  - **Hy Token Plan**：28/78/238/468 元/月（8 模型共享）
  - **通用 Token Plan**：39/99/299/599 元/月（含 DeepSeek/GLM/Kimi/MiMo 等）
  - `billing: "token_plan"` 标识，`plan_key: "agent"`（对应 agent 键名）

- `coding`：Coding Plan（可选，视官方是否保留旧版）

**常见模型**：`hunyuan-a13b`, `hunyuan-lite`, `hunyuan-large` 等。

**适用场景**：企业级大模型应用、8模型混合调度、需要按月预算的场景。

**⚠️ 备注**：注册表已补全 token_plan mode + payg 免费额度 100万 tokens/1年（见前文更新记录）。

---

## 五、海外按量付费厂家

### 5.1 Anthropic 直连

**官方入口**：[https://console.anthropic.com](https://console.anthropic.com)

**步骤**

1. 打开 [console.anthropic.com](https://console.anthropic.com) 注册（邮箱验证，部分情况要求手机验证）；
2. **预充值**：Billing 页买 credit（最低 $5）；
3. **一次性 $5 试用金**：完成手机验证后，Console 余额会自动发放 $5 试用金，有效期 30 天，可用于按量调用；
4. **API Keys**（直链：[console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)）→ **Create Key**（`sk-ant-` 开头）。

   📷 **截图 5-1** ｜ 拍什么：API Keys 页（Key 打码） ｜ 存为：`anthropic-01-apikeys.png`

**渠道参数**：协议 `anthropic`，端点 `https://api.anthropic.com`。国内直连不通时配 `proxy`。

💡 **提示**：Max/Ultra/Team 订阅后，每月会向 Console 送送额度 API credits（$100/$200/至$500），若需持续使用建议订阅对应方案。

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
2. **Console**（[opencode.ai/console](https://opencode.ai/console)）→ 订阅 **Go**（$10/月，另有 Plus/Teams）并绑定支付方式；
3. Console 内直接**复制 API Key**。

   📷 **截图 6-1** ｜ 拍什么：Console 概览页显示订阅状态与 Key（打码） ｜ 存为：`opencode-01-console.png`

**渠道参数**：

| 模式 | 端点 |
|---|---|
| Go 订阅套餐 | `https://opencode.ai/inference/go/openai/v1` |
| 按量（Models） | `https://opencode.ai/inference/openai/v1` |

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

- `payg`：按量付费，`free_quota` 中默认包含 **3 免费模型**：glyph-cluster:free（1000 requests/天）、crouton-alpha（1000 requests/天）、inkling-2-small:free（200 requests/天），无需额外配置即可使用
- `agent`：套餐订阅，Provider $15/月 + $1.01 手续费，含 $15 永不过期额度，按量扣
- `coding` / `coding_plan`：可选，视用户需求

**⚠️ 备注**：CommandCode 已在 providers.json 中补全 `payg` mode + 免费模型 `free_limits` note（见前文更新记录）。

---

## 七、在 Trae / IDE 里使用 Facet

**前提**：你的机器上已经成功启动 `facet-planrouter`，并且用下面的网关 Key 能通
（`curl -H "Authorization: Bearer <gateway_key>" http://127.0.0.1:8787/v1/models` → 200）。

**快速开始**：

1. 打开 Trae / IDE，调出 Facet 侧边栏（或命令面板：`PlanRouter: Open`）；
2. 如果是首次使用，会弹出**新手向导**（8 步）——引导您设置保护键、选择厂家、填入 Key，并自动测试连通性；
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
- 百度千帆（剩余 17 模型免费额度）
- 智谱 GLM（Flash 系列免费）
- 以及其他已标注 free_quota/free_limits 的厂家

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
| 2026-11-13 | - | 初版发布，基于 20 家厂家官网核实 |
| 2026-11-13 | - | 更新魔粒每日领取数：250 → 200 |
| 2026-11-13 | - | 更新 OVHcloud 免费层表述：匿名试用（2 RPM 限速） |
| 2026-11-13 | - | 新增 LLM7 Pro $12/月 订阅说明 |
| 2026-11-13 | - | 新增 CommandCode 免费模型 note |
| 2026-11-13 | - | 新增 Z.AI Coding Plan 订阅信息 |
| 2026-11-13 | - | 新增 Anthropic $5 试用金说明 |
| 2026-11-13 | - | 更新千魔免费额度：¥10 体验金、¥20 代金券等 |