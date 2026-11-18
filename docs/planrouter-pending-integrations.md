# 待接厂家清单（Pending Integrations）

> 更新日期：2026-11-18。本清单以 2026-11 中旬的官网/文档实测调研为准，作为后续接入的开发目标。
> 免费额度政策变化快，接入前请按 `docs/厂家调研手册.md` 复核一遍时效性。
> 来源标注的链接为调研时的官方文档/公告出处。

## 总览

| 优先级 | 厂家 | 额度性质 | 额度查询 API | 结论 |
| --- | --- | --- | --- | --- |
| P0 | 硅基流动 SiliconFlow | 长期免费模型 + 一次性赠金 | **无**（`/v1/user/info` 已于 2026-08-14 退役，替代接口未出） | **已接入**（2026-11-17，走既有 0.5h 额度对齐管线） |
| P1 | 中国电信（息壤/teleai/TokenHub） | 一次性大额试用包 | 无 | **已接入**（2026-11-18，固定额度包账本模式） |
| P1 | 国家超算互联网 SCNet | 一次性 1000 万 + 活动 | 无（仅现金余额） | **已接入**（2026-11-18，固定额度包账本模式） |
| P2 | 移动 MoMA（九天） | 一次性 2500 万 | 无 | 视 P1 两个的落地情况再定 |
| 不接 | 讯飞星火 xfyun | Lite 免费（QPS 2） | 无 | QPS 太低 + ToS 明确禁止 + 模型下线频繁 |
| 不接 | 美团 LongCat | 已转付费制（2026-06-30 起） | 无 | 循环免费额度已取消，只剩一次性赠送 |
| 不接 | 联通 元景 MaaS | 仅试用（5 次/分钟） | 无 | 限流进不了主链路，偏政企 |
| 不接 | 其他超算中心（无锡/天津/广州/深圳/上海） | 送 HPC 机时，非 token | — | 没有大模型 API 免费额度，不是一个赛道 |

## 统一接入模式说明

除硅基流动外，本清单所有厂家的免费额度均为**一次性试用包**且**无公开的剩余额度查询 API**，统一采用「固定额度包账本」模式：

1. **账本初值** = 领取面额（用户在配置里登记领取到的 token 数）。
2. **扣减** = 累计每次响应的 `usage` 字段（prompt_tokens + completion_tokens）。
3. **自愈** = 沿用既有错误码兜底（402/403 额度耗尽 → 冷却/禁用；429 → 退避）。
4. 一次性额度用完即止，不设重置窗口；UI 上标注「一次性额度包，用完为止」。

落地形态（2026-11-18）：

- 配置字段：渠道级 `free_quota`（整渠道共用一个包）与**每模型** `model_quota`（`{模型: {tokens, period:"total", expires?}}`）。额度包优先于渠道级与注册表基线；按暴露名匹配，其次 wire id。
- 过期：`expires` 接受 `YYYY-MM-DD`（含当天，存 23:59:59）或完整 ISO 时间；`free_quota_status` 到期即 `exhausted`（账本仍有余额也停止路由），输出 `expires` / `expired`；维护页 skip_reason 为「一次性额度包已过期，已停止路由」。
- UI：渠道编辑器「一次性额度包（每模型，可选）」登记模型 / 面额 / 到期日；免费页显示「已过期」徽标、有效期与来源（每模型额度包 / 渠道配置 / 注册表基线）。
- 自愈：额度耗尽或过期后该模型单独冷却，同渠道兄弟模型继续服务；只有账号级余额/密钥错误才整渠道冻结。

## P0 硅基流动 SiliconFlow ✅ 已接入（2026-11-17）

- **接入结果**：免费清单为策略级（所有账号相同），已落地为无凭证 CLI `facet-siliconflow-quota --output json`，接入既有 0.5h 额度对齐管线（sweep + `$free` 对齐）。余额用量卡**未接入**：厂商 `GET /v1/user/info` 已于 2026-08-14 退役（HTTP 410，code 20092），替代接口未发布，渠道卡不提供余额展示。渠道模板已进 seed.py（`tier: "free"`，`auto → XingChenAGI/Xing4.0-29B`，环境变量 `SILICONFLOW_API_KEY`，默认不启用）。
- **最终免费清单（3 个，2026-11-18 官方定价页复核仍免费）**：`XingChenAGI/Xing4.0-29B`（256K、原生工具调用）、`THUDM/GLM-Z1-9B-0414`、`tencent/Hunyuan-MT-7B`。调研时第三方快照列出的 `Qwen/Qwen3-8B` 已转付费（Qwen3.5-9B ¥0.5/¥4.0），剔除；PaddleOCR-VL（OCR 专用）与 Embedding/语音模型不服务 chat completions，排除。
- **实现要点**：免费模型真 0 元计费（不扣赠金），故 CLI 输出大哨兵值 `remaining = NO_CAP_TOKENS = 1_000_000_000`——同时满足 sweep 的 10,000 reserve 过滤、`free_quota_status` 预算外推、UI 显示「充足」不误告警；`credential_envs` 为空（无需管理面 AK/SK）。
- **免费清单维护机制**：官方定价页是 HTML 无 JSON API，清单内置于 CLI（`FREE_MODELS` 元组），清单变动随版本更新；后续可加价格页快照或社区上报。
- **调研留存**：`$free` 链路新增常驻候选渠道；显示名映射在 Web UI（app.js）已预留；限速为账户级（RPM 1000–10000 / TPM 5 万–500 万，随账户等级浮动）。
- **注册 / ToS**：国内站 +86 手机号 + 实名（实名后才能用全部免费模型），国际站不面向大陆用户；新用户 ¥14 赠金（用完即止）。ToS 禁止转售及「使平台服务惠及自身以外的第三方」——个人自用定位下与魔搭/百炼风险等级一致；若将来做代理商变现需重新评估。
- 来源：https://docs.siliconflow.com/en/api-reference/userinfo/get-user-info 、https://cloud-rd.siliconflow.cn/pricing 、https://docs.siliconflow.com/cn/faqs/misc_finance

## P1 中国电信（天翼云星辰 TokenHub / 息壤 / teleAI 天翼 AI 开放平台）✅ 已接入（2026-11-18）

- **接入结果**：厂家注册为 `providers.json` 的 `teleai`（`payg` 模式，OpenAI 兼容 `https://ai.ctaigw.cn/v1`，无 `free_quota` 基线、无额度查询 CLI）；seed 模板 `teleai-xing`（`tier: "free"`，`auto → Xing4.0-29B`，`model_quota` 预置 1 亿 tokens 面额，环境变量 `CTYUN_APP_KEY`，默认不启用）。TokenHub / 息壤 子平台按需在控制台另建渠道，用同一「一次性额度包」区块按模型登记面额。
- **免费额度（均一次性）**：
  - TokenHub：所有试用模型各赠 50 万 tokens、14 天有效、同模型限领一次。
  - 息壤：DeepSeek 系每模型 2500 万 tokens（约 2 周）；Qwen 部分模型 100 万。
  - teleAI：`Xing4.0-29B` 免费试用 1 亿 tokens（5 QPS、512K 上下文、支持工具调用）。
  - 星辰 MaaS 智能体平台：体验版 0 元/年，**每日 700 资源点循环**（资源点非 token，仅限平台内智能体，暂不作为接入目标）。
- **额度查询**：无公开 API，仅控制台「用量查询」网页。
- **端点**：OpenAI 兼容 `https://ai.ctaigw.cn/v1/chat/completions`，Bearer APP Key（控制台建服务组生成）；编程 Plan 另有 `/coding/v1`。
- **注册**：个人可注册天翼云/teleAI 账号，需实名（teleAI 企业认证优先）。
- **ToS**：未查到明确禁止转售/池化条款。
- **接入要点**：多子平台（TokenHub/息壤/teleAI）额度独立，按子平台分别建渠道或一个厂家多个 channel；模型清单以各平台试用页为准。
- 来源：https://www.ctyun.cn/document/11061839/11097250 、https://www.ctyun.cn/document/11094224/11094241 、https://www.teleai.com.cn/product/Xing4_29B

## P1 国家超算互联网 SCNet ✅ 已接入（2026-11-18）

- **接入结果**：厂家注册为 `providers.json` 的 `scnet`（`payg` 模式，OpenAI 兼容 `https://api.scnet.cn/api/llm/v1`，Anthropic 兼容 `…/api/llm/anthropic`，无 `free_quota` 基线、无额度查询 CLI）；seed 模板 `scnet`（`tier: "free"`，`auto → DeepSeek-R1-Distill-Qwen-7B`，`model_quota` 预置 1000 万 tokens 面额，环境变量 `SCNET_API_KEY`，默认不启用）。

- **免费额度（一次性）**：新用户注册实名（身份证 + 人脸，免费，个人可注册、无需单位挂靠）领 1000 万 tokens + 200 卡时 + 500G 存储；核心节点体验再领 1000 万；反馈最高再送 5000 万；另有阶段性活动（OpenClaw 1000 万/2 周等）。付费 Token Plan 每月重置、订户有「夜间免费模型」（循环，属付费权益，暂不作为接入目标）。
- **额度查询**：无剩余额度 API；公开 OpenAPI「用户资源」返回 `accountBalance`（现金余额，非 token）。
- **端点**：OpenAI 兼容 `https://api.scnet.cn/api/llm/v1`，Anthropic 兼容 `…/api/llm/anthropic`，Bearer Key（控制台「模型服务→API Key」创建）。
- **模型池**：千问、DeepSeek-V4、Kimi、GLM-5、MiniMax 等；支持 OpenAI/Anthropic/Responses 三协议，可接 OpenClaw/Cursor 等；tool calling 文档未查到，接入时实测。
- **限流**：未查到，接入时实测。
- **ToS**：未查到禁止转售/池化条款。
- **接入要点**：三家中端点能力最完整（Anthropic 协议可直接复用现有 anthropic-format 通道逻辑）；额度面额大且叠加活动多，账本初值需允许用户手动改。
- 来源：https://www.scnet.cn/home/news/154849.html 、https://www.scnet.cn/help/docs/mainsite/mass/tutorial/quickstart.html

## P2 移动 MoMA（中国移动九天）

- **免费额度（一次性）**：新实名用户领 2500 万 tokens；Mobile Claw 客户端活动每月 1000 万×2、每日签到 100 万×2（限两月）。无长期循环免费额度；另售 1.5 元起 Token Plan。
- **额度查询**：无公开 API，控制台可看消耗明细。
- **端点**：OpenAI 兼容 `https://moma.cmecloud.cn/v1`，Bearer API Key（官方未见公开文档页，端点引自第三方攻略，接入前需官网复核）。
- **模型池**：300+ 模型（九天、DeepSeek、GLM、Kimi 等），多数支持函数调用。
- **限流**：未公布，官方提示高峰期可能报错。
- **ToS**：未查到明确禁止转售/池化条款；生态分销需签合作协议。
- **接入要点**：排 P2 的原因——一次性 2500 万低于电信/SCNet 的可叠加总额，且文档完备度最差（端点无官方文档页）。等 P1 两家验证完「固定额度包账本」模式后再接，边际成本低。
- 来源：https://www.10086.cn/aboutus/news/groupnews/index_detail_55572.html

## 不接清单（避免重复调研）

| 厂家 | 不接原因（截至 2026-11-17） |
| --- | --- |
| 讯飞星火 xfyun | 免费模型 QPS 2 进不了主链路；无额度查询接口；ToS《用户使用须知》明确禁止向第三方转售/共享；旧模型持续下线（spark-x2.5 于 2026-11-24 下线）。若后续 X2.6 有大额循环活动可重评。 |
| 美团 LongCat | 2026-06-30 起转付费制，循环免费额度已取消（此前「每日免费额度」说法已过时）；仅剩新用户实名送 1000 万 tokens 一次性；无额度查询 API；chat/completions 未支持 tools。 |
| 联通 元景 MaaS | 试用账号限 5 次/分钟；无明确赠送 token 数；套餐购买引导"联系客户经理"，偏政企。 |
| 其他超算中心（无锡/天津/广州/深圳/上海） | 无大模型 API 免费额度，送的是 HPC 机时（如天河 4000 核时）或网页问答体验；鹏城云脑机时主要面向科研共享。 |

## 文档同步要求（按《厂家接入指南》第 8 节）

- 每接入一家：`docs/api-key-guide.md` 补「获取 Key」指引（控制台会渲染）；有套餐的厂家加「套餐使用说明」子段；`厂家模式对照表.md`、`厂家模式官网核实表.md` 补调研结论；本清单中该厂家条目改为「已接入」并注明接入版本。
- 一次性额度包的厂家，在渠道卡与订阅页标注「一次性额度，用完为止」，不进每日/周期重置逻辑。
