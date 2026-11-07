"""Responses API bridge: Codex CLI can keep wire_api='responses'."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from planrouter.core import load_config, parse_config
from planrouter.responses import ResponsesAdapter, chat_to_response, to_chat_request
from planrouter.server import App, Handler

TEXT_SSE = [
    b'data: {"choices":[{"delta":{"content":"forty-"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"two!"}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n',
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
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append(body)  # type: ignore[attr-defined]
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        mode = self.server.mode  # type: ignore[attr-defined]
        if mode == "429":
            data = b'{"error":{"message":"usage limit reached"}}'
            self.send_response(429)
            self.send_header("Retry-After", "7200")
            content_type = "application/json"
        else:
            self.send_response(200)
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
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def config_for(url: str) -> dict:
    return {
        "listen": {"host": "127.0.0.1", "port": 0},
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
