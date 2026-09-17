#!/usr/bin/env python3
"""Verify a model's declared capabilities against live API behavior.

The registry table records what upstream sources CLAIM (tool_call,
structured_output, reasoning, modalities). This tool calls the model for real
and checks whether the claim holds. It never writes back to the table: the
registry stays a pure "upstream claims" aggregation, and verification results
land in an independent report (reports/verify-results.json) for humans to
review — e.g. as evidence in an issue or a manual-overrides.json PR.

Probes (tiny requests, a few hundred tokens each):
    tools       declared tool_call   -> forced tool call actually returns one
    structured  declared structured_output -> JSON comes back and parses
    reasoning   declared reasoning   -> reasoning traces observable anywhere
    image       declared image input -> 1x1 PNG is described correctly

Verdicts:
    match         claim holds (or an explicit rejection corroborates a
                  negative claim)
    mismatch      claim contradicted by positive evidence (declared true but
                  capability explicitly rejected, or declared false but
                  capability observed working)
    discovery     capability works although the table declares nothing
                  (absent == unknown)
    inconclusive  nothing could be proven: auth/billing/network problems,
                  rate limits, 5xx, swallowed parameters, hidden traces...

API keys are yours: resolved from --api-key, --env-key, or the conventional
environment variable(s) of the provider. The tool never stores keys.

Protocol coverage: OpenAI-compatible endpoints (most providers), plus native
Anthropic and Google APIs. Anything else: pass --base-url --api-key --protocol.

Usage:
    python tools/verify_claims.py gpt-5 zhipuai/glm-5.3
    python tools/verify_claims.py --provider zhipuai
    python tools/verify_claims.py --probes tools,structured --max-models 5
    python tools/verify_claims.py --base-url http://localhost:1234/v1 \
        --api-key sk-... --protocol openai my-local-model
    python tools/verify_claims.py gpt-5 --dry-run     # plan only, no network
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TABLE = Path(__file__).resolve().parent.parent / "registry" / "model-registry.json"
DEFAULT_REPORT = Path("reports") / "verify-results.json"
GENERATOR = "verify_claims@0.1.0"
TIMEOUT = 120  # seconds; reasoning models can be slow to first byte

ALL_PROBES = ("tools", "structured", "reasoning", "image")

# 1x1 transparent PNG (70 bytes). The image probe asks for its pixel width:
# a gateway that silently drops the image cannot guess "1".
PNG_1PX_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8"
    "AAAAASUVORK5CYII="
)

PING_PROMPT = "Reply with the single word: OK"
WEATHER_PROMPT = "What is the weather in Paris?"
STRUCTURED_PROMPT = (
    'Output a JSON object with exactly one key "ok" whose value is true. Reply with JSON only.'
)
REASONING_PROMPT = "What is 17 * 23? Reason step by step, then give the final number."
IMAGE_PROMPT = "How many pixels wide is this image? Answer with the number only."

# Built-in endpoints for first-party providers. env is an ordered list of
# conventional variable names (models.dev's `env` convention); the first one
# set in the environment wins. Protocol-special vendors only: openai
# (self), anthropic, google. Everything else here speaks the OpenAI wire
# format. Providers not listed need explicit --base-url --api-key.
PROVIDER_MAP = {
    "openai": {"base_url": "https://api.openai.com/v1", "env": ["OPENAI_API_KEY"], "protocol": "openai"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "env": ["ANTHROPIC_API_KEY"], "protocol": "anthropic"},
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"],
        "protocol": "google",
    },
    "xai": {"base_url": "https://api.x.ai/v1", "env": ["XAI_API_KEY", "GROK_API_KEY"], "protocol": "openai"},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "env": ["MISTRAL_API_KEY"], "protocol": "openai"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "env": ["DEEPSEEK_API_KEY"], "protocol": "openai"},
    "zhipuai": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env": ["ZHIPU_API_KEY", "ZHIPUAI_API_KEY"],
        "protocol": "openai",
    },
    "volcengine": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "env": ["ARK_API_KEY"],
        "protocol": "openai",
    },
    "moonshotai": {"base_url": "https://api.moonshot.cn/v1", "env": ["MOONSHOT_API_KEY"], "protocol": "openai"},
    "alibaba": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env": ["DASHSCOPE_API_KEY"],
        "protocol": "openai",
    },
    "alibaba-cn": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env": ["DASHSCOPE_API_KEY"],
        "protocol": "openai",
    },
    "minimax": {"base_url": "https://api.minimax.io/v1", "env": ["MINIMAX_API_KEY"], "protocol": "openai"},
    "stepfun": {"base_url": "https://api.stepfun.com/v1", "env": ["STEPFUN_API_KEY"], "protocol": "openai"},
    "baidu": {
        "base_url": "https://qianfan.baidubce.com/v2",
        "env": ["QIANFAN_API_KEY", "BAIDU_API_KEY"],
        "protocol": "openai",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env": ["OPENROUTER_API_KEY"],
        "protocol": "openai",
    },
}

# Body keywords that tie a 4xx to the parameter under probe (vs an unrelated
# request-construction mistake, which must NOT incriminate the declaration).
PROBE_KEYWORDS = {
    "tools": ("tools", "tool", "function"),
    "structured": ("response_format", "json"),
    "reasoning": ("thinking", "reasoning"),
    "image": ("image", "multimodal", "vision", "modalit"),
}


# ---------------------------------------------------------------- HTTP layer

def http_json(url: str, headers: dict, payload: dict, timeout: int = TIMEOUT):
    """POST JSON, return (status, body). status 0 = network-level failure.

    Body is parsed JSON when possible, else the raw text. HTTPError bodies
    are returned instead of raised so the classifier can read vendor messages.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": f"network: {e}"}


def body_text(body) -> str:
    if isinstance(body, str):
        return body
    try:
        return json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(body)


def classify_error(status: int, text: str, keywords: tuple = ()) -> str:
    """Map a failed call to an error class. Ordering matters.

    capability_rejected (4xx mentioning the probed parameter or 'does not
    support') is the ONLY class allowed to produce a verdict on a claim; all
    others mean we learned nothing about the capability.
    """
    low = (text or "").lower()
    if status == 0:
        return "network"
    if any(m in low for m in ("invalid_api_key", "invalid api key", "unauthorized", "authentication")):
        return "auth_error"
    if status in (401, 403):
        return "auth_error"
    if status == 429:
        return "rate_limited"
    if status == 402 or "insufficient_quota" in low or "insufficient_balance" in low:
        return "billing"
    if status == 404 or ("model" in low and "not found" in low):
        return "model_unavailable"
    if status in (400, 422):
        if any(k in low for k in keywords) or "does not support" in low or "unsupported" in low:
            return "capability_rejected"
        return "request_error"
    if status >= 500:
        return "server_error"
    return "request_error"


def decide_verdict(declared, obs: dict) -> str:
    """Pure verdict function: declaration (True/False/None) x observation.

    Asymmetry by design: a positive claim needs positive evidence (or an
    explicit rejection to be convicted), while 'no signal' never convicts —
    vendors may swallow parameters or hide reasoning traces. A negative claim
    is considered held when nothing contradicts it.
    """
    kind = obs.get("kind")
    if kind == "protocol_unsupported":
        return "inconclusive"
    if kind == "error":
        if obs.get("error") == "capability_rejected":
            if declared is True:
                return "mismatch"
            if declared is False:
                return "match"
        return "inconclusive"
    if kind == "signal":
        if declared is True:
            return "match"
        if declared is False:
            return "mismatch"
        return "discovery"
    # no_signal with HTTP 200
    if declared is False:
        return "match"  # weak but non-contradicting evidence
    return "inconclusive"


def _observation(kind: str, detail: str, usage: dict | None = None) -> dict:
    return {"kind": kind, "detail": detail, "usage": usage or {}}


def _error_obs(status: int, body, keywords: tuple) -> dict:
    cls = classify_error(status, body_text(body), keywords)
    return {"kind": "error", "error": cls, "detail": body_text(body)[:200], "usage": {}}


def _parse_json_object(text):
    """Extract a JSON object from model text (tolerates ``` fences)."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    candidates = [t]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{"): text.rfind("}") + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ------------------------------------------------------------ protocol adapters

WEATHER_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}
WEATHER_TOOL_ANTHROPIC = {
    "name": "get_weather",
    "description": "Get current weather for a city",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}
WEATHER_TOOL_GOOGLE = {
    "functionDeclarations": [
        {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        }
    ]
}
STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


class OpenAICompat:
    protocol = "openai"

    def __init__(self, base_url: str, key: str, timeout: int = TIMEOUT):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {key}"}
        self.timeout = timeout

    def _post(self, payload: dict):
        status, body = http_json(self.url, self.headers, payload, self.timeout)
        if status == 429:  # single back-off retry: avoid misreading rate limits
            time.sleep(2)
            status, body = http_json(self.url, self.headers, payload, self.timeout)
        return status, body

    @staticmethod
    def extract_usage(body) -> dict:
        u = body.get("usage") or {}
        return {
            "prompt_tokens": u.get("prompt_tokens") or 0,
            "completion_tokens": u.get("completion_tokens") or 0,
        }

    @staticmethod
    def _message(body) -> dict:
        choices = body.get("choices") if isinstance(body, dict) else None
        return (choices or [{}])[0].get("message") or {}

    @staticmethod
    def _answer(body) -> str:
        content = OpenAICompat._message(body).get("content")
        return content if isinstance(content, str) else ""

    def ping(self, model: str):
        return self._post({"model": model, "messages": [{"role": "user", "content": PING_PROMPT}]})

    def probe_tools(self, model: str) -> dict:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": WEATHER_PROMPT}],
            "tools": [WEATHER_TOOL_OPENAI],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        }
        status, body = self._post(payload)
        if status == 200:
            if self._message(body).get("tool_calls"):
                return _observation("signal", "tool_calls returned", self.extract_usage(body))
            return _observation("no_signal", "200 without tool_calls", self.extract_usage(body))
        text = body_text(body)
        if "tool_choice" in text.lower():
            # some vendors reject the forced-choice form, not the capability
            payload.pop("tool_choice")
            status, body = self._post(payload)
            if status == 200:
                if self._message(body).get("tool_calls"):
                    return _observation("signal", "tool_calls returned (unforced)", self.extract_usage(body))
                return _observation("no_signal", "200 without tool_calls (unforced)", self.extract_usage(body))
        return _error_obs(status, body, PROBE_KEYWORDS["tools"])

    def probe_structured(self, model: str) -> dict:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": STRUCTURED_PROMPT}],
            "response_format": {"type": "json_object"},
        }
        status, body = self._post(payload)
        if status != 200:
            text = body_text(body)
            if classify_error(status, text, PROBE_KEYWORDS["structured"]) == "capability_rejected":
                # retry the narrower-but-wider-supported json_schema form
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "out", "strict": False, "schema": STRUCTURED_SCHEMA},
                }
                status, body = self._post(payload)
                if status != 200:
                    return _error_obs(status, body, PROBE_KEYWORDS["structured"])
            else:
                return _error_obs(status, body, PROBE_KEYWORDS["structured"])
        obj = _parse_json_object(self._answer(body))
        if obj and "ok" in obj:
            form = (payload.get("response_format") or {}).get("type", "json_object")
            return _observation("signal", f"valid JSON with 'ok' ({form})", self.extract_usage(body))
        return _observation("no_signal", "200 but content is not parseable JSON", self.extract_usage(body))

    def probe_reasoning(self, model: str) -> dict:
        payload = {"model": model, "messages": [{"role": "user", "content": REASONING_PROMPT}]}
        status, body = self._post(payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["reasoning"])
        msg = self._message(body)
        usage = self.extract_usage(body)
        if msg.get("reasoning_content"):
            return _observation("signal", "reasoning_content present", usage)
        if msg.get("reasoning"):
            return _observation("signal", "reasoning field present", usage)
        details = (body.get("usage") or {}).get("completion_tokens_details") or {}
        if isinstance(details.get("reasoning_tokens"), int) and details["reasoning_tokens"] > 0:
            return _observation("signal", f"reasoning_tokens={details['reasoning_tokens']}", usage)
        return _observation("no_signal", "no reasoning trace observable", usage)

    def probe_image(self, model: str) -> dict:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_1PX_B64}"}},
                    ],
                }
            ],
        }
        status, body = self._post(payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["image"])
        answer = self._answer(body)
        usage = self.extract_usage(body)
        if "1" in answer:
            return _observation("signal", f"answered: {answer[:60]!r}", usage)
        return _observation("no_signal", f"unexpected answer: {answer[:60]!r}", usage)


class AnthropicNative:
    protocol = "anthropic"

    def __init__(self, base_url: str, key: str, timeout: int = TIMEOUT):
        self.url = base_url.rstrip("/") + "/messages"
        self.headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        self.timeout = timeout

    def _post(self, payload: dict):
        status, body = http_json(self.url, self.headers, payload, self.timeout)
        if status == 429:
            time.sleep(2)
            status, body = http_json(self.url, self.headers, payload, self.timeout)
        return status, body

    @staticmethod
    def extract_usage(body) -> dict:
        u = body.get("usage") or {}
        return {
            "prompt_tokens": u.get("input_tokens") or 0,
            "completion_tokens": u.get("output_tokens") or 0,
        }

    @staticmethod
    def _text(body) -> str:
        blocks = body.get("content") if isinstance(body, dict) else None
        return "".join(b.get("text", "") for b in (blocks or []) if isinstance(b, dict))

    def ping(self, model: str):
        # anthropic messages API mandates max_tokens
        return self._post({"model": model, "max_tokens": 256, "messages": [{"role": "user", "content": PING_PROMPT}]})

    def probe_tools(self, model: str) -> dict:
        payload = {
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": WEATHER_PROMPT}],
            "tools": [WEATHER_TOOL_ANTHROPIC],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
        status, body = self._post(payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["tools"])
        blocks = body.get("content") or []
        usage = self.extract_usage(body)
        if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
            return _observation("signal", "tool_use block returned", usage)
        return _observation("no_signal", "200 without tool_use block", usage)

    def probe_structured(self, model: str) -> dict:
        # No native structured-output parameter in the messages API; faking it
        # with tools would conflate this probe with the tool_call probe.
        return _observation("protocol_unsupported", "anthropic messages API has no native structured output")

    def probe_reasoning(self, model: str) -> dict:
        payload = {
            "model": model,
            "max_tokens": 2048,  # must exceed the thinking budget
            "thinking": {"type": "enabled", "budget_tokens": 1024},
            "messages": [{"role": "user", "content": REASONING_PROMPT}],
        }
        status, body = self._post(payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["reasoning"])
        blocks = body.get("content") or []
        usage = self.extract_usage(body)
        if any(isinstance(b, dict) and b.get("type") == "thinking" for b in blocks):
            return _observation("signal", "thinking block returned", usage)
        return _observation("no_signal", "200 without thinking block", usage)

    def probe_image(self, model: str) -> dict:
        payload = {
            "model": model,
            "max_tokens": 256,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": PNG_1PX_B64},
                        },
                        {"type": "text", "text": IMAGE_PROMPT},
                    ],
                }
            ],
        }
        status, body = self._post(payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["image"])
        answer = self._text(body)
        usage = self.extract_usage(body)
        if "1" in answer:
            return _observation("signal", f"answered: {answer[:60]!r}", usage)
        return _observation("no_signal", f"unexpected answer: {answer[:60]!r}", usage)


class GoogleNative:
    protocol = "google"

    def __init__(self, base_url: str, key: str, timeout: int = TIMEOUT):
        self.base = base_url.rstrip("/")
        self.headers = {"x-goog-api-key": key}
        self.timeout = timeout

    def _post(self, model: str, payload: dict):
        url = f"{self.base}/models/{model}:generateContent"
        status, body = http_json(url, self.headers, payload, self.timeout)
        if status == 429:
            time.sleep(2)
            status, body = http_json(url, self.headers, payload, self.timeout)
        return status, body

    @staticmethod
    def extract_usage(body) -> dict:
        u = body.get("usageMetadata") or {}
        return {
            "prompt_tokens": u.get("promptTokenCount") or 0,
            "completion_tokens": u.get("candidatesTokenCount") or 0,
        }

    @staticmethod
    def _text(body) -> str:
        try:
            parts = body["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return ""
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict))

    @staticmethod
    def _parts(body):
        try:
            return body["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError):
            return []

    def ping(self, model: str):
        return self._post(model, {"contents": [{"parts": [{"text": PING_PROMPT}]}]})

    def probe_tools(self, model: str) -> dict:
        payload = {
            "contents": [{"parts": [{"text": WEATHER_PROMPT}]}],
            "tools": [WEATHER_TOOL_GOOGLE],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
        }
        status, body = self._post(model, payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["tools"])
        usage = self.extract_usage(body)
        if any(isinstance(p, dict) and "functionCall" in p for p in self._parts(body)):
            return _observation("signal", "functionCall returned", usage)
        return _observation("no_signal", "200 without functionCall", usage)

    def probe_structured(self, model: str) -> dict:
        payload = {
            "contents": [{"parts": [{"text": STRUCTURED_PROMPT}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        status, body = self._post(model, payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["structured"])
        obj = _parse_json_object(self._text(body))
        usage = self.extract_usage(body)
        if obj and "ok" in obj:
            return _observation("signal", "valid JSON with 'ok' (responseMimeType)", usage)
        return _observation("no_signal", "200 but content is not parseable JSON", usage)

    def probe_reasoning(self, model: str) -> dict:
        payload = {
            "contents": [{"parts": [{"text": REASONING_PROMPT}]}],
            "generationConfig": {"thinkingConfig": {"thinkingBudget": 512}},
        }
        status, body = self._post(model, payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["reasoning"])
        usage = self.extract_usage(body)
        thoughts = (body.get("usageMetadata") or {}).get("thoughtsTokenCount") or 0
        if isinstance(thoughts, int) and thoughts > 0:
            return _observation("signal", f"thoughtsTokenCount={thoughts}", usage)
        return _observation("no_signal", "no thoughtsTokenCount observable", usage)

    def probe_image(self, model: str) -> dict:
        payload = {
            "contents": [
                {
                    "parts": [
                        {"inline_data": {"mime_type": "image/png", "data": PNG_1PX_B64}},
                        {"text": IMAGE_PROMPT},
                    ]
                }
            ]
        }
        status, body = self._post(model, payload)
        if status != 200:
            return _error_obs(status, body, PROBE_KEYWORDS["image"])
        answer = self._text(body)
        usage = self.extract_usage(body)
        if "1" in answer:
            return _observation("signal", f"answered: {answer[:60]!r}", usage)
        return _observation("no_signal", f"unexpected answer: {answer[:60]!r}", usage)


PROTOCOLS = {"openai": OpenAICompat, "anthropic": AnthropicNative, "google": GoogleNative}


def make_adapter(endpoint: dict):
    cls = PROTOCOLS[endpoint["protocol"]]
    return cls(endpoint["base_url"], endpoint["key"], TIMEOUT)


# ---------------------------------------------------------------- resolution

# Same normalization as tools/sync_models_dev.py / examples/pick_model.py:
# case, dots and hyphens are presentation, they never distinguish capabilities.
_PREFIX_RE = re.compile(r"^[a-z0-9_~-]+/")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def identity(name: str) -> str:
    return _NON_ALNUM_RE.sub("", _PREFIX_RE.sub("", name.strip().lower()))


def build_lookup(table: dict) -> dict:
    """normalized name -> table key. Canonical keys first, then aliases."""
    lookup: dict[str, str] = {}
    for key, rec in table["models"].items():
        bare = key.split("/", 1)[1]
        lookup.setdefault(identity(bare), key)
        for alias in rec.get("aliases", []):
            lookup.setdefault(identity(alias), key)
    return lookup


def candidate_ids(key: str, rec: dict, protocol: str) -> list[str]:
    """Names to try against the endpoint, in order: bare id, then aliases.

    The table key's bare id is NOT guaranteed to be the channel's API name
    (dated snapshots, aggregator spellings), so ping falls back through the
    list. Google additionally accepts a 'models/' prefix.
    """
    bare = key.split("/", 1)[1]
    ids = [bare] + [a for a in rec.get("aliases", []) if a]
    if protocol == "google":
        ids += [f"models/{i}" for i in ids]
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def resolve_endpoint(provider: str, args) -> dict:
    """-> {base_url, key, protocol} or {error: reason}. Keys never logged."""
    spec = PROVIDER_MAP.get(provider)
    base_url = args.base_url or (spec or {}).get("base_url")
    # explicit override > built-in map > openai-compatible default
    protocol = args.protocol or (spec or {}).get("protocol") or "openai"
    if not base_url:
        return {"error": f"unknown provider {provider!r}: pass --base-url (and --api-key, --protocol)"}
    env_names = (spec or {}).get("env", [])
    if args.env_key:
        env_names = [args.env_key]
    key = args.api_key or next((os.environ[n] for n in env_names if os.environ.get(n)), None)
    if not key:
        names = ", ".join(env_names) or "(no convention known)"
        return {"error": f"no API key for {provider!r}: set one of {names} or pass --api-key"}
    if protocol not in PROTOCOLS:
        return {"error": f"unknown protocol {protocol!r}"}
    return {"base_url": base_url, "key": key, "protocol": protocol}


def resolve_names(table: dict, names: list) -> list:
    """Resolve CLI names -> [(key, rec, table_key)].

    A provider-qualified name pins the ENDPOINT to that vendor. The table
    keeps one record per model identity (the dedup winner), but the same
    model identity is served by several vendors ('glm-5.3' at zhipuai,
    volcengine, alibaba-cn...) and capability can differ per channel —
    verifying means testing the channel you name. When the vendor has no
    table record for the identity, the winner's declared facts are reused
    (table_key records where they came from) and only the endpoint changes:
        python tools/verify_claims.py volcengine/glm-5.3 zhipuai/glm-5.3
    """
    models = table["models"]
    lookup = build_lookup(table)
    out = []
    for name in names:
        if name in models:  # exact table key (may itself contain '/')
            out.append((name, models[name], name))
            continue
        if "/" in name:
            # ANY 'vendor/model' form pins the endpoint to that vendor — even
            # a vendor we have no built-in endpoint for (then --base-url is
            # required and resolve_endpoint says so). Falling back to the
            # dedup winner's channel here would silently test the wrong one.
            prefix, rest = name.split("/", 1)
            winner = lookup.get(identity(rest))
            if winner is None:
                raise LookupError(f"unknown model {rest!r}: no table record for this identity")
            rec = models[winner]
            if rec.get("provider") == prefix:  # real record at this vendor
                out.append((winner, rec, winner))
                continue
            vrec = dict(rec)
            vrec["provider"] = prefix  # virtual record: same facts, pinned endpoint
            out.append((f"{prefix}/{rest}", vrec, winner))
            continue
        key = lookup.get(identity(name))
        if key is None:
            raise LookupError(f"not found: {name!r} (name not in table keys or aliases)")
        out.append((key, models[key], key))
    return out


def declared_for(rec: dict, probe: str):
    """The table's declaration for a probe: True / False / None (unknown).

    modalities is special: a PRESENT modalities object declares its contents,
    so image-input False is only claimed when modalities exists without image.
    """
    if probe == "tools":
        return rec.get("tool_call")
    if probe == "structured":
        return rec.get("structured_output")
    if probe == "reasoning":
        return rec.get("reasoning")
    if probe == "image":
        m = rec.get("modalities")
        if not isinstance(m, dict):
            return None
        return "image" in (m.get("input") or [])
    raise ValueError(probe)


# ----------------------------------------------------------------- orchestration

def _add_usage(total: dict, usage: dict) -> None:
    for k in ("prompt_tokens", "completion_tokens"):
        v = usage.get(k)
        if isinstance(v, int) and v > 0:
            total[k] = total.get(k, 0) + v


def run_model(key: str, rec: dict, endpoint: dict, probes: tuple, table_key: str | None = None) -> dict:
    """Probe one table record. Never raises on API problems — those become
    inconclusive observations."""
    result = {
        "endpoint": endpoint.get("base_url"),
        "protocol": endpoint.get("protocol"),
        "table_key": table_key or key,
        "candidates_tried": [],
        "probes": {},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }
    if "error" in endpoint:
        for p in probes:
            result["probes"][p] = {
                "declared": declared_for(rec, p),
                "observed": None,
                "verdict": "inconclusive",
                "detail": endpoint["error"],
            }
        return result

    adapter = make_adapter(endpoint)
    chosen, stop_cls = None, None
    for name in candidate_ids(key, rec, endpoint["protocol"]):
        status, body = adapter.ping(name)
        result["candidates_tried"].append(f"{name}({status})")
        if status == 200:
            chosen = name
            _add_usage(result["usage"], adapter.extract_usage(body))
            break
        cls = classify_error(status, body_text(body), ())
        if cls == "model_unavailable":
            continue  # try the next candidate name
        stop_cls = cls
        break

    if chosen is None:
        detail = f"ping failed ({stop_cls or 'model_unavailable after all candidate names'})"
        for p in probes:
            result["probes"][p] = {
                "declared": declared_for(rec, p),
                "observed": stop_cls or "model_unavailable",
                "verdict": "inconclusive",
                "detail": detail,
            }
        return result

    probe_methods = {
        "tools": adapter.probe_tools,
        "structured": adapter.probe_structured,
        "reasoning": adapter.probe_reasoning,
        "image": adapter.probe_image,
    }
    for p in probes:
        declared = declared_for(rec, p)
        obs = probe_methods[p](chosen)
        _add_usage(result["usage"], obs.get("usage") or {})
        result["probes"][p] = {
            "declared": declared,
            "observed": obs.get("error") if obs.get("kind") == "error" else obs.get("detail"),
            "verdict": decide_verdict(declared, obs),
            "detail": obs.get("detail", ""),
        }
    return result


def select_models(table: dict, args):
    """-> (list of (key, rec, table_key), error_message or None). Applies --max-models."""
    models = table["models"]
    if args.provider:
        entries = [(k, r, k) for k, r in models.items() if r.get("provider") == args.provider]
        if not entries:
            return [], f"no models for provider {args.provider!r}"
        # cheapest output price first; unknown/zero price last
        def price(item):
            p = (item[1].get("cost") or {}).get("output_per_mtok")
            return (p is None or p == 0, p or 0.0, item[0])
        entries.sort(key=price)
    elif args.names:
        try:
            entries = resolve_names(table, args.names)
        except LookupError as e:
            return [], str(e)
    else:
        return [], "nothing to verify: pass model names or --provider"

    if len(entries) > args.max_models:
        print(f"note: {len(entries)} models selected, capped to --max-models {args.max_models}")
        entries = entries[: args.max_models]
    return entries, None


def assemble_report(entries, results: dict) -> dict:
    summary = {"match": 0, "mismatch": 0, "discovery": 0, "inconclusive": 0}
    for res in results.values():
        for p in res["probes"].values():
            summary[p["verdict"]] = summary.get(p["verdict"], 0) + 1
    return {
        "schema_version": 1,
        "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": GENERATOR,
        "results": results,
        "summary": summary,
    }


def redact(report: dict, secrets: list) -> dict:
    """Last-resort scrub: keys must never land in a report file."""
    blob = json.dumps(report, ensure_ascii=False)
    for s in secrets:
        if s and s in blob:
            blob = blob.replace(s, "[redacted]")
            print("WARNING: API key material found in report and redacted", file=sys.stderr)
    return json.loads(blob)


def print_plan(entries, endpoints: dict, probes: tuple) -> None:
    print(f"verify plan: {len(entries)} model(s), probes: {', '.join(probes)}\n")
    for key, rec, table_key in entries:
        ep = endpoints[key]
        if "error" in ep:
            ep_desc = f"SKIP ({ep['error']})"
        else:
            ep_desc = f"{ep['protocol']} {ep['base_url']}"
        names = ", ".join(candidate_ids(key, rec, ep.get("protocol") or "openai"))
        decls = ", ".join(f"{p}={declared_for(rec, p)}" for p in probes)
        facts = "" if table_key == key else f"    facts from: {table_key}\n"
        print(f"{key}")
        print(f"    endpoint: {ep_desc}")
        if facts:
            print(facts, end="")
        print(f"    candidates: {names}")
        print(f"    declared:   {decls}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify registry claims against live model APIs.")
    ap.add_argument("names", nargs="*", help="model name(s) or alias(es) from the table")
    ap.add_argument("--provider", help="verify all models of one provider (cheapest first)")
    ap.add_argument("--table", type=Path, default=DEFAULT_TABLE, help="registry table path")
    ap.add_argument("--probes", default=",".join(ALL_PROBES),
                    help=f"comma list from {','.join(ALL_PROBES)} (default: all)")
    ap.add_argument("--declared-only", action="store_true",
                    help="skip probes for capabilities the table does not declare")
    ap.add_argument("--max-models", type=int, default=20, help="cost guard (default 20)")
    ap.add_argument("--base-url", help="endpoint base URL for providers not in the built-in map")
    ap.add_argument("--api-key", help="API key (prefer environment variables)")
    ap.add_argument("--env-key", help="environment variable name holding the API key")
    ap.add_argument("--protocol", choices=sorted(PROTOCOLS), help="wire protocol for --base-url")
    ap.add_argument("--out", type=Path, default=DEFAULT_REPORT, help="report path")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, send nothing")
    ap.add_argument("--strict", action="store_true", help="exit 1 if any probe mismatches")
    args = ap.parse_args(argv)

    probes = tuple(p.strip() for p in args.probes.split(",") if p.strip())
    if not probes or any(p not in ALL_PROBES for p in probes):
        print(f"ERROR: --probes must be a comma list from {','.join(ALL_PROBES)}", file=sys.stderr)
        return 2
    if not args.names and not args.provider:
        print("ERROR: pass model name(s) or --provider", file=sys.stderr)
        return 2

    if not args.table.exists():
        print(f"ERROR: table not found: {args.table}", file=sys.stderr)
        return 2
    table = json.loads(args.table.read_text(encoding="utf-8"))
    if table.get("schema_version") != 1:
        print(f"ERROR: unsupported schema_version: {table.get('schema_version')}", file=sys.stderr)
        return 2

    entries, err = select_models(table, args)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    # keep only probes worth sending for each model
    plans = {}
    for key, rec, table_key in entries:
        wanted = probes
        if args.declared_only:
            wanted = tuple(p for p in probes if declared_for(rec, p) is not None)
        plans[key] = (rec, wanted, table_key)

    endpoints = {key: resolve_endpoint(rec.get("provider"), args) for key, (rec, _, _) in plans.items()}

    if args.dry_run:
        print_plan(entries, endpoints, probes)
        return 0

    results = {}
    for key, (rec, wanted, table_key) in plans.items():
        if not wanted:
            print(f"{key}: no declared probes left (--declared-only), skipped")
            continue
        res = run_model(key, rec, endpoints[key], wanted, table_key)
        results[key] = res
        marks = "  ".join(f"{p}={res['probes'][p]['verdict']}" for p in wanted)
        print(f"{key}: {marks}")

    if not results:
        print("nothing verified")
        return 0

    report = redact(assemble_report(entries, results), [args.api_key])
    # env-resolved keys: scrub every key value we resolved during the run
    for ep in endpoints.values():
        if ep.get("key") and ep["key"] != args.api_key:
            report = redact(report, [ep["key"]])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    s = report["summary"]
    print(f"\nsummary: match={s['match']} mismatch={s['mismatch']} "
          f"discovery={s['discovery']} inconclusive={s['inconclusive']}")
    print(f"report: {args.out}  (never written back to the table)")

    if args.strict and s["mismatch"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
