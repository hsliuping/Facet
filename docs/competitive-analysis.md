# Facet 竞品分析：OmniRoute

> 调研日期：2026-11-23。方法：GitHub API 抓取仓库元数据、README（13 万字符全文）、核心文档（RESILIENCE_GUIDE、FREE_TIERS、FREE_CATALOG_EVIDENCE）与源码树（32,462 个文件）关键词检索。
> 局限：未通读全部 2,309 个源文件；结论基于 README/文档的明确表述 + 代码结构检索交叉验证。

仓库：<https://github.com/diegosouzapw/OmniRoute>

---

## 一、OmniRoute 概况

| 项 | 数据 |
|---|---|
| 起步 | 2026-02-13 创建，约 9 个半月 |
| 规模 | 89.6k star、13.9k fork、900+ 贡献者，持续活跃提交 |
| 技术栈 | TypeScript / Node，MIT 许可；npm / Docker / Electron 桌面 / 手机 Termux / PWA |
| 接入面 | 394 个厂家、官方目录标 150+ 免费、34 个「免费池」去重口径，宣称月免费 token 约 16.2 亿（诚实数学：池去重、双周复核、CI 门禁防口径漂移） |
| 核心功能 | `auto` 虚拟模型 + 10 个变体（auto/coding、auto/cheap、auto/subscription…）、22 种路由策略（旗舰 Combo）、16 因子实时打分、三层熔断自愈、上下文接力、RTK+Caveman token 压缩（省 15–95%）、80+ CLI 命令、MCP/A2A、远程模式 |
| 隐私 | 本地运行、凭据 AES-256-GCM 加密、本地 SQLite 审计、遥测默认关 |
| 运营 | Kimi 赞助（Founding Friend）、联盟链接、Radar 付费目录覆盖层、67 种语言、多平台社区 |
| 测试 | 60,000+ 条静态测试声明、8,400+ 测试文件 |

## 二、思路重合点（比想象中严重）

同一个论点：本地一个入口、自带各家账号、免费额度用满、配额感知自动切换、接 Claude Code / Codex 等 CLI 工具。

连细节都撞：

- `auto` 命名 ↔ facet-auto；`auto/cheap` ↔ facet-cheap；
- 「免费失败绝不悄悄转计费」：它的 `auto/subscription` fail-closed ↔ Facet 免费池不转付费；
- 「诚实口径」（池去重、定期复核）↔ Facet 账本诚实。

## 三、Facet 仍站得住的差异化（README 逐词验证）

| 差异 | 证据 |
|---|---|
| **自维护闭环**：体检 → 建议 → 一键采纳 → 改前备份 → 可回滚 → 重指后真实验证 | 其 self-healing 仅指路由层熔断换渠道；README 中 `maintenance` 0 命中、`repoint` 0 命中、backup/rollback 各 1 次（开发脚注） |
| **免费额度对账官方数字** | `ledger` / `reconcile` 0 命中；有配额感知路由，无「与厂家官方数字对账、连自身探测消耗都记账」 |
| **尝试时间线**（容灾看得见） | `timeline` / `attempt` 0 命中 |
| **轻量** | 它是大型 Node 应用 + SQLite + Electron；Facet 纯标准库 Python 零依赖 |
| **Windows 双击安装** | 它的 Windows 用户需 npm / Docker / 自行 build Electron；Facet Setup.exe 面向非开发者 |
| **国内免费生态原生亲和** | 它 394 厂家为全球口径、社区全英文；火山引擎 / 百炼 / 魔搭 / GLM 计划号这条线 Facet 是主场 |

## 四、OmniRoute 领先、值得学的

1. **Combo 体系**：22 策略 + 语义化 auto 变体（auto/fast、auto/thrifty、auto/offline…），远比 facet-auto/cheap/free 三档丰富；
2. **token 压缩**（省 15–95%）：Facet 完全没有的独立卖点；
3. **熔断分层**（OAuth 8 败 / API-key 12 败阈值、HALF-OPEN 探测、success-decay 恢复）比 Facet 的 cooldown 精细；
4. **生态运营**：赞助、联盟、多语言、贡献者体系——9 个月冲到 89k star 的真正原因，功能只是门票。

## 五、维护机制深挖（核心结论）

**一句话：请求级路由做得很深，但「目录/映射」级维护基本没有——它把自维护外置成了 GitHub 上的众包流程。**

### 1. 目录层（「哪些模型免费」）：静态清单 + 众包治理

- 免费目录是**打包进发布版本的静态 TS 配置**（`open-sse/config/freeModelCatalog.data.ts`），不是运行时从厂家拉取；
- 维护靠三件套：
  - CI 证据门禁（`config/quality/FREE_CATALOG_EVIDENCE.md`）：每条「不限量免费」行必须有证据记录（来源 URL、复核日期、审核人、限制说明），债务只许缩减不许增长；
  - 社区 PR 人工复核，按厂家分批审计；
  - 约两周一轮再审计（README 里成串的 honesty correction 都是 PR 改的）；
- 厂家关停免费层时（ollama-cloud 2026-11-09 移除、longcat 降级为一次性注册额度、chutes/phind/kluster 下线）——**全部是社区发现 → PR 改文件 → 随下个版本发布**；
- 版本间想要新鲜目录：等发版，或订 Radar（签名目录覆盖层，GET-only，面向支持者）。

### 2. 运行时层（挂了怎么办）：被动熔断，明确声明「不推断上游」

- 熔断三层（DEGRADED → OPEN → HALF_OPEN），OAuth 8 败 / API-key 12 败触发；恢复靠请求路径惰性检查——文档原话「**No background timer needed**」；
- 后台定时任务只有 60 秒一次的 Codex/Claude 额度数字同步（刷新配额显示，不是健康探测）；
- 模型死了 = 熔断冷却 + combo 换下一个；冷却是可续期配额窗口（`testStatus: "unavailable"`），永不永久禁用；
- 文档原话「**OmniRoute does not discover or infer upstream policy**」——不拉厂家 /models、不做目录 diff；
- 源码检索（`open-sse/` 2,309 个文件）：`scheduler` / `cron` / `interval` / `revalidat` 均 0 命中；`tokenRefresh`（OAuth 会话保活）做得重，但那是凭证维护，不是模型映射维护。

### 3. 与 Facet 自维护的对照

| 环节 | OmniRoute | Facet |
|---|---|---|
| 谁发现「上游改了」 | 全球社区人力 + CI 证据门禁（仓库侧） | 网关自己定期体检拉厂家 /models |
| 新模型出现 | 等社区 PR + 发版（或 Radar） | 目录 diff 出 `added`，控制台一键采纳 |
| 模型名漂移/失效 | 熔断冷却后**该映射一直坏着**，直到 PR 修复或手动改 | 自动重指到同类可用模型 + 发真实请求验证，修不过就报警 |
| 改配置前备份 | 无（README 无此概念） | 自动备份快照，一键恢复 |
| 记账口径 | 静态目录宣称值 + 配额感知路由 | 运行时台账，连自身探测消耗都记账，与厂家官方数字对账 |

### 4. 定性结论

- 它强在**路由决策**（策略、压缩、熔断），弱在**配置自愈与账本**；
- 「单机产品内闭环」vs「GitHub 众包 + 付费目录」是两种打法：900 贡献者盯着时它的修复速度不差，但**单个用户自己装一份，就吃不到这个速度**——上游改个模型名，本地那份要等发版或自己动手；
- Facet 的自维护 + 对账 + 可回滚是**生存级差异**，不是锦上添花。

## 六、行动建议

1. **宣传口径主动切割**：「路由器管『转发到哪个模型』，Facet 管『配置自己会修、账永远是真的』」——避免与 OmniRoute 硬比路由策略数量；
2. **视频分镜 6/7（自维护 + 信任机制）加重**：从「可选卖点」升级为全片信任高潮；
3. **借鉴它的 CI 证据门禁**：免费额度台账可标注来源与复核日期（「此额度来自厂家 11 月活动页，11-20 复核」），把「诚实」做成可验证的产品能力；
4. **认清真正对手优势是社区运营速度**，不是功能深度——Facet 的增长抓手应放在国内生态亲和 + 零门槛安装 + 产品内闭环，而非功能数量军备竞赛。
