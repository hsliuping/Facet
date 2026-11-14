# Web UI 自动化测试 · 实现任务分解

> 配套用例清单：[planrouter-web-ui-test-cases.md](planrouter-web-ui-test-cases.md)
> 状态：**待评审** · 最后更新 2026-11-14

本文件把 53 条用例拆成可独立交付、可独立验收的任务包。每个任务包带「验收标准」与「状态」列，完成后回填，可直接当进度看板用。

---

## 一、任务总览

| 任务 | 阶段 | 交付物 | 覆盖用例 | 依赖 | 状态 |
|---|---|---|---|---|---|
| WP0 | 基础设施 | `tests/web/conftest.py`、`tests/web/ui.py`、pytest 配置 | — | 无 | 待开始 |
| WP1 | 冒烟层迁移 | `tests/web/test_smoke.py`（原 41 条修复后迁入） | 现有 41 条 | WP0 | 待开始 |
| WP2 | P0 认证 + 渠道 | `test_auth.py`、`test_channels.py` | A1 A2 · B1 B2 B3 | WP0 | 待开始 |
| WP3 | P0 只读核心页 | `test_routes.py`、`test_free.py`、`test_dashboard.py` | C1 · D1 · E1 | WP0 | 待开始 |
| WP4 | P0 商务与运维 | `test_subscriptions.py`、`test_apikeys.py`、`test_maintenance.py`、`test_usage.py` | F1 · H1 · I1 · J1 | WP0 | 待开始 |
| WP5 | P0 健壮性 | `test_resilience.py` | K1 | WP0 | 待开始 |
| WP6 | P1 认证 + 渠道 + 路由 | 追加到 WP2/WP3 文件 | A3 A4 · B4 B5 B6 B7 · C2 C3 | WP2 WP3 | 待开始 |
| WP7 | P1 免费/仪表板/订阅/厂家 | `test_vendors.py` + 追加 | D2 D3 D4 · E2 E3 E4 · F2 F3 · G1 G2 G3 | WP3 WP4 | 待开始 |
| WP8 | P1 Key/自维护/用量/体验 | 追加到 WP4/WP5 文件 | H2 H3 · I2 I3 I4 · J2 J3 · K2 K3 | WP4 WP5 | 待开始 |
| WP9 | P2 边界批 | 追加到各文件 | A5 B8 C4 D5 E5 F4 G4 H4 I5 J4 K4 K5 | WP6-WP8 | 待开始 |
| WP10 | 收尾 | README 更新、CI 接入、文档状态回填 | — | WP1-WP9 | 待开始 |

**执行顺序**：WP0 → WP1 → WP2/WP3/WP4/WP5（可并行）→ WP6 → WP7 → WP8 → WP9 → WP10。

---

## 二、任务详情

### WP0 基础设施

**目标**：让后续所有用例都能"隔离、可等待、可排障"地写。

**交付文件**
- `tests/web/__init__.py`
- `tests/web/conftest.py`
- `tests/web/ui.py`（Page Object）
- `pyproject.toml` 增加 pytest 配置（若已有 `[tool.pytest.ini_options]` 则追加）

**实现要点**

1. `gateway_server` fixture
   - 用 `tempfile.TemporaryDirectory()` 作为**每个用例独享**的 config/data 目录，config 写在目录内
   - `App(config, config_path=tmp/config.json)`，确保 state/usage/audit 全部落在临时目录，不污染仓库与系统 temp
   - 修正测试配置：routes 只保留一条 `default`（当前夹具里重复定义了两条同名链）
2. `api` fixture
   - 轻量 HTTP 客户端（标准库 `urllib` 即可，避免新增依赖）
   - 暴露 `api.get(path)` / `api.post(path, body)`，默认带 `X-Admin-Key`
3. `anon_page` / `admin_page` fixture
   - `anon_page`：不注入 key，用于 A1/A2
   - `admin_page`：`add_init_script` 注入 key（沿用现有做法）
   - 两者共用 `browser` fixture，避免每个用例重启 Chromium
4. 失败留证
   - `page` 级 fixture 在 teardown 检测用例失败时保存 screenshot + trace 到 `build/playwright-artifacts/<用例名>/`
5. `ui` Page Object
   - `nav(tab)`：点击菜单项并等待目标面板可见
   - `wait_api(path)`：`expect_response` 包装，替代 `wait_for_timeout`
   - `toast_text()` / `modal_visible()` / `close_modal()`：弹窗与提示统一入口
6. marker 注册：`smoke`（只读）、`mutating`（写配置，串行）

**验收标准**
- `python -m pytest tests/web -q` 可执行（此时仅 1 条样例用例）
- 临时目录隔离验证：用例执行后仓库内 `git status` 无新增数据文件
- 故意失败一次时，`build/playwright-artifacts/` 下生成 screenshot 与 trace
- `pytest --markers` 能看到 `smoke` / `mutating`

---

### WP1 冒烟层迁移与修复

**目标**：把现有 41 条用例变成可信的冒烟网底，并搬到 `tests/web/`。

**交付文件**
- `tests/web/test_smoke.py`（迁移后）
- 删除 `tests/test_web_ui_playwright.py`

**实现要点**

| 原问题 | 处理 |
|---|---|
| 重复 `default` 链 | 由 WP0 的测试配置修复，本任务只做回归确认 |
| 满屏 `wait_for_timeout(800)` | 改为 `ui.nav()` 内部等待面板可见 + `wait_api` |
| `time.sleep(2)` + 恒真断言 | 删除该断言，改为等待 `#conn-badge` 文案离开"连接中" |
| `fontFamily != "initial"` 恒真 | 改为断言 `getComputedStyle(body).backgroundColor` 非默认白 |
| 移动端只断言侧栏可见 | 用例合并进 K3，本任务删除该条 |
| `__main__` 带 `--headed` | 移除 |
| 数据目录污染 | 复用 WP0 的隔离 fixture |

**验收标准**
- 迁移后用例数 ≥ 35（合并/删除的已在文档中标注去向）
- 全量通过，总耗时明显低于原 78s
- `grep -c "wait_for_timeout\|time.sleep" tests/web/test_smoke.py` 结果为 0
- 原文件已删除，README 中的命令指向新路径

---

### WP2 P0 · 认证与渠道生命周期

**覆盖用例**：A1、A2、B1、B2、B3

**交付文件**：`tests/web/test_auth.py`、`tests/web/test_channels.py`

**实现要点**
- A1/A2 用 `anon_page`：断言 `.ask-backdrop` 出现 → 输入错误 key 提示失败 → 输入正确 key 后面板加载
- B1/B2/B3 打 `mutating`：走"点新增 → 填表 → 保存 → 列表断言 → `api.get('/api/config')` 落盘断言"
- B2 的关键：编辑时密钥框值为 `***`，原样保存后 `api` 读到的密钥仍是原值
- B3 删除需处理二次确认弹窗

**验收标准**
- 5 条用例通过
- 每条写操作用例都有"UI 断言 + 后端落盘断言"两段
- `pytest -m mutating tests/web/test_channels.py` 单独可跑

**依赖**：待确认事项 1、2（写操作范围、是否引入后端校验）——**当前方案按"临时目录 + 引入校验"设计，结论变化会同时影响 B1/B3/B5**

---

### WP3 P0 · 路由链 / 免费模型 / 仪表板

**覆盖用例**：C1、D1、E1

**交付文件**：`tests/web/test_routes.py`、`tests/web/test_free.py`、`tests/web/test_dashboard.py`

**实现要点**
- C1：新增链 → 添加节点 → 保存 → 断言 `/api/config.routes` 结构与顺序
- D1：测试配置需预置至少 2 个免费渠道，断言表格行数、列内容（状态/额度/限速/近 30 天）
- E1：摘要卡数值与 `api.get('/api/state')` 交叉比对，而不是只断言元素存在

**验收标准**
- 3 条通过；D1 在"无免费渠道"场景下有明确空态断言

---

### WP4 P0 · 订阅 / API Key / 自维护 / 用量

**覆盖用例**：F1、H1、I1、J1

**交付文件**：`tests/web/test_subscriptions.py`、`tests/web/test_apikeys.py`、`tests/web/test_maintenance.py`、`tests/web/test_usage.py`

**实现要点**
- F1（mutating）：新增订阅 → 列表出现 → 仪表板同步
- H1（mutating）：生成 key → 明文只出现一次（再次打开不可见）→ 用该 key 调 `/v1/models` 得 200
- I1：任务表逐列断言（开关/周期/上次运行/下次运行/状态/连续失败次数）
- J1：点 7/30 天切换，用 `wait_api('/api/usage/7')` 与 `/api/usage/30` 验证请求发出且数值变化

**验收标准**
- 4 条通过；H1 的"明文只出现一次"必须是渲染层断言（重新打开面板后 `***` 或不可见）

---

### WP5 P0 · 健壮性

**覆盖用例**：K1

**交付文件**：`tests/web/test_resilience.py`

**实现要点**
- 在 `App` 上临时替换某个只读 handler 返回 500（或用路由 mock），断言页面出现错误提示、无白屏、无永久 loading
- 断言方式：错误提示元素可见 + `#panel-*` 仍在 DOM

**验收标准**
- 1 条通过，且 mock 在 teardown 中恢复，不影响后续用例

---

### WP6 P1 · 认证 / 渠道 / 路由分支

**覆盖用例**：A3、A4、B4、B5、B6、B7、C2、C3

**实现要点**
- A3：勾选"记住"后断言 localStorage 写入；新 context 免填
- A4：构造跨站 Origin 的 POST，断言 403 且页面提示保存失败
- B4/B6：切换启用与解除冷却后，`/api/state` 中该渠道可用性变化
- B5：复制后名称带后缀、无明文密钥
- B7：搜索过滤（可先用它验证 WP0 的等待策略是否够用）
- C2：节点上移/下移/移除后保存，断言顺序
- C3：删除 `default` 被阻止并提示

**验收标准**：8 条通过

---

### WP7 P1 · 免费模型 / 仪表板 / 订阅 / 厂家

**覆盖用例**：D2、D3、D4、E2、E3、E4、F2、F3、G1、G2、G3

**实现要点**
- D2/D3：筛选与搜索的行数收敛断言（用计数而非文案）
- D4：对齐额度后页面日志出现新条目，且 `freealign.jsonl` 在临时目录内新增一行
- E2：用 `wait_for_response` 计数验证 6 秒内出现第二轮请求
- E3：跨面板跳转 + 侧边栏高亮同步
- F2/F3（mutating）：续费与删除落到 `/api/subscriptions`
- G1/G2/G3（G2 为 mutating）：账号数变化、立即维护回显结果

**验收标准**：11 条通过

---

### WP8 P1 · Key / 自维护 / 用量 / 体验

**覆盖用例**：H2、H3、I2、I3、I4、J2、J3、K2、K3

**实现要点**
- H2：吊销后调用 401
- H3（mutating）：轮换管理密钥后旧 key 失效、新 key 生效，页面自动切换
- I2：开关后**刷新页面**仍保持（验证持久化）
- I3：立即运行 → 状态机 运行中 → 完成，`last_run_at` 更新
- I4：构造连续失败任务，断言标红 + 告警区出现
- J2/J3：筛选收敛、日志列内容与上限
- K2：延迟响应场景用 `page.route` 拖慢接口
- K3：768px 下菜单按钮展开/收起 + 点击菜单后自动收起

**验收标准**：9 条通过

---

### WP9 P2 · 边界批

**覆盖用例**：A5、B8、C4、D5、E5、F4、G4、H4、I5、J4、K4、K5

**实现要点**
- 表单校验类（B8、H4）：断言报错文案 + `api` 确认未写入
- 空态类（J4、I5、E5）：断言空态文案存在
- 交互体验类（K4、K5）：键盘可达、Esc 关闭、无原生 `prompt/alert/confirm`
- 提示类（A5、F4、D5、C4、G4）：按各自预期断言

**验收标准**：12 条通过

---

### WP10 收尾

**交付内容**
1. `README.md`：测试命令更新为 `tests/web`，补充 `-m smoke` / `-m mutating` 两种跑法
2. CI 接入（如项目有 CI 配置）：安装 playwright + chromium，跑 `-m smoke`
3. 两份文档的「状态」列全部回填（用例文档 53 行、本文档 11 行）
4. 文档「待确认事项」表填写最终结论

**验收标准**
- `python -m pytest tests/web -q` 全绿
- `python -m pytest tests/web -q -m smoke` 在无写操作的前提下全绿
- 文档状态列与代码实际一致

---

## 三、核查方式

| 核查项 | 命令 / 方法 |
|---|---|
| 全量业务用例 | `python -m pytest tests/web -q` |
| 只读冒烟 | `python -m pytest tests/web -q -m smoke` |
| 写操作串行 | `python -m pytest tests/web -q -m mutating -p no:randomly` |
| 数据隔离 | 跑完 `git status --porcelain`，除代码外无新增文件 |
| 无硬等待 | `grep -rn "wait_for_timeout\|time.sleep" tests/web` 无结果 |
| 失败留证 | `build/playwright-artifacts/` 下按用例名分目录 |
| 用例覆盖对齐 | 用例文档每个 ID 在 `tests/web` 中可 `grep` 到（建议用例 docstring 里写 ID） |

**约定**：每条用例的 docstring 首行写 `[ID] 场景`，便于文档与代码双向核查（如 `"""[B1] 新增渠道后列表与配置同步"""`）。

---

## 四、阻塞与前置确认

| 对应用例文档条目 | 影响任务 | 说明 |
|---|---|---|
| 待确认 1：写操作范围 | WP2 / WP4 / WP6 / WP7 | 当前按"只改临时目录、跑完丢弃"设计 |
| 待确认 2：是否引入后端落盘校验 | WP2 / WP3 / WP4 | 当前按"引入"设计，若改为纯 E2E，B1/B3/F1/G2 需改断言 |
| 待确认 3：首批范围 | 全部 | 当前按"WP0→WP1→P0 五包"推进，再决定是否继续 P1 |

以上三项结论未定前，WP0 与 WP1 不受影响，可先行开工。
