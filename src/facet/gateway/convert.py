"""Cross-protocol conversion at the channel boundary (P1-3).

A route speaks the client's wire protocol (picked by the endpoint path); a
channel speaks its own. When they differ, the payload is converted right
before it leaves for the upstream and the answer is converted back on return.
Tool calls and tool results map both ways (openai assistant.tool_calls /
role:"tool" <-> anthropic tool_use / tool_result blocks), system messages map
to the top-level `system` parameter, and images map between image_url parts
and source blocks. Identical protocols skip all of this: the request stays a
byte-for-byte passthrough.
"""
from __future__ import annotations

import json
import time
import uuid

from .core import ANTHROPIC, OPENAI

# Anthropic requires max_tokens; the client protocol may omit it entirely.
DEFAULT_MAX_TOKENS = 4096

# Response-level finish/stop reasons (openai -> anthropic and back).
_FINISH_TO_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
_STOP_TO_FINISH = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls",
                   "stop_sequence": "stop"}


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict))
    return ""


def _system_text(system) -> str:
    """Anthropic `system` is a string or a list of text blocks."""
    return system if isinstance(system, str) else _text_of(system)


def _json_object(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _image_block(part) -> dict:
    """openai image_url part -> anthropic image block."""
    src = part.get("image_url")
    url = str(src.get("url") if isinstance(src, dict) else (src or ""))
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        media = head[5:].split(";")[0] or "image/png"
        return {"type": "image",
                "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _image_part(block) -> dict:
    """anthropic image block -> openai image_url part."""
    source = block.get("source") or {}
    if source.get("type") == "base64":
        url = "data:%s;base64,%s" % (source.get("media_type") or "image/png",
                                     source.get("data") or "")
    else:
        url = str(source.get("url") or "")
    return {"type": "image_url", "image_url": {"url": url}}


def _result_body(content):
    """Tool result payload shared by both protocols' shapes."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [{"type": "text", "text": str(p.get("text") or "")}
                  for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return blocks or ""
    return _text_of(content)


def inject_system(body: dict, prompt: str) -> dict:
    """Prepend a route-level system prompt without clobbering the client's own.

    Protocol-agnostic: works on the anthropic `system` parameter or on an
    openai-style system message. Empty prompt leaves the body untouched.
    """
    if not prompt:
        return body
    out = dict(body)
    if out.get("system") is not None:
        existing = out["system"]
        if isinstance(existing, str):
            out["system"] = prompt + "\n\n" + existing
        else:
            out["system"] = [{"type": "text", "text": prompt}, *existing]
        return out
    messages = list(out.get("messages") or [])
    messages.insert(0, {"role": "system", "content": prompt})
    out["messages"] = messages
    return out


def convert_request(body: dict, src: str, dst: str) -> dict:
    """Convert a chat request payload from protocol `src` to protocol `dst`."""
    if src == dst:
        return body
    return _to_anthropic(body) if dst == ANTHROPIC else _to_openai(body)


def _to_anthropic(body: dict) -> dict:
    out: dict = {
        "model": body.get("model"),
        "max_tokens": int(body.get("max_tokens")
                          or body.get("max_completion_tokens")
                          or DEFAULT_MAX_TOKENS),
    }
    system_parts: list[str] = []
    messages: list[dict] = []
    results: list[dict] = []

    def flush_results() -> None:
        if results:
            messages.append({"role": "user", "content": list(results)})
            results.clear()

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role in ("system", "developer"):
            system_parts.append(_text_of(content))
            continue
        if role == "tool":
            results.append({"type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": _result_body(content)})
            continue
        flush_results()
        if role == "assistant":
            blocks: list[dict] = []
            if isinstance(content, str):
                if content:
                    blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        blocks.append({"type": "text", "text": str(part.get("text") or "")})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                blocks.append({"type": "tool_use",
                               "id": str(call.get("id") or ""),
                               "name": str(fn.get("name") or ""),
                               "input": _json_object(fn.get("arguments"))})
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
            continue
        # user (or any unknown role treated as user input)
        if isinstance(content, list):
            blocks = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    blocks.append({"type": "text", "text": str(part.get("text") or "")})
                elif part.get("type") == "image_url":
                    blocks.append(_image_block(part))
            if blocks:
                messages.append({"role": "user", "content": blocks})
        else:
            messages.append({"role": "user", "content": _text_of(content)})
    flush_results()

    out["messages"] = messages
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    for key in ("temperature", "top_p", "top_k", "stream"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("stop"):
        stop = body["stop"]
        out["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    if body.get("metadata"):
        user = (body["metadata"] or {}).get("user") if isinstance(body["metadata"], dict) else None
        if user:
            out["metadata"] = {"user_id": str(user)}
    if body.get("tools"):
        out["tools"] = []
        for tool in body["tools"]:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") or tool
            out["tools"].append({
                "name": str(fn.get("name") or ""),
                "description": str(fn.get("description") or ""),
                "input_schema": fn.get("input_schema") or fn.get("parameters")
                                or {"type": "object"},
            })
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            name = (choice.get("function") or {}).get("name")
            if name:
                out["tool_choice"] = {"type": "tool", "name": name}
            elif choice.get("type") == "none":
                out.pop("tools")  # "none" means no tool calls: closest match
        elif choice == "required":
            out["tool_choice"] = {"type": "any"}
        elif choice == "none":
            out.pop("tools")
        elif choice:
            out["tool_choice"] = {"type": "auto"}
    return out


def _to_openai(body: dict) -> dict:
    out: dict = {"model": body.get("model")}
    messages: list[dict] = []
    if body.get("system"):
        messages.append({"role": "system", "content": _system_text(body["system"])})
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        blocks = content if isinstance(content, list) else \
            [{"type": "text", "text": _text_of(content)}]
        if role == "assistant":
            texts: list[str] = []
            tool_calls: list[dict] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    texts.append(str(block.get("text") or ""))
                elif kind == "tool_use":
                    tool_calls.append({
                        "id": str(block.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("input") or {},
                                                    ensure_ascii=False),
                        },
                    })
            item: dict = {"role": "assistant",
                          "content": "".join(texts) or (None if tool_calls else "")}
            if tool_calls:
                item["tool_calls"] = tool_calls
            messages.append(item)
            continue
        # user side: tool_result blocks become role:"tool" messages first
        parts: list[dict] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                messages.append({"role": "tool",
                                 "tool_call_id": str(block.get("tool_use_id") or ""),
                                 "content": _result_body(block.get("content"))})
            elif kind == "text":
                parts.append({"type": "text", "text": str(block.get("text") or "")})
            elif kind == "image":
                parts.append(_image_part(block))
        if parts:
            if all(p.get("type") == "text" for p in parts):
                messages.append({"role": "user",
                                 "content": "".join(p["text"] for p in parts)})
            else:
                messages.append({"role": "user", "content": parts})
    out["messages"] = messages
    for key in ("temperature", "top_p", "stream"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("metadata"):
        user = (body["metadata"] or {}).get("user_id") if isinstance(body["metadata"], dict) else None
        if user:
            out["user"] = str(user)
    if body.get("tools"):
        out["tools"] = []
        for tool in body["tools"]:
            if not isinstance(tool, dict):
                continue
            out["tools"].append({
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema") or {"type": "object"},
                },
            })
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            if choice.get("type") == "tool":
                out["tool_choice"] = {"type": "function",
                                      "function": {"name": choice.get("name")}}
            elif choice.get("type") == "any":
                out["tool_choice"] = "required"
            else:
                out["tool_choice"] = "auto"
        elif choice == "any":
            out["tool_choice"] = "required"
        else:
            out["tool_choice"] = "auto"
    return out


def convert_response(data: dict, src: str, dst: str) -> dict:
    """Convert a non-streaming chat response from protocol `src` to `dst`."""
    if src == dst:
        return data
    return _response_to_openai(data) if src == ANTHROPIC else _response_to_anthropic(data)


def _response_to_openai(data: dict) -> dict:
    blocks = data.get("content") or []
    text = "".join(str(b.get("text") or "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text")
    tool_calls = [{
        "id": str(b.get("id") or ""),
        "type": "function",
        "function": {"name": str(b.get("name") or ""),
                     "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
    } for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
    message: dict = {"role": "assistant",
                     "content": text if (text or not tool_calls) else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = data.get("usage") or {}
    cached = int(usage.get("cache_read_input_tokens") or 0)
    prompt = (int(usage.get("input_tokens") or 0)
              + int(usage.get("cache_creation_input_tokens") or 0) + cached)
    completion = int(usage.get("output_tokens") or 0)
    out_usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                 "total_tokens": prompt + completion}
    if cached:
        out_usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {
        "id": str(data.get("id") or "chatcmpl-router"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(data.get("model") or ""),
        "choices": [{"index": 0, "message": message,
                     "finish_reason": _STOP_TO_FINISH.get(data.get("stop_reason"), "stop")}],
        "usage": out_usage,
    }


def _response_to_anthropic(data: dict) -> dict:
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    blocks: list[dict] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        blocks.append({"type": "tool_use",
                       "id": str(call.get("id") or ""),
                       "name": str(fn.get("name") or ""),
                       "input": _json_object(fn.get("arguments"))})
    usage = data.get("usage") or {}
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    out_usage = {"input_tokens": max(prompt - cached, 0), "output_tokens": completion}
    if cached:
        out_usage["cache_read_input_tokens"] = cached
    return {
        "id": str(data.get("id") or "msg-router"),
        "type": "message",
        "role": "assistant",
        "model": str(data.get("model") or ""),
        "content": blocks,
        "stop_reason": _FINISH_TO_STOP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": out_usage,
    }


# ---------------------------------------------------------------- streaming
class _SSEConverter:
    """Shared SSE line buffering for the stream converters below.

    Mirrors ResponsesAdapter's interface: feed() consumes raw upstream SSE
    bytes and returns the converted bytes to relay; finish() flushes the
    trailing partial line and emits the closing events.
    """

    def __init__(self):
        self.buffer = b""
        self._finished = False

    def feed(self, chunk: bytes) -> bytes:
        self.buffer += chunk
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        return b"".join(self._line(line) for line in lines)

    def _line(self, line: bytes) -> bytes:  # pragma: no cover - overridden
        return b""

    def _close(self) -> bytes:  # pragma: no cover - overridden
        return b""

    def finish(self) -> bytes:
        if self._finished:
            return b""
        self._finished = True
        out = b""
        if self.buffer.strip():
            tail, self.buffer = self.buffer, b""
            out = self._line(tail)
        return out + self._close()


def _data_line(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False,
                                  separators=(",", ":")) + "\n\n").encode("utf-8")


def _event_line(kind: str, payload: dict) -> bytes:
    obj = {"type": kind, **payload}
    return (f"event: {kind}\ndata: " + json.dumps(obj, ensure_ascii=False,
                                                  separators=(",", ":")) + "\n\n").encode("utf-8")


def _parse_sse_data(line: bytes):
    line = line.strip()
    if not line.startswith(b"data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == b"[DONE]":
        return None
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


class _AnthropicToOpenAIStream(_SSEConverter):
    """Anthropic SSE (message_start / content_block_* / message_*) -> chat chunks."""

    def __init__(self, model: str = ""):
        super().__init__()
        self.model = model
        self.msg_id = "chatcmpl-" + uuid.uuid4().hex[:24]
        self.created = int(time.time())
        self.prompt_tokens = 0
        self.cached_tokens = 0
        self.completion_tokens = 0
        self.stop_reason = None
        self.tools: dict[int, int] = {}  # anthropic block index -> openai tool index
        self._closed = False

    def _chunk(self, delta: dict, finish=None, usage=None) -> bytes:
        payload = {
            "id": self.msg_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            payload["usage"] = usage
        return _data_line(payload)

    def _line(self, line: bytes) -> bytes:
        event = _parse_sse_data(line)
        if event is None:
            return b""
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self.model = str(message.get("model") or self.model)
            usage = message.get("usage") or {}
            self.cached_tokens = int(usage.get("cache_read_input_tokens") or 0)
            self.prompt_tokens = (int(usage.get("input_tokens") or 0)
                                  + int(usage.get("cache_creation_input_tokens") or 0)
                                  + self.cached_tokens)
            return self._chunk({"role": "assistant", "content": ""})
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                index = int(event.get("index") or 0)
                self.tools[index] = len(self.tools)
                return self._chunk({"tool_calls": [{
                    "index": self.tools[index],
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(block.get("name") or ""),
                                 "arguments": ""},
                }]})
            return b""
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = str(delta.get("text") or "")
                return self._chunk({"content": text}) if text else b""
            if delta.get("type") == "input_json_delta":
                partial = str(delta.get("partial_json") or "")
                if not partial:
                    return b""
                index = self.tools.get(int(event.get("index") or 0), 0)
                return self._chunk({"tool_calls": [{
                    "index": index,
                    "function": {"arguments": partial},
                }]})
            return b""
        if kind == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                self.stop_reason = str(delta["stop_reason"])
            usage = event.get("usage") or {}
            self.completion_tokens = int(usage.get("output_tokens")
                                         or self.completion_tokens)
            return b""
        return b""

    def _close(self) -> bytes:
        if self._closed:
            return b""
        self._closed = True
        prompt, completion = self.prompt_tokens, self.completion_tokens
        usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                 "total_tokens": prompt + completion}
        if self.cached_tokens:
            usage["prompt_tokens_details"] = {"cached_tokens": self.cached_tokens}
        finish = _STOP_TO_FINISH.get(self.stop_reason, "stop")
        return self._chunk({}, finish, usage) + b"data: [DONE]\n\n"


class _OpenAIToAnthropicStream(_SSEConverter):
    """Chat SSE chunks -> Anthropic events (message_start .. message_stop)."""

    def __init__(self, model: str = ""):
        super().__init__()
        self.model = model
        self.msg_id = "msg_" + uuid.uuid4().hex[:24]
        self.started = False
        self.block_index = 0
        self.open_block: tuple | None = None   # ("text", idx) | ("tool", idx, entry)
        self.tools: dict[int, dict] = {}       # openai tool index -> entry
        self.prompt_tokens = 0
        self.cached_tokens = 0
        self.completion_tokens = 0
        self.stop_reason = None
        self._closed = False

    def _ensure_started(self) -> bytes:
        if self.started:
            return b""
        self.started = True
        return _event_line("message_start", {"message": {
            "id": self.msg_id, "type": "message", "role": "assistant",
            "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }})

    def _close_block(self) -> bytes:
        if self.open_block is None:
            return b""
        _, index, *_ = self.open_block
        self.open_block = None
        return _event_line("content_block_stop", {"index": index})

    def _start_tool(self, entry: dict) -> bytes:
        out = [self._close_block()]
        index = self.block_index
        self.block_index += 1
        entry["started"] = True
        entry["index"] = index
        self.open_block = ("tool", index, entry)
        out.append(_event_line("content_block_start", {
            "index": index,
            "content_block": {"type": "tool_use",
                              "id": entry["id"], "name": entry["name"],
                              "input": {}},
        }))
        return b"".join(out)

    def _flush_pending(self, entry: dict) -> bytes:
        """Argument deltas buffered while the tool name was still missing."""
        return b"".join(
            _event_line("content_block_delta", {
                "index": entry["index"],
                "delta": {"type": "input_json_delta",
                          "partial_json": chunk},
            })
            for chunk in entry.pop("pending", []))

    def _line(self, line: bytes) -> bytes:
        event = _parse_sse_data(line)
        if event is None:
            return b""
        usage = event.get("usage") or {}
        if usage:
            self.cached_tokens = int(((usage.get("prompt_tokens_details") or {})
                                      .get("cached_tokens")) or 0)
            self.prompt_tokens = int(usage.get("prompt_tokens") or 0)
            self.completion_tokens = int(usage.get("completion_tokens") or 0)
        if event.get("model"):
            self.model = str(event["model"])
        out = [self._ensure_started()]
        choice = (event.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content")
        if text:
            if self.open_block is None or self.open_block[0] != "text":
                out.append(self._close_block())
                index = self.block_index
                self.block_index += 1
                self.open_block = ("text", index)
                out.append(_event_line("content_block_start", {
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                }))
            out.append(_event_line("content_block_delta", {
                "index": self.open_block[1],
                "delta": {"type": "text_delta", "text": str(text)},
            }))
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            key = int(call.get("index") or 0)
            entry = self.tools.setdefault(key, {
                "id": "", "name": "", "started": False, "index": -1,
                "pending": [],
            })
            if call.get("id"):
                entry["id"] = str(call["id"])
            function = call.get("function") or {}
            if function.get("name"):
                entry["name"] += str(function["name"])
            arguments = function.get("arguments")
            if arguments:
                # The block starts only once a name is in: OpenAI sends the
                # name in the first fragment, so this defers one delta at
                # most. A provider that streams arguments before the name
                # buffers them here; they flush right after the block opens
                # (at close, if the name never lands).
                if not entry["started"]:
                    if not entry["name"]:
                        entry["pending"].append(str(arguments))
                        continue
                    if not entry["id"]:
                        entry["id"] = "toolu_" + uuid.uuid4().hex[:24]
                    out.append(self._start_tool(entry))
                    out.append(self._flush_pending(entry))
                out.append(_event_line("content_block_delta", {
                    "index": entry["index"],
                    "delta": {"type": "input_json_delta",
                              "partial_json": str(arguments)},
                }))
        if choice.get("finish_reason"):
            self.stop_reason = str(choice["finish_reason"])
        return b"".join(out)

    def _close(self) -> bytes:
        if self._closed:
            return b""
        self._closed = True
        out = [self._ensure_started()]
        # Tool calls with no arguments fragment never started their block.
        for entry in self.tools.values():
            if not entry["started"]:
                if not entry["id"]:
                    entry["id"] = "toolu_" + uuid.uuid4().hex[:24]
                out.append(self._start_tool(entry))
                out.append(self._flush_pending(entry))
        out.append(self._close_block())
        usage = {"input_tokens": max(self.prompt_tokens - self.cached_tokens, 0),
                 "output_tokens": self.completion_tokens}
        if self.cached_tokens:
            usage["cache_read_input_tokens"] = self.cached_tokens
        out.append(_event_line("message_delta", {
            "delta": {"stop_reason": _FINISH_TO_STOP.get(self.stop_reason, "end_turn"),
                      "stop_sequence": None},
            "usage": usage,
        }))
        out.append(_event_line("message_stop", {}))
        return b"".join(out)


def stream_converter(src: str, dst: str, model: str = "") -> _SSEConverter | None:
    """SSE stream converter between protocols; None means byte passthrough."""
    if src == dst:
        return None
    if src == ANTHROPIC:
        return _AnthropicToOpenAIStream(model)
    return _OpenAIToAnthropicStream(model)
