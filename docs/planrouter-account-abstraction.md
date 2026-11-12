# 账号抽象（Account）开发设计

日期：2026-11-11。状态：方案待评审，评审通过后再开发。

结论：**引入可选 `Account` 实体，但不设为强制层级**。火山引擎 / 阿里云百炼这类
「一账号 → 多业务线 → 多 Key / 多套餐」的厂家用 `Account` 承载管理面 AK/SK；
OpenRouter / DeepSeek / 匿名免费站这类「单账号单 Key」厂家不建账号，行为与现状一致。

---

## 一、背景与问题

当前模型里「账号」没有独立实体，被拆散在三处：

| 位置 | 现在承担的职责 | 问题 |
| --- | --- | --- |
| [`Channel`](../src/planrouter/core.py#L258-L291) | 请求路径（base_url + api_key + models）+ 账号身份 + 管理面凭据 `creds` | 一个对象三种语义；同账号三条业务线要重复填三份 AK/SK |
| `Subscription.account` | 自由字符串标签 | 只是展示文本，改字即失联；无法按账号聚合 |
| `config.credentials[provider]` | 厂家级共享凭据 | 与 `channel.creds` 语义重叠，形成两套「账号」 |
| [`_plan_creds_for`](../src/planrouter/server.py#L671-L703) | 用订阅里第一个渠道名反查 AK/SK，`"agentplan" in vendor` 猜业务线 | 文本约定脆弱；无渠道时只能回落到固定的 `volcengine` 槽 |
| [`_creds_for`](../src/planrouter/freealign.py#L135-L147) | 「渠道即账号」 | 同账号多渠道各自复制一份凭据，改一处漏两处 |

真实厂家结构：

- 火山引擎：主账号 → IAM 子用户（AccessKey）→ 业务线（Coding Plan / Agent Plan / 企业 Agent Plan），每条业务线独立数据面 Key（`ark-` 前缀）与独立套餐余量。
- 阿里云百炼：主账号 → RAM 子账号（AccessKey）→ 业务空间成员 → 空间内套餐。
- OpenRouter / DeepSeek / Moonshot / xAI / NVIDIA NIM：一个 Key 覆盖全部模型，无管理面、无多业务线。

因此问题不是「要不要账号」，而是「账号抽象要做到什么程度才既覆盖复杂厂家，又不逼简单厂家造空实体」。

---

## 二、设计目标与非目标

**目标**

1. 同一厂家的同一账号，管理面 AK/SK 只填一次，被多条渠道共享。
2. 套餐订阅能明确挂到「账号 + 业务线」，不再靠 `vendor` / `plan` 文本推断。
3. 余量查询、免费额度对齐按账号去重与聚合。
4. 单 Key 厂家、匿名免费厂家、本地桥渠道零额外配置、行为不变。

**非目标**

- 不引入 `BusinessLine` 独立实体：注册表的 `mode`（`coding` / `agent` / `payg`）就是业务线。
- 不把数据面 `api_key` 收进 `Account`：它属于「业务线 / 端点」，不属于账号。
- 不做成员与权限（阶段 2 视团队协作需求再议）。
- 不改请求转发、协议转换、故障转移的任何路径。

---

## 三、概念模型

```
Provider（厂家，来自 providers.json 注册表，必填）
 └─ Account（账号，可选；id / provider / label / credentials[AK,SK]）
      ├─ Channel（mode=coding|agent|payg，即业务线；数据面 api_key 在这里）
      └─ Channel（另一条业务线，共享同一份 AK/SK）

Subscription（套餐）──account_id──> Account
                    └─ business_line（默认取关联渠道的 mode）
                    └─ channels[]（实际承载的渠道，保持不变）
```

**退化规则（兼容性的核心）**

| 条件 | 含义 | 行为 |
| --- | --- | --- |
| `channel.account == ""` | 渠道自成账户（implicit account） | 凭据取 `channel.creds`，与今天完全一致 |
| `channel.account != ""` | 渠道属于该账号 | 管理面凭据取 `accounts[].credentials`；`channel.creds` 降级为临时覆盖 |
| `subscription.account_id == ""` | 旧数据 / 简单厂家 | 继续按 `account` 字符串展示，不回退报错 |
| `subscription.business_line == ""` | 未指定业务线 | 取关联渠道的 `mode`；仍取不到则不做余量查询 |

账号与厂家的关系是「多对一」（`Account.provider` 必填且必须在注册表中）；
账号与 AK/SK 是「一对一」（`Account.credentials` 是唯一载体）；
账号与数据面 Key 是「一对多」（每条业务线一个 `Channel`）；
账号与套餐是「一对多」（每个 `Subscription` 指向一条业务线）。

---

## 四、数据模型

### 4.1 新增顶层 `accounts: []`（存于 `router.json`）

```json
{
  "accounts": [
    {
      "id": "acc-volc-7f3a",
      "provider": "volcengine",
      "label": "火山主账号",
      "credentials": {
        "VOLC_ACCESSKEY": "env:VOLC_AK",
        "VOLC_SECRETKEY": "env:VOLC_SK"
      },
      "note": ""
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 稳定标识（`acc-<provider>-<hash8>`），创建后不可改，渠道/订阅引用它 |
| `provider` | string | 是 | 注册表厂家 id；一个账号只属于一个厂家 |
| `label` | string | 是 | 展示名，如「火山主账号」「百炼-研发」；与旧 `Subscription.account` 同名可自动关联 |
| `credentials` | object | 否 | `{env_name: value \| "env:VAR"}`；`env_name` 由注册表该厂家的 `credential_envs` 并集决定 |
| `note` | string | 否 | 备注 |

`credentials` 的键一律使用注册表声明的环境变量名（火山 `VOLC_ACCESSKEY` / `VOLC_SECRETKEY`，
百炼 `BAILIAN_ACCESS_KEY_ID` / `BAILIAN_ACCESS_KEY_SECRET`），不在 Python 里硬编码厂家常量。
若厂家未声明 `credential_envs`（匿名站、自定义厂家），允许任意非空键，作为逃生口。

### 4.2 `Channel` 增量（都可选，缺省即现状）

| 字段 | 说明 |
| --- | --- |
| `account` | 所属账号 id；空 = 渠道自成账户 |
| `business_line` | 业务线标签；空 = 取 `mode`。仅火山需要区分 Agent Plan / 企业 Agent Plan 时填写 |

`channel.creds` 保留不动：既是旧配置的兼容载体，也是「本渠道借用其他账号凭据」的显式覆盖。

### 4.3 `Subscription` 增量（[subscriptions.py](../src/planrouter/subscriptions.py) `normalize`）

| 字段 | 说明 |
| --- | --- |
| `account_id` | 指向 `accounts[].id`；空则按 `account` 字符串匹配 label（匹配不到只展示、不关联） |
| `business_line` | `coding` / `agent` / `payg` / 空；空则取关联渠道的 `mode` |

`account` 字符串保留，作为展示与旧数据兼容字段，不再参与凭据查找。

### 4.4 `Config`（[core.py](../src/planrouter/core.py)）

新增 `accounts: tuple[Account, ...]`（`Account` 为 frozen dataclass），
`parse_config` 在解析 `channels` 之前先解析 `accounts`，供渠道引用校验。

---

## 五、凭据解析：统一入口

新增单函数 `resolve_creds(channel, config)`（放在新的 `src/planrouter/accounts.py`，
与 `subscriptions.py` 同级），替换 [freealign.py](../src/planrouter/freealign.py#L135-L147)
与 [server.py](../src/planrouter/server.py#L671-L703) 里的散装逻辑：

```python
def resolve_creds(channel, config) -> dict[str, str]:
    """Management-plane credentials: channel override > account > vendor default."""
    explicit = dict(getattr(channel, "creds", None) or {})
    if explicit:
        return explicit
    account = config.account(getattr(channel, "account", ""))
    if account and account.credentials:
        return dict(account.credentials)
    return dict((config.credentials or {}).get(channel.provider) or {})
```

优先级（与今天严格对齐，只在中间插入账号层）：

```
channel.creds（显式覆盖）
 > account.credentials（account 非空时）
 > config.credentials[provider]（厂家级共享，旧配置）
 > 进程环境变量（由子进程继承 / 数据面 key 自带）
```

没有 `account` 时链路首尾与现状完全相同，旧配置行为零变化。
`freealign.run_vendor_align` 的去重键由 `(cmd, creds 键值)` 改为
`(cmd, account 引用, creds 键值)`，`account` 引用取 `channel.account or channel.name`，
保证同账号多渠道只执行一次厂家 CLI，且结果归属稳定。

---

## 六、校验规则

解析与保存所有配置的必经点是 `parse_config` + [`apply_config`](../src/planrouter/server.py#L968-L978)。
新增校验（错误消息中文、指名对象与修复方法）：

| 对象 | 规则 |
| --- | --- |
| Account | `id` 唯一、非空且匹配 `^[A-Za-z0-9_-]+$`；`provider` 必须在注册表中；`label` 非空 |
| Account.credentials | 厂家声明了 `credential_envs` 时，键必须是其并集子集；值的 `env:VAR` 与明文均可 |
| Channel.account | 必须指向已存在的账号，且 `channel.provider == account.provider`，否则拒绝 |
| Channel.business_line | 仅在 `mode` 存在时允许；空值合法 |
| Subscription.account_id | 必须指向已存在账号（跨文件校验，见 §八） |
| 删除账号 | 被任何渠道或订阅引用时拒绝删除，提示先改绑或删除引用方 |

`_check_vendor_binding` 的「新渠道必须有厂家」规则不变；`accounts` 不参与该规则，
避免简单厂家被迫建账号。

---

## 七、余量与套餐查询改造

### 7.1 `_plan_creds_for` 重写

```
1. kind: 取 record.business_line；为空取关联渠道的 mode；仍为空则 None（不查）
2. creds: record.account_id → account.credentials
          否则第一个关联渠道 → resolve_creds(channel, config)
          否则厂家级 config.credentials[record.vendor 对应的 provider]
3. 都取不到 → 返回 ("", "", "")，plan_for 返回 None
```

好处：套餐可以先于渠道存在（「先买套餐、后配渠道」不再查不到余量）；
不再依赖 `"agentplan" in vendor` 文本匹配，也不再固定回落 `volcengine`。

### 7.2 厂家守卫改为注册表声明

`providers.json` 的模式新增可选标量 `plan_api: "coding" | "agent"`（默认缺省 = 该模式不支持余量查询）：

- `volcengine`：`coding` → `plan_api: "coding"`，`agent` → `plan_api: "agent"`，`payg` 不声明。
- 其它厂家暂不声明，行为与今天一致（不查余量、不报错）。

`plan_api` 加入 [`INHERIT_IF_EMPTY`](../src/planrouter/providers.py#L44)，旧 overlay 模式条目
不会因缺字段而抹掉新能力。`kind` 的最终解析顺位：
`business_line` → 关联渠道 `mode` 的 `plan_api` → 账号 `provider` 对应模式下唯一的 `plan_api`。

### 7.3 缓存与持久化

- `ark_quota` 缓存键 `f"{kind}:{sha1(ak:sk)[:12]}"` 保持不变：它是远端资源指纹，语义正确；
  引入账号后同账号同 kind 自然共用一份快照。
- `vendorquota.json` 在阶段 0 仍按 `channel.name` 存快照（零风险）；
  阶段 1 改为 `account_id:model` 键，并在 `_load_vendor_quota` 做旧键兼容读取（读到旧键时按渠道名回填）。

---

## 八、控制台 UI

| 位置 | 改动 |
| --- | --- |
| 厂家页 | 每个厂家凭据区下新增「账号」列表：新建 / 编辑 / 删除，字段为 label + AK/SK（输入控件由 `credential_envs` 生成），沿用现有 `credential_help` 展开块 |
| 厂家页 | 厂家级凭据输入保留，标注为「默认凭据（未分组渠道共用）」；有账号后默认折叠 |
| 渠道编辑器 | 新增「所属账号」下拉：默认「独立（本渠道自带 AK/SK）」，选账号后隐藏渠道 creds 输入 |
| 渠道编辑器 | 选择厂家 / 模式后，账号下拉只列该厂家的账号 |
| 套餐订阅 | 表单新增「账号」下拉（可空）与「业务线」下拉（`coding` / `agent` / `payg` / 空）；旧数据自动按 `account` 字符串匹配 label 预选 |
| 套餐订阅 | 余量卡片按账号分组：同一账号的多条业务线归到一张卡片下 |
| 厂家页 | 一次性动作「合并为账号」：扫描同厂家、同 `creds` 内容的渠道，生成账号并把渠道改为引用（**仅用户点击才写盘**） |

前端数据来源不变：`GET /api/config` 的 `public_config` 带上 `accounts`（凭据做 `***` 掩码），
订阅页数据由 `GET /api/subscriptions` 增加派生字段 `account_label` / `business_line`。

---

## 九、兼容性矩阵

| 厂家形态 | 代表 | 需要 Account | 落法 |
| --- | --- | --- | --- |
| 单账号单 Key | OpenRouter、DeepSeek、Moonshot、xAI、NVIDIA NIM | 否 | `channel.account=""`，不出现账号 UI 提示，凭据仍在渠道 / 厂家级 |
| 匿名或免费 | Pollinations、LLM7、OVHcloud、魔搭 | 否 | 无凭据、无账号、无订阅，零改动 |
| 本地桥 | claude-plan / codex-plan | 否 | 渠道自成账户 |
| 单账号多业务线 | 火山引擎、百炼、智谱（coding + payg） | 是 | 1 Account + N Channel，AK/SK 只填一次 |
| 多账号同厂家 | 火山两个主账号 | 是 | 2 Account，渠道按账号分组 |
| 无管理面的套餐厂家 | 小米 Token Plan、OpenCode Go、Command Code | 否 | 订阅只挂 channel，`account_id` 留空 |

结论：`Account` 在复杂厂家显式启用、在简单厂家自然退化，模型对两类厂家都成立。

---

## 十、迁移与向后兼容

1. **缺省即旧模型**：`accounts` 缺失 = `[]`；`channel.account`、`channel.business_line`、
   `subscription.account_id`、`subscription.business_line` 缺失 = `""`。
2. **不自动写盘迁移**。首次加载只在内存里识别「同厂家 + 同凭据内容」的渠道组，
   在控制台提示「可合并为账号」，用户点击后才生成 `accounts` 并改写渠道引用。
3. **掩码与回填**：`public_config` 需对 `accounts[].credentials` 做 `***`（与
   `channels[].creds`、`config.credentials` 同规则）；`merge_secrets` 需把 `***` 回填为
   `self.raw` 中的旧值，否则保存一次就把真实 AK/SK 覆盖成掩码。
4. **删除引用防护**：删除账号前检查 `channels[].account` 与 `subscriptions[].account_id`。
5. **跨文件一致性**：账号在 `router.json`，订阅在 `subscriptions.json`；保存订阅时
   `save_subscriptions` 校验 `account_id` 是否存在，悬空引用降级为「仅展示」并回告警，
   不阻断保存（避免订阅文件被配置误锁死）。

---

## 十一、分阶段实施

### 阶段 0：数据模型 + 凭据统一 + 控制台账号管理（本次）

| 文件 | 改动 |
| --- | --- |
| `src/planrouter/accounts.py`（新） | `Account` 校验工具、`resolve_creds`、凭据键校验、合并候选扫描 |
| `src/planrouter/core.py` | `Config.accounts`、`Account` dataclass、`parse_config` 解析与校验、`Config.account(id)` 查询 |
| `src/planrouter/server.py` | `public_config` 掩码、`merge_secrets` 回填、`apply_config` 引用校验、`_plan_creds_for` 重写 |
| `src/planrouter/freealign.py` | `_creds_for` 改调 `resolve_creds`，去重键带账号 |
| `src/planrouter/subscriptions.py` | `normalize` 增加 `account_id` / `business_line`，保留 `account` |
| `src/planrouter/providers.json` | 模式新增可选 `plan_api`（仅火山声明） |
| `src/planrouter/providers.py` | `INHERIT_IF_EMPTY` 增加 `plan_api` |
| `src/planrouter/static/app.js` / `style.css` | 厂家页账号列表、渠道编辑器「所属账号」、订阅表单账号 / 业务线、合并动作 |
| `tests/` | 见 §十三 |

请求转发路径零改动，风险集中在配置读写与 UI。

### 阶段 1：余量与展示按账号聚合

- `vendorquota.json` 键改 `account_id:model`（兼容读旧键）。
- `GET /api/subscriptions` 返回按账号分组的余量卡片数据。
- `freealign` 报表与 `quota` 字段带账号归属。

### 阶段 2：团队协作（按需）

- `accounts[].members` 与角色，账号成为可共享资源；此时才需要强实体与权限模型。

---

## 十二、风险

| 风险 | 缓解 |
| --- | --- |
| `***` 掩码被写回磁盘，真实凭据丢失 | `merge_secrets` 覆盖 `accounts[].credentials` 并有单测；保存前后比对快照 |
| 账号与渠道 provider 不一致导致查错账号的余量 | `parse_config` 强校验 `channel.provider == account.provider` |
| 旧订阅的 `account` 字符串与新 label 不匹配 | 仅降级展示，不报错；表单提示可手动绑定 |
| 订阅文件与配置文件的悬空引用 | 保存订阅时校验并回告警，不阻断 |
| 阶段 0 改动面波及配置保存 | 所有改动经 `apply_config` 单点；补 config 往返 e2e 用例 |

---

## 十三、测试计划

- **单元**：`Account` 校验（非法 id、缺 provider、未知厂家、凭据键越界）；
  `resolve_creds` 四层优先级（含无账号时与旧链路等价）；
  `subscriptions.normalize` 新字段默认值与非法值。
- **配置往返**：`public_config` 掩码 → 控制台保存 → `merge_secrets` 回填 → 磁盘凭据不变（含
  `channel.creds`、`config.credentials`、`accounts[].credentials` 三处同时存在的情况）。
- **兼容**：无 `accounts` 的旧 `router.json` 加载、保存、路由行为完全不变；
  旧 `vendorquota.json` 可读。
- **功能**：同账号三渠道共享 AK/SK 时 `_plan_creds_for` 结果一致；无渠道的订阅（先订阅后配渠道）
  能查到余量；`freealign` 对同账号多渠道只执行一次命令。
- **回归**：全量 `pytest`（当前基线 712 passed）。
- **浏览器**：厂家页建账号 → 渠道绑定账号 → 订阅绑定账号 + 业务线 → 余量卡片可见；
  未建账号时新建 OpenRouter 渠道流程与改动前一致。

---

## 十四、已否决的方案

| 方案 | 否决原因 |
| --- | --- |
| 完整三层 `Account → BusinessLine → Subscription` 强实体 | 90% 厂家只有一个 Key，会造空实体、抬高配置成本；业务线已由 `mode` 表达 |
| `Account` 设为渠道必填 | OpenRouter / 匿名站用户被迫建无意义账号，破坏现有配置 |
| 把数据面 `api_key` 放进 `Account` | 数据面 Key 属于业务线与端点，一个账号下多条业务线 Key 不同；混放会让渠道无法独立启停 |
| 自动迁移旧 `channel.creds` 到账号 | 自动改盘风险高（可能改错分组），改为用户显式点击合并 |

---

## 十五、待评审问题

1. 账号 `label` 与旧 `Subscription.account` 的自动匹配是否足够？还是强制用户重新绑定？
2. `plan_api` 是否只给火山声明，其余厂家等有真实查询接口再逐个加？
3. 阶段 0 是否要一并做「合并为账号」按钮，还是先只做手工新建？
