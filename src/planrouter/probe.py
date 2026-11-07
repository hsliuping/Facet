"""One-shot upstream probes behind the console's test buttons.

Deliberately independent of App / StateStore: a probe must never change
cooldowns, usage counters, sticky sessions or the request audit log. It sends
one minimal non-streaming completion, so an unsaved channel or subscription
can be validated before it is applied.
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .core import (ANTHROPIC, OPENAI, PROTOCOLS, VERSIONED_BASE, open_with_proxy,
                   resolve_secret, upstream_path_for)

DEFAULT_TIMEOUT = 8.0
MAX_CHANNELS = 8
PING_PROMPT = "ping"
ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

STATUS_HINTS = {
    400: "请求被上游拒绝，检查模型名与参数是否受支持",
    401: "密钥无效或权限不足，检查 api_key / env 变量",
    403: "密钥无效或权限不足，检查 api_key / env 变量",
    404: "上游路径不存在，检查 base_url 与 upstream_path 是否重复版本段",
    429: "上游额度或限流，渠道可能仍在冷却",
}


def hint_for(status: int) -> str:
    if status in STATUS_HINTS:
        return STATUS_HINTS[status]
    if 500 <= status < 600:
        return "上游服务异常，稍后重试"
    return "上游返回非 2xx 状态"


def _mask_secret(value) -> str:
    """Recognisable, but never persisted in plaintext."""
    text = str(value or "")
    if not text:
        return "<empty>"
    if text == "***":
        return "***"
    if len(text) <= 8:
        return f"<{len(text)} chars>"
    return f"{text[:4]}…{text[-4:]} ({len(text)} chars)"


def describe_key(raw) -> dict:
    """Explain how a draft api_key was interpreted; never expose the secret."""
    text = str(raw.get("api_key") or "") if isinstance(raw, dict) else ""
    if not text or text == "***":
        return {"raw": "<empty>" if not text else "***", "source": "none",
                "resolved": False,
                "note": "密钥字段为空或仍是 ***：新增渠道请直接粘贴明文 API Key"}
    if text.startswith("env:"):
        var = text[4:]
        legal = bool(ENV_VAR_NAME.fullmatch(var))
        shown = var if legal else _mask_secret(var)
        present = bool(legal and var in os.environ)
        if not legal:
            note = ("env: 后面不是合法变量名，看起来是把明文 API Key 误加到了 env: 后面；"
                    "请去掉 env: 前缀，直接粘贴密钥")
        elif not present:
            note = f"容器里没有环境变量 {var}；需在 compose.yaml 的 environment 注入并重建容器"
        else:
            note = f"已从环境变量 {var} 读取"
        return {"raw": "env:" + shown, "source": "env-ref", "env_var": shown,
                "env_var_valid_name": legal, "env_var_present": present,
                "resolved": bool(resolve_secret(text)), "note": note}
    return {"raw": _mask_secret(text), "source": "literal", "resolved": True,
            "note": "已使用明文密钥"}


def _usable(value) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and text != "*" and not text.startswith("REPLACE_")


def pick_model(models, requested: str | None = None) -> str | None:
    """Resolve a concrete wire model from an exposed -> wire mapping."""
    if not isinstance(models, dict):
        return None
    if requested:
        mapped = models.get(requested)
        return mapped if _usable(mapped) else requested
    auto = models.get("auto")
    if _usable(auto):
        return auto
    for exposed, wire in models.items():
        if exposed in ("auto", "*") or not _usable(wire):
            continue
        return wire
    return None


def validate_channel(raw) -> dict:
    """Validate one draft channel; mirrors the single-channel rules in parse_config."""
    if not isinstance(raw, dict):
        raise ValueError("channel 必须是对象")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("渠道名称不能为空")
    protocol = raw.get("protocol") or OPENAI
    if protocol not in PROTOCOLS:
        raise ValueError(f"protocol 必须是 {PROTOCOLS} 之一")
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"渠道 {name}: base_url 必须是 http(s) URL")
    upstream_path = raw.get("upstream_path") or None
    if upstream_path is not None:
        if (not isinstance(upstream_path, str) or not upstream_path.startswith("/")
                or "://" in upstream_path):
            raise ValueError(f"渠道 {name}: upstream_path 必须是以 / 开头的路径")
        upstream_path = upstream_path.rstrip("/") or "/"
    models = raw.get("models") or {}
    if not isinstance(models, dict) or not models:
        raise ValueError(f"渠道 {name}: 至少配置一条模型映射")
    api_key = raw.get("api_key")
    if api_key in (None, "", "***"):
        api_key = None
    proxy = resolve_secret(str(raw.get("proxy") or "").strip()) or ""
    if proxy and not proxy.lower().startswith(("http://", "https://")):
        raise ValueError(f"渠道 {name}: 代理必须是 http(s):// 地址")
    return {
        "name": name,
        "protocol": protocol,
        "base_url": base_url,
        "api_key": resolve_secret(api_key),
        "models": dict(models),
        "upstream_path": upstream_path,
        "proxy": proxy,
    }


def _reply_excerpt(raw: bytes, protocol: str) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace").strip()[:120]
    if protocol == OPENAI:
        choices = data.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        text = (first.get("message") or {}).get("content") or ""
    else:
        blocks = data.get("content") or []
        first = blocks[0] if blocks and isinstance(blocks[0], dict) else {}
        text = first.get("text") or ""
    return str(text).strip()[:120]


def _error_excerpt(raw: bytes) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace").strip()[:300] or "上游未返回可读错误"
    if not isinstance(data, dict):
        return str(data)[:300]
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err or data.get("message") or data)[:300]


def probe_channel(raw, model: str | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Send one minimal completion to a draft channel and report the outcome."""
    fallback_name = str(raw.get("name") or "") if isinstance(raw, dict) else ""
    try:
        channel = validate_channel(raw)
    except ValueError as exc:
        return {"ok": False, "name": fallback_name, "error": str(exc),
                "hint": "请先补齐渠道配置"}

    name = channel["name"]
    wire = pick_model(channel["models"], model)
    if not wire:
        return {"ok": False, "name": name,
                "error": "没有可用的测试模型（auto 映射为 * 或仍是占位值）",
                "hint": "在「测试模型」里填一个该渠道支持的模型名"}

    protocol = channel["protocol"]
    path = upstream_path_for(channel["base_url"], channel["upstream_path"], protocol)
    url = channel["base_url"] + path

    payload = {"model": wire, "messages": [{"role": "user", "content": PING_PROMPT}],
               "max_tokens": 16}
    if protocol == OPENAI:
        payload["stream"] = False
    headers = {"Content-Type": "application/json"}
    auth = "none"
    if channel["api_key"]:
        if protocol == OPENAI:
            headers["Authorization"] = "Bearer " + channel["api_key"]
            auth = "bearer"
        else:
            headers["x-api-key"] = channel["api_key"]
            auth = "x-api-key"
    if protocol == ANTHROPIC:
        headers["anthropic-version"] = "2023-06-01"

    safe_headers = dict(headers)
    if "Authorization" in safe_headers:
        safe_headers["Authorization"] = "Bearer " + _mask_secret(channel["api_key"])
    if "x-api-key" in safe_headers:
        safe_headers["x-api-key"] = _mask_secret(channel["api_key"])

    result = {
        "ok": False, "name": name, "model": wire, "path": path,
        "protocol": protocol, "url": url, "auth": auth,
        "key": describe_key(raw),
        "debug": {
            "request": {"method": "POST", "url": url,
                        "headers": safe_headers, "body": dict(payload)},
            "response": None,
        },
    }
    started = time.monotonic()
    try:
        req = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers=headers, method="POST")
        with open_with_proxy(req, timeout, channel.get("proxy") or "") as resp:
            body = resp.read(8192)
            reply = _reply_excerpt(body, protocol)
            result.update(ok=True, http_status=resp.status,
                          latency_ms=round((time.monotonic() - started) * 1000),
                          reply=reply)
            result["debug"]["response"] = {"http_status": resp.status, "body": reply}
    except HTTPError as exc:
        text = _error_excerpt(exc.read(4096))
        hint = hint_for(exc.code)
        if exc.code in (401, 403):
            hint = ("请求没有携带密钥：在「密钥」字段填明文 API Key；"
                    "若写 env:VAR，需把该变量注入容器环境变量"
                    if auth == "none" else
                    "密钥已发送但被上游拒绝：确认 API Key 正确、未过期，且对该模型有权限")
        elif exc.code == 404 and re.search(r"/v\d+/v\d+/", url):
            hint = ("URL 里出现了重复版本段（如 /v3/v1/）：清空「上游路径」让系统自动选择，"
                    "或只填 /chat/completions")
        result.update(http_status=exc.code,
                      latency_ms=round((time.monotonic() - started) * 1000),
                      error=text, hint=hint)
        result["debug"]["response"] = {"http_status": exc.code, "body": text[:600]}
    except (URLError, TimeoutError, OSError) as exc:
        result.update(latency_ms=round((time.monotonic() - started) * 1000),
                      error=str(exc)[:200],
                      hint="无法连接或超时，检查 base_url、网络与代理")
        result["debug"]["response"] = {"transport_error": str(exc)[:200]}
    except Exception as exc:  # keep the console alive on unexpected upstream shapes
        result.update(latency_ms=round((time.monotonic() - started) * 1000),
                      error=repr(exc)[:200], hint="探测失败")
        result["debug"]["response"] = {"exception": repr(exc)[:200]}
    return result


def models_path_for(base_url: str, upstream_path: str | None) -> str:
    """Path of the upstream model listing (GET) for a channel."""
    if upstream_path:
        stem = re.sub(r"/(?:chat/completions|messages|responses)$", "", upstream_path)
        return stem + "/models"
    if VERSIONED_BASE.search(base_url.rstrip("/")):
        return "/models"
    return "/v1/models"


def parse_model_list(payload) -> list[str]:
    """Extract model ids from the many shapes /models endpoints return."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
        if not isinstance(items, list):
            return []
    else:
        return []
    ids = set()
    for item in items:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model")
            if isinstance(name, str) and name:
                ids.add(name)
    return sorted(ids)


def probe_models(raw, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Fetch one channel's upstream model catalog (GET .../models).

    Read-only: sends no completion, touches no cooldown/usage/audit state.
    """
    fallback_name = str(raw.get("name") or "") if isinstance(raw, dict) else ""
    try:
        channel = validate_channel(raw)
    except ValueError as exc:
        return {"ok": False, "name": fallback_name, "error": str(exc),
                "hint": "请先补齐渠道配置"}

    name = channel["name"]
    protocol = channel["protocol"]
    path = models_path_for(channel["base_url"], channel["upstream_path"])
    url = channel["base_url"] + path

    headers = {}
    if channel["api_key"]:
        if protocol == OPENAI:
            headers["Authorization"] = "Bearer " + channel["api_key"]
        else:
            headers["x-api-key"] = channel["api_key"]
    if protocol == ANTHROPIC:
        headers["anthropic-version"] = "2023-06-01"

    started = time.monotonic()
    try:
        req = Request(url, headers=headers, method="GET")
        with open_with_proxy(req, timeout, channel.get("proxy") or "") as resp:
            models = parse_model_list(json.loads(resp.read(2_000_000)))
            return {"ok": True, "name": name, "endpoint": url, "path": path,
                    "http_status": resp.status,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                    "models": models, "count": len(models)}
    except HTTPError as exc:
        text = _error_excerpt(exc.read(4096))
        return {"ok": False, "name": name, "endpoint": url, "path": path,
                "http_status": exc.code,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": text, "hint": hint_for(exc.code)}
    except (URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "name": name, "endpoint": url, "path": path,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": str(exc)[:200],
                "hint": "无法连接或超时，检查 base_url、网络与代理"}
    except Exception as exc:  # keep the console alive on unexpected upstream shapes
        return {"ok": False, "name": name, "endpoint": url, "path": path,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "error": repr(exc)[:200],
                "hint": "上游响应不是可解析的模型清单"}
