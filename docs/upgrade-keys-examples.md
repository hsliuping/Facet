# 升级后密钥配置示例（`listen.admin_key`）

移除内置默认密码 `admin` 后，管理面凭据按以下三态工作。本文给出每种场景
可直接复制的配置写法。升级步骤见 README「认证」小节的「升级：管理密钥改为
随机生成」。

| 配置状态 | 管理接口行为 |
|---|---|
| `listen.admin_key` 已设置 | 只认这个密钥 |
| `api_key`、`admin_key` 都未设置 | 控制台开放（默认仅监听 `127.0.0.1`） |
| `api_key` 已设置、`admin_key` 未设置 | **锁定**，管理接口一律返回 401 |

先判断当前状态：

```bash
facet serve --check
# WARNING: listen.admin_key is unset while listen.api_key is set — the
#   management API is locked; set admin_key in router.json
```

## 场景一：裸部署（`facet serve`）——写进 router.json

### 写法 A：明文密钥（最直观，改动最小）

改前：

```json
{
  "listen": { "host": "127.0.0.1", "port": 8787, "api_key": "gw-secret" }
}
```

改后：

```json
{
  "listen": {
    "host": "127.0.0.1",
    "port": 8787,
    "api_key": "gw-secret",
    "admin_key": "My-Console-Pa55w0rd"
  }
}
```

### 写法 B：`env:` 引用（推荐，密钥不落盘）

```json
{
  "listen": {
    "host": "127.0.0.1",
    "port": 8787,
    "api_key": "gw-secret",
    "admin_key": "env:ROUTER_ADMIN_KEY"
  }
}
```

启动前设置环境变量：

```bash
# bash / zsh
export ROUTER_ADMIN_KEY='My-Console-Pa55w0rd'
facet serve --config router.json
```

```powershell
# PowerShell
$env:ROUTER_ADMIN_KEY = 'My-Console-Pa55w0rd'
facet serve --config router.json
```

### 写法 C：完全不设密钥（保持控制台开放）

`api_key` 与 `admin_key` 都不写即可，仅适用于回环监听的本机开发场景——
监听 `0.0.0.0` 等非回环地址必须配 `api_key`，因此非回环部署不存在这种状态。

## 场景二：Docker compose 升级

在 `compose.yaml` 旁的 `.env` 文件里设置（`.env` 已 gitignore）：

```dotenv
# 调用网关的成员密钥（已有默认，可不改）
ROUTER_API_KEY=gw-secret

# 管理面密钥——必须显式设置，否则旧配置依赖的 admin 已被移除，管理接口锁定
ROUTER_ADMIN_KEY=My-Console-Pa55w0rd
```

然后重建容器：

```bash
docker compose up -d
```

如果不想用 `.env`，也可以直接改 `./data/router.json` 的 `listen.admin_key`
（写明文或 `env:ROUTER_ADMIN_KEY` 再配合 compose 的 `environment` 透传），
二选一即可。

## 场景三：全新部署（无 `./data/router.json`）

不用任何配置：entrypoint / 种子化流程会自动生成随机密钥并打印一次。

```bash
# 裸部署
facet serve
#   管理控制台密钥（listen.admin_key，仅此处打印一次）：sk-admin-<32位十六进制>
#   打开管理控制台时填入上述密钥；建议登录后在「管理密钥」中换成自己的值

# Docker
docker compose logs router | grep 管理控制台密钥
```

浏览器打开控制台，右上角「管理密钥」填入打印的密钥，之后可在该页面换成
自己的值。

## 验证配置是否生效

```bash
# 无凭据 / 错误密钥 → 401
curl -i http://127.0.0.1:8787/api/config
curl -i http://127.0.0.1:8787/api/config -H "X-Admin-Key: wrong-key"

# 正确管理密钥 → 200
curl -i http://127.0.0.1:8787/api/config -H "X-Admin-Key: My-Console-Pa55w0rd"
```

改 `router.json` 后的生效方式：

- 管理接口可鉴权时：`curl -X POST http://127.0.0.1:8787/api/reload -H "X-Admin-Key: <当前密钥>"`
- 处于锁定状态（401）时：只能重启 `facet serve` 或 `docker compose restart`
