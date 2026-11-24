# 主流模型跑分汇总与解读（设计稿 v0）

状态：V0 数据底座已实施；V1 自动来源（Vals Terminal-Bench 4.0，开发期 sync + 运行时自动刷新）已实施（见 §9）
日期：2026-11-18
关联：[planrouter-model-status-and-scoring.md](planrouter-model-status-and-scoring.md)（稳定性评分，不是本文）、[planrouter-competitive-analysis.md](planrouter-competitive-analysis.md) §7.2

## 1. 要解决的问题

用户不缺模型列表（厂商官网、models.dev、发布会 PPT 都有），缺的是**判断**：
每个月都有厂商宣称"我们又更强了"，但没人告诉用户"对你这个活，它到底强不强"。

Facet 已有的三层能力里：

| 层 | 回答什么 | 现状 |
| --- | --- | --- |
| 管道层 | 给定模型名，渠道怎么找、挂了怎么换、额度怎么管 | 已完成 |
| 可靠性层 | 这个模型在这个渠道稳不稳 | 已完成（成功率评分） |
| 适配层 | 这个模型适不适合我的业务 | **空缺，本文要补的第一块** |

本文只做适配层的**基础部分**：把厂商参数与公开跑分整理成可对比、可追溯的数据，并给出解读。

**不做的事**（留给后续阶段）：
- 不做自建评测台，不跑自己的 prompt 套件。
- 不做长尾/小模型，只覆盖主流旗舰。
- 不产出跨榜单一总分。

理由：自建评测的边际成本靠免费池才能摊平，那是用户量起来之后的事；当前先做数据底座，同时把后续自建评测要用的身份归一体系先跑通。

## 2. 设计原则

1. **可信优先于全面。** 宁可某模型显示"暂无公开数据"，也不用推测值填充。
2. **每条数据可追溯。** 缺 `source_url` 或 `date` 的记录在校验阶段直接拒绝。
3. **不造黑箱总分。** 一旦自己加权算一个"综合分"，就等于给了厂商一个新的吹牛靶子，也毁掉了"独立"这个唯一卖点。综合分只用外部权威指数（并标注来源），或者干脆只给分项。
4. **厂商自报与独立来源分开标。** 这是"不能让他们吹牛"的机制基础。
5. **口径不可比就不比。** 榜单名相同不代表可比（见 §6）。

## 3. 数据模型

新增三个数据文件，都不写进 `model-registry.json`（后者 2671 条、绝大多数无跑分，混在一起会拖慢加载且难维护）。

> 源文件放在 `registry/`（`registry/benchmarks.json`、`registry/model-scores.json`、`registry/model-coverage.json`），
> 由 `tools/build_package.py` 在构建时拷贝进 `src/facet/data/`（该目录被 .gitignore 忽略，是打包快照）。
>
> **运行时叠加层**：网关启动后，`BenchmarkRefreshRunner` 定时从公开榜单（默认每周）抓取最新分数，写入用户配置目录的 `model-scores.override.json`，由 `benchmarks.py` 叠加在打包快照之上——新分数无需重装即生效，且只覆盖对应 `(bench, harness)` 条目，不冻结其它来源的打包数据（详见 §9）。

### 3.1 `registry/benchmarks.json` → `src/facet/data/benchmarks.json` —— 榜单词典

一次写清、全站复用，是"解读"的底料。

```json
{
  "schema": 1,
  "benches": {
    "gpqa_diamond": {
      "name": "GPQA Diamond",
      "category": "reasoning",
      "measures": "研究生级物理/化学/生物选择题，抗搜索",
      "scale": "百分制准确率",
      "higher_is_better": true,
      "saturation": "2026 年旗舰集中在 80-92，仍有区分度",
      "settings": ["no_tools", "with_tools"],
      "caveat": "带工具的分数与不带工具不可直接比较；厂商常只报带工具那一个",
      "homepage": "https://arxiv.org/abs/2311.12022"
    }
  }
}
```

字段：`name / category / measures / scale / higher_is_better / saturation / settings / caveat / homepage`。
`saturation` 是必需项——MMLU 这类已经饱和的榜必须写明"已无区分度"，否则会误导用户。

### 3.2 `registry/model-scores.json` → `src/facet/data/model-scores.json` —— 每模型分数

键是**模型身份**，不是渠道路由。

```json
{
  "schema": 1,
  "vendor_fallback": {
    "glm": "zai",
    "deepseek": "deepseek",
    "doubao": "bytedance",
    "kimi": "moonshotai",
    "qwen": "alibaba"
  },
  "models": {
    "openai/gpt-5.6": {
      "display_name": "GPT-5.6",
      "vendor": "openai",
      "tier": "flagship",
      "release_date": "2026-09-18",
      "scores": [
        {
          "bench": "gpqa_diamond",
          "value": 88.4,
          "unit": "pct",
          "setting": "no_tools",
          "harness": "vendor",
          "source_type": "vendor",
          "source_name": "OpenAI model card",
          "source_url": "https://openai.com/index/gpt-5-6/",
          "date": "2026-09-18"
        }
      ],
      "profile": "",
      "notes": []
    }
  }
}
```

`source_type` 三档：`independent`（独立第三方榜单）、`vendor`（厂商自报）、`aggregator`（媒体/聚合站转述，仅作线索，不参与对比）。

`profile` 是人工撰写的模型画像（强在哪、弱在哪），v0 允许留空。

### 3.3 覆盖清单：`registry/model-coverage.json` → `src/facet/data/model-coverage.json`

```json
{
  "schema": 1,
  "groups": [
    {
      "vendor": "openai",
      "display": "OpenAI",
      "models": [
        { "id": "openai/gpt-5.6", "label": "GPT-5.6", "tier": "flagship" },
        { "id": "openai/gpt-5.5-pro", "label": "GPT-5.5 Pro", "tier": "flagship" }
      ]
    }
  ]
}
```

这份清单同时是"面板要展示哪些行"的唯一来源——面板不遍历 2671 个模型。

### 3.4 身份键的坑（已在注册表中确认）

设计上必须区分两件事：

- **模型身份**（`openai/gpt-5.6`、`zai/glm-5.3`）——跑分挂在身份上。
- **渠道路由**（`alibaba-cn/glm-5.3`、`mistral/zai-glm-5-3`）——同一身份的不同供货方。

注册表实际分布证明**不能靠前缀推断身份**：

| 模型 | 注册表现状 | 结论 |
| --- | --- | --- |
| GLM-5.3 完整版 | `zhipuai/` 下没有；只在 `alibaba-cn/glm-5.3`、`mistral/zai-glm-5-3`、`venice/...` 等转售方 | 需 `vendor_fallback` 表指定归属 `zai` |
| DeepSeek-V4-Pro | `deepseek/` 下没有；在 `alibaba-cn/deepseek-v4-pro`、`volcengine/deepseek-v4-pro-ga-260813` | 同上 |
| Kimi K3 | `moonshotai/` 只有 k3.1-pro；`alibaba-cn/kimi-k3` 存在 | 以第一方为准，转售方归并 |

因此 `model-scores.json` 的键由 `family + 版本` 决定，`vendor_fallback` 负责把 family 映射到真正的归属厂商。展示层再反查注册表，列出"这条身份在哪些渠道可调用"。

### 3.5 国产覆盖缺口（需要决策）

核实结果：`baidu/` 0 条、`hunyuan/`/`tencent/` 0 条。ERNIE 与混元**不在注册表**中（混元仅以 `siliconflow-cn/hunyuan-a13b` 转售形式出现）。火山有 `volcengine/doubao-seed-1-8-251228` 等。

若目标用户以国内为主，百度和腾讯是绕不开的。两条路：
- (a) 先不管，等 registry 补数据源；
- (b) 在 `model-coverage.json` 里允许"注册表外"的条目，跑分能录但标"当前无可用渠道"。

建议 (b)：跑分是内容，渠道是能力，两者不该强绑定。

## 4. v0 覆盖清单（约 30 个模型）

每家取「当前代旗舰 + 一个轻量/专用变体 + 少量前代参照」，不做全变体。

| 厂商 | 模型（注册表键） |
| --- | --- |
| OpenAI | `openai/gpt-5.6`、`openai/gpt-5.5-pro`、`openai/gpt-5.3-codex-spark`、`openai/gpt-5-mini` |
| Anthropic | `anthropic/claude-opus-5-5`、`anthropic/claude-sonnet-5-5`、`anthropic/claude-haiku-5-5` |
| Google | `google/gemini-4-argon`、`google/gemini-4-carbon`、`google/gemini-3.1-pro-preview`、`google/gemini-3.5-flash` |
| xAI | `xai/grok-4.5`、`xai/grok-4.20-0309-reasoning` |
| DeepSeek | `alibaba-cn/deepseek-v4-pro`（身份 `deepseek/deepseek-v4-pro`）、`volcengine/deepseek-v4-flash-ga-260731` |
| 阿里 | `alibaba-cn/qwen3.8-max`、`alibaba-cn/qwen3.7-plus`、`alibaba-cn/qwen3-coder-plus` |
| 智谱 | `alibaba-cn/glm-5.3`（身份 `zai/glm-5.3`）、`zhipuai/glm-5.3-flash` |
| 月之暗面 | `moonshotai/kimi-k3.1-pro` |
| MiniMax | `minimax/minimax-m3`、`minimax/minimax-m3.1-flash` |
| 阶跃 | `stepfun/step-5-preview` |
| 字节 | `volcengine/doubao-seed-1-8-251228`（+ 确认是否有 doubao-seed-2-0 第一方条目） |
| 百度/腾讯 | 待定（见 §3.5） |

## 5. 数据来源与分级

| 级别 | 来源 | 用途 | 抓取难度 |
| --- | --- | --- | --- |
| A 独立 | Artificial Analysis（综合指数 + 速度 + 价格）、LMArena（Elo）、LiveBench、HF Open LLM Leaderboard、SWE-bench 官方榜、Terminal-Bench | 参与对比与排序 | 中，部分有 API/数据集 |
| A 独立（中文） | OpenCompass（上海 AI Lab）、C-Eval、CMMLU、SuperCLUE | 中文能力对比 | 中 |
| B 厂商自报 | model card、技术报告、发布会 | 补覆盖，标 `vendor` | 低（但 PDF 表格需人工） |
| C 转述 | 媒体文章、聚合对比站 | 仅作线索，不展示为分数 | 低 |

v0 策略：**A 级为主、B 级补充**。厂商数据能覆盖很多 A 级没有的模型，但必须可区分、可关闭。

## 6. 可比性规则（"不能让他们吹牛"的机制）

同名字段往往不可比，原因有三类：

1. **版本不同**：MMLU vs MMLU-Pro、SWE-bench vs SWE-bench Verified、GPQA vs GPQA Diamond。
2. **条件不同**：是否允许工具、CoT / non-CoT、pass@1 vs pass@k、是否 majority voting。
3. **评测框架不同**：厂商自研 harness vs lm-evaluation-harness vs 第三方复跑。

因此引入 **comparable group**：

```
comparable_group = bench_id + bench_version + setting + harness
```

规则：
- 只有同一 group 内才计算分位/排名。
- 跨 group 的数值在表格里只显示原始分，不参与着色，鼠标悬停提示"口径不同，不可比"。
- group 内样本数 < 3 时不做分位，只显示原始分。
- 厂商在带工具条件下报的分数，若其他模型只有不带工具的数据，则该分数单独标注 `with_tools`，默认视图隐藏（可用"显示全部口径"打开）。

这一条是全篇最关键的设计：**打假不是靠嘴，是靠把口径摊开给用户看。**

## 7. 解读层

三层，权重和依据都公开。

**L1 榜单词典**（来自 `benchmarks.json`）
回答"这个榜测什么、多少算好、还剩多少区分度"。例：MMLU 已饱和，别拿它当卖点。

**L2 模型画像**（`profile` 字段，人工撰写）
按能力域给相对位置，不排绝对名次：
> 代码与 Agent 任务表现靠前（SWE-bench Verified 82.1，同级前三）；长文档略弱于同代旗舰；中文写作缺独立数据。

**L3 场景适配**（加权映射，权重公开）

| 场景 | 主要榜单 | 参考权重 |
| --- | --- | --- |
| 写代码 / 改代码 | SWE-bench Verified、LiveCodeBench、Terminal-Bench | 5:3:2 |
| 客服 / 对话 | LMArena Elo、IFEval、成本 | 5:3:2 |
| 合同 / 长文档 | LongBench v2、RULER、MRCR | 5:3:2 |
| 结构化抽取 | BFCL、IFEval(JSON)、structured_output 能力位 | 4:3:3 |
| 中文内容 | C-Eval、CMMLU、SuperCLUE | 4:3:3 |
| 多模态理解 | MMMU-Pro、Video-MME | 6:4 |

必须标注：**L3 是启发式映射，不是实测结论**；后续自建评测上线后逐步用实测替代。

## 8. 控制台呈现

新增页签「模型对比」（位于「用量报表」之后）。

- **主表格**：行 = 覆盖清单模型，列 = 能力域分组（推理 / 代码 / 对话 / 长文 / 中文 / 多模态 / 成本速度）。单元格显示原始分 + 组内分位色阶。
- **来源徽章**：每格右上角标 `独立` / `厂商`。顶部开关「仅显示独立来源」。
- **详情抽屉**：点模型展开，列出全部分数（含 setting、日期、来源链接）、L2 画像、该身份在哪些渠道可用。
- **场景选择器**：选一个业务场景，按 L3 权重重排模型，并显示权重明细。
- **空状态**：`暂无公开跑分` —— 不允许显示推测值或留白无提示。

后端接口：
- `GET /api/models/compare` —— 返回覆盖清单 + 分数 + 词典元数据（一次拉全，数据量很小）。
- 不改造 `/api/state`，避免把静态数据混进实时状态。

## 9. 采集与维护流程

| 阶段 | 做法 |
| --- | --- |
| V0 | 人工录入 + 种子数据。约 30 模型 × 8 榜 = 240 条，一周内可完成。配 `tools/validate_benchmarks.py` 校验。 |
| V1（已实施） | `tools/sync_benchmarks.py` 接 Vals Terminal-Bench 4.0（开发期人工跑，校验后合入 registry 快照，随版本发布）；运行时 `BenchmarkRefreshRunner` 定时抓取写入用户目录 `model-scores.override.json`，由 `benchmarks.py` 自动叠加。 |
| V2 | 接 `watch.py` 的模型集合变更事件：新旗舰出现 → 标记为"待补数据" → 控制台提示。 |

V1 双轨：registry 快照仍走「产出 diff → 校验 → 人工审」后合入，保证随版本发布的数据可控；运行时 override 是无人值守的增量刷新，只覆盖 vals harness 条目，且不修改打包快照（新版本发布时快照更新会自然取代旧 override）。录错一条数据的代价比省那点人力大得多，所以 registry 合入必须人工审。

## 10. 校验（V0 必须做）

`tools/validate_benchmarks.py` 检查：
1. JSON schema 合法。
2. 每条分数有 `source_url` + `date` + `setting`。
3. `bench` 必须存在于 `benchmarks.json`。
4. `setting` 必须在词典声明的 `settings` 列表内。
5. `comparable group` 内数值不重复冲突（同一模型同一 group 只允许一条，或标注差异来源）。
6. `model-coverage.json` 中的 id 必须能在 `model-scores.json` 找到，或在注册表中存在（§3.5 的例外需显式标记）。

接入 pytest，纳入 CI。

## 11. 分期

- **V0**：三个数据文件 + 校验脚本 + 只读面板（表格 + 详情）。约 30 模型。
- **V1**（已实施）：场景适配视图 + 画像文案补齐 + 一个自动来源（Vals Terminal-Bench 4.0，开发期 `tools/sync_benchmarks.py` + 运行时 `BenchmarkRefreshRunner` 自动刷新）。
- **V2**：watch 联动 + 来源扩充 + 对外可分享的榜单页。

## 12. 开放问题

1. **是否引入外部综合分**（Artificial Analysis Index）？引入能省事，但会把一个第三方口径当成事实标准；不引入则用户要自己看分项。
2. **中文榜单选谁**：OpenCompass 开源但更新慢，SuperCLUE 商业但影响大。
3. **是否做"厂商自报 vs 独立实测"并排对照视图**？这是打假最直观的形式，但需要两边都有数据，v0 覆盖不到。
4. **L2 画像谁写**：人工撰写是公信力核心，但不可规模化；是否接受"少量高价值模型人工写，其余只有数据"。
5. **百度/腾讯是否纳入**（§3.5），以及是否需要先补 registry 数据源。

## 13. 风险

- **榜单饱和**：MMLU/SWE-bench 原版已失去区分度，继续展示会误导。靠 `saturation` 字段强制说明。
- **口径污染**：厂商只报有利条件。靠 comparable group 与来源分级对冲，但需要编辑纪律。
- **维护成本**：每月新模型都要补数据，人工核对是瓶颈。V2 的 watch 联动是解法。
- **利益冲突**：Facet 聚合厂商渠道，评测结论必须与渠道商业利益隔离，否则整个定位崩塌。
- **抓取合规**：各榜单站点条款不同，V1 接来源前需逐一确认。
