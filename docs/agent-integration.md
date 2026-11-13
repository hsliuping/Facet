# Facet · Agent / SDK 接入指南

## 1. 一句话概述

Facet 是一个 OpenAI/Anthropic 兼容的 LLM 网关。所有对外接口均兼容 OpenAI Chat Completions 格式，**只需在请求 `Authorization: Bearer <gateway_key>`** 即可访问。网关内部会根据配置在多个上游厂家（OpenAI、Z.ai、火山引擎、百炼、DeepSeek、Kimi、魔搭、腾讯混元等）之间自动负载均衡、轮换、冷却与回退。

> 本文档主要针对下游 Agent、SDK 或脚本直接调用 Facet 网关；若需要在 Trae IDE 内部使用请参考章节 7。

## 2. 认证

- **网关密钥**：启动 Facet 后，控制台首次访问需携带 `Authorization: Bearer <gateway_key>` 的 Header。
- 获取方式：`curl -H "Authorization: Bearer <key>" http://127.0.0.1:8787/v1/models` → `200 OK`。
- 若未配置 `listen.api_key`，网关将**开放匿名访问**（任何人都能调用，生产环境请务必设置密钥）。
- 本地开发可省略 Header，但在生产环境中务必始终带上。

## 3. 核心端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/v1/models` | **列出所有可用模型**（附协议、上游映射、免费额度），用于首次连通性探测或模型选择。 |
| `POST` | `/v1/chat/completions` | **主要接口**（OpenAI 兼容），使用 `messages` 格式。<br>> 推荐使用：`model` 填入 **品牌模型名**（如 `facet-auto`、`facet-cheap`、`facet-free`）或具体上游模型名。 |
| `POST` | `/v1/messages` | 同 `/v1/chat/completions`，兼容 Anthropic `messages` 格式的请求。 |
| `POST` | `/v1/completions` | 遗留接口，用于 `/v1/completions` 形状的请求（会自动转为 chat 形式）。 |
| `POST` | `/v1/embeddings` | Embeddings 接口，OpenAI 兼容格式。 |
| `POST` | `/v1/rerank` | Reranking 接口，OpenAI 兼容。 |
| `POST` | `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/audio/translations` | TTS / STT 接口。 |
| `POST` | `/v1/responses` | 新一代 OpenAI Responses API（实验性）。 |
| `GET` | `/health` | 健康检查。返回 `{"status":"ok"}` 或 `{"status":"degraded", "reason":"...}"`。 |

## 4. 请求与响应示例

### 4.1 列出模型

```bash
curl -s http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer ${GATEWAY_KEY}" | jq '.data[] | {name, protocol, tier, free_quota}'
```

### 4.2 主流聊天请求

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${GATEWAY_KEY}" \
  -d '{
    "model": "facet-free",
    "messages": [{"role": "user", "content": "Hello"}],
    "max_tokens": 1024,
    "temperature": 0.7
  }'
```

**响应**（OpenAI 兼容）：

```json
{
  "id": "cmpl-abc123",
  "object": "chat.completion",
  "created": 1731620008,
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "Hi there!"}, "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
}
```

> **模型名称约定**：
> - `facet-auto` → 走 `default` 路由（手动配置的链序，当前 priority 40 免费渠道）
> - `facet-cheap` → 走 `cheap` 路由（性价比渠道，带 `payg` 模式）
> - `facet-free` → 走 `free` 路由（内置的 `tier=="free"` 渠道，自动轮换免费模型）
> - 具体上游模型名（如 `qwen-turbo`、`gpt-4o` 等）直接映射至对应厂家的 `models.auto` 字段

## 5. 常用配合指令（在 Trae / IDE 内部）

| 指令 | 说明 |
|------|------|
| `/scheduler` | **自动调度**：根据模型特性和当前负载，在所有可用厂家/渠道中挑最合适的一个返回。 |
| `/route <模型名>` | **手动指定路由**：直接把请求路由至指定的厂家渠道（相当于在配置里写死路由名）。 |
| `/free-quota` | **查询当前可用免费模型**：返回所有状态为 `usable` / `low_quota` 的 `tier=="free"` 渠道模型列表（Facet 每日自动对齐厂家额度）。 |
| `/billing` | **查看计费模式**：返回当前渠道的 `mode`（`payg` / `token_plan` / `coding_plan` / `agent_plan`）。 |

## 6. 错误与兜底

| 状态码 | 说明 | 推荐处理 |
|--------|------|----------|
| `200` | 正常 | 按照常规流程使用。 |
| `401` | 未授权 / Key 无效 | 检查 `Authorization` Header 是否带上，或在控制台重置 Key。 |
| `429` | 单一渠道配额耗尽或冷却中 | **不要直接重试同一渠道**，应交给 `/scheduler` 让网关自动轮到下一个可用渠道。 |
| `502 / 503` | 上游服务不可达 | 网关会自动 failover 到同一链里的下一个可用渠道。 |
| `529` | 配额已用尽（厂家侧） | Channel 冷却后自动切换；若全链路不可用，`/scheduler` 会返回 `no available channel`。 |

## 7. 在 Trae / IDE 里使用 Facet

1. **启动**：确保 `facet-planrouter` 服务已启动（`docker compose up -d` 或者本地 `python -m planrouter.server`）。
2. **快速开始**：
   - 在 Trae / IDE 调出 Facet 侧边栏（或命令面板：`PlanRouter: Open`）。
   - 弹出的 **新手向导**（8 步）引导设置保护键、选择厂家、填入 Key，并自动测试连通性。
   - 向导完成后，配置写入 `~/.facet/router.json`，此后可直接在聊天中使用 `/scheduler`、`/route` 等指令。
3. **修改配置**：如需修改，可通过右上角「快速开始」重新打开向导，或直接编辑 `~/.facet/router.json`。

## 8. 附：完整 HTTP 规范（供 SDK 实现）

```
POST /v1/chat/completions
Headers:
  Authorization: Bearer <gateway_key>      (推荐始终带上)
  Content-Type: application/json
Body (JSON):
{
  "model": "facet-free",   // 或具体模型名、facet-auto/facet-cheap
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Say hello"}
  ],
  "max_tokens": 1024,
  "temperature": 0.7,
  "stream": false    // 或 true，返回 data: 格式的 SSE 流
}

Response 200:
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1234567890,
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "Hello!"},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
}

Error Response:
{
  "error": {"message": "Invalid API key", "type": "invalid_request_error", "param": null, "code": "invalid_api_key"}
}
```