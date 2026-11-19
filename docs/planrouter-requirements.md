# PlanRouter 需求清单

日期：2026-11-09。性质：需求视图——把散落在 [PROGRESS.md](PROGRESS.md)（唯一进度基准）、
superagent 行动清单与各设计文档的需求项汇总编号，补齐验收标准。
**进度状态以 PROGRESS.md 为准，本文件负责需求口径与验收。**

## 1. 需求来源

| 来源 | 内容 |
|---|---|
| [PROGRESS.md](PROGRESS.md) 进行中 / 接下来 | 近期全部待办与观察项 |
| [planrouter-superagent-integration.md](planrouter-superagent-integration.md) 第 6 节 | superagent 对接 P0–P3 行动清单 |
| [planrouter-vendor-binding-and-docker-seed.md](planrouter-vendor-binding-and-docker-seed.md) | 设计已出、尚未实施的种子对齐方案 |
| [PROGRESS.md](PROGRESS.md) 产品方向 | 远期方向（仅记录，现在不做） |
| 2026-11-09 会话讨论 | Facet 数据层自维护（R9）与适合度路由联动 |

## 2. 近期需求（进行中 / 待办）

| 编号 | 需求 | 验收标准 | 状态 |
|---|---|---|---|
| R1 | **正式试用 → 打 tag 发版**：容器栈常驻网关，把日常真实工具（DSH 等）指向网关跑实际工作，观察故障转移、配额冷却、每日维护在真实负载下的表现；通过后定版本号打 tag | 每小时监控快照（`D:\Temp\facet-trial\metrics.jsonl`）连续多日无告警；available 渠道稳定不骤减；试用负载 e2e 通过（见 [planrouter-container-trial.md](planrouter-container-trial.md) 监控节） | 进行中 |
| R2 | **桥接渠道恢复**：需要 Claude/Codex coding plan 时跑起对应本地桥（8080/8081，见密钥手册 6.3）或设置 User 级 `GLM_PLAN_KEY`；**容器适配点**——渠道地址需从 `127.0.0.1` 改为 `host.docker.internal`；渠道改回 enabled | `/api/test/channels` 对应渠道 200；`/health` available 增加；auto 链不再首跳即 fallback | 待办 |
| R3 | **superagent 对接 P0 后半**：部署 super_agent 原型，验证 Router / Worker 能否分别指定模型（理想形态：Router 走 `facet-cheap` 分派、Worker 走强模型干活）；若原型不支持，退化为 Worker 档位选型问题 | 原型 headless 跑通；审计日志（`data/requests.jsonl`）可见分工请求分别落在对应链 | 待办 |
| R4 | **superagent P1**：观察模式跑通后评估 Worker 模型档位（`facet-cheap` 够不够） | 给出评估结论与建议档位（以实测任务质量为准） | 依赖 R3 |
| R5 | **superagent P2**：policy 草案——运维操作走 `/api/*` + admin key，禁改配置文件 | 草案文档成稿（操作清单 + 权限边界） | 依赖 R3 |
| R6 | **superagent P3**：吸收层面 3 三条设计原则到维护层文档与实现 | 设计原则落进维护层文档；可实现项进实现 | 无硬依赖，渐进 |
| R7 | **密钥手册补图**：按 [api-key-guide.md](api-key-guide.md) 实操截图，放入 `docs/images/api-key-guide/`（文件名对上即自动显示；NVIDIA 已补 4 张）；界面与手册不符处回改手册 | 各厂家小节截图齐全；手册口径与控制台一致 | 待办（用户实操） |
| R8 | **tab 跳回 bug**：控制台自动刷新偶发跳回渠道配置 tab | 先拿到复现路径；复现即修并补 e2e | 观察中 |
| R9 | **Facet 注册表自维护**：`tools/sync_models_dev.py`（上游 models.dev + OpenRouter）同步定时化、自动产出变更报告；与网关渠道目录实况交叉验证（渠道在售而表未收录 → 补录/告警；表有而全渠道下架 → 标记过时）；表更新后联动路由建议生成器（facet-auto / facet-cheap 适合度排序，2026-11-09 讨论定调：免费 ≠ 小模型，按用途选最适合的模型） | registry `updated_at` 按周期自动滚动（首期每周）；交叉差集出现在维护日志；人工只保留 git 确认 | 已落地（2026-11-09）：`tools/weekly_maintenance.py` 一键编排（staging 先验后落盘，坏抓取不毁正本）+ 计划任务 `FacetWeeklyMaintenance` 每周日 03:30 自动跑；交叉差集进 `registry/maintenance-log.jsonl`；路由链提案写 `registry/route-suggestions.json`（仅报告，合入 router.json 仍是人工编辑；注意区别于网关运行时的「维护建议」，后者是渠道配置建议、控制台一键采纳） |

## 3. 远期方向（仅记录，现在不做）

- **轻量团队版**（2026-11-08 讨论结论）：给小团队做内部共享 AI 网关，解决「员工各自
  充值、各自报销」。目标形态 per-user key + 按人配额 + 成本分摊报表；存储保持
  JSON / SQLite、仍单文件零依赖；不做 SSO / RBAC / 多实例。合规红线：个人免费额度
  聚合与订阅桥只留个人侧，团队场景用企业账号对接厂家。个人版核心稳定后先出
  最小改动清单。
- **容器种子对齐**（设计已出、未实施）：entrypoint 播种行为与普通运行种子对齐
  （见 [planrouter-vendor-binding-and-docker-seed.md](planrouter-vendor-binding-and-docker-seed.md)）。

## 4. 需求边界（非目标）

与 [planrouter-architecture.md](planrouter-architecture.md) 第 11 节一致：事实表不做路由
决策、不管 key、不遥测；网关路由不依赖表的新鲜度；不做编排 / 多实例；Node 生态组件
（super_agent 等）跑在网关之上、不嵌入包内。

## 5. 维护口径

- 状态流转：待办 → 进行中 → 已完成；完成即移入 PROGRESS.md「已完成」并在其更新记录加行；
- 本文件只维护需求与验收标准，不重复记录进度细节。
