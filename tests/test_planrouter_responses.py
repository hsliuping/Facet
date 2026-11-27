"""Responses API bridge: Codex CLI can keep wire_api='responses'."""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from facet.gateway.core import load_config, parse_config
from facet.gateway.responses import (
    ResponsesAdapter, _cached_tokens, chat_to_response, to_chat_request,
)
from facet.gateway.server import App, Handler

TEXT_SSE = [
    b'data: {"choices":[{"delta":{"content":"forty-"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"two!"}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"prompt_tokens_details":{"cached_tokens":4}}}\n\n',
    b'data: [DONE]\n\n',
]
TOOL_SSE = [
    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"shell","arguments":"{\\"cmd\\""}}]}}]}\n\n',
    b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"ls\\"}"}}]}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}\n\n',
    b'data: [DONE]\n\n',
]


def codex_request(**extra):
    body = {
        "model": "gpt-5-codex",
        "instructions": "You are Codex.",
        "input": [{
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "add a diagram"}],
        }],
        "tools": [{
            "type": "function", "name": "shell", "description": "run",
            "strict": False, "parameters": {"type": "object"},
        }],
        "store": False,
    }
    body.update(extra)
    return body


def chat_response(text="ok", tool_calls=None):
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "prompt_tokens_details": {"cached_tokens": 6}},
    }


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append(body)  # type: ignore[attr-defined]
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        mode = self.server.mode  # type: ignore[attr-defined]
        if (mode in ("balance", "429") and body.get("model") == "wire-b"
                and getattr(self.server, "wire_b_healthy", False)):
            # plan-b is a healthy sibling channel: only plan-a's wire model
            # hits the rejected account / rate limit.
            mode = "text"
        if mode == "balance":
            # Account-level rejection: money, not a per-model grant. It never
            # resets on its own, so the gateway must retire the channel.
            data = b'{"error":{"message":"Your account balance is insufficient, please top up"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if mode == "429":
            data = b'{"error":{"message":"usage limit reached"}}'
            self.send_response(429)
            self.send_header("Retry-After", "7200")
            content_type = "application/json"
        else:
            self.send_response(200)
            if self.server.truncate_first > 0:  # type: ignore[attr-defined]
                # 200 with a body that never starts: the first read raises
                # IncompleteRead at the gateway, before any byte is relayed.
                self.server.truncate_first -= 1  # type: ignore[attr-defined]
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(32 * 1024 * 1024))
                self.end_headers()
                self.close_connection = True
                return
            if mode == "truncated":
                # Content-Length promises a huge body, then the socket closes:
                # http.client raises IncompleteRead at the gateway.
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(32 * 1024 * 1024))
                self.end_headers()
                self.wfile.write(b'{"choices":[{"message":{"content":"partial"')
                self.close_connection = True
                return
            if mode == "stream_truncated":
                # A few SSE events arrive, then the socket closes short of the
                # promised length: the gateway gets IncompleteRead mid-stream.
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(32 * 1024 * 1024))
                self.end_headers()
                self.wfile.write(b"".join(TEXT_SSE[:2]))
                self.wfile.flush()
                self.close_connection = True
                return
            if mode == "stream":
                data = b"".join(TEXT_SSE)
                content_type = "text/event-stream"
            elif mode == "stream_tool":
                data = b"".join(TOOL_SSE)
                content_type = "text/event-stream"
            elif mode == "tool":
                data = json.dumps(chat_response(None, [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "shell", "arguments": '{"cmd":"ls"}'},
                }])).encode()
                content_type = "application/json"
            else:
                data = json.dumps(chat_response()).encode()
                content_type = "application/json"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass


def start_upstream():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    httpd.seen = []  # type: ignore[attr-defined]
    httpd.paths = []  # type: ignore[attr-defined]
    httpd.mode = "text"  # type: ignore[attr-defined]
    httpd.truncate_first = 0  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def config_for(url: str) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
        # 本文件测的是 Responses 桥接/流转/故障转移机制，请求里的 "wire-a"
        # 等只是载体名字：显式开启 fallback_unknown 保持按链调度，不依赖
        # 已翻转的默认值（fallback_unknown 现默认拒绝未知名）。
        "models": {"fallback_unknown": True},
        "channels": [
            {"name": "plan-a", "protocol": "openai", "base_url": url,
             "models": {"auto": "wire-a"}, "priority": 10},
            {"name": "plan-b", "protocol": "openai", "base_url": url,
             "models": {"auto": "wire-b"}, "priority": 20},
            {"name": "claude", "protocol": "anthropic", "base_url": url,
             "models": {"auto": "claude-sonnet"}},
        ],
        "routes": [
            {"name": "default", "protocol": "openai",
             "channels": ["plan-a", "plan-b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class ConversionTests(unittest.TestCase):
    def test_request_conversion_covers_codex_input_items(self):
        body = codex_request(
            input=[
                {"type": "message", "role": "developer",
                 "content": [{"type": "input_text", "text": "sandbox"}]},
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "add a diagram"}]},
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}],
                 "encrypted_content": "opaque"},
                {"type": "function_call", "name": "shell", "arguments": '{"command":"ls"}',
                 "call_id": "call_1"},
                {"type": "function_call_output", "call_id": "call_1", "output": "file.txt"},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "done"}]},
            ],
            tool_choice={"type": "function", "name": "shell"},
            max_output_tokens=2048,
            stream=True,
            previous_response_id="resp_old",
        )
        body["tools"].append({"type": "web_search", "external_web_access": False})
        chat = to_chat_request(body)
        self.assertEqual(chat["model"], "gpt-5-codex")
        self.assertEqual([m["role"] for m in chat["messages"]],
                         ["system", "system", "user", "assistant", "assistant",
                          "tool", "assistant"])
        self.assertEqual(chat["messages"][5]["tool_call_id"], "call_1")
        self.assertEqual(chat["messages"][0]["content"], "You are Codex.")
        self.assertEqual(chat["max_tokens"], 2048)
        self.assertEqual(len(chat["tools"]), 1)
        self.assertEqual(chat["tools"][0]["function"]["name"], "shell")
        self.assertEqual(chat["tool_choice"],
                         {"type": "function", "function": {"name": "shell"}})
        self.assertTrue(chat["stream"])
        for key in ("instructions", "input", "store", "previous_response_id", "max_output_tokens"):
            self.assertNotIn(key, chat)

    def test_string_input_and_auto_tool_choice(self):
        chat = to_chat_request({"model": "facet-auto", "input": "hello",
                                "tool_choice": "auto"})
        self.assertEqual(chat["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(chat["tool_choice"], "auto")
        self.assertNotIn("tools", chat)

    def test_non_stream_response_conversion(self):
        upstream = json.dumps(chat_response("hello", [{
            "id": "call_1", "type": "function",
            "function": {"name": "shell", "arguments": '{"cmd":"ls"}'},
        }])).encode()
        obj = json.loads(chat_to_response(upstream, "gpt-5-codex", "resp_x", 1700000000))
        self.assertEqual(obj["id"], "resp_x")
        self.assertEqual(obj["object"], "response")
        self.assertEqual(obj["status"], "completed")
        self.assertEqual(obj["model"], "gpt-5-codex")
        self.assertEqual(obj["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(obj["output"][1]["type"], "function_call")
        self.assertEqual(obj["output"][1]["call_id"], "call_1")
        self.assertEqual(obj["usage"]["total_tokens"], 15)
        self.assertEqual(obj["usage"]["input_tokens"], 10)
        self.assertEqual(obj["usage"]["input_tokens_details"]["cached_tokens"], 6)

    def test_cached_tokens_reads_every_vendor_shape(self):
        self.assertEqual(_cached_tokens({"prompt_tokens_details": {"cached_tokens": 7}}), 7)
        self.assertEqual(_cached_tokens({"prompt_cache_hit_tokens": 8}), 8)
        self.assertEqual(_cached_tokens({"cache_read_input_tokens": 9}), 9)
        self.assertEqual(_cached_tokens({"cached_tokens": 10}), 10)
        self.assertEqual(_cached_tokens({}), 0)
        self.assertEqual(_cached_tokens({"prompt_tokens_details": None}), 0)

    def test_stream_adapter_emits_complete_event_sequence(self):
        adapter = ResponsesAdapter("gpt-5-codex", True, "resp_x", 1700000000)
        chunks = TEXT_SSE[:2] + TOOL_SSE[:2] + [TEXT_SSE[2], TEXT_SSE[3]]
        text = (b"".join(adapter.feed(c) for c in chunks) + adapter.finish()).decode()
        kinds = [line[7:] for line in text.splitlines() if line.startswith("event: ")]
        self.assertEqual(kinds[0], "response.created")
        self.assertIn("response.in_progress", kinds)
        self.assertIn("response.output_item.added", kinds)
        self.assertIn("response.content_part.added", kinds)
        self.assertEqual(kinds.count("response.output_text.delta"), 2)
        self.assertIn("response.output_text.done", kinds)
        self.assertIn("response.output_item.done", kinds)
        self.assertEqual(kinds[-1], "response.completed")
        self.assertNotIn("[DONE]", text)
        completed = [json.loads(line[6:]) for line in text.splitlines()
                     if line.startswith("data: ") and '"response.completed"' in line]
        response = completed[0]["response"]
        self.assertEqual(response["output"][0]["content"][0]["text"], "forty-two!")
        self.assertEqual(response["output"][1]["arguments"], '{"cmd":"ls"}')
        self.assertEqual(response["usage"]["total_tokens"], 15)
        self.assertEqual(response["usage"]["input_tokens_details"]["cached_tokens"], 4)

    def test_stream_adapter_handles_tool_only(self):
        adapter = ResponsesAdapter("m", True)
        out = adapter.feed(TOOL_SSE[0]) + adapter.finish()
        text = out.decode()
        self.assertNotIn("response.content_part.added", text)
        self.assertIn('"type":"function_call"', text)
        self.assertIn("response.completed", text)

    def test_non_stream_adapter_finish(self):
        adapter = ResponsesAdapter("m", False)
        obj = json.loads(adapter.finish(json.dumps(chat_response()).encode()))
        self.assertEqual(obj["output"][0]["content"][0]["text"], "ok")
        self.assertEqual(obj["status"], "completed")

    def test_upstream_path_validation(self):
        raw = config_for("http://127.0.0.1:9000")
        raw["channels"][0]["upstream_path"] = "chat/completions"
        with self.assertRaisesRegex(ValueError, "upstream_path"):
            parse_config(raw)
        raw["channels"][0]["upstream_path"] = "/chat/completions"
        self.assertEqual(parse_config(raw).channels[0].upstream_path,
                         "/chat/completions")


class ResponsesGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.upstream = start_upstream()
        url = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        self.config_path = self.root / "router.json"
        self.config_path.write_text(json.dumps(config_for(url)), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(  # type: ignore[attr-defined]
            load_config(self.config_path), config_path=self.config_path,
            audit_file=self.root / "requests.jsonl",
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.upstream.seen.clear()
        self.upstream.paths.clear()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def audit(self):
        return self.httpd.app.audit.recent()  # type: ignore[union-attr]

    def test_response_headers_expose_prompt_cache(self):
        req = Request(self.base + "/v1/responses",
                      data=json.dumps(codex_request()).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.headers.get("X-Input-Tokens"), "10")
            self.assertEqual(resp.headers.get("X-Cached-Tokens"), "6")
            obj = json.loads(resp.read())
        self.assertEqual(obj["usage"]["input_tokens_details"]["cached_tokens"], 6)

    def test_non_stream_responses_are_bridged(self):
        status, raw = self.request("POST", "/v1/responses", codex_request())
        self.assertEqual(status, 200)
        obj = json.loads(raw)
        self.assertEqual(obj["object"], "response")
        self.assertEqual(obj["output"][0]["content"][0]["text"], "ok")
        upstream = self.upstream.seen[-1]
        self.assertEqual(upstream["messages"][0]["content"], "You are Codex.")
        self.assertEqual(upstream["tools"][0]["function"]["name"], "shell")
        self.assertEqual(upstream["model"], "wire-a")
        for key in ("instructions", "input", "store"):
            self.assertNotIn(key, upstream)
        entry = self.audit()[0]
        self.assertEqual(entry["api"], "responses")
        self.assertEqual(entry["model"], "gpt-5-codex")
        self.assertEqual(entry["mode"], "fallback")

    def test_branded_model_still_uses_internal_schedule(self):
        status, raw = self.request("POST", "/v1/responses",
                                   codex_request(model="facet-auto"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["model"], "facet-auto")
        self.assertEqual(self.audit()[0]["mode"], "auto")

    def test_stream_responses_emits_sse_events(self):
        self.upstream.mode = "stream"
        status, raw = self.request("POST", "/v1/responses",
                                   codex_request(stream=True))
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn("response.output_text.delta", text)
        self.assertIn("response.completed", text)
        self.assertNotIn("[DONE]", text)
        self.assertIn("stream_options", self.upstream.seen[-1])
        self.assertEqual(self.audit()[0]["tokens"], 15)

    def test_non_stream_tool_calls(self):
        self.upstream.mode = "tool"
        status, raw = self.request("POST", "/v1/responses", codex_request())
        self.assertEqual(status, 200)
        output = json.loads(raw)["output"]
        self.assertEqual(output[0]["type"], "function_call")
        self.assertEqual(output[0]["name"], "shell")
        self.assertEqual(output[0]["arguments"], '{"cmd":"ls"}')

    def test_stream_tool_calls_are_aggregated(self):
        self.upstream.mode = "stream_tool"
        status, raw = self.request("POST", "/v1/responses",
                                   codex_request(stream=True))
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn("response.output_item.done", text)
        self.assertIn('"type":"function_call"', text)
        self.assertEqual(self.audit()[0]["tokens"], 12)

    def test_failover_keeps_responses_bridge(self):
        self.upstream.mode = "429"
        status, _ = self.request("POST", "/v1/responses", codex_request())
        self.assertEqual(status, 503)
        entries = self.audit()
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["api"] == "responses" for e in entries))
        self.assertTrue(all(e["status"] == "upstream_error" for e in entries))

    def test_bad_request_is_reported_without_failover(self):
        # A payload the gateway cannot prepare for a channel (protocol
        # conversion blowing up) must come back as a 400 at once, without
        # failover or cooldown — a client error is not a channel outage.
        def fake_forward(h, protocol, channel, wire, body, attempt, handoff,
                         audit=None, delivery=None):
            return (400, "bad_request", 0.0)

        with mock.patch.object(self.httpd.app, "_forward",
                               side_effect=fake_forward):
            status, raw_body = self.request(
                "POST", "/v1/chat/completions",
                {"model": "wire-a", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        body = raw_body.decode()
        self.assertIn("cannot prepare", body)
        self.assertIn("plan-a", body)
        entries = self.audit()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "bad_request")
        self.assertEqual(self.upstream.seen, [])

    def test_truncated_upstream_fails_over_then_reports_relay_error(self):
        self.upstream.mode = "truncated"
        status, _ = self.request(
            "POST", "/v1/chat/completions",
            {"model": "wire-a", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        entries = self.audit()
        self.assertEqual(len(entries), 2)  # both channels were tried
        self.assertTrue(all(e["reason"] == "relay_error" for e in entries))
        self.assertTrue(all(e["http_status"] == 502 for e in entries))
        self.assertEqual(len(self.upstream.seen), 2)

    def test_mid_stream_relay_failure_closes_the_connection(self):
        self.upstream.mode = "stream"

        class Boom:
            def feed(self, chunk):
                raise RuntimeError("converter exploded")

            def finish(self):
                return b""

        with mock.patch("facet.gateway.server.stream_converter",
                        return_value=Boom()):
            status, raw_body = self.request(
                "POST", "/v1/chat/completions",
                {"model": "wire-a", "messages": [{"role": "user", "content": "hi"}],
                 "stream": True})
        self.assertEqual(status, 200)  # headers were already on the wire
        self.assertEqual(raw_body, b"")  # the truncated body was closed out
        entries = self.audit()
        self.assertEqual(len(entries), 1)  # no failover once headers went out
        self.assertEqual(entries[0]["status"], "relay_error")
        self.assertEqual(entries[0]["http_status"], 200)

    def _last_event(self, raw: bytes) -> dict:
        return json.loads(raw.decode().rstrip().rsplit("\n\n", 1)[-1]
                          .split("data: ", 1)[1])

    def test_truncated_stream_fails_over_before_the_first_byte(self):
        # 200 whose body never starts is not a commitment: nothing has reached
        # the client, so the gateway moves on and the client never notices.
        self.upstream.truncate_first = 2  # type: ignore[attr-defined]  # both tries on plan-a
        self.upstream.mode = "stream"  # type: ignore[attr-defined]  # plan-b answers in SSE
        status, raw = self.request(
            "POST", "/v1/chat/completions",
            {"model": "wire-a", "messages": [{"role": "user", "content": "hi"}],
             "stream": True})
        self.assertEqual(status, 200)
        self.assertIn(b'"content":"forty-"', raw)
        self.assertIn(b'"content":"two!"', raw)
        self.assertIn(b"data: [DONE]", raw)
        self.assertNotIn(b'"error"', raw)
        entries = self.audit()
        # audit() is newest first: plan-b answered after both tries on plan-a
        self.assertEqual([(e["channel"], e["status"], e["http_status"]) for e in entries],
                         [("plan-b", "ok", 200),
                          ("plan-a", "network_error", 502),
                          ("plan-a", "network_error", 502)])
        self.assertEqual([e["reason"] for e in entries[1:]],
                         ["transient_network"] * 2)

    def test_stream_cut_mid_response_tells_the_client_and_cools_the_channel(self):
        self.upstream.mode = "stream_truncated"
        req = Request(self.base + "/v1/chat/completions", data=json.dumps(
            {"model": "wire-a", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json", "X-Session-Id": "sess-1"})
        with urlopen(req, timeout=5) as resp:
            status, raw = resp.status, resp.read()
        self.assertEqual(status, 200)  # the first byte already committed the channel
        self.assertIn(b'"content":"two!"', raw)  # what did arrive was relayed
        self.assertNotIn(b"[DONE]", raw)  # never dressed up as a complete answer
        event = self._last_event(raw)["error"]
        self.assertEqual(event["code"], "stream_truncated")
        self.assertIn("plan-a", event["message"])
        self.assertIn("IncompleteRead", event["message"])
        entries = self.audit()
        self.assertEqual(len(entries), 1)  # no failover once bytes went out
        self.assertEqual((entries[0]["status"], entries[0]["http_status"]),
                         ("relay_error", 200))
        self.assertIn("IncompleteRead", entries[0]["reason"])
        state = self.httpd.app.store.states["plan-a"]  # type: ignore[union-attr]
        self.assertEqual(state.reason, "transient_network")
        self.assertGreater(state.cooldown_until, time.time())
        # ...and a channel whose stream just died is not the session's sticky pick
        self.assertIsNone(self.httpd.app.store.session_channel(  # type: ignore[union-attr]
            "sess-1", time.time()))

    def test_responses_and_anthropic_clients_get_their_own_error_event(self):
        self.upstream.mode = "stream_truncated"
        stream_codex = codex_request(stream=True)
        status, raw = self.request("POST", "/v1/responses", stream_codex)
        self.assertEqual(status, 200)
        tail = raw.decode().rstrip().rsplit("\n\n", 1)[-1]
        self.assertTrue(tail.startswith("event: error\n"), tail)
        event = self._last_event(raw)
        self.assertEqual((event["type"], event["code"]), ("error", "server_error"))
        self.assertNotIn(b"response.completed", raw)

        status, raw = self.request(
            "POST", "/v1/messages",
            {"model": "auto", "max_tokens": 16, "stream": True,
             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        tail = raw.decode().rstrip().rsplit("\n\n", 1)[-1]
        self.assertTrue(tail.startswith("event: error\n"), tail)
        event = self._last_event(raw)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["error"]["type"], "api_error")
        self.assertIn("claude", event["error"]["message"])
        self.assertNotIn(b"message_stop", raw)

    def test_balance_429_disables_the_channel_and_persists_it(self):
        self.upstream.mode = "balance"
        self.upstream.wire_b_healthy = True
        status, raw = self.request("POST", "/v1/responses", codex_request())
        self.assertEqual(status, 200)  # the request itself still succeeded
        self.assertEqual(json.loads(raw)["output"][0]["content"][0]["text"], "ok")
        failed = [e for e in self.audit() if e["channel"] == "plan-a"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"], "account_disabled")
        self.assertEqual(failed[0]["http_status"], 429)
        self.assertEqual(failed[0]["cooldown_seconds"], 0.0)  # retired, not cooled
        # Out of the rotation in memory...
        self.assertIs(self.httpd.app.config.channel("plan-a").enabled, False)
        # ...and on disk, so a restart does not resurrect a broke account.
        raw_cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        plan_a = next(c for c in raw_cfg["channels"] if c["name"] == "plan-a")
        plan_b = next(c for c in raw_cfg["channels"] if c["name"] == "plan-b")
        self.assertIs(plan_a["enabled"], False)
        self.assertIs(plan_b.get("enabled", True), True)

    def test_plain_quota_429_still_cools_instead_of_disabling(self):
        # Rate limiting / a per-model grant exhausts and recovers on its own:
        # only the money markers retire a channel.
        self.upstream.mode = "429"
        self.upstream.wire_b_healthy = True
        status, raw = self.request("POST", "/v1/responses", codex_request())
        self.assertEqual(status, 200)
        failed = [e for e in self.audit() if e["channel"] == "plan-a"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["reason"], "usage limit")
        self.assertGreater(failed[0]["cooldown_seconds"], 0.0)
        self.assertIs(self.httpd.app.config.channel("plan-a").enabled, True)
        raw_cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        plan_a = next(c for c in raw_cfg["channels"] if c["name"] == "plan-a")
        self.assertIs(plan_a.get("enabled", True), True)

    def _raw_post(self, headers_blob: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1",
                                       self.httpd.server_address[1]),
                                      timeout=5) as sock:
            sock.sendall(headers_blob)
            chunks = []
            while True:
                try:
                    piece = sock.recv(4096)
                except socket.timeout:
                    break
                if not piece:
                    break
                chunks.append(piece)
            return b"".join(chunks)

    def test_oversized_body_gets_413_and_connection_close(self):
        # Under keep-alive the unread body would otherwise be parsed as the
        # next request; the gateway must reject and close in one move.
        blob = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 999999999\r\n\r\n")
        raw = self._raw_post(blob)
        self.assertTrue(raw.startswith(b"HTTP/1.1 413"))
        self.assertIn(b"connection: close", raw.lower())
        # after the reject the server closed, not parse the next request
        blob += (b'{"model":"wire-a","messages":[{"role":"user",'
                 b'"content":"hi"}]}')
        raw = self._raw_post(blob)
        self.assertTrue(raw.startswith(b"HTTP/1.1 413"))
        self.assertNotIn(b"400 Bad Request", raw)

    def test_invalid_content_length_gets_400_and_connection_close(self):
        blob = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: abc\r\n\r\n")
        raw = self._raw_post(blob)
        self.assertTrue(raw.startswith(b"HTTP/1.1 400"))
        self.assertIn(b"connection: close", raw.lower())

    def test_slow_body_gets_408_request_timeout(self):
        # The half-open client sends headers and never the body: the socket
        # timeout must free the handler thread with a 408.
        with mock.patch.object(Handler, "timeout", 0.5):
            blob = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 100\r\n\r\n")
            raw = self._raw_post(blob)
        self.assertTrue(raw.startswith(b"HTTP/1.1 408"))
        self.assertIn(b"connection: close", raw.lower())

    def test_compaction_endpoint_is_explicitly_unsupported(self):
        status, raw = self.request("POST", "/v1/responses/compact", {})
        self.assertEqual(status, 404)
        self.assertIn("not found", raw.decode().lower())


    def test_custom_upstream_path_is_used(self):
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["channels"][0]["upstream_path"] = "/custom/chat"
        app = self.httpd.app  # type: ignore[attr-defined]
        app.apply_config(raw, persist=False)
        self.upstream.paths.clear()
        status, _ = self.request("POST", "/v1/responses", codex_request())
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.paths[-1], "/custom/chat")


if __name__ == "__main__":
    unittest.main()
