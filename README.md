# Facet

**Facet 是一个自维护的 LLM 免费额度聚合网关：把你手里各家厂商的免费额度聚合成一个 OpenAI / Anthropic 兼容端点，并每天自己探测、修复、汇报。**

> Facet 最初是一张"模型事实表"。它现在长成了完整的产品：**网关是主体**，事实表退居为配套的数据层（见文末）。

## 为什么需要它

免费额度很散：智谱、火山引擎、阿里百炼、魔搭、NVIDIA NIM、OpenRouter `:free`……每家协议不同、密钥不同、限额规则不同，逐家接入、逐家盯梢的成本远超额度本身的价值。

这尤其是个人和小团队的日常：额度自己申请、agent 自己写、没有平台团队替你
盯渠道。企业级网关（要配数据库、集群、专职运维）在这个量级上是杀鸡用牛刀。

Facet 把它们收敛成三件事：

- **一个端点、一把密钥**——下游 agent 只接 Facet，不必逐家对接；
- **一个控制台**——渠道、路由、请求日志、配额、维护，全在浏览器里；
- **一套自动运维**——探测、冷却、目录修复、维护报告，按周期自动跑。

## 特点

- **适合个人和小团队**——单进程、单配置文件、状态与日志全部落盘为普通
  文件：无数据库、无外部中间件，一个 Python 进程或一个容器就是全部部署，
  一台最小的机器即可自托管。
- **零依赖**——纯 Python 标准库实现，`pip` 装完即用，不拖任何第三方运行时包。
- **零配置可跑**——没有 `router.json` 也能启动：自动种子化免费层模板
  渠道，进控制台填 key 启用就能用。
- **双协议全兼容**——OpenAI 与 Anthropic 两套 API 同端口并存，下游改个
  `base_url` 就完成迁移。
- **品牌模型名**——`facet-auto` / `facet-cheap` / `facet-free` / `facet-agent`
  / `facet-embedding`，语义即路由，下游不感知任何上游模型 id。
- **自维护**——每天自动探测冷却渠道、修复目录漂移、刷新厂家清单，产出
  「已应用 / 建议 / 告警」三桶报告，绝不自动应用未经目录确认的变更。
- **免费优先**——免费池答题验证队列、`$free` 宏、免费渠道轮转；维护调用
  绝不烧付费额度。
- **全程可追溯**——每次选型打印一行 `[route]` 日志：mode / reason / sticky /
  rotate / lb / picked，决策过程完整可复盘。
- **状态可恢复**——冷却、粘滞、负载均衡打分与按天用量全部落盘，重启不丢。

## 功能清单

| 类别 | 功能 |
|---|---|
| API 端点 | OpenAI 兼容：`/v1/chat/completions`、`/v1/completions`、`/v1/responses`、`/v1/embeddings`、`/v1/rerank`、`/v1/audio/speech`、`/v1/audio/transcriptions`、`/v1/audio/translations`；Anthropic 兼容：`/v1/messages`、`/v1/messages/count_tokens`（本地估算，不调上游）；`GET /v1/models` |
| 流式 | SSE 流式透传（chat 与 messages） |
| 路由 | `(protocol, hint) → 渠道链`；品牌名映射；`default` 回退；`$free` 宏；同优先级轮转 |
| 负载均衡 | `order` 链式 / `weighted` 打分（成功率 + 错误 + 延迟 EWMA）；会话粘滞 30 分钟 |
| 容错 | 配额/错误触发冷却；HealthProber 指数退避探活（30s → 300s）；额度恢复自动复活 |
| 自我维护 | 每日目录刷新、自动修复（先备份）、订阅/厂家清单刷新、LLM 顾问意见、三桶报告、维护 API |
| 免费池 | tier=free 渠道捐候选、合成测验验证、复验除名、冷却兜底 |
| 控制台 | 渠道/路由编辑、API Key 与管理员密钥管理、请求日志、维护日志、接入文档、热加载 |
| 认证 | 多成员 API Key、管理员密钥、`env:VAR` 密钥引用（明文不落配置） |
| 运营 | 按天用量统计、订阅到期提醒、通知投递、请求审计日志（`requests.jsonl`）、渠道出站代理 |

## 安装

需要 Python 3.10+（Windows 安装包与 macOS/Linux 一键脚本会自动处理，无需自备）。网关与事实表都是纯标准库实现，**零第三方依赖**。

- Windows：Inno Setup 安装包（含托盘、开机自启），本地 `python tools/build_windows.py` 构建；
- macOS / Linux：见下方一键安装小节（缺失或过旧时自动装 Python：Homebrew/系统包管理器 → 独立 CPython 兜底）；
- 容器：见下文[「Docker 部署」](#docker-部署)。

```bash
pip install facet-models     # PyPI 包名沿用历史名，装出来的命令是 facet
```

或从源码安装：

```bash
git clone https://github.com/hsliuping/Facet && cd Facet
pip install -e .
```

### macOS 一键安装（launchd 服务）

```bash
git clone https://github.com/hsliuping/Facet && cd Facet
tools/macos/install.sh            # venv 装到 ~/.facet，注册开机自启 + 崩溃重启
```

- 装完自动做健康检查并打印控制台地址；服务崩溃后 launchd 自动拉起。
- **无需自备 Python**：缺失或版本过旧时自动安装（Homebrew → 独立 CPython 兜底，
  铺到 `~/.facet/python`，不碰系统环境）。
- 配置与日志在 `~/.facet/`（`data/router.json`、`logs/gateway.err.log`）。
- 自定义：`FACET_HOME`/`FACET_PORT`/`FACET_HOST` 环境变量，或 `install.sh <路径|git URL>` 换安装源。
- 卸载：`tools/macos/uninstall.sh`（数据保留；`--purge` 连 `~/.facet` 一起删）。

### Linux 一键安装（systemd 服务）

```bash
git clone https://github.com/hsliuping/Facet && cd Facet
tools/linux/install.sh            # venv 装到 ~/.facet，注册开机自启 + 崩溃自动重启
```

- 无需自备 Python：缺 3.10+ 或系统仓库版本过旧（如 Ubuntu 20.04 / CentOS 7）时自动安装——先用包管理器，装不上再自动铺独立 CPython 到 `~/.facet/python`，不碰系统环境。
- 装完自动做健康检查并打印控制台地址；服务崩溃后 systemd 自动拉起。
- 配置与数据在 `~/.facet/`（`data/router.json`）；日志：`journalctl -u facet -f`。
- 自定义：`FACET_HOME`/`FACET_PORT`/`FACET_HOST` 环境变量，或 `install.sh <路径|git URL>` 换安装源。
- 卸载：`tools/linux/uninstall.sh`（数据保留；`--purge` 连 `~/.facet` 一起删）。

容器部署见下文[「Docker 部署」](#docker-部署)。

## 快速开始

```bash
facet serve                  # 默认监听 127.0.0.1:8787
```

- **首启引导**：没有 `router.json` 时自动生成种子配置（仅含禁用的免费层模板渠道：NVIDIA NIM / Google AI Studio / Z.AI），需注册 Key 后在控制台启用。
- 浏览器打开 `http://127.0.0.1:8787/` 进入控制台；管理密钥首次启动随机生成，
  只在启动日志里打印一次（打开控制台时填入，之后可在「管理密钥」页换成自己的值）。
- 全新环境会自动弹出「新手配置向导」（右上角「快速开始」可随时重开）：
  设置管理密码 → 选厂家填 Key（自动测试连通，火山/百炼可一并填 AK/SK 供免费额度对齐）
  → 生成调用密钥，一路走完即可路由。
- 也可以跳过向导，在渠道页给模板渠道填入各厂家的 key 并启用，保存后即可路由。

用 curl 验证（`facet-free` 只走免费渠道，同优先级轮转）：

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer <listen.api_key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "facet-free", "messages": [{"role": "user", "content": "你好"}]}'
```

常用检查：

```bash
curl http://127.0.0.1:8787/health        # 健康与渠道冷却状态
curl http://127.0.0.1:8787/stats         # 用量统计
curl http://127.0.0.1:8787/v1/models     # 网关侧模型清单
facet serve --check                      # 校验配置、打印路由链，不启动服务
```

## Docker 部署

```bash
docker compose up -d         # 控制台 http://localhost:8787（ROUTER_PORT 可改）
```

- 首次运行自带一个 mock 假上游做冒烟测试（`ROUTER_SEED=example`）。真实使用
  时：停掉服务 → 删除 `./data/router.json` → 去掉该环境变量，下次启动会
  种子化厂家预置渠道，随后删掉 compose 里的 `mock` 服务即可。
- 配置、状态、日志都落在宿主机 `./data/`（bind mount）。改了
  `./data/router.json` 后 `POST /api/reload` 热加载，或直接在控制台编辑。
- 密钥经环境变量注入：`ROUTER_API_KEY`、`ROUTER_ADMIN_KEY`；未设置
  `ROUTER_ADMIN_KEY` 时，entrypoint 会在首启生成随机管理密钥并打印在容器日志
  （`docker compose logs router`）。火山引擎 IAM
  AK/SK 用 `VOLC_ACCESSKEY` / `VOLC_SECRETKEY`（也可在控制台渠道编辑器里填，
  UI 配置优先）。

## 服务化运行

`facet serve` 的完整参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--config` | `router.json` | 渠道配置文件路径 |
| `--host` / `--port` | `127.0.0.1:8787` | 监听地址；绑非回环地址必须配 `listen.api_key` |
| `--check` | — | 校验配置并打印路由链后退出，不启动服务 |
| `--keys-file` | 配置旁 `keys.json` | 成员 API key 存储 |
| `--state-file` | 配置旁 `router.state.json` | 冷却与负载均衡状态（重启恢复） |
| `--usage-file` | 配置旁 `usage.json` | 按天用量 |
| `--audit-file` | 配置旁 `requests.jsonl` | 请求路由审计日志 |
| `--subscriptions` | 配置旁 `subscriptions.json` | 订阅套餐记录（到期提醒） |
| `--notifications-file` | 配置旁 `notifications.json` | 提醒投递记录 |

## 认证

- **`listen.api_key`**——调用 `/v1/*` 与 `/api/*` 的成员密钥；支持
  `env:VAR` 写法从环境变量读取，明文不落配置。控制台「API Keys」可为每个
  成员签发独立 key。
- **`listen.admin_key`**——管理面密钥；没有内置默认密码：首次启动会随机生成
  并在日志里打印一次。已配置 `api_key` 但未设置 `admin_key` 时管理接口直接
  拒绝（401），`--check` 会提醒；登录控制台后可在「管理密钥」里换成自己的值。

### 升级：管理密钥改为随机生成

本版本移除了内置默认管理密码 `admin`（硬编码凭据，CWE-798）。管理接口的
行为按凭据状态分三种：

| 配置状态 | 管理接口行为 |
|---|---|
| `listen.admin_key` 已设置 | 只认这个密钥 |
| `api_key`、`admin_key` 都未设置 | 控制台开放（默认仅监听 `127.0.0.1`） |
| `api_key` 已设置、`admin_key` 未设置 | **锁定**，管理接口一律返回 401 |

全新安装不受影响：首启自动随机生成密钥。**旧版本升级、且原来一直用默认
`admin` 登录的部署**，按下面步骤补齐密钥（旧配置不会被静默改写）：

1. 先检查状态：

   ```bash
   facet serve --check
   ```

   出现 `WARNING: listen.admin_key is unset while listen.api_key is set`
   即说明管理接口已锁定。
2. 在 `router.json` 的 `listen` 块里显式写入 `admin_key`（想让密钥只走环境
   变量、不落盘，就写 `"admin_key": "env:ROUTER_ADMIN_KEY"`）：

   ```json
   { "listen": { "api_key": "…", "admin_key": "你的管理密钥" } }
   ```

3. 重启 `facet serve` 生效。注意管理接口处于锁定状态时无法调用
   `POST /api/reload` 热加载（它也要鉴权），所以此处只能重启。
4. 重新打开控制台，右上角「管理密钥」会要求填入刚设置的密钥，填入即可正常
   管理；之后可在「管理密钥」页面里再验证旧值、换成自己的值。

Docker 部署同理：

- 已有 `./data/router.json`：停掉容器，在 `compose.yaml` 旁的 `.env` 里设置
  `ROUTER_ADMIN_KEY=<你的密钥>` 再 `docker compose up -d`。此前依赖 compose
  默认值 `admin` 的部署必须补这一步，否则管理接口会锁定（配置文件里是
  `env:` 引用，entrypoint 不会改写已存在的配置）；也可以直接改
  `./data/router.json` 里的 `listen.admin_key`。
- 全新部署（还没有 `./data/router.json`）：不设置 `ROUTER_ADMIN_KEY` 时，
  entrypoint 会生成随机密钥并打印在首启日志里。

以上各场景可直接复制的完整配置片段（明文 / `env:` 引用 / `.env` 文件 /
验证命令）见 [docs/upgrade-keys-examples.md](docs/upgrade-keys-examples.md)。

下游 agent 的完整接入指南（端点、模型命名、错误码、SDK 示例）：
[docs/agent-integration.md](docs/agent-integration.md)（控制台「数据 → 接入文档」页面可直接阅读）。

## 下游 agent 怎么用

OpenAI 兼容端点族（`/v1/chat/completions`、`/v1/responses`、`/v1/embeddings`
等，见上文功能清单）与 Anthropic 兼容端点族（`/v1/messages` 及
`count_tokens`）同端口并存，`GET /v1/models` 列出网关侧的可用模型名。

### 品牌模型名

不必记住任何上游 id——用 Facet 的品牌名，语义即路由：

| 模型名 | 语义 |
|---|---|
| `facet-auto` | 按路由链自动挑渠道 |
| `facet-cheap` | 优先低成本渠道 |
| `facet-free` | 只走免费渠道（`$free` 宏展开的全部 tier=free 渠道，同优先级轮转） |
| `facet-agent` | agent 任务预排序队列 |
| `facet-embedding` | 嵌入模型路由 |

也接受上游原始模型名（如 `deepseek-v3.2`）——命中哪个渠道由路由表决定。

### 会话粘滞

请求带 `X-Session-Id` 头（或请求体 `user` / `metadata.user_id` 字段）即启用
会话亲和：同一会话 30 分钟内粘在同一渠道，避免上下文在不同供应商之间漂移。

## 核心机制

### 路由与选型

- 路由表是 `(protocol, hint) → 渠道链` 的映射；品牌模型名映射为路由提示
  （`facet-free` → `free`），没命中提示路由时回退到该协议的 `default` 路由。
- 渠道链按 `priority` 升序排列，跳过禁用与冷却中的渠道；`$free` 宏展开为
  **所有启用的 tier=free 渠道**（按优先级、名称排序），不用逐个列名。
- 每次选型打印一行 `[route]` 日志，整个决策过程可追溯：

```
[route] openai:free model='facet-free' mode=auto reason=internal-model-name sticky=no rotate=1/4 lb=order skipped=- picked=free-tier->nvidia/nemotron-3.5-lightning:free chain=...
```

### 负载均衡与容错

- `lb_strategy`：`order`（链式顺序）或 `weighted`（按成功率、错误次数、
  延迟 EWMA 打分排序）；同优先级段内轮转（round-robin）。
- 配额耗尽 / 上游报错触发冷却；后台 HealthProber 对冷却渠道按指数退避
  （30s → 300s）探活，额度恢复自动复活。
- 有会话粘滞时优先粘住的渠道，粘滞过期或渠道冷却后才重新选型。

### 自我维护

启动约 2 分钟后先跑一次，之后每 `defaults.maintenance_hours`（默认 24；
`0` 关闭）跑一次。一次运行：刷新渠道目录 → 自动应用目录可确认的修复
（先备份配置）→ 刷新订阅套餐模型与厂家注册表 → 请维护 LLM 写顾问意见 →
落一份报告。每条发现只落进三个桶之一：

| 桶 | 含义 | 谁来处理 |
|---|---|---|
| `applied` | 目录可确认（上游下架模型、`auto` 重指向、套餐/厂家模型清单刷新） | 无人——已自动应用，且有配置备份 |
| `suggestions` | 值得做但目录无法确认（上游新模型、反复配额冷却、LLM 意见——仅供参考，绝不自动应用） | 你——控制台一键采纳 |
| `alerts` | 密钥失效、目录为空、抓取失败 | 你，线下处理 |

报告保留在内存（最近 20 份）并追加到配置旁的 `maintenance.jsonl`，重启不丢。
同样的数据也是普通 HTTP：

```bash
GET  /api/maintenance              # 当前状态：开关、周期、模型池、上次运行
GET  /api/maintenance/logs/50      # 运行历史，最新在前
POST /api/maintenance/run          # 立即手动跑一次
POST /api/maintenance/test-models  # 重新测验免费池，重建队列
POST /api/maintenance/apply        # 采纳建议（{"channel", "models"}）
```

### 免费池

`defaults.maintenance_model` 留空或设为 `auto` 时，所有维护 LLM 调用都跑在
一个「已验证免费模型队列」上，而不是钉死某个 id：

- 只有显式标记 `tier: "free"` 的渠道捐出候选——探测绝不烧付费额度。
- 候选要过一道测验：合成的文档页 + 已知标准答案，全对的进队列。队列持久化
  在配置旁的 `maintenance_models.json`，每次探测都重新复验；真干活时掉链子
  的模型当场除名。探测有冷却兜底，全灭的免费池不会把维护变成探测风暴。

`defaults.maintenance_model` 写显式 id（逗号/空白分隔）则绕过免费池，原样使用。

### 维护调用走直达，不走路由

维护模型的 id 是上游 id，所以按上游的方式寻址：直接打到第一个在自己的
models 映射里暴露它的启用渠道（tier-`free` 优先，其余按配置顺序），单轮
非流式。路由表从不参与；维护不许依赖路由成员。只有没有任何渠道暴露的
品牌 id（如 `facet-auto`）才回退为经网关自身的 loopback 调用。

### 渠道出站代理

渠道可带 `proxy: "http://..."`（明文 URL 或密钥引用）。该渠道名下的所有
上游请求——转发流量与维护直达调用——都走它；不填则直连。

## 配置

一切围绕一份 `router.json`（示例见 [data/router.example.json](data/router.example.json)）：

- **channels**——上游渠道：`name`、`protocol`（`openai` / `anthropic`）、
  `base_url`、`api_key`（支持密钥引用）、`models`（模型映射，`"auto"` 指向
  目录拉取）、`priority`、`tier`（`free` / `paid`）、`proxy`。
- **routes**——`(protocol, hint) → 渠道链`；渠道名支持 `$free` 宏。
- **defaults**——负载均衡策略、探活节奏、粘滞 TTL、维护周期与维护模型等。
- **listen**——`host`、`port`、`api_key`、`admin_key`。

厂家接入元数据在内置 `src/facet/gateway/providers.json` + 用户覆盖层
（配置目录下的 `providers.json`，按 id 整体替换内置条目）。给内置表提 PR
或本地放一条覆盖条目皆可。维护每天刷新厂家模型清单，变更带备份持久化进
覆盖文件。

## 控制台

与网关同端口（`http://127.0.0.1:8787/`），浏览器即用：渠道与路由管理、
API Key 与管理员密钥、请求日志、维护日志、接入文档。所有 `/api/*` 接口与
页面同源，可用同一把管理员密钥脚本化。

## 事实表（配套数据层）

`registry/model-registry.json`——单文件 < 1 MB，覆盖 217 个 provider、
约 2,400 个模型的能力、限制与价格，每周从
[models.dev](https://models.dev) + [OpenRouter](https://openrouter.ai/api/v1/models)
同步（GitHub Action 自动开 PR），质量门与字段标准见
[schema/model-registry.schema.json](schema/model-registry.schema.json)。

事实表既可以独立使用（读表 → 自己的规则 → 自己决策）：

```python
import facet

t = facet.load()                    # 包内捆绑快照
facet.resolve(t, "glm-5.3")         # -> 规范键，容忍拼写漂移
facet.find(t, tool_call=True, min_context=200_000, free=True, limit=10)
```

```bash
facet "glm-5.3"                     # 解析并显示一个模型的事实
facet --free --tools --limit 10     # 按条件筛选免费模型
```

也可以作为网关的配套：网关用它做名字解析与 `--free` 新手指引。网关的**路由
只看维护层观测到的渠道状态**，绝不依赖表的新鲜度——两层之间只有这一座桥。

## 开发

```bash
python -m pytest tests/ -q          # 全量回归（单元/接口测试）
python -m pytest tests/web/test_smoke.py -v --tb=short  # Web UI 冒烟测试（Playwright，需自行启动 gateway）
python tools/sync_models_dev.py     # models.dev -> 事实表（每周或按需）
python tools/validate_registry.py   # 事实表质量门（体积、null、结构、数量）
python tools/verify_claims.py gpt-5 --dry-run   # 实测验证模型能力声明
```

## 非目标

事实表不做路由、不管 API key、不采集遥测、不做逐请求排序、不聚合跑分。
网关层做路由，但只依据维护层观测到的渠道状态，从不依赖表的新鲜度。

## 治理

MIT 许可。贡献须接受 [docs/CLA.md](docs/CLA.md) 中的协议，流程见
[docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)。
