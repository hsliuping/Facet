# 渠道厂家强关联校验 + Docker 预设种子

日期：2026-11-09。原则：先设计，后开发。

## 背景（两个问题）

1. **Docker 容器首启丢预设**。普通运行（`facet serve` 无配置）首次启动会由
   [seed.py](src/facet/gateway/seed.py) 生成带厂家标注的预设渠道
   （pollinations / nvidia-nim / google-ai-studio / z-ai-glm / anthropic-template，均含
   `provider` + `mode`）。而 [docker/entrypoint.py](../docker/entrypoint.py) 首启拷贝的是
   `router.example.json` —— 一个指向 mock 上游的联调测试配置，完全没有厂家预设。
   容器里看起来就是「厂家信息都丢了」。
   （注：厂家注册表本体 `providers.json` 已随 wheel 打包，重建镜像即可恢复「厂家」页数据；
   这里丢的是种子配置里的预设渠道。）

2. **渠道创建缺少厂家强关联判断**。`cd527d1` 只做了三件事：种子渠道打标、旧编辑页
   保存时继承 provider/mode、编辑器 UI 前端必选校验。后端 `POST /api/config`
   （[server.py](src/facet/gateway/server.py) `apply_config`）从不校验厂家：
   不选厂家、或写一个不存在的厂家/模式，都能保存成功。

## 设计

### 一、厂家强关联校验（后端）

校验发生在 `App.apply_config`（所有配置保存的必经点：控制台保存、`/api/reload`、
维护/freealign/应用映射等），校验依据是当前厂家注册表（builtin + overlay）：

| 规则 | 说明 |
| --- | --- |
| 新渠道必须带厂家 | 渠道名不在旧配置（`self.raw`）中 → `provider` + `mode` 必填 |
| 成对出现 | 只给 `provider` 或只给 `mode` → 拒绝（厂家与模式必须同时标注） |
| 厂家必须存在 | `provider` 必须在注册表中（含用户 overlay），否则拒绝 |
| 模式必须匹配 | `mode` 必须是该厂家的合法模式键，否则拒绝 |
| 旧渠道宽容 | 已存在的渠道若无厂家（历史遗留），启动加载与维护任务不受影响；控制台编辑时前端已强制补选 |
| 仅限文件部署 | 校验只在有 `config_path` 的部署（磁盘上有前一次配置可作 legacy 依据）生效；无文件的内嵌/测试用法不强制 |
| 空值视同未填 | `merge_secrets` 把 `""` 同「未提供」处理：旧渠道被旧标签页整页保存时厂家不被清空（继承旧值） |

含义：**创建即强关联**；历史遗留渠道不会被突然拒载，直到用户在控制台编辑它时补选厂家。
错误消息中文并指名渠道与修复方法。

不校验的路径：启动加载（`parse_config`，位于 cli → App 构造之前，保持宽容）；
`/api/test/channels` 草稿探测（只是探针，不入配置）。

### 二、Docker 首启种子对齐普通运行

`entrypoint.py` 首启（`/data/router.json` 不存在时）默认改为调用
`planrouter.seed.build_seed()` 生成预设配置，再做容器化改写：

- `listen.host = "0.0.0.0"`（容器外可达）、`port = 8787`
- `api_key = "env:ROUTER_API_KEY"`、`admin_key = "env:ROUTER_ADMIN_KEY"`
  （core 拒绝无密钥的非回环监听，与 example 模板语义一致）；环境变量缺失时
  entrypoint 把 `admin_key` 改为随机生成值并打印一次——网关没有默认管理密码，
  悬空的 `env:` 引用会锁死管理面

保留 `ROUTER_SEED=example` 环境变量开关：设为 `example` 时维持旧行为（拷贝
`router.example.json`），供 compose 冒烟栈（mock 上游）开箱使用。
`compose.yaml` 为 router 服务显式加 `ROUTER_SEED: "example"`，保证冒烟测试不变；
真实使用的用户删掉该行（及 `./data/router.json`）即可获得厂家预设种子。

已存在部署不受影响：配置文件是用户状态，从不静默覆写；已有容器要补预设渠道，可在
「厂家」页新建，或删掉 `./data/router.json` 后重建容器。

## 测试计划

- 新增 `tests/test_planrouter_vendor_binding.py`：
  新渠道缺厂家 / 厂家不存在 / 模式不匹配 / 单边标注 / 合法标注（pollinations/payg）
  / 旧渠道宽容 / 旧标签页继承空值 / seed 配置整体过校验。
- entrypoint 种子：加载 `docker/entrypoint.py` 调用种子函数，断言 listen 改写正确且
  能通过 `parse_config`。
- 全量 `unittest discover tests` 回归（550+）。
