# Facet 渠道密钥获取指南

> **本手册怎么用**：按厂家分节，每节给出「注册 → 拿 Key → 填进 Facet」的完整步骤。
> 文中所有 📷 是**截图位**——按说明操作到对应页面时截图，保存到
> `docs/images/api-key-guide/` 目录并使用标注的文件名，图片会自动显示在本文档中。
>
> **为什么值得照着注册（不花您的钱）**：国内主流厂家（百炼、火山、魔搭、智谱、千帆、DeepSeek、Kimi）
> 都给实名个人账号**免费额度**——新模型 90 天窗口、每日登录领魔粒、新人 100 万 token……
> 这些额度**不用就作废，用了不扣您的钱**。用户不知道有免费、也不知道去哪领，是普遍痛点；
> 本手册把注册入口、Key 位置、免费额度规则聚合在一起，照着点一遍即可。Facet 还会**每日自动对齐**
> 哪些模型还有免费额度（见 [4.3 百炼](#43-阿里云百炼) 的 `usage free` 定时任务），帮您把白嫖额度一直用满。
> 真正「匿名免注册」现在只剩极少数端点（且限速极低，见 [3.1](#31-pollinations免注册免-key)），
> 其余各家**注册是拿免费额度的前提**，不是收费门槛。
>
> 密钥属于敏感信息：截图前请**遮住或截掉完整 Key**，只保留开头几个字符（如 `nvapi-…`）。
>
> 平台界面会随时间变化，本手册按 2026-11 时点整理；若界面不同，以实际为准并顺手更新本文。

---

## 目录

- [一、密钥怎么填进 Facet](#一密钥怎么填进-facet)
- [二、厂家总览](#二厂家总览)
  - [密钥为什么填在渠道上](#21-密钥为什么填在渠道上)
  - [同一账号开通多个套餐怎么配](#22-同一账号开通多个套餐怎么配)
- [三、免费渠道（零成本起步）](#三免费渠道零成本起步)
  - [Pollinations（免注册）](#31-pollinations免注册免-key)
  - [NVIDIA NIM（免费层）](#32-nvidia-nim免费层)
  - [OpenRouter 免费池](#33-openrouter免费池)
  - [Google AI Studio](#34-google-ai-studio)
  - [LLM7（匿名可用、注册提额）](#35-llm7匿名可用注册提额)
  - [OVHcloud AI Endpoints（匿名可用、token 可选）](#36-ovhcloud-ai-endpoints匿名可用-token-可选)
  - [魔搭 ModelScope（免费推理）](#37-魔搭-modelscope免费推理)
- [四、国内按量付费厂家](#四国内按量付费厂家)
  - [火山引擎方舟（豆包）](#41-火山引擎方舟豆包)
  - [智谱 GLM](#42-智谱-glm)
  - [阿里云百炼](#43-阿里云百炼)
  - [腾讯元宝（混元）](#44-腾讯元宝混元)
  - [百度千帆](#45-百度千帆)
  - [DeepSeek](#46-deepseek)
  - [Kimi（Moonshot）](#47-kimimoonshot)
  - [小米 MiMo](#48-小米-mimo)
- [五、海外按量付费厂家](#五海外按量付费厂家)
  - [Anthropic 直连](#51-anthropic-直连)
  - [OpenAI 直连](#52-openai-直连)
  - [xAI Grok](#53-xai-grok)
- [六、订阅聚合类](#六订阅聚合类)
  - [OpenCode](#61-opencode)
  - [Command Code](#62-command-code)
  - [本地订阅桥（claude-plan / codex-plan）](#63-本地订阅桥claude-plan--codex-plan)
- [七、在 Trae / IDE 里使用 Facet](#七在-trae--ide-里使用-facet)
  - [Trae（推荐优先试这节）](#71trae推荐优先试这节)
  - [VS Code / Cursor / Zed（通用 OpenAI SDK）](#72vs-code--cursor--zed通用-openai-sdk)
  - [Python SDK 快速示例](#73python-sdk-快速示例)
  - [Docker 里也要用？](#74docker-里也要用)
  - [内置别名：facet-auto / facet-cheap / facet-free / facet-agent](#741-内置别名facet-auto--facet-cheap--facet-free--facet-agent)
  - [DSH（DeepSeek CLI）PlanRouter 集成](#75-dshdeepseek-cli-planrouter-集成)
  - [OpenCode（AI coding CLI）自定义模型配置](#76-opencodeai-coding-cli自定义模型配置)
  - [CodeBuddy Code CLI 自定义模型配置](#77-codebuddy-code-cli-自定义模型配置)
  - [CodeBuddy（插件/IDE）自定义模型配置](#78-codebuddy插件ide自定义模型配置)
  - [Claude Code（CLI Agent）](#79claude-codecli-agent)
  - [Cline / Roo Code（VS Code 插件 Agent）](#710cline--roo-codevs-code-插件-agent)
  - [OpenHands / Devika / Devin（开源 DevAgent）](#711openhands--devika--devin开源-devagent)
  - [Windsurf / Zed / Cursor（AI 原生编辑器）](#712windsurf--zed--cursorai-原生编辑器)
  - [LangChain / LlamaIndex（Python Agent 框架）](#713langchain--llamaindexpython-agent-框架)
  - [LangGraph / AutoGen / CrewAI（多 Agent 框架）](#714langgraph--autogen--crewaimulti-agent-框架)
  - [故障排查](#715-故障排查)
- [八、附录：端点与协议速查表](#八附录端点与协议速查表)
- [九、控制台观测：免费池与缓存率](#九控制台观测免费池与缓存率)
  - [免费池：哪些模型真的会被调用](#91-免费池哪些模型真的会被调用)
  - [缓存命中率怎么看](#92-缓存命中率怎么看)

---

## 一、密钥怎么填进 Facet

所有厂家拿到 Key 后，填法都一样：

1. 打开控制台（`http://127.0.0.1:8787/ui`）→ **渠道配置** → **新增渠道**；
2. 照着对应厂家小节里的「渠道参数」填 **协议 / 端点 / 模型映射 / 优先级**；
3. **密钥**栏直接粘贴明文 Key（需要引用容器环境变量时才写 `env:VAR`）；
4. 保存即生效，网关热加载，无需重启。

要点：

- 编辑已有渠道时，密钥栏显示 `***` 表示不改已有 Key，留空保存即可；
- **导出配置不含密钥明文**，配置文件在本地 `<config 目录>/router.json`；
- 想用环境变量：写 `env:NVIDIA_API_KEY` 这样的引用，Facet 启动时解析；
- 添加渠道后点该行的 **测试** 按钮验证连通性（可在编辑器里预先填好「测试模型」并随渠道保存）；
- 一个账号开通了多个套餐（如 Agent + Coding）时，每个套餐单独建一个渠道，见 [2.2](#22-同一账号开通多个套餐怎么配)。

📷 **截图 1-1** ｜ 拍什么：控制台「新增渠道」弹窗全貌（可遮住密钥值） ｜ 存为：`general-01-add-channel.png`

![新增渠道弹窗](images/api-key-guide/general-01-add-channel.png)

---

## 二、厂家总览

| 厂家 | 费用 | 注册门槛 | Key 形态 | 本节 |
|---|---|---|---|---|
| Pollinations | 匿名只读 / 对话需注册 Key | 匿名仅可拉模型清单；对话调用需免费注册 | 匿名无 Key / 注册取 Bearer Token | [3.1](#31-pollinations免注册免-key) |
| NVIDIA NIM | 免费层 | 邮箱 + 手机验证 | `nvapi-` | [3.2](#32-nvidia-nim免费层) |
| OpenRouter | 免费模型 / 充值解锁更高限额 | 邮箱即可 | `sk-or-v1-` | [3.3](#33-openrouter免费池) |
| Google AI Studio | 有免费额度 | Google 账号 | `AIza…` | [3.4](#34-google-ai-studio) |
| LLM7 | 免费匿名（token 可选提额） | 无需注册 | 无 Key | [3.5](#35-llm7免注册免-key) |
| OVHcloud AI Endpoints | 免费匿名（2 RPM） | 无需注册 | 无 Key | [3.6](#36-ovhcloud-ai-endpoints免注册) |
| 魔搭 ModelScope | 每日登录领 250 魔粒（约 250–500 次调用） | 手机号（需绑阿里云） | `ms-` | [3.7](#37-魔搭-modelscope免费推理) |
| 火山引擎方舟 | 按量 + 部分免费模型 + Coding 套餐 | 实名认证 | 火山方舟 Key；免费额度对齐另需 IAM AK/SK | [4.1](#41-火山引擎方舟豆包) |
| 智谱 GLM | 按量 + 新人额度 + Coding 套餐 | 手机号 | `id.` 长串 | [4.2](#42-智谱-glm) |
| 阿里云百炼 | 按量 + 新人免费额度（新模型 90 天窗口）+ Token Plan | 实名认证 | `sk-` | [4.3](#43-阿里云百炼) |
| 腾讯元宝（混元） | 按量 | 实名认证 | 混元 API KEY | [4.4](#44-腾讯元宝混元) |
| 百度千帆 | 按量 + ERNIE-Speed 永久免费 | 实名认证 | `bce-v3/ALTAK-…` | [4.5](#45-百度千帆) |
| DeepSeek | 按量（便宜） | 手机号 | `sk-` | [4.6](#46-deepseek) |
| Kimi (Moonshot) | 按量 + 新人额度 | 手机号 | `sk-` | [4.7](#47-kimimoonshot) |
| 小米 MiMo | 按量 + Token Plan | 小米账号 | `sk-` / `tp-` | [4.8](#48-小米-mimo) |
| Anthropic | 按量（预充值） | 邮箱（可能验证手机） | `sk-ant-` | [5.1](#51-anthropic-直连) |
| OpenAI | 按量（预充值） | 邮箱 | `sk-proj-` / `sk-` | [5.2](#52-openai-直连) |
| xAI Grok | 按量 | X 账号 | `xai-` | [5.3](#53-xai-grok) |
| Z.AI（智谱海外） | 按量 | 邮箱 | `sk-` 类 | [5.4](#54-zai智谱海外) |
| OpenCode | 订阅（$10/月起） | 邮箱 | Console 内复制 | [6.1](#61-opencode) |
| Command Code | 订阅（$15/月起） | 邮箱 | `cc-…` 类 | [6.2](#62-command-code) |

### 2.0 优先注册顺序（免费量最大、模型最多的在前）

如果只注册 1–3 家，按这个顺序来，免费额度最够用：

1. **阿里云百炼**（[4.3](#43-阿里云百炼)）：新模型 90 天免费窗口、模型多、OpenAI 兼容，实名认证一次搞定；
2. **火山引擎方舟**（[4.1](#41-火山引擎方舟)）：豆包 lightweight 系列长期有免费额度，且能接 Coding / Agent 套餐；
3. **魔搭 ModelScope**（[3.7](#37-魔搭-modelscope免费推理)）：每天登录领 250 魔粒，白嫖国产开源模型。

其余匿名档（Pollinations / LLM7 / OVHcloud / OpenRouter 免费池）只当**首启体验和最末兜底**——限速低、且 Pollinations 的对话接口已要求注册 Key（见 [3.1](#31-pollinations免注册免-key)），不建议当主力。

### 2.1 密钥为什么填在渠道上

Facet 的「渠道」是一个**可独立计费、独立限流、独立失效**的调用单元：一个端点 + 一把 Key + 一组模型映射 + 一条优先级/冷却策略。密钥的粒度天然与此对齐：

- 同一厂家的不同产品线（按量、Coding 套餐、Agent 套餐）端点不同；
- 套餐 Key 往往是**专属**的：以火山引擎为例，Coding Plan 的 Key 不能用于 Agent Plan，Agent Plan 的 Key 也不能用于普通按量接口；
- 配额、冷却、额度耗尽、套餐到期都按 Key／套餐独立计算。

所以「Key 填在渠道上」不是临时妥协，而是唯一正确的位置：渠道就是 Key 的作用域。换 Key 只改这一个渠道，不会影响同账号的其它套餐。**不要**把同一账号的多把 Key 合并进一个渠道，也不要把 Key 提到全局——网关无法从请求内容推断这次想用哪个套餐。

### 2.2 同一账号开通多个套餐怎么配

规则一句话：**账号 × 套餐 = 一个渠道**。N 个账号、每个 M 个套餐，就是 N×M 个渠道，每个渠道填该套餐自己的端点与专属 Key。

例如 2 个火山引擎账号、每个账号都开了 Agent 套餐和 Coding 套餐，就建 4 个渠道。配好第一个后，剩下的直接在控制台渠道行点 **复制**：端点、密钥、模型映射、限流整体克隆（新名自动加 `-copy` 后缀），再点编辑改掉名称、换上新账号的 Key、调整优先级即可；复制出的渠道**不会自动加入路由链**，改好后记得在「路由链」里添加。

```json
"channels": [
  {"name": "volc-a-agent", "provider": "volcengine", "mode": "agent",
   "protocol": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
   "api_key": "env:VOLC_A_AGENT_KEY", "tier": "paid", "priority": 10,
   "models": {"auto": "doubao-seed-2.1-pro", "doubao-seed-2.1-pro": "doubao-seed-2.1-pro"}},
  {"name": "volc-a-coding", "provider": "volcengine", "mode": "coding",
   "protocol": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
   "api_key": "env:VOLC_A_CODING_KEY", "tier": "paid", "priority": 20,
   "models": {"auto": "doubao-seed-2.1-pro", "doubao-seed-2.1-pro": "doubao-seed-2.1-pro"}},
  {"name": "volc-b-agent", "provider": "volcengine", "mode": "agent",
   "protocol": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
   "api_key": "env:VOLC_B_AGENT_KEY", "tier": "paid", "priority": 30,
   "models": {"auto": "doubao-seed-2.1-pro", "doubao-seed-2.1-pro": "doubao-seed-2.1-pro"}},
  {"name": "volc-b-coding", "provider": "volcengine", "mode": "coding",
   "protocol": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
   "api_key": "env:VOLC_B_CODING_KEY", "tier": "paid", "priority": 40,
   "models": {"auto": "doubao-seed-2.1-pro", "doubao-seed-2.1-pro": "doubao-seed-2.1-pro"}}
],
"routes": [
  {"name": "default", "protocol": "openai",
   "channels": ["volc-a-agent", "volc-a-coding", "volc-b-agent", "volc-b-coding"]}
]
```

> 以上是 `router.json` 的**片段**，只截取与本例相关的部分：实际文件里还要保留原有的 `listen`、以及未展示的其它路由（含 `anthropic:default`）。`agent` 模式已内置在注册表，控制台里厂家选「火山引擎方舟」、模式选「Agent 套餐」即可，不必手抄 JSON。

**账号维度不进渠道，而是进「订阅」**——订阅记录回答「哪个账号、哪个套餐、多少钱、何时到期、提醒几天」，并用 `channels` 关联到具体渠道名：

```json
{"vendor": "火山引擎", "account": "账号 A", "plan": "Agent 套餐",
 "price": 199, "currency": "CNY", "billing_cycle": "monthly",
 "started_at": "2026-10-10", "expires_at": "2026-12-10", "remind_days": 7,
 "channels": ["volc-a-agent"]}
```

为什么必须一分为二：

- **命名**：推荐 `<厂家>-<账号简称>-<套餐>`。渠道名会进入订阅关联、路由列表和用量统计，建好后尽量别改。
- **不能合并套餐**：一个渠道只能填一个 `base_url`；两个套餐塞进去，模型目录、冷却、到期、限流都会互相污染。
- **到期互不影响**：开启「过期阻断」后，只有过期套餐对应的渠道被摘掉，同账号另一个套餐继续可用。这正是「账号」与「渠道」分开记录的价值。
- **路由顺序**：同账号的多个套餐一起进 `default`（或对应协议的路由），用 `priority` 决定先试谁；各套餐配额独立，一个限流冷却不会拖累另一个。
- **Key 引用**：用 `env:VOLC_A_AGENT_KEY` 这类引用，不要明文；两把 Key 恰好相同时，也分别写在不同渠道的 `api_key` 字段里。

---

## 三、免费渠道（零成本起步）

### 3.1 Pollinations（免注册、免 Key）

**官方入口**：[接口文档（APIDOCS）](https://raw.githubusercontent.com/pollinations/pollinations/master/APIDOCS.md) ｜ [模型清单（匿名可拉）](https://gen.pollinations.ai/v1/models) ｜ [注册取 Key](https://enter.pollinations.ai/keys)

零密钥首启的默认渠道，网关种子配置已自动建好——但要注意**匿名档的实际能力**（2026-11 实测）：

- `GET /v1/models` 匿名可拉（模型目录同步不受影响）；
- `POST /v1/chat/completions` 匿名调用一律 **401 Unauthorized**，必须填注册 Key 才能对话；
- 官方四档：**Anonymous**（仅可拉清单）→ **Seed**（免费注册、对话约 12 次/分钟）→ **Flower**（付费、高级模型）→ **Nectar**（企业、全模型无限额）。

渠道参数：协议 `openai`，端点 `https://gen.pollinations.ai/v1`。匿名档只够当首启体验和最末兜底；要真正能用，到 [enter.pollinations.ai/keys](https://enter.pollinations.ai/keys) 免费注册拿 Token 填进密钥栏。**它不建议作为主力，优先注册百炼 / 火山 / 魔搭**（见 [2.0](#20-优先注册顺序免费量最大模型最多的在前)）。

📷 **截图 3-1** ｜ 拍什么：控制台中 pollinations 渠道行（显示「测试通过」的绿色提示更佳） ｜ 存为：`pollinations-01-channel.png`

![Pollinations 渠道](images/api-key-guide/pollinations-01-channel.png)

### 3.2 NVIDIA NIM（免费层）

**官方入口**：[注册/登录](https://build.nvidia.com) ｜ [API Key 页](https://build.nvidia.com/settings/api-keys) ｜ [接口文档](https://docs.api.nvidia.com/nim/)

当前推荐的免费兜底：免充值、OpenAI 兼容、免费层约 40 RPM。

**步骤**

1. 打开 [build.nvidia.com](https://build.nvidia.com)，用邮箱注册 / 登录；
   注册需完成**邮箱验证**——验证码发到邮箱，填回页面验证通过后才能注册成功；
   ![NVIDIA 注册页](images/api-key-guide/nvidia-01-signup.png)
   然后还需**手机验证码**（部分国家地区不在支持列表时，可发邮件到
   help@build.nvidia.com 申诉）。
   ![NVIDIA 手机验证码](images/api-key-guide/nvidia-02-signup.png)


2. 登录后点右上角头像 → **API Keys**（直链：[build.nvidia.com/settings/api-keys](https://build.nvidia.com/settings/api-keys)）。

   ![NVIDIA API Keys 页](images/api-key-guide/nvidia-02-keys-page.png)

3. 点 **Generate API Key**，复制生成的 Key（`nvapi-` 开头，**只显示一次**）。

   ![NVIDIA 生成 Key](images/api-key-guide/nvidia-03-key-created.png)

**渠道参数**（种子配置已内置 `nvidia-nim` 渠道，填上 Key、启用即可）：

| 项 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://integrate.api.nvidia.com/v1` |
| 模型映射（auto） | `nvidia/nemotron-3.5-nano-30b-a3b` |
| 建议优先级 | 50（排在链尾当兜底） |
| 限速建议 | `rpm: 40` |

### 3.3 OpenRouter（免费池）

**官方入口**：[注册/登录](https://openrouter.ai) ｜ [API Key 页](https://openrouter.ai/settings/keys) ｜ [接口文档](https://openrouter.ai/docs)

聚合几百个上游，`:free` 后缀模型免费。Facet 示例配置的 `free-tier` 渠道就是它。

**步骤**

1. 打开 [openrouter.ai](https://openrouter.ai) → **Sign in**（Google/GitHub/邮箱均可，无需手机验证）；
2. 右上角头像 → **Keys**（直链：[openrouter.ai/settings/keys](https://openrouter.ai/settings/keys)）。

   📷 **截图 3-5** ｜ 拍什么：Keys 页面 ｜ 存为：`openrouter-01-keys-page.png`

   ![OpenRouter Keys 页](images/api-key-guide/openrouter-01-keys-page.png)

3. **Create Key**，命名后复制（`sk-or-v1-` 开头，只显示一次）。可给单 Key 设额度上限。

   📷 **截图 3-6** ｜ 拍什么：创建 Key 弹窗（Key 值打码） ｜ 存为：`openrouter-02-key-created.png`

   ![OpenRouter 创建 Key](images/api-key-guide/openrouter-02-key-created.png)

**限额须知**：账户不充值时，免费模型约 **50 请求/天**；在 Credits 充值 $10 后提升到 **1000 请求/天**。免费模型池共享限速，高峰期容易 429——Facet 的冷却与故障转移就是为这种场景准备的，`free-tier` 的 `cooldown_minutes: 1440` 即每日额度耗尽后冷却一天。

**渠道参数**（示例配置 `free-tier`）：

| 项 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://openrouter.ai/api/v1` |
| 模型映射 | 只映射 `:free` 后缀模型 |
| 建议限速 | `rpm: 20` |

### 3.4 Google AI Studio

**官方入口**：[控制台（Google 账号登录）](https://aistudio.google.com) ｜ [API Key 页](https://aistudio.google.com/apikey) ｜ [接口文档](https://ai.google.dev/gemini-api/docs)

Google 官方免费额度（Gemini 系列），零密钥种子模板内置了禁用占位渠道。

**步骤**

1. 打开 [aistudio.google.com](https://aistudio.google.com)，用 Google 账号登录（部分地区需代理，渠道可配 `proxy`）；
2. 左侧 **Get API key**（直链：[aistudio.google.com/apikey](https://aistudio.google.com/apikey)）。

   📷 **截图 3-7** ｜ 拍什么：API Keys 页面 ｜ 存为：`google-01-keys-page.png`

   ![Google AI Studio Keys 页](images/api-key-guide/google-01-keys-page.png)

3. **Create API key** → 复制（`AIza` 开头）。

   📷 **截图 3-8** ｜ 拍什么：创建结果（Key 值打码） ｜ 存为：`google-02-key-created.png`

   ![Google 创建 Key](images/api-key-guide/google-02-key-created.png)

**渠道参数**（种子配置 `google-ai-studio`）：

| 项 | 值 |
|---|---|
| 协议 | `openai`（Google 提供 OpenAI 兼容端点） |
| 端点 | `https://generativelanguage.googleapis.com/v1beta/openai` |
| 备注 | 国内直连不通，渠道里配出站代理 |

---

### 3.5 LLM7（匿名可用、注册提额）

**官方入口**：[接口文档](https://docs.llm7.io) ｜ [模型清单](https://api.llm7.io/v1/models) ｜ [注册/获取 Token（可选提额）](https://dash.llm7.io)

无需注册即可调用的 OpenAI 兼容免费端点，模型清单以官方 `GET /v1/models` 实时返回为准。

📷 **截图 3-9** ｜ 拍什么：docs.llm7.io 文档首页 ｜ 存为：`llm7-01-docs.png`

![LLM7 官方文档](images/api-key-guide/llm7-01-docs.png)

**步骤**：无需注册，控制台直接新增渠道即可用。想要更高限额（60 RPM / 每小时 250 次 / 10 万 tokens 每天）再去 [dash.llm7.io](https://dash.llm7.io) 免费注册拿 token，可选。

**渠道参数**：

| 项 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://api.llm7.io/v1` |
| 限额 | 免费档 60 RPM / 250 次/小时 / 10 万 tokens/24h（匿名档未公布） |

---

### 3.6 OVHcloud AI Endpoints（匿名可用、token 可选）

**官方入口**：[接口文档（Getting started）](https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-getting-started) ｜ [模型目录](https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/) ｜ [OVHcloud 账号（仅 token 档需要）](https://www.ovhcloud.com)

OVHcloud 公共 AI 端点，匿名档 2 次/分钟/IP/模型——很慢，但完全免费、无需注册，适合兜底而非主力；模型有 3 个月宽限期后下线的机制。

📷 **截图 3-10** ｜ 拍什么：OVHcloud AI Endpoints 模型目录页 ｜ 存为：`ovhcloud-01-catalog.png`

![OVHcloud 模型目录](images/api-key-guide/ovhcloud-01-catalog.png)

**步骤**：无需注册，控制台直接新增渠道即可用。带 key 档需要 OVHcloud 项目绑定支付方式且按量计费，不建议。

**渠道参数**：

| 项 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1` |
| 限额 | 匿名 2 RPM/IP/模型；超限 429 |

### 3.7 魔搭 ModelScope（免费推理）

**官方入口**：[注册/登录](https://modelscope.cn) ｜ [访问令牌页](https://modelscope.cn/my/myaccesstoken) ｜ [接口文档](https://modelscope.cn/docs)

阿里魔搭社区的免费推理 API（API-Inference）：OpenAI 兼容，覆盖 4 万+ 开源模型（Qwen / DeepSeek / GLM 系基本都在）。免费额度走「魔粒」积分制：**每天登录魔搭领 250 魔粒**（登录 +200，绑定阿里云后每日再 +50），单次调用约消耗 0.5–1 魔粒，折算下来每天约 250–500 次调用。两个要点：魔粒**当日有效、不累积**，不领就作废；「每日登录领魔粒」是网页上的手动动作，网关代不了——哪天忘了领，这个渠道当天就没额度，Facet 的冷却与故障转移会自动落到下一渠道。

**步骤**

1. 打开 [modelscope.cn](https://modelscope.cn)，手机号注册 / 登录（支持阿里云等第三方登录；**建议绑定阿里云账号**——每日魔粒从 200 涨到 250）。
2. 登录后进入「访问令牌」页（直链：[modelscope.cn/my/myaccesstoken](https://modelscope.cn/my/myaccesstoken)），点 **创建访问令牌**，复制生成的令牌（`ms-` 开头）。
3. **之后每天登录一次**魔搭领当日魔粒——「我的魔粒」页可查发放与消耗记录，不领当日作废。

📷 **截图 3-11** ｜ 拍什么：魔搭「访问令牌」页创建令牌后的界面 ｜ 存为：`modelscope-01-token.png`

![魔搭访问令牌](images/api-key-guide/modelscope-01-token.png)

📷 **截图 3-12** ｜ 拍什么：「我的魔粒」页的发放记录（每日 250 魔粒的构成一目了然） ｜ 存为：`modelscope-02-moli.png`

![魔搭我的魔粒](images/api-key-guide/modelscope-02-moli.png)

**渠道参数**：

| 项 | 值 |
|---|---|
| 协议 | `openai` |
| 端点 | `https://api-inference.modelscope.cn/v1` |
| 上游路径 | `/chat/completions`（端点已含 `/v1`，防止重复拼接） |
| 建议优先级 | 45（排 OpenRouter 免费池之后，当日常补充燃料） |
| 限额 | 每日登录领 250 魔粒（登录 200 + 绑阿里云 50），单次调用约 0.5–1 魔粒，当日有效不累积；渠道限速建议 `rpd: 250` |

新增渠道保存后点「拉取模型」拉取目录，挑带 **Free 限免** 标记的模型填映射与「测试模型」，再启用渠道即可。

---

## 四、国内按量付费厂家

> 国内平台注册普遍需要**实名认证**；新用户多有免费 token 额度，按量价格以各官网为准。

### 4.1 火山引擎方舟（豆包）

**官方入口**：[注册/登录火山引擎](https://console.volcengine.com) ｜ [方舟 API Key 管理](https://console.volcengine.com/ark/region:cn-beijing/apiKey) ｜ [方舟接口文档](https://www.volcengine.com/docs/82379)

**步骤**

1. 打开 [console.volcengine.com](https://console.volcengine.com) 注册火山引擎账号并完成**实名认证**；

   📷 **截图 4-1** ｜ 拍什么：实名认证入口/完成状态页 ｜ 存为：`volcengine-01-realname.png`

2. 进入**方舟**控制台（直链：[console.volcengine.com/ark](https://console.volcengine.com/ark)）→ 左侧 **API Key 管理** → **创建 API Key**。

   📷 **截图 4-2** ｜ 拍什么：方舟「API Key 管理」页面 ｜ 存为：`volcengine-02-apikey-page.png`

3. 开通模型：在方舟的**开通管理**里启用要用的模型。豆包 `doubao-seed-*-lite` 等 lightweight 模型常有**免费额度**（示例配置的「火山引擎免费」渠道即由此而来）。

   📷 **截图 4-3** ｜ 拍什么：模型开通管理页（显示免费额度标识） ｜ 存为：`volcengine-03-models.png`

**渠道参数**：

| 模式 | 端点 |
|---|---|
| 按量付费 | `https://ark.cn-beijing.volces.com/api/v3` |
| Coding 套餐（订阅后） | `https://ark.cn-beijing.volces.com/api/coding/v3` |
| Agent 套餐（订阅后，OpenAI 协议） | `https://ark.cn-beijing.volces.com/api/plan/v3` |
| Agent 套餐（订阅后，Anthropic 协议） | `https://ark.cn-beijing.volces.com/api/plan` |

**Agent 套餐要点**

- Agent 套餐、Coding 套餐、按量付费是**三条独立产品线**：分别购买、分别签发 Key，Key 互不通用（Agent Plan 的 Key 用不到 Coding Plan，反之亦然）。
- 在控制台建渠道时，厂家选「火山引擎方舟」、模式选「**Agent 套餐**」，端点会自动带出 OpenAI 协议的 `/api/plan/v3`；想在 Facet 后面接 Claude Code 这类说 Anthropic 协议的工具，再单独建一个 `protocol: anthropic`、端点 `/api/plan` 的渠道（两者不能共用一个渠道）。
- Agent 套餐的模型清单由每日维护任务抓取「[套餐概览](https://console.volcengine.com/ark/region:cn-beijing/docs/agent-plan-personal-plan-overview)」文档写入注册表。**刚建好渠道就点「从上游拉取模型清单」可能只拿到一部分**——套餐端点本身不返回完整目录，等维护跑过一轮（或手动触发一次维护）再拉，才是文档里的完整清单。
- Agent 套餐按 AFP（Agent 燃料值）计费，额度用尽后的行为与 Coding 套餐一致：上游报额度错误时渠道进入冷却，冷却结束自动重试；这条路不参与「协作奖励计划」的免费额度对齐。

**查哪些模型还有免费额度（协作奖励计划）**

步骤 3 里说的免费额度（如 `doubao-seed-*-lite` 的赠送 token）来自火山「协作奖励计划」。它的剩余额度没有公开查询 API，需要走**管理面**接口查询——这需要 **IAM AK/SK**：控制台右上角头像 → **访问控制 IAM** → **API 访问密钥**（直链：[console.volcengine.com/iam](https://console.volcengine.com/iam)）创建。注意区分两种 Key：方舟「API Key 管理」发的是 `ark-` 开头的**数据面** Key，只用于调用模型；免费额度对齐用的是**管理面** IAM 密钥，两者互不通用。

**配置推荐走控制台界面**：管理台 → 渠道配置 → 编辑「火山引擎免费」渠道，厂家选火山引擎（按量付费）后，弹窗底部会出现 **IAM Access Key（AK）／ IAM Secret Key（SK）** 输入框，填入保存即可。凭据写入 `router.json` 的 `credentials` 段（与渠道 API Key 同级保护）；提交后回显为 `***`，留空表示保持已配值；界面配置值优先于环境变量。无头部署可用 `VOLC_ACCESSKEY` / `VOLC_SECRETKEY` 环境变量兜底（compose.yaml 已透传）。配好后在渠道编辑器点「从上游拉取模型清单」，拉到的就是管理面上**有免费额度的模型**清单（未配 IAM AK/SK 时仍走数据面 `/models` 的全量清单）。

这套对齐与百炼一样内置为 Facet 的**每日定时任务**（`defaults.free_align_hours` 控制间隔，0 关闭）：网关自动执行随包安装的 `facet-ark-quota` CLI，只保留仍有免费额度的模型作为渠道映射，额度耗尽/过期的自动摘掉，**日常无需手动管理**。想手动核对可单独执行 `facet-ark-quota --output json`；机制细节见[方舟免费额度接入](planrouter-ark-free-quota.md)。

### 4.2 智谱 GLM

**官方入口**：[注册/登录](https://open.bigmodel.cn) ｜ [API Key 页（按量）](https://bigmodel.cn/usercenter/proj-mgmt/apikeys) ｜ [接口文档](https://docs.bigmodel.cn)

**步骤**

1. 打开 [bigmodel.cn](https://bigmodel.cn) 注册（手机号 + 验证码），新用户有各模型免费 token 额度；
2. 右上角头像 → **API Keys**（直链：[bigmodel.cn/usercenter/proj-mgmt/apikeys](https://bigmodel.cn/usercenter/proj-mgmt/apikeys)）→ 复制 API Key。

   📷 **截图 4-4** ｜ 拍什么：API Keys 页面（Key 打码） ｜ 存为：`zhipu-01-apikeys.png`

3. GLM **Coding 套餐**用户注意：套餐 Key 与按量 Key **相互独立**，在
   [bigmodel.cn/apikey/coding-plan-per](https://bigmodel.cn/apikey/coding-plan-per) 单独获取，端点也不同（见下表）。

   📷 **截图 4-5** ｜ 拍什么：Coding 套餐 Key 页面（如已订阅） ｜ 存为：`zhipu-02-coding-plan.png`

**渠道参数**：

| 模式 | 端点 |
|---|---|
| 按量付费 | `https://open.bigmodel.cn/api/paas/v4` |
| Coding 套餐 | `https://open.bigmodel.cn/api/coding/paas/v4` |
| 国际版 z.ai（种子模板 `z-ai-glm`） | `https://api.z.ai/api/paas/v4` |

### 4.3 阿里云百炼

**官方入口**：[注册/登录阿里云](https://www.aliyun.com) ｜ [百炼控制台（右上角 API-KEY）](https://bailian.console.aliyun.com) ｜ [接口文档](https://help.aliyun.com/zh/model-studio/)

**步骤**

1. 注册阿里云账号并完成**实名认证**；
2. 打开**百炼**控制台（直链：[bailian.console.aliyun.com](https://bailian.console.aliyun.com)）→ 右上角选择**业务空间/地域** → **API-KEY** 页 → **创建 API Key**（`sk-` 开头）。

   📷 **截图 4-6** ｜ 拍什么：百炼 API-KEY 管理页（Key 打码） ｜ 存为：`bailian-01-apikeys.png`

3. 新用户免费额度规则（2026-10 官方口径）：开通百炼后各模型自动发放**新人免费额度**，每个模型 100 万 token；有效期 **90 天**，从账号开通、**模型发布**或模型申请通过之日起算（以较晚者为准——新上线的模型会刷新一个 90 天窗口）。仅**华北2（北京）**地域模型有额度，且只抵扣**实时推理**（Batch 调用不算）。建议在控制台开启「**免费额度用完即停**」，额度耗尽自动停止调用、不转按量扣费；
4. **注意**：API Key 按地域/业务空间隔离，不同空间的 Key 不可互换。

**查哪些模型还有免费额度（官方 CLI）**

百炼没有公开的免费额度查询 REST API；官方 CLI（npm 包 `bailian-cli`，命令 `bl`）的 `usage free` 可以查。Facet 镜像已内置 `bl 2.8.3`（`BAILIAN_CLI_VERSION` 可覆盖），容器内无需再装 Node 或 npm：

```powershell
# 容器部署（推荐）：镜像已内置 bl，直接在容器里完成一次授权；
# 令牌落在 /data/.bailian（compose 已把 ./data 挂成持久卷），重建镜像不丢。
docker exec -it facet-planrouter-router-1 bl auth login --console
docker exec facet-planrouter-router-1 bl usage free --output json --sort expires

# 非容器 / 本机排查时才需要自己装：
npm install -g bailian-cli                  # 无 Node.js 时：irm https://bailian.aliyun.com/cli/install.ps1 | iex
                                            # 若报 notarget（npm 发布缺陷），改用 npm install -g bailian-cli@2.8.3
bl auth login --console                     # 拉起浏览器完成阿里云控制台授权
bl usage free --output json --sort expires  # 全部模型剩余额度 + 到期时间（JSON）
bl usage free --expiring 7                  # 只看 7 天内到期的额度
```

**渠道映射必须以 `usage free` 的输出为准**——不要用控制台「拉取模型」目录来填百炼的映射：目录不区分免费/计费，拉进来的可能都是按量计费模型，路由打过去就变成后付费扣费。

这套对齐已经内置为 Facet 的**每日定时任务**（与目录刷新、每日自维护同一批后台任务，`defaults.free_align_hours` 控制间隔，0 关闭）：网关会自动执行 `bl usage free --output json`，只保留**type 为 Text、剩余额度 > 0、未过期**的模型作为渠道映射（恒等映射），额度耗尽/过期的自动摘掉，命令失败则保留原映射等下一轮。因此**日常无需手动管理**——你只需要在首次使用时完成一次控制台授权（`usage free` 属于控制台类命令，**只配 API Key 不够**，必须 `--console` 浏览器授权；授权过期后重新登录一次即可，定时任务下一轮自动恢复对齐）。想立即对齐（不等定时）可在管理台手动触发；`bl usage free` 命令本身也适合人工核对额度状态。

**渠道参数**：

| 模式 | 端点 |
|---|---|
| 按量付费 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Token Plan 套餐（订阅后） | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |

### 4.4 腾讯元宝（混元）

**官方入口**：[注册/登录腾讯云](https://console.cloud.tencent.com) ｜ [混元 Key 创建页](https://console.cloud.tencent.com/hunyuan/start) ｜ [接口文档](https://cloud.tencent.com/document/product/1729)

**步骤**

1. 注册腾讯云账号并完成**实名认证**；
2. 开通混元服务：[console.cloud.tencent.com/hunyuan/settings](https://console.cloud.tencent.com/hunyuan/settings)；

   📷 **截图 4-7** ｜ 拍什么：开通服务页面 ｜ 存为：`hunyuan-01-activate.png`

3. 控制台混元页 → **立即接入**（[console.cloud.tencent.com/hunyuan/start](https://console.cloud.tencent.com/hunyuan/start)）→ **创建 API KEY**；
   ⚠️ 主账号才能创建，**删除不可恢复**。

   📷 **截图 4-8** ｜ 拍什么：API KEY 管理页面（Key 打码） ｜ 存为：`hunyuan-02-apikeys.png`

**渠道参数**：协议 `openai`，端点 `https://api.hunyuan.cloud.tencent.com/v1`。

### 4.5 百度千帆

**官方入口**：[注册/登录百度智能云](https://console.bce.baidu.com) ｜ [API Key 管理](https://console.bce.baidu.com/iam/#/iam/apikey/list) ｜ [接口文档](https://cloud.baidu.com/doc/WENXINWORKSHOP/index.html)

**步骤**

1. 注册百度智能云账号并完成**实名认证**；
2. 打开千帆控制台（直链：[console.bce.baidu.com/qianfan](https://console.bce.baidu.com/qianfan)）→ **API Key 管理** → **创建 API Key**（选工作空间、勾选要启用的服务）；
   Key 形如 `bce-v3/ALTAK-…/…`，整串复制；一页管理 ModelBuilder / AppBuilder / 开放能力全部鉴权。

   📷 **截图 4-9** ｜ 拍什么：千帆 API Key 管理页（Key 打码） ｜ 存为：`qianfan-01-apikeys.png`

3. `ERNIE-Speed` 系列模型**永久免费**，适合做低成本兜底；新模型多有试用额度。

**渠道参数**：协议 `openai`，端点 `https://qianfan.baidubce.com/v2`。

### 4.6 DeepSeek

**官方入口**：[注册/登录开放平台](https://platform.deepseek.com) ｜ [API Key 页](https://platform.deepseek.com/apikeys) ｜ [接口文档](https://api-docs.deepseek.com)

**步骤**

1. 打开 [platform.deepseek.com](https://platform.deepseek.com)，手机号注册；
2. 左侧 **API keys** → **创建 API key**（`sk-` 开头，只显示一次）。

   📷 **截图 4-10** ｜ 拍什么：API keys 页面（Key 打码） ｜ 存为：`deepseek-01-apikeys.png`

3. **充值**：左侧「充值」页，人民币预付费，价格很低，几块钱够个人用很久。

**渠道参数**：协议 `openai`，端点 `https://api.deepseek.com/v1`。

### 4.7 Kimi（Moonshot）

**官方入口**：[注册/登录开放平台](https://platform.moonshot.cn) ｜ [API Key 页](https://platform.moonshot.cn/console/api-keys) ｜ [接口文档](https://platform.moonshot.cn/docs)

**步骤**

1. 打开 [platform.moonshot.cn](https://platform.moonshot.cn)，手机号注册，新用户赠送体验额度；
2. 左侧 **API Key 管理** → **新建**（`sk-` 开头，只显示一次）。

   📷 **截图 4-11** ｜ 拍什么：API Key 管理页（Key 打码） ｜ 存为：`moonshot-01-apikeys.png`

**渠道参数**：协议 `openai`，端点 `https://api.moonshot.cn/v1`。

### 4.8 小米 MiMo

**官方入口**：[MiMo 开放平台（注册 / Key / 文档同站）](https://platform.xiaomimimo.com)

**步骤**

1. 用小米账号登录 [platform.xiaomimimo.com](https://platform.xiaomimimo.com)；
2. **按量付费**：控制台充值（预付费）→ **API Keys** 页创建（`sk-` 开头，只显示一次）；

   📷 **截图 4-12** ｜ 拍什么：API Keys 页（Key 打码） ｜ 存为：`mimo-01-payg-keys.png`

3. **Token Plan 套餐**：[platform.xiaomimimo.com/#/token-plan](https://platform.xiaomimimo.com/#/token-plan) 订阅 → 在**订阅管理**（`#/console/plan-manage`）复制 Key（`tp-` 开头，团队版 `ttp-`）；
   ⚠️ `tp-` 与 `sk-` 两套 Key **互不通用**：`tp-` 走 Token Plan 端点，套餐过期即失效；`sk-` 走按量端点。

   📷 **截图 4-13** ｜ 拍什么：订阅管理页显示 tp- Key（打码） ｜ 存为：`mimo-02-tokenplan.png`

**渠道参数**：

| 模式 | 端点 |
|---|---|
| 按量付费 | `https://api.xiaomimimo.com/v1` |
| Token Plan 套餐 | `https://token-plan-cn.xiaomimimo.com/v1` |

---

## 五、海外按量付费厂家

### 5.1 Anthropic 直连

**官方入口**：[注册/登录 Console](https://console.anthropic.com) ｜ [API Key 页](https://console.anthropic.com/settings/keys) ｜ [接口文档](https://docs.anthropic.com)

**步骤**

1. 打开 [console.anthropic.com](https://console.anthropic.com) 注册（邮箱验证，部分情况要求手机验证）；
2. **预充值**：Billing 页买 credit（最低 $5）；
3. **API Keys**（直链：[console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)）→ **Create Key**（`sk-ant-` 开头）。

   📷 **截图 5-1** ｜ 拍什么：API Keys 页（Key 打码） ｜ 存为：`anthropic-01-apikeys.png`

**渠道参数**：协议 `anthropic`，端点 `https://api.anthropic.com`。国内直连不通时配 `proxy`。

### 5.2 OpenAI 直连

**官方入口**：[注册/登录 Platform](https://platform.openai.com) ｜ [API Key 页](https://platform.openai.com/api-keys) ｜ [接口文档](https://platform.openai.com/docs)

**步骤**

1. 打开 [platform.openai.com](https://platform.openai.com) 注册；
2. Billing 页预充值（最低 $5）；
3. **API keys**（直链：[platform.openai.com/api-keys](https://platform.openai.com/api-keys)）→ **Create new secret key**（`sk-proj-` 开头）。

   📷 **截图 5-2** ｜ 拍什么：API keys 页（Key 打码） ｜ 存为：`openai-01-apikeys.png`

**渠道参数**：协议 `openai`，端点 `https://api.openai.com/v1`。国内直连不通时配 `proxy`。

### 5.3 xAI Grok

**官方入口**：[注册/登录 Console](https://console.x.ai) ｜ [接口文档](https://docs.x.ai)

**步骤**

1. 打开 [console.x.ai](https://console.x.ai)，用 X（Twitter）账号登录；
2. 团队/计费初始化后 → **API Keys** → **Create API Key**（`xai-` 开头）；新账号常有少量赠送额度。

   📷 **截图 5-3** ｜ 拍什么：API Keys 页（Key 打码） ｜ 存为：`xai-01-apikeys.png`

**渠道参数**：协议 `openai`，端点 `https://api.x.ai/v1`。

### 5.4 Z.AI（智谱海外）

**官方入口**：[注册/登录](https://z.ai) ｜ [API Key 管理页](https://z.ai/manage-api-key/apikey-keys) ｜ [接口文档](https://docs.z.ai)

智谱 GLM 的国际站（`api.z.ai`），账号体系与 bigmodel.cn 相互独立，Key 不通用。种子模板 `z-ai-glm` 用的是这一家。

**步骤**

1. 打开 [z.ai](https://z.ai) 注册登录；
2. 进入 **API Keys**（直链：[z.ai/manage-api-key/apikey-keys](https://z.ai/manage-api-key/apikey-keys)）→ 创建并复制 API Key。

**渠道参数**：协议 `openai`，端点 `https://api.z.ai/api/paas/v4`。

---

## 六、订阅聚合类

### 6.1 OpenCode

**官方入口**：[注册/登录（含订阅与 Key）](https://opencode.ai/console) ｜ [接口文档](https://opencode.ai/docs)

SST/OpenCode 的订阅推理服务（Zen），Go 套餐 $10/月。

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

### 6.2 Command Code

**官方入口**：[注册/订阅](https://commandcode.ai) ｜ [API Key 页](https://commandcode.ai/settings/keys)

编码 CLI + Provider API 聚合（Claude / GPT / Gemini / 开源模型，不加价转售）。

**步骤**

1. 打开 [commandcode.ai](https://commandcode.ai) 注册；
2. Studio → **Billing**（[commandcode.ai/billing](https://commandcode.ai/billing)）选套餐：**Provider** $15/月 + $1.01 手续费，含 $15 永不过期额度，按量扣；Go 档（$1/月）**没有 API 权限**，勿选；
3. **Settings → Keys**（[commandcode.ai/settings/keys](https://commandcode.ai/settings/keys)）创建并复制。

   📷 **截图 6-2** ｜ 拍什么：Keys 页（Key 打码） ｜ 存为：`commandcode-01-keys.png`

**渠道参数**：协议 `openai`，端点 `https://api.commandcode.ai/provider/v1`。

### 6.3 本地订阅桥（claude-plan / codex-plan）

示例配置里的 `claude-plan`（`http://127.0.0.1:8080`，anthropic 协议）和
`codex-plan`（`http://127.0.0.1:8081`，openai 协议）是**本地桥接服务**——把
Claude Code / Codex 的订阅额度转成 API 供网关调用。它们不填官方 API Key，
**密钥栏填的是桥自己约定的口令**（与桥的配置保持一致，示例配置用
`env:CLAUDE_PLAN_KEY` / `env:CODEX_PLAN_KEY` 引用）。

前置条件：本机先跑起对应的桥，再启用这两个渠道。

📷 **截图 6-3** ｜ 拍什么：桥启动后的终端 + 控制台渠道行测试通过 ｜ 存为：`bridge-01-running.png`

---

## 附：安全清单

- [ ] 每张截图都检查过：完整 Key 不可见（至少打码到只剩前 6 位）
- [ ] 密钥只存进 Facet 渠道配置或 `env:` 环境变量，不进聊天记录 / 截图 / 仓库
- [ ] 给每把 Key 设了平台侧的额度上限或限速（能设的都设上）
- [ ] 不再用的 Key 已在平台上删除或轮换

---

## 七、在 Trae / IDE 里使用 Facet

**前提**：你的机器上已经成功启动 `facet-planrouter`，并且用下面的网关 Key 能通
（`curl -H "Authorization: Bearer <gateway_key>" http://127.0.0.1:8787/v1/models` → 200）。

- **网关 Key**（用于 `/v1/*` 请求）：从 `.env` 的 `ROUTER_API_KEY` 取值
- **管理密钥**（用于 `/api/*` 控制台）：默认 `admin`；首次登录后可在控制台右上角「管理密钥」重设或随机生成

### 7.1 Trae（推荐优先试这节）

> 若你的 Trae 版本有「自定义模型 / OpenAI 兼容」入口：

1. 打开 **设置** → **AI / LLM** → **添加提供方**（类型选择 **OpenAI** / **Custom Model**）
2. 填写参数：

| 字段 | 值 |
|---|---|
| API Base / Endpoint | `http://127.0.0.1:8787/v1` |
| API Key | `<ROUTER_API_KEY>`（网关 key，不要填管理密钥） |
| 模型名 | **具体模型**（推荐先写明确：`deepseek-flash` 或 `deepseek-v4-pro`；或用路由链 `facet-auto`/`facet-cheap`） |

3. 保存后点 **测试**（或发一条只问两个字的短消息），应能看到回复。

**注意**：
- `facet-auto` 按 `default` 链顺序调度（付费优先、`$free` 免费兜底）；如果看到 Trae 不回答（链上模型能力不匹配），请把模型名改成**具体渠道模型**（如 `deepseek-flash`）或 `facet-agent`。
- Trae 的 Agent 模式强依赖 tool calling；不少免费上游不支持 tools → 会 400。建议把支持 tools 的渠道模型（例如 DeepSeek 组、GPT-5 等）作为首选。
- 远程/容器部署时，Base URL 的 IP 要换成宿主机 LAN IP，并确保 8787 可访问。

📷 **截图 7-1**｜拍什么：Trae 的 AI 配置页面（API Base / Key / 模型名清晰可见）｜存为：`trae-01-config.png`

### 7.2 VS Code / Cursor / Zed（通用 OpenAI SDK）

以 VS Code 插件为例（Cursor/Zed同理）：

1. 安装任一 OpenAI 兼容的 Chat 插件（如 "Chat with Open Source Models"、"Continue"）
2. 新增 Provider：

```jsonc
// 示例：continue.config.ts 里的 provider
{
  "name": "Facet",
  "provider": "openai",
  "apiKey": "<ROUTER_API_KEY>",
  "apiBase": "http://127.0.0.1:8787/v1",
  "model": "deepseek-flash"   // 或用 facet-auto / facet-cheap
}
```

3. 验证连通性：输入 `ping`，期望返回简短文本。

### 7.3 Python SDK 快速示例

```python
import openai

client = openai.OpenAI(
    api_key="<ROUTER_API_KEY>",
    base_url="http://127.0.0.1:8787/v1",
)

chat = client.chat.completions.create(
    model="facet-cheap",       # 或用具体模型 deepseek-flash
    messages=[{"role":"user","content":"只用两个字回答：通了"}],
    stream=False,
)
print(chat.choices[0].message.content)
```

流式调用只需加 `stream=True`，并在返回对象上迭代 `chunk.choices[0].delta.content`。

### 7.4 Docker 里也要用？

如果你的应用跑在容器内，**容器的 host 解析**要指向宿主机：

- Linux 使用 `--add-host=host.docker.internal:host-gateway`
- Windows/macOS 容器通常已内置 `host.docker.internal`，端口透传正常即可

然后在代码里把 `base_url` 改为 `http://host.docker.internal:8787/v1`。

### 7.4.1 内置别名：`facet-auto` / `facet-cheap` / `facet-free` / `facet-agent`

Facet 网关提供四个可直接填进工具「模型名」框的内置别名：

| 模型名 | 含义 | 适用场景 |
|--------|------|----------|
| `facet-auto` | 在 `default` 链上按链序挑健康渠道（当前默认部署：火山两个付费套餐 → deepseek → `$free` 免费兜底，即**付费优先**） | 普通对话、单轮问答 |
| `facet-cheap` | 在 `cheap` 链上挑最便宜的（通常全是免费） | 大批量补全、闲聊 |
| `facet-free` | 只走 `free` 链——`$free` 宏自动展开的全部启用免费渠道，**付费渠道永远不会被调用** | 免费额度优先的批量任务 |
| **`facet-agent`** | **只挑「支持 tool calling」的模型**（免费优先，自动剔除已冷却）；具体落到哪个由网关运行时决定 | **Cline / Claude Code / OpenHands / LangGraph / 任意需要 function calling 的 Agent 工具** |

**别名 = 路由链的快捷方式**

内置别名只是「别名 → 链名」的映射：`facet-auto`→`default`、`facet-cheap`→`cheap`、`facet-free`→`free`、`facet-agent`→`default`（agent 模式挑选）。**链的排列顺序决定别名行为**：在控制台「渠道配置 → 路由链」重排 `default` 链，就能切换 `facet-auto` 的付费/免费优先级；想「纯免费、不碰付费」，直接用 `facet-free`，无需改链。

两点注意：

- 请求走 Anthropic 端点（`/v1/messages`）时，`facet-free` 找的是 Anthropic 协议的 `free` 链，没有则回退该协议的 `default` 链。`$free` 宏只在协议下确实存在启用的免费渠道时才有意义，因此 Facet 不会创建空的 Anthropic 免费链；
- 想要自己的别名，在控制台 `models.branded` 里加 `名字=链名` 即可（如 `fast=cheap`）；也可直接写 `auto:链名` / `facet:链名` 形式的临时别名（如 `auto:cheap`）。

**为什么要有 `facet-agent`？**

`facet-auto` 不区分模型能力，可能随机落到一个只输出 reasoning、不支持 tools 的免费上游，导致 Agent 工具反复报 `tool_call failed`。`facet-agent` 在运行时会：

1. 遍历当前启用、未冷却的所有渠道；
2. 从每个渠道已记录的模型目录里挑候选人：registry 标记 `tool_call=True` 的排最前，**能力未知的排在后面，明确标记 `tool_call=False` 的直接剔除**（不会「把目录里有什么就试什么」）；
3. 免费优先、按稳定性分排序（失败过的模型分数下降，自然沉底，不会反复撞同一个坏模型）；
4. 上游失败自动切下一个候选。

**怎么验证？**

```bash
curl -H "Authorization: Bearer $ROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"facet-agent","messages":[{"role":"user","content":"hi"}],"max_tokens":20}' \
  http://127.0.0.1:8787/v1/chat/completions -i
```

---

### 7.5 DSH（DeepSeek CLI）PlanRouter 集成

访问地址：[planrouter-dsh-integration.md](planrouter-dsh-integration.md) 详细文档。

**配置入口**：DSH 的配置分三层：bundle 默认 → `cordis.yml` → 用户的 `cordis.patch.yml`（顶层 YAML 数组，id 定向 patch）。我们只需写 patch 层，模板升级时不会被冲。

**配置步骤**：

1. **安装**：`npm install -g @deepseek-ai/dsh@latest --registry=https://registry.npmjs.org`
2. **创建两个 profile**（跑命令行 headless 和 Web UI 两种场景）：

   ```bash
   dsh --profile planrouter     --from-default-profile web
   dsh --profile planrouter-web --from-default-profile web
   ```

   生成在 `~/.dsh/profiles/<name>/`，各含 `package.json`、`cordis.yml`、`cordis.patch.yml`。

3. **换 bundle（headline profile）**：编辑 `~/.dsh/profiles/planrouter/package.json`：

   ```json
   {
     "name": "dsh-profile-planrouter",
     "private": true,
     "dependencies": {},
     "dsh": {
       "profile": {
         "bundles": [
           "@deepseek-ai/dsh-base",
           "@deepseek-ai/dsh-headless"
         ]
       }
     }
   }
   ```

4. **写 patch 层**：两个 profile 的 `cordis.patch.yml` 内容相同（实测可用）：

   ```yaml
   # Your patch layer for this dsh profile, applied after every bundle layer:
   # a top-level YAML array of loader patch entries (id-targeted config
   # overrides, disables, and insert lists; `!!js` expressions allowed).
   
   # PlanRouter gateway integration:
   # - provider "planrouter" points at the local gateway (OpenAI Chat Completions).
   # - apiKeyEnv reuses the same ROUTER_API_KEY the gateway itself authenticates
   #   with; no secret is copied into this file.
   # - compat flags request the most conservative wire shape (system role,
   #   max_tokens) so heterogeneous free-tier upstreams all accept the request.
   # - agent-default-model makes every new session default to facet-auto.
   - id: agent-default-model
     config:
       provider: planrouter
       model: facet-auto
   - id: llm-pi-ai
     config:
       providers:
         planrouter:
           apiKeyEnv: ROUTER_API_KEY
           api: openai-completions
           baseURL: http://127.0.0.1:8787/v1
           compat:
             supportsDeveloperRole: false
             maxTokensField: max_tokens
           models:
             - id: facet-auto
             - id: facet-cheap
   ```

5. **启动与验证**：

   - Headless：`dsh --profile planrouter "只输出 PLANROUTER_OK 五个字母，不要做任何别的事"`
   - Web UI：`dsh --profile planrouter-web`，启动后打开浏览器访问日志里的一次性 token 地址。

   **验证要点**：
   - 网关侧必须检查：free-tier 渠道的 `auto` 映射不能是占位符 `REPLACE_WITH_A_FREE_MODEL_ID`，建议实测用 `nvidia/nemotron-3.5-lightning:free`；`defaults.max_failover` 建议至少 5。
   - DSH 与网关共用环境变量 `ROUTER_API_KEY`，密钥不落任何文件。
   - 两个 Windows 特有坑：用 `dsh.cmd` 而不是 `dsh`；受限后台进程启动会 `EPERM`，建议先前台验证再按 `Start-Process` 脱离。

---

### 7.6 OpenCode（AI coding CLI）自定义模型配置

**配置文件**：项目根目录 `opencode.json`（或 `~/.config/opencode/opencode.json`）。

**配置示例**（可直接复制到 `providers` 区块）：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "planrouter": {
      "name": "PlanRouter",
      "env": ["ROUTER_API_KEY"],
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": { "baseURL": "http://127.0.0.1:8787/v1" },
      "models": {
        "deepseek-flash": { "name": "deepseek-flash" },
        "facet-auto":     { "name": "facet-auto" }
      }
    }
  }
}
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `name` | 显示名称，在 `/models` 时可见 |
| `env` | 提供凭据的环境变量名；`ROUTER_API_KEY` 与网关 key 共用，不落文件 |
| `package` | 运行时提供包，固定为 `@opencode/ai/providers/openai-compatible` |
| `settings.baseURL` | 网关根路径，带 `/v1`，如 `http://127.0.0.1:8787/v1` |
| `models.<id>` | 可用模型列表，任意 ID 皆可，会在 `/models` 列表中出现 |

**使用方法**：

1. 保存上述 JSON 到 `opencode.json`；
2. 启动 OpenCode：`opencode`；
3. 通过 `/models` 查看可选模型，或直接 `/model deepseek-flash` 切换；
4. 会话内 `/model` 可随时切换。

**注意**：凭据通过 `env` 读取环境变量，切记不要在 `opencode.json` 写明文 Key，改用 `env:ROUTER_API_KEY` 并在操作系统层面设置 `set ROUTER_API_KEY=<value>`（Windows）或 `export ROUTER_API_KEY=<value>`（macOS/Linux）。

---

### 7.7 CodeBuddy Code CLI 自定义模型配置

**配置文件**：`~/.codebuddy/models.json`（用户级）或项目根目录 `.codebuddy/models.json`（项目级，优先级高于用户级）。

**配置示例**（以大模型 `hy3` 为例，可直接复制并修改 id/name/url/apiKey）：

```json
{
  "models": [{
    "id": "hy3",
    "name": "hy3 (PlanRouter)",
    "vendor": "Custom",
    "url": "http://127.0.0.1:8787/v1",
    "apiKey": "<网关密钥>",
    "supportsToolCall": true,
    "supportsImages": false
  }],
  "availableModels": ["hy3"]
}
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `id` | 大模型 ID，在模型选择器中引用 |
| `name` | 模型显示名称 |
| `vendor` | 厂商标识，`Custom` 表示自定义 |
| `url` | Base URL，PlanRouter 网关固定为 `http://127.0.0.1:8787/v1` |
| `apiKey` | 调用密钥，建议通过操作系统环境变量 `ROUTER_API_KEY` 注入，不落文件 |
| `supportsToolCall` | 是否支持函数调用（建议设为 `true`，兼容 Agent 工具） |
| `supportsImages` | 是否支持多模odal（按实际情况填写） |

**使用方法**：

1. 保存上述 JSON 到 `~/.codebuddy/models.json`（或项目根 `.codebuddy/models.json`）；
2. 启动 CodeBuddy Code：`codebuddy`；
3. 通过 `/model hy3` 切换到对应模型，或在交互中使用 `/model` 命令；
4. 也可直接启动：`codebuddy --model hy3`。

**常见错误**：

| 报错 | 解决方法 |
|------|----------|
| `404`：未找到模型 | 检查 `url` 字段是否正确，PlanRouter 网关固定 `http://127.0.0.1:8787/v1` |
| `401`：认证失败 | `apiKey` 错误或未设置环境变量 `ROUTER_API_KEY`，确保已设置且不为空 |
| 模型列表中看不到 | 确认 `availableModels` 数组中包含该模型 ID，且已保存文件 |

---

### 7.8 CodeBuddy（插件/IDE）自定义模型配置

> 此节参见 [CodeBuddy 插件/IDE 配置文档](https://docs.cloudbase.net/ai/ai-tools/codebuddy) 中的「通过配置文件配置」章节。核心步骤与 CodeBuddy Code 一致，配置文件路径为：
> - Windows：`C:\Users\<你的用户名>\.codebuddy\models.json`
> - Mac/Linux：`~/.codebuddy/models.json`
> 格式同上（`id`/`name`/`vendor`/`url`/`apiKey` 三选二即可），但 `availableModels` 建议保留以利模型列表展示。

> **快捷方式**：界面中选择「配置自定义模型」→ 填写 AI 控制台复制的 Base URL / API Key / 模型名，保存后重启 CodeBuddy 面板即可生效。

响应头里应该能看到：

```
X-Routed-Model-Mode: agent
X-Routed-Via: <某个渠道>/<具体模型>
```

`X-Routed-Via` 就是这次实际落到的模型——**你不需要关心它**，但调试时可以用来确认 tool calling 能力。

**自维护页「Agent 模型推荐」卡片**

控制台「自维护」页会实时列出 `facet-agent` 当前能挑到的所有候选模型（含渠道、档位、是否支持 tools、稳定性分）。顶部的蓝色高亮行就是一键复制的 `facet-agent`。

> 旧说明里曾建议「手动指定 deepseek-v4-pro / GPT-5 等具体名字」。现在直接用 `facet-agent` 即可——只有当你想锁定一个特定付费模型时才需要写具体名。

### 7.9 Claude Code（CLI Agent）

Claude Code 本身支持订阅桥，如果你想让它通过 Facet 路由：

1. **方式 A - 本地桥接**（推荐）：保持种子配置里的 `claude-plan` 渠道，密钥填桥的口令；
2. **方式 B - 直接走网关**：修改 Claude Code 的环境变量指向你的 Facet 端点。

方式 B 的用法（环境变量方式）：

```powershell
# PowerShell
$env:CLAUDE_CODE_API_BASE = "http://127.0.0.1:8787/v1"
$env:CLAUDE_CODE_API_KEY = "<ROUTER_API_KEY>"
claude --model facet-agent
```

⚠️ Claude Code 强依赖 tool calling，务必用 `facet-agent`（它自动只挑支持 tools 的模型），不要用 `facet-auto`。

📷 **截图 7-2**｜拍什么：终端运行 Claude Code + 输出显示调用成功｜存为：`claude-code-01-run.png`

### 7.10 Cline / Roo Code（VS Code 插件 Agent）

Cline 和 Roo Code 都是 VS Code 里最火的 Agent 插件，都支持自定义 OpenAI 兼容端点：

**Cline 配置步骤**：

1. 安装 Cline 插件 → 打开设置 → **API Provider** 选择 **Custom OpenAI**
2. 填写参数：

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | `<ROUTER_API_KEY>` |
| Model Name | **`facet-agent`**（网关自动挑支持 tools 的模型） |

3. 点击 **Test Connection** 验证。

**Roo Code 配置步骤**：

1. 安装 Roo Code → Settings → **Custom Modes** 新建一个 mode
2. Provider settings JSON：

```jsonc
{
  "provider": "openai",
  "openAiApiKey": "<ROUTER_API_KEY>",
  "openAiBaseUrl": "http://127.0.0.1:8787/v1",
  "openAiModelId": "facet-agent",
  "openAiStreamingEnabled": true
}
```

⚠️ **关键提示**：直接填 `facet-agent` 即可，网关会自动从所有渠道里挑一个支持 function calling 的模型（免费优先）。只有当你需要锁定某个具体付费模型（比如 DeepSeek v4-pro）时，才写具体名。

📷 **截图 7-3**｜拍什么：Cline/Roo Code 设置页面（端点与模型名清晰可见）｜存为：`cline-01-config.png`

### 7.11 OpenHands / Devika / Devin（开源 DevAgent）

这类 DevAgent 通常需要：

1. 在 `.env` 或配置文件里指定 API 端点
2. 强制用支持工具调用的模型

**OpenHands 示例**（`.env`）：

```bash
LLM_API_KEY=<ROUTER_API_KEY>
LLM_BASE_URL=http://127.0.0.1:8787/v1
LLM_MODEL=facet-agent  # 网关自动挑支持 tools 的免费/付费模型
```

**Devika 示例**（`config.yaml`）：

```yaml
llm:
  api_key: <ROUTER_API_KEY>
  base_url: http://127.0.0.1:8787/v1
  model: deepseek-v4-pro
  function_calling: true   # 一定要开
```

### 7.12 Windsurf / Zed / Cursor（AI 原生编辑器）

这些新一代编辑器都有内置 AI，配置类似 Trae：

**Windsurf**: Settings → AI → Custom Provider
- Endpoint: `http://127.0.0.1:8787/v1`
- API Key: `<ROUTER_API_KEY>`
- Model: `deepseek-flash`

**Zed**: `.zed/settings.json`

```jsonc
{
  "ai_settings": {
    "open_ai_api_base": "http://127.0.0.1:8787/v1",
    "open_ai_api_key": "<ROUTER_API_KEY>",
    "default_model": "deepseek-v4-pro"
  }
}
```

**Cursor**: Settings → General → LLM Provider
- Provider: Custom / OpenAI
- Base URL: `http://127.0.0.1:8787/v1`
- API Key: `<ROUTER_API_KEY>`
- Model: `deepseek-v4-pro`

📷 **截图 7-4**｜拍什么：Cursor 的 AI 配置页｜存为：`cursor-01-config.png`

### 7.13 LangChain / LlamaIndex（Python Agent 框架）

如果你用自己的 Agent 代码，Facet 可以作为后端统一切换上游：

**LangChain 示例**：

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

llm = ChatOpenAI(
    model="facet-cheap",           # 或用具体模型
    openai_api_key="<ROUTER_API_KEY>",
    openai_api_base="http://127.0.0.1:8787/v1",
)

response = llm.invoke([HumanMessage(content="hi")])
print(response.content)
```

**带 Function Calling**：

```python
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """Get weather for a city."""
    return f"Weather in {city}: sunny"

# Agent / tool calling 场景直接用 facet-agent —— 网关只挑支持 tools 的模型
llm_with_tools = ChatOpenAI(
    model="facet-agent",
    openai_api_key="<ROUTER_API_KEY>",
    openai_api_base="http://127.0.0.1:8787/v1",
).bind_tools([get_weather])

response = llm_with_tools.invoke("What's the weather in Beijing?")
# 网关会自动把请求落到一个支持 function calling 的上游
```

**LlamaIndex 示例**：

```python
from llama_index.core.llms import ChatMessage
from llama_index.llms.openai import OpenAI

llm = OpenAI(
    model="facet-auto",
    api_key="<ROUTER_API_KEY>",
    additional_kwargs={"api_base": "http://127.0.0.1:8787/v1"},
)

response = llm.chat([ChatMessage(role="user", content="hi")])
print(response.message.content)
```

### 7.14 LangGraph / AutoGen / CrewAI（多 Agent 框架）

多 Agent 框架对 tool calling 的要求更高。**直接用 `facet-agent`**——网关会自动从所有渠道目录里挑一个 registry 标记为「支持工具调用」的模型（免费优先，已冷却的自动剔除）。只有当 `facet-agent` 返回 tool_call 失败时，才需要退一步填具体模型名。

**LangGraph 示例**（精简版）：

```python
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="facet-agent",           # 网关自动挑工具调用模型；若失败可换成 deepseek-v4-pro
    openai_api_key="<ROUTER_API_KEY>",
    openai_api_base="http://127.0.0.1:8787/v1",
)

agent = create_react_agent(llm, tools=[...])
```

**AutoGen 示例**：

```python
from autogen import ConversableAgent, config_list_from_json

# config_list.json:
# [{"model": "deepseek-v4-pro", "base_url": "http://127.0.0.1:8787/v1", "api_type": "open_ai", "api_key": "<ROUTER_API_KEY>"}]

config_list = config_list_from_json(env_or_file="config_list.json")
assistant = ConversableAgent("assistant", llm_config={"config_list": config_list})
user_proxy = ConversableAgent("user", human_input_mode="NEVER")

user_proxy.initiate_chat(assistant, message="Write a hello world python")
```

### 7.15 故障排查

| 症状 | 可能原因 | 处置 |
|---|---|---|
| 401 Unauthorized | 用了 admin 密钥而不是网关 key；或 `.env` 里 `ROUTER_API_KEY` 没生效 | 换用 `sk-router-...`；或在控制台的「订阅」→「凭据」页检查是否漏了 Gateway API Key |
| 502 / 超时 | 渠道未启用或模型映射错误 | 进控制台「渠道配置」，看对应渠道是否为 enabled，且 auto 映射存在；可用 `GET /v1/models` 看列表 |
| 200 但没内容（只 return reasoning） | 落到了不支持 content 的免费上游 | 把模型名改成支持完整对话的上游（如 `deepseek-flash`） |
| 429 Too Many Requests | 上游免费额度耗尽 | 观察控制台的「自维护」→「定时任务」→「免费对齐」日志，下一轮会自动摘掉失效渠道 |
| Agent 反复报 tool_call 失败 | 模型不支持 function calling | 工具里改填 `facet-agent`（网关只挑支持 tools 的模型）；或明确指定 deepseek-v4-pro / GPT-5 等付费模型 |

---

## 八、附录：端点与协议速查表

| 厂家 | 端点 (按量) | 端点 (套餐) | 协议 |
|---|---|---|---|
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | — | openai |
| OpenRouter | `https://openrouter.ai/api/v1` | — | openai |
| Google AI Studio | `https://generativelanguage.googleapis.com/v1beta/openai` | — | openai |
| LLM7 | `https://api.llm7.io/v1` | — | openai |
| OVHcloud | `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1` | — | openai |
| ModelScope | `https://api-inference.modelscope.cn/v1` | — | openai |
| 火山引擎方舟 | `https://ark.cn-beijing.volces.com/api/v3` | Coding：`https://ark.cn-beijing.volces.com/api/coding/v3`；Agent：`https://ark.cn-beijing.volces.com/api/plan/v3`（OpenAI）/ `https://ark.cn-beijing.volces.com/api/plan`（Anthropic） | openai；Agent 另支持 anthropic |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `https://open.bigmodel.cn/api/coding/paas/v4` | openai |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | openai |
| DeepSeek | `https://api.deepseek.com/v1` | — | openai |
| Kimi Moonshot | `https://api.moonshot.cn/v1` | — | openai |
| Anthropic | `https://api.anthropic.com` | — | anthropic |
| OpenAI | `https://api.openai.com/v1` | — | openai |
| xAI Grok | `https://api.x.ai/v1` | — | openai |
| 小米 MiMo | `https://api.xiaomimimo.com/v1` | `https://token-plan-cn.xiaomimimo.com/v1` | openai |

## 九、控制台观测：免费池与缓存率

### 9.1 免费池：哪些模型真的会被调用

**位置**：控制台 → **免费池**。页面顶部汇总行回答「一共多少、多少能用」：

| 汇总项 | 含义 |
|---|---|
| 渠道数 | 标记为免费的渠道数量 |
| 模型数 | 这些渠道下发现的模型总数（含仅目录候选、失效模型） |
| 可路由 | **真正会进入路由尝试**的模型数，即下面「参与路由」为是的数量 |
| 失效 | 测验失败、暂时打上失效标记的模型数 |
| 排队 / 已测 / 通过 | 维护模型池的测验进度 |
| 已用 / tokens | 近 30 天内至少被真实请求调用过的模型数，以及这些请求上报的 token 总量 |

一个模型显示「**参与路由**」，必须同时满足（与网关实际选路完全一致）：

1. 渠道处于启用状态；
2. 渠道没有被订阅过期阻断；
3. 渠道不在冷却期；
4. 模型没有被标记失效（测验失败后会失效 6 小时，复验通过自动解除）；
5. 模型来自渠道的 `models` 映射，而不是「仅在目录里出现」。

不满足时该行会显示「不参与路由」并给出唯一原因：

| 状态文案 | 含义 |
|---|---|
| 渠道已停用 | 渠道的启用开关被关掉了 |
| 订阅已过期（附具体文案） | 关联订阅到期，过期阻断生效 |
| 测验失败已标记失效，待复验 | 维护测验没通过，暂时从路由中摘除 |
| 渠道冷却中：<上游原因> | 上游限流／额度／错误触发的冷却，时间到自动恢复 |
| 仅目录候选，未映射到渠道 | 上游目录里有、但渠道 `models` 没配，永远不会发请求；加映射或采纳维护建议即可 |

模型行上还有三个容易误读的信息：

- **测验**：通过／失败／排队中／未测。它只证明「这个模型能正常返回」，**不等于**它一定被调用；
- **近 30 天**：调用 N 次 · X tok · 缓存 y% · 最近调用时间。显示「近 30 天未调用」表示从未被真实请求命中，常见原因是优先级靠后、请求总被更靠前的渠道先满足，或刚加入池子还没轮到；
- **缓存率**：按模型统计的缓存命中率，上游没报缓存字段时该模型不显示。

三个常见误会：

- 「测验通过却没被调用」：测验只是准入条件，路由还受优先级、冷却、失效标记、订阅状态影响；
- 「没被调用过 = 不可用」：只有真实请求才算调用，维护测验不计入调用次数；
- 「重启后数据清零」：用量按天落盘到 `usage.json`，重启不清零；面板默认展示 30 天窗口。

### 9.2 缓存命中率怎么看

**位置**：控制台 → **用量**，渠道分布下方新增「渠道缓存命中率（缓存输入 / 总输入，近 N 天）」；免费池的渠道标题和每个模型行也显示缓存率。

**口径**：`cached_tokens / input_tokens`，即上游明确告知「本次输入中有多少 token 命中缓存」占全部输入 token 的比例。Anthropic 的输入按 `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` 计算。

**字段来源**（按响应里 `usage` 的形状识别，不按渠道协议）：

| 上游 | 字段 |
|---|---|
| OpenAI 兼容 | `usage.prompt_tokens_details.cached_tokens`（兼容顶层 `cached_tokens`） |
| DeepSeek | `usage.prompt_cache_hit_tokens` |
| Anthropic | `usage.cache_read_input_tokens` / `cache_creation_input_tokens` |

流式请求同样统计：从 SSE 的 usage 事件累加（OpenAI 是最后一个 chunk，Anthropic 是 `message_start` + `message_delta`）。

**什么时候显示「暂无数据」**：

- 上游完全没有上报缓存字段 → 显示「暂无数据」。这既不是 0%，也不是网关漏统计；
- 有缓存统计但一次都没命中 → 显示 0%；
- 渠道缓存率是该渠道所有请求的汇总；某个模型没报缓存字段时，它的输入仍计入分母、缓存按 0 计，会轻微拉低渠道比率。

**怎么看这个数**：缓存率取决于上游的上下文缓存策略和你的请求是否复用相同前缀，不同厂家之间不能横向比较；同一个渠道短期内波动大，通常是请求前缀变化（例如每轮都改 system prompt）导致的。

为什么之前看不到：旧版用量只按渠道记「请求数／成功／失败／token」，既没有模型维度也没有缓存字段，控制台自然呈现不出来；现在记录下沉到「渠道 → 模型」，两处都能看到。

---

## 更新记录

| 日期 | 变动 |
|---|---|
| 2026-11-11 | 7.4.1 扩为「内置别名」四种：新增 **`facet-free`**（`free` 链，纯免费不碰付费渠道）；说明 `facet-auto` 语义由链序决定（当前默认部署付费优先：火山 Agent/Coding 套餐 → deepseek → `$free` 免费兜底）；配置新增 `openai:free` 路由链；新增 7.5–7.8（DSH / OpenCode / CodeBuddy Code CLI / CodeBuddy 插件），原 7.5–7.11 顺延为 7.9–7.15 |
| 2026-11-10 | 新增第七章「在 Trae / IDE 里使用 Facet」及第八章端点速查；补充容器 host 与常见 401/200 空白问题；**扩充分支**：Claude Code、Cline/Roo Code、OpenHands/Devika、Windsurf/Zed/Cursor、LangChain/LlamaIndex、LangGraph/AutoGen/CrewAI 共 9 个流行框架的配置示例，并强化 Agent tool calling 注意事项 |
| 2026-11-10 | 新增 7.4.1 「三种内置别名」章节，说明 `facet-auto` / `facet-cheap` / **`facet-agent`** 的区别与适用场景；同步把 Claude Code / Cline / Roo Code / LangChain / OpenHands 的示例模型名统一改为 `facet-agent`，并说明响应头 `X-Routed-Model-Mode: agent` 的验证方式 |
| 2026-11-10 | 新增 2.1「密钥为什么填在渠道上」与 2.2「同一账号开通多个套餐怎么配」，讲清「账号 × 套餐 = 一个渠道」与订阅只记账号/套餐/到期；4.1 补火山 Agent 套餐双协议端点（`/api/plan/v3`、`/api/plan`）与专属 Key 说明；附录同步；新增第九章「控制台观测：免费池与缓存率」，解释 `routable` 判定、`skip_reason` 与缓存率口径，消除「测验通过≠被调用」「无缓存字段≠0%」等误会 |
