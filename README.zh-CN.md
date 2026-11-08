# Facet

[English](https://github.com/hsliuping/Facet/blob/main/README.md) | **简体中文**

**Facet 是一个自维护的 LLM 免费额度聚合网关——名字里的"事实表"是它的数据层。**

**Facet 回答两个问题："这个任务该用哪个模型？"（事实表）与"这个模型现在能免费跑吗？"（自维护网关）。**

`facet serve` 启动一个本地 OpenAI 兼容网关：聚合主流厂商的免费额度，并自己保持
渠道目录新鲜——探测、修复、报告，给你的 LLM 基础设施配一个 AI SRE。与之一同交付
的还有名字里的**事实表**：单个 JSON 文件，覆盖 217 个 provider、约 2,400 个模型的
能力、限制与价格（`pip install facet-models`，零依赖）。你的项目读表、套自己的
业务规则、自己决定用谁。

```
models.dev（主源，217 个 provider）   OpenRouter /api/v1/models（辅源）
        │                                        │
        └──────────── 每周同步 ──────────────────┘
                        │  tools/sync_models_dev.py
                        ▼
registry/model-registry.json   ← 核心交付物：单文件 < 1 MB，零依赖
        │
        ├── 你的服务（拉取 JSON → 过滤 → 决策）
        └── examples/pick_model.py（参考消费方 / demo）
```

## 一个包，两层

包内同时交付两层；命令只有一族。

| 层 | 负责 | 落在 |
|---|---|---|
| **事实层**（数据） | 模型级能力、价格、`quota_tier`、别名 | `registry/model-registry.json` → 内置快照 |
| **网关层**（运行时） | 渠道、路由、免费池、每日自我维护 | `src/planrouter/` |

两层之间的桥：

- **名字解析**——`planrouter/facts.py` 把知名名称映射到规范键；只读、纯
  stdlib、快照缺失时退化为"不可用"。网关的**路由**依然只看维护层观测到的
  渠道数据，绝不依赖表格的新鲜度。
- **`quota_tier`**——人工标注（`registry/manual-overrides.json`）为新手指引
  （`facet --free`）供料；零密钥首启已内置：`facet serve` 在没有
  `router.json` 时自动生成种子配置（匿名 Pollinations + 禁用的免费层模板
  渠道）并直接启动。维护层的探测结果只能**建议**更新该文件，同步流程绝不
  自动填写。

```
facet "glm-5.3"            # 事实层：解析并显示一个模型
facet --free --tools ...   # 事实层：按条件筛选
facet serve                # 网关层：启动自维护代理
planrouter                 # `facet serve` 的过渡别名（已弃用）
```

## 为什么事实层是一张表？

- 网关的路由策略是我们自己的、可见的；你的选型规则仍然属于你自己。JSON
  文件是数据——可以拉取、可以 vendor、也可以只抄需要的字段。
- 事实每周变（价格、窗口、新模型），策略每天变。我们只维护每周变的部分，
  网关自己适应每天变的部分。
- 同一张表还驱动名字解析——`gpt-4o` 今天实际能解析到哪些免费层。`quota_tier`
  是两层之间的连接键。

## 这张表

`registry/model-registry.json` — 以 `provider/model_id` 为键，每个模型身份
一条记录，跨 provider 去重（聚合渠道副本让位于官方厂商；平票时选信息更全的）。

| 字段 | 含义 | 说明 |
|---|---|---|
| `provider` | 端点归属（如 `openai`、`volcengine`） | 唯一必填字段 |
| `aliases` | 同一模型的其他写法，含聚合渠道命名 | 名称匹配忽略大小写、点、连字符：`glm-5.3` = `glm5.3` = `GLM5.3` |
| `context_window`、`max_output` | token 限制 | |
| `tool_call`、`reasoning`、`structured_output`、`attachment` | 能力布尔位 | |
| `modalities` | `{"input": [...], "output": [...]}` | |
| `open_weights` | 权重是否公开 | |
| `cost` | `{"input_per_mtok", "output_per_mtok", "cache_read_per_mtok"}` | **统一 USD / 百万 token** |
| `quota_tier` | `free_forever` / `free_daily` / `paid` | 人工标注，同步绝不猜测（主要免费层厂商已预置） |
| `release_date`、`last_updated` | `YYYY-MM-DD` 或 `YYYY-MM` | 新鲜度提示 |
| `family` | 模型家族 | |
| `quality_hint` | 1-5 主观质量先验 | 唯一主观字段，人工维护 |

### 硬规则

1. **单文件 < 1 MB。**
2. **缺席 ≡ null（未知）。** 未知事实直接省略键（文件里不写 `null`，更不填
   默认值）。消费方用 `.get()`，把缺席当未知处理。
3. **只放事实**——`quality_hint` 除外，它被明确标为主观、可忽略。
4. **一律 USD / MTok。** 其他币种、单位永远不进表。
5. **字段只增不改。** 同一 `schema_version` 内语义永不变更；破坏性变更递增
   版本号。消费方必须忽略未知字段。

完整字段标准：[schema/model-registry.schema.json](schema/model-registry.schema.json)。

## 怎么用

从本仓库（raw 地址 / CDN）按你喜欢的频率拉取 JSON，然后用你自己的规则决策：

```python
import json, urllib.request

table = json.load(urllib.request.urlopen(
    "https://raw.githubusercontent.com/hsliuping/Facet/main/registry/model-registry.json"))

candidates = [
    (key, rec) for key, rec in table["models"].items()
    if (rec.get("context_window") or 0) >= 200_000          # 缺席 == 未知
    and rec.get("tool_call") is True
    and "image" in (rec.get("modalities") or {}).get("input", [])
    and (rec.get("cost") or {}).get("output_per_mtok", 1e9) <= 2.0
]
```

或者直接跑 demo（纯标准库）：

```bash
python examples/pick_model.py --min-context 200000 --tools --image-input --limit 5
python examples/pick_model.py claude-sonnet-4.5      # 按名称/别名解析单个模型
```

## 安装（pip）

同一套 读表 → 解析 → 过滤 逻辑已打包发布，**零运行时依赖**（纯标准库）：

```bash
pip install facet-models
```

```python
import facet

t = facet.load()                      # 包内捆绑快照（包版本号 = 表日期）
t = facet.load("path/to/table.json")  # 显式本地路径
t = facet.load(refresh=True)          # 拉取最新表（见下）

facet.resolve(t, "glm-5.3")           # -> "alibaba-cn/glm-5.3"
facet.find(t, tool_call=True, min_context=200_000, image_input=True,
           max_output_price=2.0, provider="zhipuai", limit=10)
```

- wheel 里捆绑一份表的快照，**包版本号就是快照日期**（`2026.9.0` = 2026 年
  9 月的表）。装完即用、离线可用；要新鲜度就升级包或 `load(refresh=True)`。
- refresh 的 URL 优先级：`url=` 参数 → `FACET_TABLE_URL` 环境变量 → 内置
  默认地址（本仓库 main 分支的原始表）。拉取失败直接报错，绝不静默回退
  到旧事实。
- 直接拉 JSON 的用法（上文）继续支持——包只是便利层，不是锁定。

CLI：`facet "glm-5.3"`、`facet --tools --min-context 200000 --limit 5`。

### 集成契约：三套名字，各归其位

模型在你的全栈里有三套名字，别混用：

| 名字 | 例子 | 用在哪 |
|---|---|---|
| facet key | `zhipuai/glm-5.3` | 调度层：选型、日志、配额、审计的贯穿 ID |
| 线名（wire name） | `rec.model_id`（请求体 `model` 字段） | 接入层 |
| 网关名 | 你 one-api/new-api 部署里 `/v1/models` 列出的名字 | 启动时自动推导，不落配置 |

表记录就是**交接物**：调度层把（key、provider、model_id、aliases）递给接入
层；接入层按 `provider` 选适配器和凭据，`model_id` 进请求体。

经网关的渠道**不需要任何映射配置**——网关自己会告诉你它叫什么
（`GET /v1/models`），用已有的 `resolve()` 在启动时推导即可（它天生容忍
拼写漂移）：

```python
names = [m["id"] for m in get(f"{GW_URL}/v1/models").json()["data"]]
wire = {name: facet.resolve(t, name) for name in names}   # 网关名 -> facet key
```

让这套方案成立的约定只有一条：网关侧保留厂商原始名（one-api/new-api
加渠道时默认就是原始名）。某个名字 `resolve` 返回 `None`，说明是网关
管理员自创的叫法——在网关侧改回原名即可，不是加配置的理由。

## 怎么维护

```bash
python tools/sync_models_dev.py            # models.dev -> 表（每周或按需）
python tools/validate_registry.py          # 质量门（体积、null、结构、数量）
python -m unittest discover -s tests       # 单元测试
```

GitHub Action（`sync.yml`）每周自动刷新表格并开 PR。人工标注
（`quota_tier`、`quality_hint`）在刷新时自动保留。数据来源：
[models.dev](https://models.dev)（主源，provider 覆盖最广）+
[OpenRouter](https://openrouter.ai/api/v1/models)（辅源，补能力标注缺口，
尤其是 `structured_output`）。能力字段可跨渠道借用（只补缺、永不覆盖）；
价格永不跨渠道借用，因为各渠道定价确实不同。不含 benchmark 分数——那是
评价不是事实；如有需要，通过 `quality_hint` 自行补充。

## 实测验证（可选）

表里的能力字段全部是上游的**声明**，不是实测。
`tools/verify_claims.py` 真实调用模型，验证声明是否成立——工具调用、
结构化输出、推理痕迹、图片输入四项探针。结果写入
`reports/verify-results.json`（match / mismatch / discovery / inconclusive），
**绝不回写主表**：表保持"上游声明聚合"的单一语义，矛盾怎么处理由人来定。

```bash
python tools/verify_claims.py gpt-5 --dry-run        # 只打印计划，不发网
python tools/verify_claims.py gpt-5                  # 按名称/别名解析
python tools/verify_claims.py --provider zhipuai     # 便宜的模型优先
python tools/verify_claims.py --base-url http://localhost:1234/v1 --api-key ... local-model
```

表内每个模型身份只保留一条记录（去重赢家），但同一个模型往往由多家厂商
提供——且各渠道能力表现可能不同。用**厂商限定名**钉死要测的渠道：
`volcengine/glm-5.3`、`zhipuai/glm-5.3`、`alibaba-cn/glm-5.3` 测的是同一
模型在不同厂商端点上的实际表现（声明字段取自表记录，报告中经 `table_key`
溯源）。裸名测的是赢家渠道。

API key 自备（环境变量——`OPENAI_API_KEY`、`ZHIPU_API_KEY`、`ARK_API_KEY`
等，或 `--api-key`），key 永不落报告。一线厂商端点已内置，其余用
`--base-url --protocol` 指定。每个探针只花几百 token；`--max-models`
（默认 20）为 `--provider` 扫描兜底。发现 mismatch？带着报告开 issue，
或走 `manual-overrides.json` 修正对应行。

## 网关层

网关层（`src/planrouter/`）是包的运行时一半：`facet serve` 聚合渠道——你自己
配置的上游厂商端点——在其间路由 OpenAI / Anthropic 兼容流量，并每天自我
维护。控制台与 `/api/*` 与网关同端口。跑起来之前值得知道的五件事：

### 自我维护

启动约 2 分钟后先跑一次，之后每 `defaults.maintenance_hours`（默认 24；`0`
关闭）跑一次。一次运行：刷新渠道目录 → 自动应用目录可确认的修复（先备份
配置）→ 刷新订阅套餐模型与厂家注册表 → 请维护 LLM 写顾问意见 → 落一份
报告。每条发现只落进三个桶之一：

| 桶 | 含义 | 谁来处理 |
|---|---|---|
| `applied` | 目录可确认（上游下架模型、`auto` 重指向、套餐/厂家模型清单刷新） | 无人——已自动应用，且有配置备份 |
| `suggestions` | 值得做但目录无法确认（上游新模型、反复配额冷却、LLM 意见——仅供参考，绝不自动应用） | 你——控制台「自动运维」卡片一键采纳 |
| `alerts` | 密钥失效、目录为空、抓取失败 | 你，线下处理 |

报告保留在内存（最近 20 份）并追加到 `config.json` 旁的
`maintenance.jsonl`，重启不丢。控制台的「维护日志」标签页读的就是这两处；
同样的数据也是普通 HTTP：

```bash
GET  /api/maintenance              # 当前状态：开关、周期、模型池、上次运行
GET  /api/maintenance/logs/50      # 运行历史，最新在前（默认 50 条）
POST /api/maintenance/run          # 立即手动跑一次
POST /api/maintenance/test-models  # 重新测验免费池，重建队列
POST /api/maintenance/apply        # 采纳建议（{"channel", "models"}）
```

### 免费池

`defaults.maintenance_model` 留空或设为 `auto` 时，所有维护 LLM 调用都跑在
一个「已验证免费模型队列」上，而不是钉死某个 id：

- 只有显式标记 `tier: "free"` 的渠道捐出候选——探测绝不烧付费额度。
- 混合目录（OpenRouter 式，带 `:free` 后缀）只捐 `:free` 子集；一个目录里
  没有任何 `:free` id，说明整个渠道都免费，全部模型都是候选。首次目录拉取
  之前，用渠道自身的 models 映射顶替。
- 候选要过一道测验：合成的文档页 + 已知标准答案，全对的进队列。队列持久化
  在 `config.json` 旁的 `maintenance_models.json`，每次探测都重新复验；真
  干活时掉链子的模型当场除名。探测有冷却兜底，全灭的免费池不会把维护变成
  探测风暴。

`defaults.maintenance_model` 写显式 id（逗号/空白分隔）则绕过免费池，原样
使用。

### 维护调用走直达，不走路由

维护模型的 id 是上游 id，所以按上游的方式寻址：直接打到第一个在自己的
models 映射里暴露它的启用渠道（tier-`free` 优先，其余按配置顺序），单轮
非流式——Anthropic 渠道用 `x-api-key` + `anthropic-version: 2023-06-01`，
OpenAI 兼容渠道用 `Bearer`，该渠道的 `proxy` 照常生效。路由表从不参与；
维护不许依赖路由成员。只有没有任何渠道暴露的品牌 id（如 `facet-auto`）才
回退为经网关自身的 loopback 调用——普通路由，正常故障转移。

### 渠道出站代理

渠道可带 `proxy: "http://..."`（明文 URL 或密钥引用）。该渠道名下的所有
上游请求——转发流量与维护直达调用——都走它；不填则直连。

### 新厂家接入

两层，两份注册表：

| 层 | 注册表 | 怎么扩展 |
|---|---|---|
| 网关 | 内置 `src/planrouter/providers.json` + 用户覆盖层 `<config 目录>/providers.json` | 给内置表提 PR，或本地放一条覆盖条目 |
| 事实层 | `registry/model-registry.json` | 厂商随 `tools/sync_models_dev.py` 自动进来；人工事实（如 `quota_tier`）走 `registry/manual-overrides.json` |

网关厂家条目需要 `id`（`[a-z0-9-]+`）、`name` 和至少一个模式——`coding` /
`agent` / `payg`——每个模式带 `label`、`protocol`（`openai` / `anthropic`）、
`base_url`，可选 `tier`（`free` / `paid`）、`docs_url`、`models_url`、
`models`。覆盖条目按 id 整体替换内置条目。维护每天刷新模型清单：带
`docs_url` 的模式先走 LLM 抽取（确定性清单行规则兜底），`models_url` 当
JSON 目录读——变更带备份持久化进你的覆盖文件。事实层这边档位永远是人工：
`quota_tier` 绝不自动填写。

## 非目标

表不做路由、不管 API key、不采集遥测、不做逐请求排序、不聚合跑分——需要
这些，请基于这张表自建。网关层做路由，但只依据维护层观测到的渠道状态
（见[网关层](#网关层)），从不依赖表的新鲜度。

## 治理

MIT 许可。贡献须接受 [CLA.md](CLA.md) 中的协议，流程见
[CONTRIBUTING.md](CONTRIBUTING.md)。
