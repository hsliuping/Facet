# Facet

**Facet 回答你的应用里最常见的一个问题："这个任务该用哪个模型？"**

它不是路由器，而是一张**模型事实表**——单个 JSON 文件，覆盖 217 个
provider、约 2,400 个模型的能力、限制与价格——外加保持表格新鲜的同步
工具和一个示例消费方。你的项目读表、套自己的业务规则、自己决定用谁。

```
models.dev（上游事实源）
        │  每周同步（tools/sync_models_dev.py）
        ▼
registry/model-registry.json   ← 核心交付物：单文件 < 1 MB，零依赖
        │
        ├── 你的服务（拉取 JSON → 过滤 → 决策）
        └── examples/pick_model.py（参考消费方 / demo）
```

## 为什么是表而不是路由器？

- 路由器编码的是**别人的**策略；你的业务规则属于你自己。
- 路由器是依赖，JSON 文件是数据。可以拉取、可以 vendor、也可以只抄需要的字段。
- 事实每周变（价格、窗口、新模型），策略每天变。我们只维护每周变的部分。

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
| `quota_tier` | `free_forever` / `free_daily` / `paid` | 人工标注，同步绝不猜测 |
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
    "https://raw.githubusercontent.com/<org>/Facet/main/registry/model-registry.json"))

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

## 怎么维护

```bash
python tools/sync_models_dev.py            # models.dev -> 表（每周或按需）
python tools/validate_registry.py          # 质量门（体积、null、结构、数量）
python -m unittest discover -s tests       # 26 个单元测试
```

GitHub Action（`sync.yml`）每周自动刷新表格并开 PR。人工标注
（`quota_tier`、`quality_hint`）在刷新时自动保留。数据来源：
[models.dev](https://models.dev)（不含 benchmark 分数——那是评价不是事实；
如有需要，通过 `quality_hint` 自行补充）。

## 非目标

不做路由、不管 API key、不采集遥测、不做逐请求排序服务、不聚合跑分。
需要这些，请基于这张表自建。

## 治理

MIT 许可。贡献须接受 [CLA.md](CLA.md) 中的协议，流程见
[CONTRIBUTING.md](CONTRIBUTING.md)。English docs: [README.md](README.md).
