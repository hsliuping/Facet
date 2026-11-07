"""End-to-end smoke test for the PlanRouter container.

On the host, after `docker compose up -d`:
    python docker/smoke_test.py
Inside the router container:
    ROUTER_URL=http://127.0.0.1:8787 python /opt/planrouter/smoke_test.py

Override ROUTER_URL / ROUTER_KEY / ROUTER_ADMIN_KEY when you change them in
compose.yaml. Exits non-zero if any check fails.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("ROUTER_URL", "http://127.0.0.1:8787").rstrip("/")
KEY = os.environ.get("ROUTER_KEY", "test-router-key")
ADMIN = os.environ.get("ROUTER_ADMIN_KEY", "test-admin-key")
TEXT = "hello from the mock upstream"

results: list[tuple[str, bool]] = []


def header(headers: dict, name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def call(method: str, path: str, payload=None, admin: bool = False,
         bare: bool = False) -> tuple[int, dict, bytes]:
    headers = {"Content-Type": "application/json"}
    if not bare:
        headers["Authorization"] = f"Bearer {KEY}"
        if admin:
            headers["X-Admin-Key"] = ADMIN
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except urllib.error.URLError as exc:
        return 0, {}, str(exc).encode("utf-8")


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok))
    print(("PASS  " if ok else "FAIL  ") + label + (f"   {detail}" if detail else ""), flush=True)


def as_json(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def reset(name: str) -> bool:
    status, _, _ = call("POST", f"/api/channels/{name}/reset", admin=True)
    return status == 200


def main() -> int:
    # A previous run may have left the primary channel in cooldown after the
    # failover check. Clear it so this script is deterministic on every run.
    check("admin channel reset", reset("mock-primary") and reset("mock-backup"),
          "clears cooldown from any previous run")

    status, _, body = call("GET", "/")
    check("console page", status == 200 and b"report-month" in body, f"HTTP {status}")

    status, _, body = call("GET", "/health")
    health = as_json(body)
    check("health endpoint", status == 200 and health.get("status") == "ok",
          f"HTTP {status}, available={len(health.get('available') or [])}")

    status, _, _ = call("GET", "/v1/models", bare=True)
    check("gateway key enforced", status == 401, f"HTTP {status}")

    status, _, body = call("GET", "/v1/models")
    models = as_json(body)
    ids = [m.get("id") for m in (models.get("data") or []) if isinstance(m, dict)]
    check("model list advertises facet-auto", status == 200 and "facet-auto" in ids,
          f"HTTP {status}, models={len(ids)}")

    chat = {"model": "facet-auto", "messages": [{"role": "user", "content": "hi"}]}
    status, hdrs, body = call("POST", "/v1/chat/completions", chat)
    reply = as_json(body)
    content = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content")
    check("chat completion", status == 200 and content == TEXT,
          f"HTTP {status}, via={header(hdrs, 'X-Routed-Via')}")
    check("non-stream usage", (reply.get("usage") or {}).get("total_tokens") == 20,
          f"tokens={(reply.get('usage') or {}).get('total_tokens')}")
    check("no failover on healthy channel", header(hdrs, "X-Failover-Count") == "0",
          f"count={header(hdrs, 'X-Failover-Count')}")

    status, hdrs, body = call("POST", "/v1/chat/completions", {**chat, "stream": True})
    check("streaming SSE", status == 200 and b"data: [DONE]" in body and TEXT.encode() in body,
          f"HTTP {status}, bytes={len(body)}")

    status, _, body = call("POST", "/v1/messages", {
        "model": "facet-auto", "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
    })
    reply = as_json(body)
    blocks = reply.get("content") or []
    check("anthropic messages", status == 200 and blocks and blocks[0].get("text") == TEXT,
          f"HTTP {status}")

    status, hdrs, body = call("POST", "/v1/messages/count_tokens", {
        "model": "facet-auto", "messages": [{"role": "user", "content": "hi"}],
    })
    check("count_tokens local estimate", status == 200
          and header(hdrs, "X-Token-Estimate") == "local-heuristic", f"HTTP {status}")

    status, _, body = call("POST", "/v1/responses",
                           {"model": "facet-auto", "input": "hi"})
    reply = as_json(body)
    check("responses bridge", status == 200 and reply.get("object") == "response"
          and bool(reply.get("output")), f"HTTP {status}")

    status, _, body = call("GET", "/stats")
    stats = as_json(body)
    check("usage counters", status == 200 and bool(stats.get("channels")), f"HTTP {status}")

    status, _, body = call("GET", "/api/requests", admin=True)
    check("admin request log", status == 200 and (b"channel" in body or b"api" in body),
          f"HTTP {status}, bytes={len(body)}")

    status, _, body = call("GET", "/api/report", admin=True)
    check("admin monthly report", status == 200 and "month" in as_json(body), f"HTTP {status}")

    status, _, body = call("POST", "/api/test/channels", admin=True, payload={
        "channels": [{
            "name": "probe-draft", "protocol": "openai",
            "base_url": "http://mock:9000", "api_key": "docker-mock-key",
            "models": {"auto": "mock-model"},
        }],
    })
    probe = as_json(body)
    probe_results = probe.get("results") or []
    check("draft channel probe", status == 200 and probe.get("ok") is True
          and probe_results and probe_results[0].get("http_status") == 200, f"HTTP {status}")

    # Last on purpose: it cools the primary channel for a short Retry-After.
    reset("mock-primary")
    status, hdrs, body = call("POST", "/v1/chat/completions", {
        "model": "facet-auto",
        "messages": [{"role": "user", "content": "please fail now"}],
    })
    reply = as_json(body)
    content = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content")
    via = header(hdrs, "X-Routed-Via") or ""
    check("429 cooldown + failover", status == 200 and content == TEXT
          and "mock-backup" in via and header(hdrs, "X-Failover-Count") != "0",
          f"HTTP {status}, via={via}, count={header(hdrs, 'X-Failover-Count')}")

    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} checks passed", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
