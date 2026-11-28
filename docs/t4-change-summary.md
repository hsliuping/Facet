# T4 收尾变更摘要

- 日期：2026-11-28
- 范围：dev-plan M4「后台与官网」T4.3–T4.7 全部完成并验证（T4.1 管理后台、T4.2 投稿入口、T4.8 受控发版此前已完成）
- 状态：所有变更未提交 git；1.1.0 发版整体暂缓

## 本次收尾的三项用户决策

1. **删除公开 RSS**：情报是人工核实的独家价值，不公开分发；`/rss.xml` 路由、导航、页脚、首页卡全部移除。客户端同步走签名接口 `/v1/intel.json`，不受影响。
2. **官网情报页加访问口令门槛**：「不公开，至少说明我是在乎的」——`CENTER_INTEL_TOKEN` 未配置或口令不对时，情报页不放出任何数据。
3. **发版暂缓**：全部产物停留在工作区，等待明确指示。

## 各任务变更明细

### T4.3 诊断上报（默认关闭，显式开启）
- 客户端 `src/facet/gateway/center.py`：`report_diagnostics` 开关，开启后按厂家聚合错误类型（渠道名归一为厂家 id），随心跳上报并清空；关闭时心跳照常但不带 diag。
- 中心 `center/app.py` + `center/db.py`：心跳白名单收录 diag（vendor/err_type 只允许安全短标识，计数 0~100000），异步批量落 `diagnostics` 表；`/admin/stats` 聚合，后台健康页新增诊断卡。

### T4.4 撤回下发
- 中心：`POST /admin/intel/retract` 落 `retractions` 表、`GET /admin/intel/retractions` 后台可查，后台情报详情页加撤回按钮；重复撤回更新原因。
- 客户端：`_push_intel` 分流 `status=retracted`——收到过的条目标「已撤回」+ 移除操作入口 + webhook 通知；从未见过的直接下发更正说明（不推原文）；不重复推送。schema.json 支持 `retracted_reason`。

### T4.5 媒体草稿生成
- 新增 `tools/media/drafts.py`（纯标准库）：`--ledger` / `CENTER_LEDGER` / 布局探测，`--out` 默认 `data/media`。
- 输出：`toutiao/{act-id}.md`、`wechat/weekly-YYYYMMDD.md`（窗口锚定本周一）、`bilibili/monthly-YYYYMM.md`；全部带 GENERATED 头与「一键领取」落地链接（已指向 `/intel`）。
- 只出 verified/published 活动，retracted/expired 不出；空账本不崩。

### T4.6 官网四页 + 情报页门槛
- 中心直接渲染 `/`、`/free`、`/intel`、`/privacy`（stdlib 渲染 + HTML 转义，数据来自 data_dir 的 registry/intel/releases 静态产物）；厂家一律文字呈现（徽标未授权不上图）。
- 隐私页带「草稿 · 待审阅」横幅；后台健康页新增「上线前待办」卡。
- **情报页门槛**：`CENTER_INTEL_TOKEN` 环境变量，每次请求现读；未带口令只返回门槛页（零情报数据泄露）；`?token=` / Cookie `facet_intel` / Authorization Bearer 三路径校验（`hmac.compare_digest`）；口令进入时下发 HttpOnly Cookie（`Path=/intel; SameSite=Lax; Max-Age=30 天`）；未配置口令时提示「口令未配置，情报暂不开放」+ 运营提示。

### T4.7 公网地址配置化
- `CENTER_PUBLIC_URL` 环境变量：页面 canonical、媒体草稿落地链接统一取它；docker-compose 已加（默认 `https://aifacet.cn`，备案通过前可指到可用域名）。
- 客户端 `DEFAULT_URL` 已是 `https://api.aifacet.cn`；上线卡域名备案。

## 涉及文件

**facet-center**（未建 git 仓库）：
- `center/app.py`（T4.3/T4.4/T4.6/T4.7 + 门槛）、`center/db.py`、`center/static/index.html`（诊断卡 + 上线前待办）
- `tools/media/drafts.py`（新增）
- `docker-compose.yml`（`CENTER_PUBLIC_URL` / `CENTER_INTEL_TOKEN`）
- `docs/dev-plan.md`（T4.3–T4.7 标 [x] 附实现说明）

**Facet**（工作区未提交）：
- `src/facet/gateway/center.py`（诊断上报 + 撤回分流）、`src/facet/gateway/ui.py`、`src/facet/gateway/server.py`、`src/facet/gateway/static/*`、`tools/windows/facet_tray.py`

## 验证结果

| 项目 | 命令 | 结果 |
|---|---|---|
| 中心 selftest | `python -m center.app --selftest` | 全绿（含门槛 8 断言、`/rss.xml` 404、T4.8 发版门禁） |
| 媒体草稿自测 | `python tools/media/drafts.py --selftest` | 8 项断言全过 |
| 客户端 selftest | `python -m facet.gateway.center` | 全绿 |
| Facet 全量测试 | `python -m pytest -q` | 1408 passed, 1 xfailed |
| 浏览器实测 | — | 官网五页 + 后台页面渲染正常 |

## 上线前待办（已写入 `/admin/health` 的 pending_review）

- 隐私政策文本人工审阅（`/privacy` 当前为草稿态）
- 厂家徽标授权
- 域名备案：DNS 记录 + 公安备案（T4.7 切换的前提）
- 环境变量：`CENTER_INTEL_TOKEN`（情报页口令，未配置则情报页不放数据）、`CENTER_ADMIN_TOKEN`、`CENTER_INTAKE_TOKEN`、`CENTER_GITHUB_TOKEN`、`CENTER_PUBLIC_URL`
- GitHub secrets：`CENTER_INTAKE_URL`、`CENTER_INTAKE_TOKEN`（投稿入口）
