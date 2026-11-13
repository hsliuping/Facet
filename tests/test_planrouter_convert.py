"""P1-3: cross-protocol conversion at the channel boundary.

Request/response round-trips (tool calls included), the streaming
StreamConverter, and the _forward/_relay integration: an openai client
must be able to use an anthropic channel and vice versa.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from facet.gateway.convert import (
    DEFAULT_MAX_TOKENS, convert_request, convert_response, inject_system,
    stream_converter,
)
from facet.gateway.core import ANTHROPIC, OPENAI, load_config
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
        {"name": "anth", "protocol": "openai", "channels": ["claude"]},
        {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
    ],
}


def write_config(data: dict) -> Path:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(data, tmp)
    tmp.close()
    return Path(tmp.name)


OPENAI_TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}]

OPENAI_TOOL_REQUEST = {
    "model": "auto",
    "messages": [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "Weather in Paris?"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "18C rainy"},
    ],
    "tools": OPENAI_TOOLS,
    "tool_choice": "auto",
    "max_tokens": 128,
}


class RequestConversionTests(unittest.TestCase):
    """openai <-> anthropic request shapes, tools and tool results."""

    def test_same_protocol_is_byte_identical(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        self.assertIs(convert_request(body, OPENAI, OPENAI), body)
        self.assertIs(convert_request(body, ANTHROPIC, ANTHROPIC), body)

    def test_openai_to_anthropic_tools_and_results(self):
        out = convert_request(OPENAI_TOOL_REQUEST, OPENAI, ANTHROPIC)
        self.assertEqual(out["max_tokens"], 128)
        self.assertEqual(out["system"], "Be terse.")
        self.assertEqual(out["tools"], [{
            "name": "get_weather", "description": "Get the weather",
            "input_schema": OPENAI_TOOLS[0]["function"]["parameters"],
        }])
        self.assertEqual(out["tool_choice"], {"type": "auto"})
        # tool_calls -> tool_use blocks, role:"tool" -> tool_result block
        assistant = out["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"], [{
            "type": "tool_use", "id": "call_1", "name": "get_weather",
            "input": {"city": "Paris"},
        }])
        results = out["messages"][2]
        self.assertEqual(results["role"], "user")
        self.assertEqual(results["content"], [{
            "type": "tool_result", "tool_use_id": "call_1", "content": "18C rainy",
        }])

    def test_anthropic_to_openai_tools_and_results(self):
        body = {
            "model": "claude-x", "max_tokens": 64, "system": "Be terse.",
            "messages": [
                {"role": "user", "content": "Weather in Paris?"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                     "input": {"city": "Paris"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1",
                     "content": "18C rainy"},
                ]},
            ],
            "tools": [{"name": "get_weather", "description": "d",
                       "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "get_weather"},
            "stop_sequences": ["STOP"],
        }
        out = convert_request(body, ANTHROPIC, OPENAI)
        self.assertEqual(out["messages"][0],
                         {"role": "system", "content": "Be terse."})
        self.assertEqual(out["messages"][3], {
            "role": "tool", "tool_call_id": "toolu_1", "content": "18C rainy"})
        assistant = out["messages"][2]
        self.assertEqual(assistant["content"], "Checking.")
        self.assertEqual(assistant["tool_calls"], [{
            "id": "toolu_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }])
        self.assertEqual(out["tools"], [{
            "type": "function",
            "function": {"name": "get_weather", "description": "d",
                         "parameters": {"type": "object"}},
        }])
        self.assertEqual(out["tool_choice"],
                         {"type": "function", "function": {"name": "get_weather"}})
        self.assertEqual(out["stop"], ["STOP"])
        self.assertEqual(out["max_tokens"], 64)

    def test_tool_choice_matrix(self):
        base = {"model": "m", "messages": [], "tools": OPENAI_TOOLS}
        self.assertEqual(convert_request({**base, "tool_choice": "required"},
                                         OPENAI, ANTHROPIC)["tool_choice"],
                         {"type": "any"})
        none_out = convert_request({**base, "tool_choice": "none"}, OPENAI, ANTHROPIC)
        self.assertNotIn("tools", none_out)
        self.assertEqual(convert_request(
            {"model": "m", "messages": [], "tools": [{"name": "t", "input_schema": {}}],
             "tool_choice": {"type": "any"}}, ANTHROPIC, OPENAI)["tool_choice"],
            "required")

    def test_max_tokens_defaults_for_anthropic(self):
        out = convert_request({"model": "m", "messages": []}, OPENAI, ANTHROPIC)
        self.assertEqual(out["max_tokens"], DEFAULT_MAX_TOKENS)

    def test_images_map_both_ways(self):
        url = "data:image/png;base64,AAAA"
        body = {"model": "m", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": url}},
        ]}]}
        anth = convert_request(body, OPENAI, ANTHROPIC)
        self.assertEqual(anth["messages"][0]["content"][1], {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        })
        back = convert_request(anth, ANTHROPIC, OPENAI)
        self.assertEqual(back["messages"][0]["content"][1],
                         {"type": "image_url", "image_url": {"url": url}})

    def test_round_trip_keeps_tool_semantics(self):
        there = convert_request(OPENAI_TOOL_REQUEST, OPENAI, ANTHROPIC)
        back = convert_request(there, ANTHROPIC, OPENAI)
        self.assertEqual(back["messages"][0]["content"], "Be terse.")
        self.assertEqual(back["messages"][2]["tool_calls"][0]["function"]["name"],
                         "get_weather")
        self.assertEqual(json.loads(back["messages"][2]["tool_calls"][0]["function"]["arguments"]),
                         {"city": "Paris"})
        self.assertEqual(back["messages"][3]["tool_call_id"], "call_1")


class ResponseConversionTests(unittest.TestCase):
    def test_anthropic_response_to_openai(self):
        data = {
            "id": "msg_1", "model": "claude-x",
            "content": [{"type": "text", "text": "Hi"},
                        {"type": "tool_use", "id": "toolu_1", "name": "f",
                         "input": {"a": 1}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 4,
                      "output_tokens": 5},
        }
        out = convert_response(data, ANTHROPIC, OPENAI)
        message = out["choices"][0]["message"]
        self.assertEqual(message["content"], "Hi")
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "f")
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]),
                         {"a": 1})
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(out["usage"], {
            "prompt_tokens": 14, "completion_tokens": 5, "total_tokens": 19,
            "prompt_tokens_details": {"cached_tokens": 4},
        })

    def test_openai_response_to_anthropic(self):
        data = {
            "id": "c1", "model": "model-a",
            "choices": [{"message": {"role": "assistant", "content": "Hi",
                                     "tool_calls": [{
                                         "id": "call_1", "type": "function",
                                         "function": {"name": "f", "arguments": '{"a":1}'},
                                     }]},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3,
                      "prompt_tokens_details": {"cached_tokens": 2}},
        }
        out = convert_response(data, OPENAI, ANTHROPIC)
        self.assertEqual(out["stop_reason"], "max_tokens")
        self.assertEqual(out["content"][0], {"type": "text", "text": "Hi"})
        self.assertEqual(out["content"][1]["input"], {"a": 1})
        self.assertEqual(out["usage"], {"input_tokens": 6, "output_tokens": 3,
                                        "cache_read_input_tokens": 2})

    def test_finish_reason_normalization(self):
        for finish, stop in (("stop", "end_turn"), ("length", "max_tokens"),
                             ("tool_calls", "tool_use")):
            out = convert_response(
                {"choices": [{"message": {"content": "x"}, "finish_reason": finish}],
                 "usage": {}}, OPENAI, ANTHROPIC)
            self.assertEqual(out["stop_reason"], stop)


class InjectSystemTests(unittest.TestCase):
    def test_empty_prompt_is_a_noop(self):
        body = {"messages": []}
        self.assertIs(inject_system(body, ""), body)

    def test_prepends_to_string_system(self):
        out = inject_system({"system": "Mine"}, "Injected")
        self.assertEqual(out["system"], "Injected\n\nMine")

    def test_prepends_to_block_system(self):
        out = inject_system({"system": [{"type": "text", "text": "Mine"}]}, "Injected")
        self.assertEqual(out["system"][0], {"type": "text", "text": "Injected"})
        self.assertEqual(out["system"][1], {"type": "text", "text": "Mine"})

    def test_openai_shape_gets_a_system_message(self):
        out = inject_system({"messages": [{"role": "user", "content": "hi"}]}, "Injected")
        self.assertEqual(out["messages"][0], {"role": "system", "content": "Injected"})
        self.assertEqual(out["messages"][1]["role"], "user")


def _sse(*events: dict) -> bytes:
    return b"".join(
        f'event: {e["type"]}\ndata: {json.dumps(e, separators=(",", ":"))}\n\n'.encode()
        for e in events)


class StreamConverterTests(unittest.TestCase):
    def test_anthropic_stream_to_openai(self):
        upstream = _sse(
            {"type": "message_start", "message": {
                "id": "msg_1", "model": "claude-x",
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 4}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Hel"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "lo"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        )
        converter = stream_converter(ANTHROPIC, OPENAI)
        out = b""
        for i in range(0, len(upstream), 17):  # arbitrary split points
            out += converter.feed(upstream[i:i + 17])
        out += converter.finish()
        chunks = [json.loads(line[6:]) for line in out.split(b"\n")
                  if line.startswith(b"data: ") and line != b"data: [DONE]"]
        self.assertIn(b"data: [DONE]\n\n", out)
        texts = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        self.assertEqual(texts, "Hello")
        final = chunks[-1]
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")
        self.assertEqual(final["usage"], {
            "prompt_tokens": 14, "completion_tokens": 5, "total_tokens": 19,
            "prompt_tokens_details": {"cached_tokens": 4},
        })

    def test_anthropic_tool_stream_to_openai(self):
        upstream = _sse(
            {"type": "message_start", "message": {"id": "m", "model": "c",
                                                  "usage": {"input_tokens": 1}}},
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "toolu_1",
                               "name": "get_weather", "input": {}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"city":'}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        )
        converter = stream_converter(ANTHROPIC, OPENAI)
        out = converter.feed(upstream) + converter.finish()
        chunks = [json.loads(line[6:]) for line in out.split(b"\n")
                  if line.startswith(b"data: ") and line != b"data: [DONE]"]
        tool_chunks = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks
                       if c["choices"][0]["delta"].get("tool_calls")]
        first = tool_chunks[0]
        self.assertEqual(first["id"], "toolu_1")
        self.assertEqual(first["function"]["name"], "get_weather")
        arguments = "".join(
            c["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments") or ""
            for c in chunks if c["choices"][0]["delta"].get("tool_calls"))
        self.assertEqual(json.loads(arguments), {"city": "Paris"})
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_openai_stream_to_anthropic(self):
        upstream = (
            b'data: {"choices":[{"delta":{"role":"assistant","content":"He"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
            b'"function":{"name":"f","arguments":""}}]}}]}\n\n'
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            b'"function":{"arguments":"{\\"a\\""}}]}}]}\n\n'
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            b'"function":{"arguments":":1}"}}]}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
            b'"usage":{"prompt_tokens":8,"completion_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        )
        converter = stream_converter(OPENAI, ANTHROPIC)
        out = converter.feed(upstream) + converter.finish()
        events = []
        for line in out.split(b"\n"):
            if line.startswith(b"event: "):
                events.append(line[7:].decode())
        self.assertEqual(events[0], "message_start")
        self.assertEqual(events[-1], "message_stop")
        datas = [json.loads(line[6:]) for line in out.split(b"\n")
                 if line.startswith(b"data: ")]
        texts = "".join(d["delta"].get("text") or "" for d in datas
                        if d.get("type") == "content_block_delta"
                        and d["delta"].get("type") == "text_delta")
        self.assertEqual(texts, "Hello")
        partials = "".join(d["delta"].get("partial_json") or "" for d in datas
                           if d.get("type") == "content_block_delta"
                           and d["delta"].get("type") == "input_json_delta")
        self.assertEqual(json.loads(partials), {"a": 1})
        start = next(d for d in datas if d.get("type") == "content_block_start"
                     and d["content_block"]["type"] == "tool_use")
        self.assertEqual(start["content_block"]["id"], "call_1")
        self.assertEqual(start["content_block"]["name"], "f")
        delta = next(d for d in datas if d.get("type") == "message_delta")
        self.assertEqual(delta["delta"]["stop_reason"], "tool_use")
        self.assertEqual(delta["usage"], {"input_tokens": 8, "output_tokens": 3})

    def test_finish_is_idempotent_and_flushes_partial_line(self):
        converter = stream_converter(ANTHROPIC, OPENAI)
        out = converter.feed(b'data: {"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
                             b'"usage":{"output_tokens":1}}') + converter.finish()
        self.assertEqual(converter.finish(), b"")
        self.assertIn(b'"finish_reason":"length"', out)
        self.assertIn(b"data: [DONE]", out)

    def test_same_protocol_needs_no_converter(self):
        self.assertIsNone(stream_converter(OPENAI, OPENAI))
        self.assertIsNone(stream_converter(ANTHROPIC, ANTHROPIC))


class _CaptureHandler:
    """Handler stand-in that records the relayed response."""

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
    """Fake non-streaming upstream response."""

    def __init__(self, payload: dict, headers=None):
        self._data = json.dumps(payload).encode("utf-8")
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


class _SseResp:
    """Fake streaming upstream response yielding prepared chunks."""

    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
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


class CrossProtocolForwardTests(unittest.TestCase):
    """An openai client routed to an anthropic channel (and back)."""

    def setUp(self):
        self.app = App(load_config(write_config(BASE)))
        self.channel = self.app.config.channel("claude")
        self.captured: dict = {}

    def forward(self, resp, body, protocol=OPENAI):
        def fake_open(req, timeout, proxy=""):
            self.captured["url"] = req.full_url
            self.captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            self.captured["body"] = json.loads(req.data)
            return resp

        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            error = self.app._forward(h, protocol, self.channel, "claude-sonnet-4-5",
                                      body, 0, False)
        self.assertIsNone(error)
        return h

    def test_request_leaves_in_anthropic_shape(self):
        resp = _JsonResp({
            "id": "msg_1", "type": "message", "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        })
        self.forward(resp, dict(OPENAI_TOOL_REQUEST))
        self.assertTrue(self.captured["url"].endswith("/v1/messages"))
        sent = self.captured["body"]
        self.assertEqual(sent["system"], "Be terse.")
        self.assertEqual(sent["max_tokens"], 128)
        self.assertEqual(sent["tools"][0]["input_schema"],
                         OPENAI_TOOLS[0]["function"]["parameters"])
        self.assertEqual(sent["messages"][1]["content"][0]["type"], "tool_use")
        self.assertEqual(sent["messages"][2]["content"][0]["type"], "tool_result")
        headers = self.captured["headers"]
        self.assertEqual(headers["x-api-key"], "sk-ant")
        self.assertIn("anthropic-version", headers)
        self.assertNotIn("authorization", headers)

    def test_response_comes_back_in_openai_shape(self):
        resp = _JsonResp({
            "id": "msg_1", "type": "message", "model": "claude-sonnet-4-5",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                         "input": {"city": "Paris"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        })
        h = self.forward(resp, {"model": "auto", "messages": [
            {"role": "user", "content": "Weather?"}]})
        out = json.loads(h.body)
        self.assertEqual(out["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(out["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
                         "get_weather")
        self.assertEqual(out["usage"]["prompt_tokens"], 3)
        self.assertEqual(h.resp_headers["Content-Type"], "application/json")

    def test_stream_is_converted_to_openai_chunks(self):
        upstream = _sse(
            {"type": "message_start", "message": {"id": "m", "model": "claude-x",
                                                  "usage": {"input_tokens": 10}}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        )
        h = self.forward(_SseResp([upstream]),
                         {"model": "auto", "messages": [], "stream": True})
        self.assertIn(b'"object":"chat.completion.chunk"', h.body)
        self.assertIn(b"data: [DONE]", h.body)
        self.assertIn(b'"content":"Hi"', h.body)

    def test_same_protocol_stream_stays_byte_identical(self):
        chunks = [b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n']
        channel = self.app.config.channel("plan-a")

        def fake_open(req, timeout, proxy=""):
            self.captured["url"] = req.full_url
            return _SseResp(chunks)

        h = _CaptureHandler()
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            error = self.app._forward(h, OPENAI, channel, "model-a",
                                      {"model": "auto", "messages": [], "stream": True},
                                      0, False)
        self.assertIsNone(error)
        self.assertEqual(h.body, chunks[0])
        self.assertTrue(self.captured["url"].endswith("/v1/chat/completions"))

    def test_anthropic_client_to_openai_channel(self):
        # The other direction: anthropic client, openai channel.
        channel = self.app.config.channel("plan-a")
        captured = {}

        def fake_open(req, timeout, proxy=""):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["body"] = json.loads(req.data)
            return _JsonResp({
                "id": "c1", "model": "model-a",
                "choices": [{"message": {"role": "assistant", "content": "Hi",
                                         "tool_calls": [{
                                             "id": "call_1", "type": "function",
                                             "function": {"name": "f",
                                                          "arguments": '{"a":1}'},
                                         }]},
                             "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            })

        h = _CaptureHandler()
        body = {
            "model": "auto", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "f", "description": "d",
                       "input_schema": {"type": "object"}}],
        }
        with mock.patch("facet.gateway.server.open_with_proxy", side_effect=fake_open):
            error = self.app._forward(h, ANTHROPIC, channel, "model-a", body, 0, False)
        self.assertIsNone(error)
        self.assertTrue(captured["url"].endswith("/v1/chat/completions"))
        self.assertEqual(captured["headers"].get("authorization"), "Bearer sk-openai")
        self.assertNotIn("x-api-key", captured["headers"])
        self.assertEqual(captured["body"]["tools"][0]["function"]["name"], "f")
        out = json.loads(h.body)
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertEqual(out["content"][1]["type"], "tool_use")
        self.assertEqual(out["content"][1]["input"], {"a": 1})

    def test_system_prompt_injection_reaches_the_wire(self):
        # The injection happens in _completion (route lookup), so exercise the
        # real inbound path and capture what _forward would put on the wire.
        data = json.loads(json.dumps(BASE))
        data["routes"][0]["system_prompt"] = "Injected rule."
        app = App(load_config(write_config(data)))
        h = _CaptureHandler()
        raw = json.dumps({"model": "auto", "messages": [
            {"role": "user", "content": "hi"}]}).encode()
        with mock.patch.object(App, "_forward", return_value=None) as fwd:
            app._completion(h, OPENAI, raw)
        sent = fwd.call_args[0][4]
        self.assertEqual(sent["messages"][0],
                         {"role": "system", "content": "Injected rule."})
        self.assertEqual(sent["messages"][1]["role"], "user")

    def test_no_system_prompt_leaves_body_untouched(self):
        app = App(load_config(write_config(BASE)))
        h = _CaptureHandler()
        raw = json.dumps({"model": "auto", "messages": [
            {"role": "user", "content": "hi"}]}).encode()
        with mock.patch.object(App, "_forward", return_value=None) as fwd:
            app._completion(h, OPENAI, raw)
        sent = fwd.call_args[0][4]
        self.assertEqual(sent["messages"], [{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()
