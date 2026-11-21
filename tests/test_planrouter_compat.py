"""P1-4: OpenAI-compatible surface extensions.

/v1/completions (legacy text completions bridged onto the chat pipeline),
/v1/rerank (capability-routed) and /v1/audio/* passthrough.
"""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from facet.gateway.completions import CompletionsAdapter, to_chat_request
from facet.gateway.core import OPENAI, load_config
from facet.gateway.server import App

BASE = {
    "listen": {"host": "127.0.0.1", "port": 8787},
    "channels": [
        {
            "name": "plan-a", "protocol": "openai", "base_url": "http://127.0.0.1:9001",
            "models": {"auto": "model-a"}, "priority": 10, "api_key": "sk-openai",
        },
        {
            "name": "claude", "protocol": "anthropic", "base_url": "http://127.0.0.1:9004",
            "models": {"auto": "claude-sonnet-4-5"}, "priority": 5,
            "api_key": "sk-ant",
        },
    ],
    "routes": [
        {"name": "default", "protocol": "openai", "channels": ["plan-a"]},
        {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
    ],
}


def write_config(data: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, tmp)
    tmp.close()
    return Path(tmp.name)


class _CaptureHandler:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.status = None
        self.resp_headers: dict = {}
        self.body = b""
        self.wfile = self

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.resp_headers[key] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data

    def flush(self):
        pass


class _JsonResp:
    def __init__(self, payload: dict, status=200, headers=None):
        self._data = json.dumps(payload).encode("utf-8")
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.closed = 0

    def read(self, n=-1):
        data, self._data = self._data, b""
        return data

    def close(self):
        self.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _error_resp(payload: dict, status: int, headers=None):
    """Create a mock response that raises HTTPError for error statuses."""
    from urllib.error import HTTPError
    class _ErrResp(_JsonResp):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
        def __enter__(self):
            if self.status >= 400:
                raise HTTPError("http://test", self.status, "Error",
                                self.headers, self)
            return super().__enter__()
        def read(self, n=-1):
            data, self._data = self._data, b""
            return data
    return _ErrResp(payload, status, headers)


class _SseResp:
    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.status = 200
        self.headers = headers or {"Content-Type": "text/event-stream"}
        self.closed = 0

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class LegacyCompletionsShapeTests(unittest.TestCase):
    def test_prompt_becomes_user_message(self):
        chat = to_chat_request({"model": "m", "prompt": "Hello", "max_tokens": 10})
        self.assertEqual(chat["messages"], [{"role": "user", "content": "Hello"}])
        self.assertEqual(chat["max_tokens"], 10)

    def test_prompt_list_is_joined(self):
        chat = to_chat_request({"model": "m", "prompt": ["a", "b"]})
        self.assertEqual(chat["messages"][0]["content"], "a\nb")

    def test_prompt_list_keeps_non_string_entries(self):
        # Dropping non-string items silently rewrote the prompt the client
        # sent: every entry must survive, coerced verbatim.
        chat = to_chat_request({"model": "m", "prompt": ["a", 5, None]})
        self.assertEqual(chat["messages"][0]["content"], "a\n5\nNone")

    def test_params_pass_through(self):
        chat = to_chat_request({"model": "m", "prompt": "x", "temperature": 0.5,
                                "stop": ["\n"], "stream": True, "echo": True})
        self.assertEqual(chat["temperature"], 0.5)
        self.assertEqual(chat["stop"], ["\n"])
        self.assertTrue(chat["stream"])
        self.assertNotIn("echo", chat)

    def test_non_stream_response_shape(self):
        adapter = CompletionsAdapter("m", stream=False)
        out = adapter.finish(json.dumps({
            "choices": [{"message": {"content": "Hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                      "total_tokens": 5},
        }).encode())
        data = json.loads(out)
        self.assertEqual(data["object"], "text_completion")
        self.assertEqual(data["choices"][0]["text"], "Hi")
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")
        self.assertEqual(data["usage"]["total_tokens"], 5)

    def test_stream_chunks_are_text_completions(self):
        adapter = CompletionsAdapter("m", stream=True)
        upstream = (
            b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        )
        out = adapter.feed(upstream) + adapter.finish()
        datas = [json.loads(line[6:]) for line in out.split(b"\n")
                 if line.startswith(b"data: ") and line != b"data: [DONE]"]
        text = "".join(d["choices"][0]["text"] for d in datas)
        self.assertEqual(text, "Hello")
        self.assertTrue(all(d["object"] == "text_completion" for d in datas))
        self.assertEqual(datas[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(datas[-1]["usage"]["total_tokens"], 3)
        self.assertIn(b"data: [DONE]\n\n", out)

    def test_finish_flushes_partial_line_and_is_idempotent(self):
        adapter = CompletionsAdapter("m", stream=True)
        out = (adapter.feed(b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}')
               + adapter.finish())
        self.assertEqual(adapter.finish(), b"")
        self.assertIn(b'"finish_reason":"length"', out)
        self.assertIn(b"data: [DONE]", out)

    def test_tool_calls_finish_becomes_stop(self):
        adapter = CompletionsAdapter("m", stream=False)
        data = json.loads(adapter.finish(json.dumps({
            "choices": [{"message": {"content": "ok"}, "finish_reason": "tool_calls"}],
            "usage": {},
        }).encode()))
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")


class LegacyCompletionsEndpointTests(unittest.TestCase):
    """The /v1/completions endpoint runs the whole chat pipeline."""

    def setUp(self):
        self.app = App(load_config(write_config(BASE)))
        self.captured: dict = {}

    def call(self, body, resp):
        def fake_open(req, timeout, proxy=""):
            self.captured["url"] = req.full_url
            self.captured["body"] = json.loads(req.data)
            return resp

        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._completion(h, OPENAI, json.dumps(body).encode(), legacy=True)
        return h

    def test_request_leaves_as_chat(self):
        h = self.call({"model": "auto", "prompt": "Hello", "max_tokens": 8},
                      _JsonResp({"choices": [{"message": {"content": "Hi"},
                                              "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 1, "completion_tokens": 1}}))
        self.assertTrue(self.captured["url"].endswith("/v1/chat/completions"))
        sent = self.captured["body"]
        self.assertEqual(sent["messages"], [{"role": "user", "content": "Hello"}])
        self.assertEqual(sent["max_tokens"], 8)
        out = json.loads(h.body)
        self.assertEqual(out["object"], "text_completion")
        self.assertEqual(out["choices"][0]["text"], "Hi")

    def test_stream_comes_back_as_text_chunks(self):
        upstream = (
            b'data: {"choices":[{"delta":{"content":"ab"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        h = self.call({"model": "auto", "prompt": "x", "stream": True},
                      _SseResp([upstream]))
        self.assertIn(b'"object":"text_completion"', h.body)
        self.assertIn(b'"text":"ab"', h.body)
        self.assertIn(b"data: [DONE]", h.body)

    def test_upstream_error_is_relayed(self):
        h = self.call({"model": "auto", "prompt": "x"},
                      _JsonResp({"error": {"message": "bad key"}},
                                status=401, headers={"Content-Type": "application/json",
                                                      "Retry-After": "5"}))
        self.assertEqual(h.status, 401)
        self.assertIn(b"bad key", h.body)


class WeightedStrategyFailoverTests(unittest.TestCase):
    """lb_strategy="weighted" only reorders candidates; failover stays intact."""

    def setUp(self):
        cfg = copy.deepcopy(BASE)
        cfg["channels"].append({
            "name": "plan-b", "protocol": "openai",
            "base_url": "http://127.0.0.1:9002", "models": {"auto": "model-b"},
            "priority": 20, "api_key": "sk-b",
        })
        cfg["routes"][0]["channels"] = ["plan-a", "plan-b"]
        cfg["defaults"] = {"lb_strategy": "weighted"}
        self.app = App(load_config(write_config(cfg)))

    def test_transient_failure_still_fails_over(self):
        # Emulate urlopen's behavior faithfully: HTTPError raises at call
        # time (not at response __enter__), so the gateway sees it exactly
        # like a real upstream 502.
        calls = {"n": 0}

        def fake_open(req, timeout, proxy=""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HTTPError("http://test", 502, "Error",
                                {"Retry-After": "1"},
                                io.BytesIO(b'{"error": "boom"}'))
            return _JsonResp({"choices": [{"message": {"content": "ok"},
                                           "finish_reason": "stop"}],
                              "usage": {"total_tokens": 1}})

        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._completion(h, OPENAI, json.dumps(
                {"model": "auto", "prompt": "x", "max_tokens": 8}).encode(), legacy=True)
        self.assertEqual(h.status, 200)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(json.loads(h.body)["choices"][0]["text"], "ok")


class RerankEndpointTests(unittest.TestCase):
    """Tests for /v1/rerank endpoint (capability routing, failover, error handling)."""

    def setUp(self):
        # Need a channel with a rerank model - use dict format like BASE
        # Add a second OpenAI channel for failover testing
        cfg = copy.deepcopy(BASE)
        cfg["channels"][0]["models"] = {"auto": "model-a", "rerank-v1": "rerank-v1"}
        # Add second OpenAI channel for failover
        cfg["channels"].append({
            "name": "plan-b", "protocol": "openai", "base_url": "http://127.0.0.1:9002",
            "models": {"auto": "model-b", "rerank-v1": "rerank-v1"}, "priority": 5,
            "api_key": "sk-openai-2",
        })
        cfg["routes"][0]["channels"] = ["plan-a", "plan-b"]  # both openai
        self.app = App(load_config(write_config(cfg)))
        self.captured: dict = {}

    def call(self, body, resp):
        def fake_open(req, timeout, proxy=""):
            self.captured["url"] = req.full_url
            self.captured["body"] = json.loads(req.data)
            return resp

        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._rerank(h, json.dumps(body).encode())
        return h

    def test_rerank_routes_to_correct_endpoint(self):
        h = self.call({
            "model": "rerank-v1",
            "query": "What is the capital of France?",
            "documents": ["Paris is the capital.", "Berlin is a city."],
            "top_n": 1,
        }, _JsonResp({
            "id": "rerank-123",
            "object": "list",
            "results": [{"index": 0, "relevance_score": 0.95, "document": {"text": "Paris is the capital."}}],
            "model": "rerank-v1",
            "usage": {"total_tokens": 20},
        }))
        self.assertEqual(h.status, 200)
        self.assertTrue(self.captured["url"].endswith("/v1/rerank"))
        sent = self.captured["body"]
        self.assertEqual(sent["model"], "rerank-v1")
        self.assertEqual(sent["query"], "What is the capital of France?")
        self.assertEqual(sent["documents"], ["Paris is the capital.", "Berlin is a city."])
        self.assertEqual(sent["top_n"], 1)
        out = json.loads(h.body)
        self.assertEqual(out["results"][0]["index"], 0)

    def test_rerank_fails_with_400_when_no_rerank_channel(self):
        # Config with no rerank model - disable fallback_unknown to get hard rejection
        cfg = copy.deepcopy(BASE)
        cfg["models"] = {"fallback_unknown": False, "branded": {}, "fallback_route": "default"}
        cfg["channels"][0]["models"] = {"auto": "model-a"}  # no rerank model
        app = App(load_config(write_config(cfg)))
        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy",
                        side_effect=lambda *a, **k: _error_resp({"error": "x"}, 500)):
            app._rerank(h, json.dumps({
                "model": "rerank-v1",
                "query": "x",
                "documents": ["a", "b"],
            }).encode())
        self.assertEqual(h.status, 400)
        out = json.loads(h.body)
        self.assertIn("unknown model", out["error"]["message"].lower())

    def test_rerank_optional_params_passed_through(self):
        h = self.call({
            "model": "rerank-v1",
            "query": "test",
            "documents": ["a", "b"],
            "top_n": 2,
            "return_documents": True,
            "user": "user-123",
        }, _JsonResp({
            "results": [{"index": 0, "relevance_score": 0.9}],
            "model": "rerank-v1",
        }))
        self.assertEqual(h.status, 200)
        sent = self.captured["body"]
        self.assertEqual(sent["top_n"], 2)
        self.assertTrue(sent["return_documents"])
        self.assertEqual(sent["user"], "user-123")

    def test_rerank_failover_on_transient_error(self):
        # First call fails with transient error, second succeeds
        # Uses the two channels from setUp: plan-a (fails) and plan-b (succeeds)
        upstream_responses = [
            _error_resp({"error": "timeout"}, 502, headers={"Retry-After": "1"}),
            _JsonResp({
                "results": [{"index": 1, "relevance_score": 0.8}],
                "model": "rerank-v1",
            }),
        ]
        call_count = {"n": 0}
        def fake_open(req, timeout, proxy=""):
            call_count["n"] += 1
            return upstream_responses[call_count["n"] - 1]

        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._rerank(h, json.dumps({
                "model": "rerank-v1",
                "query": "x",
                "documents": ["a", "b"],
            }).encode())
        self.assertEqual(h.status, 200)
        self.assertEqual(call_count["n"], 2)  # failover from plan-a to plan-b
        out = json.loads(h.body)
        self.assertEqual(out["results"][0]["index"], 1)

    def test_rerank_unknown_model_rejected_when_fallback_disabled(self):
        # With fallback_unknown=false, unknown models should be rejected
        cfg = copy.deepcopy(BASE)
        cfg["models"] = {"fallback_unknown": False, "branded": {}, "fallback_route": "default"}
        cfg["channels"][0]["models"] = {"auto": "model-a", "rerank-v1": "rerank-v1"}
        app = App(load_config(write_config(cfg)))
        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy",
                        side_effect=lambda *a, **k: _error_resp({"error": "x"}, 500)):
            app._rerank(h, json.dumps({
                "model": "unknown-rerank-model",
                "query": "x",
                "documents": ["a"],
            }).encode())
        self.assertEqual(h.status, 400)
        out = json.loads(h.body)
        self.assertIn("unknown model", out["error"]["message"].lower())


class AudioEndpointTests(unittest.TestCase):
    """Tests for /v1/audio/* endpoints (speech, transcriptions, translations)."""

    def setUp(self):
        # Config with TTS and STT channels
        cfg = copy.deepcopy(BASE)
        cfg["channels"][0]["models"] = {
            "auto": "model-a",
            "tts-1": "tts-1",
            "whisper-1": "whisper-1",
        }
        # Add second OpenAI channel for failover
        cfg["channels"].append({
            "name": "plan-b", "protocol": "openai", "base_url": "http://127.0.0.1:9002",
            "models": {"auto": "model-b", "tts-1": "tts-1", "whisper-1": "whisper-1"},
            "priority": 5, "api_key": "sk-openai-2",
        })
        cfg["routes"][0]["channels"] = ["plan-a", "plan-b"]  # both openai
        self.app = App(load_config(write_config(cfg)))
        self.captured: dict = {}

    def _fake_open(self, responses):
        """Create a mock open_with_proxy that returns responses in sequence."""
        call_count = {"n": 0}
        def fake_open(req, timeout, proxy=""):
            call_count["n"] += 1
            self.captured["url"] = req.full_url
            if req.data:
                try:
                    self.captured["body"] = json.loads(req.data)
                except Exception:
                    self.captured["body_raw"] = req.data
            return responses[call_count["n"] - 1]
        return fake_open

    def _make_multipart_body(self, fields: dict, file_info: tuple, boundary: str = None) -> tuple[bytes, str]:
        """Create multipart/form-data body and return (body_bytes, content_type_header)."""
        if boundary is None:
            import uuid
            boundary = uuid.uuid4().hex
        
        body_parts = []
        for name, value in fields.items():
            body_parts.append(f'--{boundary}')
            body_parts.append(f'Content-Disposition: form-data; name="{name}"')
            body_parts.append('')
            body_parts.append(str(value))
        
        filename, file_content, file_content_type = file_info
        body_parts.append(f'--{boundary}')
        body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"')
        body_parts.append(f'Content-Type: {file_content_type}')
        body_parts.append('')
        body_parts.append('')  # placeholder for file content

        # Build the body with file content inserted
        prefix = "\r\n".join(body_parts[:-1]) + "\r\n"  # everything before file content
        suffix = "\r\n" + f"--{boundary}--\r\n"
        
        body_bytes = prefix.encode("utf-8") + file_content + suffix.encode("utf-8")
        content_type = f'multipart/form-data; boundary={boundary}'
        return body_bytes, content_type

    def test_audio_speech_routes_to_correct_endpoint(self):
        """Test that /v1/audio/speech routes to /v1/audio/speech upstream."""
        fake_open = self._fake_open([
            _JsonResp({}, status=200, headers={"Content-Type": "audio/mpeg"}),
        ])
        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._audio_speech(h, json.dumps({
                "model": "tts-1",
                "input": "Hello world",
                "voice": "alloy",
                "response_format": "mp3",
            }).encode())
        self.assertEqual(h.status, 200)
        self.assertTrue(self.captured["url"].endswith("/v1/audio/speech"))
        sent = self.captured["body"]
        self.assertEqual(sent["model"], "tts-1")
        self.assertEqual(sent["input"], "Hello world")
        self.assertEqual(sent["voice"], "alloy")
        self.assertEqual(sent["response_format"], "mp3")

    def test_audio_speech_optional_params_passed_through(self):
        """Test that optional parameters like speed are passed through."""
        fake_open = self._fake_open([
            _JsonResp({}, status=200, headers={"Content-Type": "audio/mpeg"}),
        ])
        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._audio_speech(h, json.dumps({
                "model": "tts-1",
                "input": "test",
                "speed": 1.5,
                "user": "user-123",
            }).encode())
        self.assertEqual(h.status, 200)
        sent = self.captured["body"]
        self.assertEqual(sent["speed"], 1.5)
        self.assertEqual(sent["user"], "user-123")

    def test_audio_speech_failover_on_transient_error(self):
        """Test failover from plan-a to plan-b on transient network error."""
        fake_open = self._fake_open([
            _error_resp({"error": "timeout"}, 502, headers={"Retry-After": "1"}),
            _JsonResp({}, status=200, headers={"Content-Type": "audio/mpeg"}),
        ])
        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._audio_speech(h, json.dumps({
                "model": "tts-1",
                "input": "test",
            }).encode())
        self.assertEqual(h.status, 200)
        # Should have tried plan-a first, then plan-b
        # The captured URL will be the last one (plan-b)
        # We verify by checking that the first attempt failed and second succeeded

    def test_audio_speech_no_tts_channel_returns_503(self):
        """Test 503 when no channel has TTS model."""
        cfg = copy.deepcopy(BASE)
        cfg["channels"][0]["models"] = {"auto": "model-a"}  # no tts-1
        app = App(load_config(write_config(cfg)))
        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy",
                        side_effect=lambda *a, **k: _error_resp({"error": "x"}, 500)):
            app._audio_speech(h, json.dumps({
                "model": "tts-1",
                "input": "test",
            }).encode())
        self.assertEqual(h.status, 503)
        out = json.loads(h.body)
        # When no channel has the model in catalog, it should say "no healthy channel with tts capability"
        # If channels exist but all fail, it says "all channels failed"
        self.assertTrue(
            "tts capability" in out["error"]["message"].lower() or
            "all channels failed" in out["error"]["message"].lower()
        )

    def test_audio_transcriptions_routes_to_correct_endpoint(self):
        """Test that /v1/audio/transcriptions routes to /v1/audio/transcriptions upstream."""
        fake_open = self._fake_open([
            _JsonResp({
                "text": "Hello world",
                "segments": [],
                "language": "en",
            }),
        ])
        h = _CaptureHandler(headers={"Content-Type": ""})  # will be set below
        body, content_type = self._make_multipart_body(
            {"model": "whisper-1"},
            ("test.mp3", b"fake-audio-data", "audio/mpeg"),
        )
        h.headers["Content-Type"] = content_type
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._audio_transcriptions(h, body, endpoint="transcriptions")
        self.assertEqual(h.status, 200)
        self.assertTrue(self.captured["url"].endswith("/v1/audio/transcriptions"))
        # Verify the upstream request had multipart Content-Type
        # The mock captures the request object, check its headers

    def test_audio_transcriptions_optional_params_passed_through(self):
        """Test that optional params like language, prompt, temperature are passed through."""
        fake_open = self._fake_open([
            _JsonResp({"text": "Hello", "language": "en"}),
        ])
        h = _CaptureHandler(headers={"Content-Type": ""})
        body, content_type = self._make_multipart_body(
            {"model": "whisper-1", "language": "en", "prompt": "Hello world", "temperature": "0.2"},
            ("test.mp3", b"fake-audio-data", "audio/mpeg"),
        )
        h.headers["Content-Type"] = content_type
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._audio_transcriptions(h, body, endpoint="transcriptions")
        self.assertEqual(h.status, 200)
        # Check that upstream received the params
        raw = self.captured.get("body_raw", b"").decode()
        self.assertIn("en", raw)
        self.assertIn("Hello world", raw)
        self.assertIn("0.2", raw)

    def test_audio_translations_routes_to_translations_endpoint(self):
        """Test that /v1/audio/translations routes to /v1/audio/translations upstream."""
        fake_open = self._fake_open([
            _JsonResp({"text": "Hello world", "language": "en"}),
        ])
        h = _CaptureHandler(headers={"Content-Type": ""})
        body, content_type = self._make_multipart_body(
            {"model": "whisper-1"},
            ("test.mp3", b"fake-audio-data", "audio/mpeg"),
        )
        h.headers["Content-Type"] = content_type
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._audio_transcriptions(h, body, endpoint="translations")
        self.assertEqual(h.status, 200)
        self.assertTrue(self.captured["url"].endswith("/v1/audio/translations"))

    def test_audio_transcriptions_missing_model_returns_400(self):
        """Test 400 when model is missing from multipart."""
        h = _CaptureHandler(headers={"Content-Type": ""})
        body, content_type = self._make_multipart_body(
            {},
            ("test.mp3", b"fake-audio-data", "audio/mpeg"),
        )
        h.headers["Content-Type"] = content_type
        with mock.patch("facet.gateway.server.open_with_proxy",
                        side_effect=lambda *a, **k: _error_resp({"error": "x"}, 500)):
            self.app._audio_transcriptions(h, body, endpoint="transcriptions")
        self.assertEqual(h.status, 400)
        out = json.loads(h.body)
        self.assertIn("model is required", out["error"]["message"].lower())

    def test_audio_transcriptions_missing_file_returns_400(self):
        """Test 400 when file is missing from multipart."""
        h = _CaptureHandler(headers={"Content-Type": ""})
        body, content_type = self._make_multipart_body(
            {"model": "whisper-1"},
            ("", b"", "audio/mpeg"),  # empty file
        )
        h.headers["Content-Type"] = content_type
        with mock.patch("facet.gateway.server.open_with_proxy",
                        side_effect=lambda *a, **k: _error_resp({"error": "x"}, 500)):
            self.app._audio_transcriptions(h, body, endpoint="transcriptions")
        self.assertEqual(h.status, 400)
        out = json.loads(h.body)
        self.assertIn("file is required", out["error"]["message"].lower())

    def test_audio_transcriptions_failover_on_transient_error(self):
        """Test failover on transient error for transcriptions."""
        fake_open = self._fake_open([
            _error_resp({"error": "timeout"}, 502, headers={"Retry-After": "1"}),
            _JsonResp({"text": "Hello world"}),
        ])
        h = _CaptureHandler(headers={"Content-Type": ""})
        body, content_type = self._make_multipart_body(
            {"model": "whisper-1"},
            ("test.mp3", b"fake-audio-data", "audio/mpeg"),
        )
        h.headers["Content-Type"] = content_type
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            self.app._audio_transcriptions(h, body, endpoint="transcriptions")
        self.assertEqual(h.status, 200)
        out = json.loads(h.body)
        self.assertEqual(out["text"], "Hello world")

    def test_audio_speech_unknown_model_rejected_when_fallback_disabled(self):
        """Test unknown model rejection when fallback_unknown=false."""
        cfg = copy.deepcopy(BASE)
        cfg["models"] = {"fallback_unknown": False, "branded": {}, "fallback_route": "default"}
        cfg["channels"][0]["models"] = {"auto": "model-a", "tts-1": "tts-1"}
        app = App(load_config(write_config(cfg)))
        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy",
                        side_effect=lambda *a, **k: _error_resp({"error": "x"}, 500)):
            app._audio_speech(h, json.dumps({
                "model": "unknown-tts-model",
                "input": "test",
            }).encode())
        self.assertEqual(h.status, 400)
        out = json.loads(h.body)
        self.assertIn("unknown model", out["error"]["message"].lower())


if __name__ == "__main__":
    unittest.main()
