# 容器化部署与正式试用运行态

日期：2026-11-09。原则：先设计，后开发。本文档记录网关容器化部署的产品与开发设计、
打包链上发现并修复的三个问题，以及正式试用的运行态与监控方案（均已实施并验证）。

## 背景（为什么容器化）

1. **试用要求网关可持续服务**。「正式试用 → 打 tag 发版」的前置是把日常真实工具指向
   网关跑一段实际工作（见 [PROGRESS.md](PROGRESS.md)），这要求网关脱离单次会话生存、
   可长期驻留。
2. **宿主常驻进程不可靠**。实测两种拉起方式（命令运行器 `Start-Process` / WMI 脱离进程
   树）都存在进程树被连带回收的风险，且与开发工作区共享 Python 环境、端口等资源，
   稳定性不可控。
3. **决策**：把项目打包进容器，用独立环境跑试用——依赖隔离、状态集中挂载、
   `restart: unless-stopped` 兜底，宿主只通过端口映射访问。

## 产品设计

### 部署形态

| 项 | 设计 |
| --- | --- |
| 运行单元 | compose 两服务栈（[compose.yaml](../compose.yaml)）：`router`（网关）+ `mock`（假上游，冒烟联调用，真实使用可忽略） |
| 端口 | 宿主 `${ROUTER_PORT:-8787}` → 容器 8787 |
| 密钥 | `ROUTER_API_KEY` / `ROUTER_ADMIN_KEY` 经 compose 旁 `.env` 文件（gitignored）注入，不进镜像、不进配置文件；配置里用 `env:` 引用；无 `.env` 时网关 key 回落 compose 默认 test key（冒烟栈用），管理密钥由 entrypoint 随机生成并在首启日志打印一次（网关没有默认管理密码） |
| 状态 | `./data` bind mount 到 `/data`：router.json 配置、用量、审计日志、订阅、维护日志全部持久化，容器可随时重建 |
| 重启策略 | `restart: unless-stopped`，宿主重启后自动恢复服务 |
| 健康检查 | 打容器内控制台 `/`（免认证）做纯 liveness；**不打 `/health`**——全渠道冷却时 `/health` 返回 503，会把健康进程误杀重启 |

### 非目标

- 不做镜像发布流水线 / CI 构建，镜像由本地 `docker compose build` 产出
- 不做编排（K8s）、多实例、水平扩展——单机单栈
- 不做 per-user 隔离（团队版方向见 PROGRESS.md「产品方向 · 后续发展」）

### 安全边界

- `.env`（真实密钥）与 `data/`（含密钥的实配）均 gitignored，绝不入库
- [`.dockerignore`](../.dockerignore) 明确排除全部本地 runtime 数据
  （router.json、usage.json、requests.jsonl 等），不烘焙进镜像
- 容器内以非 root 用户（planrouter）运行

## 开发设计

### 镜像构建（[Dockerfile](../Dockerfile)，多阶段）

1. **builder**：`python:3.12-slim`（`BASE_IMAGE` ARG 可指本地缓存镜像离线构建），先跑
   [tools/build_package.py](../tools/build_package.py) 生成 `facet/_version.py` 与
   model-registry（clean checkout 上 wheel 构建的硬前置），再
   `pip wheel --no-build-isolation --no-deps`
2. **runtime**：建非 root 用户、装 wheel、COPY 容器辅助脚本
   （entrypoint / mock_upstream / 模板配置 / smoke_test）、HEALTHCHECK
3. 应用本体纯 stdlib 零依赖，镜像里除 setuptools 构建残留外无多余物

### entrypoint 约定（[docker/entrypoint.py](../docker/entrypoint.py)）

- `/data/router.json` 不存在时首启播种（当前从 `router.example.json` 模板拷贝，即冒烟
  联调配置；与普通运行种子对齐的设计见
  [planrouter-vendor-binding-and-docker-seed.md](planrouter-vendor-binding-and-docker-seed.md)，
  尚未实施）
- 然后 `exec planrouter --config /data/router.json`；所有 runtime 文件
  （state / usage / subscriptions / notifications / 审计日志）由配置路径派生 →
  **单一 /data 卷持久化全部状态**

### 打包链三问题与修复（本次实施记录）

| # | 问题 | 根因 | 修复 |
| --- | --- | --- | --- |
| 1 | 容器 crash loop：`FileNotFoundError .../planrouter/providers.json` | 内置厂家注册表 [src/planrouter/providers.json](../src/planrouter/providers.json) 被宽泛的 `providers.json` gitignore 规则覆盖，且 pyproject package-data 未包含 → wheel 缺文件 | [pyproject.toml](../pyproject.toml) package-data 增 `planrouter = ["static/*", "providers.json"]`，重建镜像 |
| 2 | 首启播种路径不存在 | entrypoint 写的模板名（router.template.json）与 Dockerfile COPY 目标（router.example.json）不一致 | entrypoint 对齐为 `/opt/planrouter/router.example.json` |
| 3 | 宿主访问容器 HTTP 全部空响应（TCP 可握手、HTTP 无数据） | `data/router.json` 沿用宿主配置 `listen.host: "127.0.0.1"`——在容器内只绑容器回环，端口发布成死路 | 容器实配改 `listen.host: "0.0.0.0"`（密钥仍 `env:` 引用，安全性不变） |

问题 3 的定位方法论（值得复用）：现象是「TCP 通、HTTP 无数据」，逐层隔离——宿主
curl -4/-6、去代理、Test-NetConnection、WSL 侧、容器内自打、VM 内——最后锁定为绑定地址。

## 验证与运行态（2026-11-09 实测）

- **端到端**：宿主 `http://127.0.0.1:8787/health` 200，available 六渠道（free-tier、
  魔搭免费、百炼免费、nvidia-nim、deepseek、火山引擎免费）；facet-auto 试探请求 200，
  响应 `trial-ok`，首跳直达 free-tier
  （`X-Routed-Via: free-tier/nvidia/nemotron-3.5-lightning:free`，`X-Failover-Count: 0`）
- **运行态**：`facet-planrouter-router-1` / `facet-planrouter-mock-1` 均 healthy；
  `data/` 延续宿主时期的用量与状态（free-tier 56 请求 / 47 成功 / 约 24 万 tokens；
  codex-plan、glm-plan 禁用后请求计数冻结，不再被白撞）

## 监控（试用期间）

每小时整点定时巡检（计划任务「PlanRouter 试用期指标监控」）：

1. 读 User 级 `ROUTER_API_KEY`（sandbox shell 不继承 User 环境变量，须
   `[Environment]::GetEnvironmentVariable(...,'User')` 显式读取），调 `/health` 与 `/stats`
2. `docker ps` 检查两容器状态与 health 字段
3. 快照追加一行 JSON 到 `D:\Temp\facet-trial\metrics.jsonl`（中间产物，不入工作区）

告警条件：health 非 200 / status 不是 ok；available 为空或较上次快照骤减；容器
unhealthy / exited / 缺失；失败请求较上次快照激增。报告策略：正常一句话带过；异常详报
异常项、与上次快照的数字对比、冷却渠道与预计恢复、处理建议（如 `docker compose restart`）。

## 运维要点

- **容器内访问宿主服务必须用 `host.docker.internal`**：当前 claude-plan / codex-plan
  桥接渠道指向 `127.0.0.1:8080/8081`，容器内即容器自身（现 disabled 无影响）；
  恢复启用前必须改，否则必然失败
- **改配置**：编辑 `./data/router.json` 后 POST `/api/reload`（admin key）或控制台保存，
  无需重建容器
- **常用命令**（本机 docker 需非沙箱执行；Docker Desktop 用 WMI
  `Win32_Process.Create` 拉起以脱离命令进程树）：

  ```
  docker compose build           # 代码变更后重建镜像
  docker compose up -d           # 启动 / 更新栈
  docker compose restart router  # 重启网关
  docker compose logs -f router  # 跟日志
  ```

- 试用通过后定版本号打 tag（发版前置项，见 PROGRESS.md「接下来」）
