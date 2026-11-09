# 方舟免费额度接入（协作奖励计划 · facet-ark-quota）

火山方舟的「协作奖励计划」按调用返还免费资源包：授权后调用指定模型（如
`kimi-k3-1-pro-261024`、`deepseek-v4-1-flash-economy-261017`），次日按当日
token 用量返还 30 天有效的免费推理包，每模型每日上限 500 万 tokens
（企业认证可提升）。该免费额度只暴露在方舟**管理面** OpenAPI 上——聊天
用的 `ark-...` 数据面 Key 读不到它，必须用 IAM Access Key 签名调用：

```
POST https://ark.cn-beijing.volcesapi.com/?Action=ListModelActivations&Version=2024-01-01
Body: {"WithFreeUsage": true, "PageNumber": n, "PageSize": 100}
```

PlanRouter 的 free-align sweep 需要知道「哪些模型还有免费额度」才能把
免费渠道映射对齐（未映射但被计费、已过期但仍在映射，都是线上事故）。
本集成把管理面状态接进这套机制，与百炼的 `bl usage free` 同构。

## 工作方式

- **CLI**：`facet-ark-quota --output json`（`src/planrouter/ark_quota.py`，
  纯 stdlib）。火山引擎 V4 签名（Service=ark, Region=cn-beijing，签名
  `host;x-content-sha256;x-date`），自动翻页直到取尽，stdout 输出
  freealign 契约的裸 JSON 数组：

  ```json
  [
    {"model": "kimi-k3-1-pro-261024", "type": "Text",
     "remaining": 3800000, "expires": "2026-12-09"}
  ]
  ```

- **行映射规则**（`rows_from_items`）：只保留 `State == "Available"` 的
  模型；`remaining` 取「初始免费额度剩余（Total−Consumed）」与
  「FreeInference 资源包剩余（Total−Consumed−Reclaimed，逐包钳到 ≥0）」
  的较大者；`DataPermission` 包不产生推理额度，不计入。
- **expires 是合成的**：管理面 API 不返回资源包有效期，CLI 按奖励周期
  报「今天 + 30 天」。真正的门闩是 `remaining > 0`，每次 sweep 刷新。
- **注册表**：`providers.json`（内置与 overlay 均已写入）给 volcengine
  的 `payg` 模式挂了
  `"free_quota_cmd": ["facet-ark-quota", "--output", "json"]`；overlay
  缺该字段时会从内置注册表继承（`INHERIT_LISTS`）。
- **sweep 联动**：只有 `tier == "free"` 且关联到带 `free_quota_cmd`
  模式的渠道会被对齐（如运行时渠道「火山引擎免费」，provider=volcengine,
  mode=payg）。调度周期 `defaults.free_align_hours`（默认 24h），
  手动触发 `POST /api/freealign/run`（管理员）。命令失败不抛出：记入
  report 告警并保留旧映射，下次 sweep 重试。

## 挂载进路由（$free 宏，无手工步骤）

新接入的免费渠道（如「火山引擎免费」）**不需要**往 route 里逐个手工挂载：
`router.json` 的 openai 链里已经放了 `$free` 宏（见
`data/router.json`），展开规则统一由 `parse_config` 运行时推导，无硬编码：

- 宏展开为**当前配置里**所有满足条件的渠道：协议与所在 route 相同、
  `tier == "free"`、`enabled: true`，按 `(priority, name)` 升序排列；
- 显式写出的渠道保持原位且不重复（宏只补缺，位置以显式为准）；
- `tier` 为空或 `paid` 的渠道、被停用的渠道、协议不符的渠道一律不进宏；
- 展开后为空（没有任何免费渠道）直接报配置错误，fail fast。

因此任何新免费渠道只要配好 `tier: free` 并启用，下一次加载配置就自动
进入所有带 `$free` 的路由链；免费/付费的比例调整、渠道增删都不需要改
route。付费渠道始终手工摆位（决定成本优先级）。

## 接入步骤

1. **申请 IAM 凭据**：火山引擎控制台 → 访问控制（IAM）→ API 访问密钥，
   为子用户签发 Access Key（建议新建只授权方舟管理面只读的子账号，
   不要复用主账号 AK）。`ark-` 开头的数据面 API Key **无效**。
2. **提供环境变量** `VOLC_ACCESSKEY` / `VOLC_SECRETKEY`：
   - 本地直接运行：export 两个变量后手动执行 CLI 验证；
   - Docker Compose：在 `compose.yaml` 旁的 `.env` 里写
     `VOLC_ACCESSKEY=...`、`VOLC_SECRETKEY=...`，router 服务已透传。
     缺省为空——sweep 会告警「免费额度命令执行失败」并保留原映射。
3. **验证 CLI**：

   ```bash
   facet-ark-quota --output json
   # 退出码 0 → stdout 是 JSON 行数组；2 → 缺凭据；1 → 网络/API 错误（stderr 有详情）
   ```

4. **触发对齐**：`curl -X POST -H "Authorization: Bearer <admin-key>" \
   http://127.0.0.1:<port>/api/freealign/run`，在 report 的 `aligned`
   中确认「火山引擎免费」拿到了奖励模型清单；之后渠道映射会随 sweep
   自动跟随额度变化（额度耗尽的模型被移出映射，返还新包的模型进入映射）。

## 注意事项

- **数据会被采集**：参与协作奖励计划即同意「通过奖励额度产生的调用数据
  可被用于模型训练/评测」。不要把隐私敏感流量路由进免费渠道； PlanRouter
  的免费渠道仅适合跑无敏感内容的任务。
- **额度有上限**：每模型每日返还上限 500 万 tokens（企业认证可提升），
  耗尽后该模型在下次 sweep 时自动移出映射，由路由降级到付费渠道。
- **不要手写映射**：渠道模型映射以 sweep 结果为准；手工加进去的模型若
  无免费额度，调用会直接按量计费。
- **AK/SK 安全**：凭据只进环境变量，不落仓库；`.env` 不入库（保持
  `.gitignore` 现状）；IAM 子账号权限最小化。

## 测试

`tests/test_planrouter_ark_quota.py` 覆盖：V4 签名确定性/规范性复算、
行映射（初始额度/资源包/Reclaimed/Unavailable）、翻页终止条件、CLI
stdout 契约与退出码、注册表 `free_quota_cmd`（含 overlay 继承）。
`python -m unittest discover tests` 全量回归通过。
