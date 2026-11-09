# DSH 接入 PlanRouter：patch 配置样例

> **接入对象**：@deepseek-ai/dsh 0.2.2-rc.1（DeepSeek Harness，2026-11-05 发布）
> **我方产品**：PlanRouter（本地免费额度聚合网关，http://127.0.0.1:8787）
> **数据截至**：2026-11-09 · **方法**：本机实测（headless e2e + Web UI 全链路验证）
> **姊妹篇**：定位与调研见 [planrouter-superagent-integration.md](planrouter-superagent-integration.md)

---

## 1. 这份文档解决什么问题

调研文档已经定调：DSH 是底座 runtime，super_agent 是装在它之上的插件应用；PlanRouter 接 DSH **零代码改动**。本文是那篇调研「层面 1（立即可做）」的落地样例，只回答一件事——

**怎么用一份 patch 配置，让 DSH 的所有会话默认走本地 PlanRouter 网关。**

全链路一览：

```
DSH（agent 客户端）
  └─ cordis.patch.yml：provider "planrouter"
       └─ POST http://127.0.0.1:8787/v1/chat/completions
            └─ PlanRouter auto 路由：codex-plan → glm-plan → free-tier → …
                 （429/5xx 自动冷却并故障转移到下一个健康渠道）
```

## 2. 前置条件

| 项 | 要求 | 实测环境 |
|---|---|---|
| Node | DSH engines 标注 ≥24.9.0；Node 22.18.0 **实测可用**（仅警告不阻断） | v22.18.0 |
| npm | 能访问 registry.npmjs.org（镜像未同步该包） | npm 10+ |
| PlanRouter | 网关已启动且加载了 `listen.api_key` | http://127.0.0.1:8787 |
| 环境变量 | `ROUTER_API_KEY` 已设置，DSH 与网关共用 | Windows User 级 |

网关侧还有两处必须先检查的配置（详见第 6 节）：free-tier 渠道的 `auto` 映射不能是模板占位符；`defaults.max_failover` 建议至少 5。

## 3. 安装 DSH

```bash
npm install -g @deepseek-ai/dsh@latest --registry=https://registry.npmjs.org
```

两个参数都不能省，都是实测踩过的坑：

1. **必须带 `@latest` dist-tag**。截至 2026-11-09，该包所有已发布版本都是 prerelease（rc/alpha），npm 的 semver 范围（`@*`、`@^0.2.0`）按规范不匹配 prerelease，裸装会报 `ETARGET: No matching version found for @deepseek-ai/dsh@*`。
2. **必须指官方 registry**。npmmirror 等国内镜像当时未同步该包。

安装完 `dsh --version` 确认（实测 0.2.2-rc.1）。

## 4. 创建 profile

DSH 的配置分三层：bundle 自带默认 → 模板生成的 `cordis.yml` → 用户的 `cordis.patch.yml` 覆盖层。**我们不改前两层，只写 patch 层**——这是 DSH 设计给用户覆盖配置的正式入口，模板升级时不会被冲掉。

建两个 profile，一份跑命令行 agent（headless），一份跑浏览器 Web UI：

```bash
dsh --profile planrouter     --from-default-profile web
dsh --profile planrouter-web --from-default-profile web
```

生成在 `~/.dsh/profiles/<name>/`，各含 `package.json`、`cordis.yml`、`cordis.patch.yml`。

### 4.1 headless profile：换 bundle

`~/.dsh/profiles/planrouter/package.json` 的 bundles 改为：

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

`dsh-headless` bundle 会禁用 credentials / authorization / hmr / deepseek-account 等需要交互的插件，适合脚本化任务；`apiKeyEnv` 走进程环境变量，不受影响。Web UI profile 保持模板的 `@deepseek-ai/dsh-web-app` 不动。

### 4.2 patch 层：完整样例

两个 profile 的 `cordis.patch.yml` 内容相同（实测可用原样照抄）：

```yaml
# Your patch layer for this dsh profile, applied after every bundle layer:
# a top-level YAML array of loader patch entries (id-targeted config
# overrides, disables, and insert lists; `!!js` expressions allowed).
#
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

只有两个 patch 条目，`id` 指向要覆盖的插件：

- **`agent-default-model`**：两个 `config` 字段（provider + model）决定每个新会话的默认模型；
- **`llm-pi-ai`**：DSH 的 provider 注册表，`providers` 下每新增一个 key 就是一个自定义模型源。

### 4.3 逐字段解释

| 字段 | 值 | 作用 |
|---|---|---|
| `agent-default-model.provider` / `.model` | `planrouter` / `facet-auto` | 新会话默认走 PlanRouter 的 auto 路由链（free 额度优先） |
| `providers.<name>.apiKeyEnv` | `ROUTER_API_KEY` | 从进程环境变量读 key——**密钥不落任何文件**，且与网关自身的鉴权密钥共用同一变量，改一处即全局生效 |
| `providers.<name>.api` | `openai-completions` | OpenAI Chat Completions 协议（对应网关 `/v1/chat/completions`） |
| `providers.<name>.baseURL` | `http://127.0.0.1:8787/v1` | 网关地址，带 `/v1` |
| `compat.supportsDeveloperRole` | `false` | 不发 OpenAI 新版 `developer` 角色，改发传统 `system`——免费异构上游兼容性更好 |
| `compat.maxTokensField` | `max_tokens` | 发老字段 `max_tokens` 而不是 `max_completion_tokens` |
| `providers.<name>.models[].id` | `facet-auto` / `facet-cheap` | 声明该 provider 下可用的模型名，在模型选择器里可见；也可继续加 |

两个 `compat` 开关合起来就是「**最保守的请求形状**」，专为异构 OpenAI 兼容网关（免费渠道聚合）设计——它没法保证每个上游都跟上了 OpenAI 最新协议。如果你的上游全是一线厂商 API，可以不配。

## 5. 启动与验证

### 5.1 headless（命令行）

```bash
dsh --profile planrouter "只输出 PLANROUTER_OK 五个字母，不要做任何别的事"
```

注意参数顺序：**自定义 profile 启动时直接跟任务文本**，不要再写 `headless`——`headless`/`web` 本身是内置 profile 名，重复指定会报 `select a profile only once`，或把任务文本当成多余的 app 参数报 `too many arguments`。

### 5.2 Web UI（浏览器）

```powershell
# 前台直接跑（Windows PowerShell，推荐先这样验证）
dsh --profile planrouter-web

# 验证通过后可后台脱离运行
Start-Process -FilePath "dsh.cmd" -ArgumentList "--profile","planrouter-web" -WindowStyle Hidden
```

同样不带位置参数。DSH 启动后会自动打开浏览器，地址带一次性访问 token（形如 `http://127.0.0.1:3080/?token=…`）。直接访问不带 token 会 401——那是访问令牌保护，不是故障，从启动日志里复制完整地址即可。

两个 Windows 特有坑：

1. **必须用 `dsh.cmd` 不能用 `dsh`**：npm 全局安装的 `dsh` 无扩展名文件是 sh 脚本，`Start-Process` 直接调会报「%1 不是有效的 Win32 应用程序」。
2. **受限后台 shell 里启动会 EPERM**：DSH 启动时要写 `~/.dsh` 下的文件，从沙箱/受限后台进程启动报 `EPERM: operation not permitted, open cordis.yml`。先在前台验证，再按上面 `Start-Process` 的方式脱离。

### 5.3 实测结果（2026-11-09）

| 验证项 | 结果 |
|---|---|
| headless e2e | 输出 `PLANROUTER_OK`，任务完成 |
| 审计日志（requests.jsonl） | 记录完整故障转移链：codex-plan 502（冷却 30s）→ glm-plan 401（冷却 30s）→ **free-tier 200 OK**（nvidia/nemotron-3.5-lightning:free，6652 tokens） |
| Web UI 模型探测 | 直接列出 facet-auto / facet-cheap（依赖网关 `GET /v1/models` 返回 `{"data": [...]}` 格式，PlanRouter 已实现） |
| Web UI 会话 | 正常走通，响应带 `X-Routed-Via` 等路由头可核对渠道 |

线 A 的冷却映射与 failover 逻辑在真实 Agent 客户端下工作正常。

## 6. 网关侧配套清单

DSH 这边一份 patch 就够了，但网关侧有几处必须对齐，否则请求会 503。以下改动都在本地的 `router.json`（模板见仓库 `router.example.json`）：

| 检查项 | 位置 | 说明 |
|---|---|---|
| free-tier 的 `auto` 映射 | 该渠道 `models` 对象 | 模板占位符 `REPLACE_WITH_A_FREE_MODEL_ID` 必须换成真实免费模型 id（实测用 `nvidia/nemotron-3.5-lightning:free`，1M 上下文），否则上游 400 |
| `max_failover` | `defaults` | 默认 3 太浅：顶层渠道挂掉时请求要能穿透到健康渠道，实测改到 5 后 e2e 通过 |
| `ROUTER_API_KEY` | 环境变量（Windows 建议设 User 级） | DSH 与网关共用；`[Environment]::SetEnvironmentVariable($n, $v, "User")` 设置后**新开的 shell 才继承** |
| 网关鉴权 | `listen.api_key` | 一旦网关加载了 key，**所有端点（含 /health）都要求 Bearer**；没加载 key 的裸奔状态才不鉴权。手工排查时记得带 `Authorization: Bearer <ROUTER_API_KEY>` |

## 7. 踩坑清单（速查）

| 症状 | 原因 | 解法 |
|---|---|---|
| `ETARGET: No matching version found for @deepseek-ai/dsh@*` | 全部版本是 prerelease，semver 范围不匹配 | 装 `@deepseek-ai/dsh@latest`（dist-tag 不受 prerelease 排除规则影响） |
| 镜像源 404 / 无此包 | npmmirror 未同步 | 加 `--registry=https://registry.npmjs.org` |
| `too many arguments. Expected 0 arguments but got 2` | 自定义 profile 启动带了多余位置参数（如 `web`/`headless`） | `headless`/`web` 是内置 profile 名；自定义 profile 直接 `dsh --profile <name> [task…]` |
| `select a profile only once` | profile 被指定了两次（`--profile` 与位置参数混用） | 一次只给一个 profile 来源 |
| `%1 不是有效的 Win32 应用程序` | `Start-Process` 调了 sh 脚本形态的 `dsh` | 用 `dsh.cmd` wrapper |
| `EPERM: operation not permitted, open cordis.yml` | 受限后台进程写 `~/.dsh` 被拒 | 前台启动，或 `Start-Process` 从前台脱离 |
| 503 all channels failed | 网关侧：auto 映射是占位符 / failover 太浅 | 见第 6 节前两项 |
| `invalid gateway key` | 网关加载 key 后全端点强制鉴权（预期行为） | 请求带 `Authorization: Bearer <ROUTER_API_KEY>` |

## 8. 与 super_agent 的关系

见 [planrouter-superagent-integration.md](planrouter-superagent-integration.md)：DSH 是底座 runtime，super_agent 是装在它之上的仓库运维应用——部署 super_agent 的第一步就是 `npm install -g @deepseek-ai/dsh`。本文接好底座后，super_agent 的 Custom model API 配置与本文同源：模型层面同样走 `facet-auto` / `facet-cheap`，Router 用 cheap 链做分派、Worker 用 default 链干活的理想分配届时再实测（调研文档 P0 项）。
