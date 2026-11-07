"""Compatibility bridge: OpenAI Responses API <-> OpenAI Chat Completions.

The router still never converts between *channels*: this adapter only
translates at the client edge, so Codex CLI can keep its default
`wire_api = "responses"` while every channel stays a plain chat endpoint.

Known limits (documented in router.example.json): non-function tools such as
`web_search` are dropped, reasoning items are flattened to text, and
`previous_response_id` / the compaction endpoint are not supported. Codex does
not use `previous_response_id`, so its stateless agent loop works as-is.
"""
from __future__ import annotations

import json
import time
import uuid

TEXT_PARTS = ("input_text", "output_text", "summary_text", "text")
PASSTHROUGH_PARAMS = (
    "temperature", "top_p", "presence_penalty", "frequency_penalty", "seed",
    "stop", "n", "response_format", "parallel_tool_calls", "user", "service_tier",
)


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:24]


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and part.get("type") in TEXT_PARTS:
            value = part.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _to_chat_content(content):
    """Responses content parts -> chat content (string or multipart list)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind == "input_image":
            url = part.get("image_url") or part.get("image")
            if isinstance(url, str) and url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if not parts:
        return ""
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts


def _to_chat_tools(tools) -> list[dict]:
    converted = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = {
            "name": str(tool.get("name") or ""),
            "description": str(tool.get("description") or ""),
        }
        if tool.get("parameters") is not None:
            function["parameters"] = tool["parameters"]
        if tool.get("strict") is not None:
            function["strict"] = tool["strict"]
        converted.append({"type": "function", "function": function})
    return converted


def _to_chat_tool_choice(choice):
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            return {"type": "function", "function": {"name": str(choice.get("name") or "")}}
        return choice.get("type") or "auto"
    return None


def _input_items(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": value}]}]
    return value if isinstance(value, list) else []


def to_chat_request(body: dict) -> dict:
    """Build a chat/completions payload from a Responses API request."""
    messages: list[dict] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    for item in _input_items(body.get("input")):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message" or (kind is None and "role" in item):
            role = item.get("role") or "user"
            if role == "developer":
                role = "system"
            content = _to_chat_content(item.get("content"))
            if content:
                messages.append({"role": role, "content": content})
        elif kind == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": str(item.get("call_id") or ""),
                "content": _content_text(item.get("output")) or str(item.get("output") or ""),
            })
        elif kind == "function_call":
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": str(item.get("call_id") or _new_id("call_")),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }],
            })
        elif kind == "reasoning":
            text = _content_text(item.get("summary"))
            if text:
                messages.append({"role": "assistant", "content": text})
        # compaction / item_reference / image_generation_call are ignored.

    chat: dict = {"model": str(body.get("model") or "auto"), "messages": messages}
    for key in PASSTHROUGH_PARAMS:
        if body.get(key) is not None:
            chat[key] = body[key]
    if body.get("max_output_tokens") is not None:
        chat["max_tokens"] = body["max_output_tokens"]
    tools = _to_chat_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
    tool_choice = _to_chat_tool_choice(body.get("tool_choice"))
    if tool_choice is not None:
        chat["tool_choice"] = tool_choice
    if body.get("stream"):
        chat["stream"] = True
    return chat


def _message_item(item_id: str, text: str, status: str = "completed") -> dict:
    content = [] if status == "in_progress" else [
        {"type": "output_text", "text": text, "annotations": []},
    ]
    return {"id": item_id, "type": "message", "status": status,
            "role": "assistant", "content": content}


def _output_from_chat_message(message: dict, text=None, tool_calls=None) -> list[dict]:
    output: list[dict] = []
    value = message.get("content") if text is None else text
    if value:
        output.append(_message_item(_new_id("msg_"), str(value)))
    for call in (message.get("tool_calls") or []) if tool_calls is None else tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        output.append({
            "id": _new_id("fc_"), "type": "function_call", "status": "completed",
            "call_id": str(call.get("id") or _new_id("call_")),
            "name": str(function.get("name") or ""),
            "arguments": str(function.get("arguments") or "{}"),
        })
    return output


def build_response(model: str, response_id: str, created_at: int,
                   output: list[dict], usage: dict | None = None,
                   status: str = "completed") -> dict:
    usage = usage or {}
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": True,
        "temperature": 1.0,
        "tool_choice": "auto",
        "tools": [],
        "top_p": 1.0,
        "max_output_tokens": None,
        "previous_response_id": None,
        "store": False,
        "truncation": "disabled",
        "reasoning": {"effort": None, "summary": None},
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
    }


def chat_to_response(data: bytes, model: str, response_id: str | None = None,
                     created_at: int | None = None) -> bytes:
    """Convert a non-streaming chat response into a Responses API object."""
    try:
        obj = json.loads(data)
    except (ValueError, TypeError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    choice = (obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    response = build_response(
        model, response_id or _new_id("resp_"), int(created_at or time.time()),
        _output_from_chat_message(message), obj.get("usage") or {},
    )
    return json.dumps(response, ensure_ascii=False).encode("utf-8")


class ResponsesAdapter:
    """Turn chat SSE chunks into Responses SSE events; also handles non-stream."""

    def __init__(self, model: str, stream: bool, response_id: str | None = None,
                 created_at: int | None = None):
        self.model = model
        self.stream = bool(stream)
        self.response_id = response_id or _new_id("resp_")
        self.created_at = int(created_at or time.time())
        self.item_id = _new_id("msg_")
        self.buffer = b""
        self.text_parts: list[str] = []
        self.tool_calls: dict[int, dict] = {}
        self.usage: dict = {}
        self._response_started = False
        self._message_started = False
        self._finished = False

    @staticmethod
    def _event(kind: str, payload: dict) -> bytes:
        obj = {"type": kind, **payload}
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        return f"event: {kind}\ndata: {line}\n\n".encode("utf-8")

    def _ensure_response(self) -> bytes:
        if self._response_started:
            return b""
        self._response_started = True
        base = build_response(self.model, self.response_id, self.created_at,
                              [], status="in_progress")
        return (self._event("response.created", {"response": base})
                + self._event("response.in_progress", {"response": base}))

    def _ensure_message(self) -> bytes:
        out = [self._ensure_response()]
        if not self._message_started:
            self._message_started = True
            out.append(self._event("response.output_item.added", {
                "output_index": 0,
                "item": _message_item(self.item_id, "", status="in_progress"),
            }))
            out.append(self._event("response.content_part.added", {
                "item_id": self.item_id, "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }))
        return b"".join(out)

    def _line(self, line: bytes) -> bytes:
        line = line.strip()
        if not line.startswith(b"data:"):
            return b""
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return b""
        try:
            event = json.loads(payload)
        except ValueError:
            return b""
        if not isinstance(event, dict):
            return b""
        usage = event.get("usage")
        if isinstance(usage, dict) and usage:
            self.usage.update(usage)
        choice = (event.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        out: list[bytes] = []
        text = delta.get("content")
        if text:
            out.append(self._ensure_message())
            out.append(self._event("response.output_text.delta", {
                "item_id": self.item_id, "output_index": 0, "content_index": 0,
                "delta": text,
            }))
            self.text_parts.append(text)
        for call in delta.get("tool_calls") or []:
            out.append(self._ensure_response())
            index = int(call.get("index") or 0)
            entry = self.tool_calls.setdefault(index, {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if call.get("id"):
                entry["id"] = call["id"]
            function = call.get("function") or {}
            if function.get("name"):
                entry["function"]["name"] += function["name"]
            if function.get("arguments"):
                entry["function"]["arguments"] += function["arguments"]
        return b"".join(out)

    def feed(self, chunk: bytes) -> bytes:
        """Consume raw upstream SSE bytes; return bytes to send to the client."""
        if not self.stream:
            return b""
        self.buffer += chunk
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        return b"".join(self._line(line) for line in lines)

    def finish(self, data: bytes = b"") -> bytes:
        if self._finished:
            return b""
        self._finished = True
        if not self.stream:
            return chat_to_response(data, self.model, self.response_id, self.created_at)

        out: list[bytes] = [self._ensure_response()]
        if self.buffer.strip():
            tail, self.buffer = self.buffer, b""
            out.append(self._line(tail))

        output: list[dict] = []
        if self.text_parts:
            text = "".join(self.text_parts)
            out.append(self._event("response.output_text.done", {
                "item_id": self.item_id, "output_index": 0, "content_index": 0,
                "text": text,
            }))
            out.append(self._event("response.content_part.done", {
                "item_id": self.item_id, "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            }))
            message = _message_item(self.item_id, text)
            out.append(self._event("response.output_item.done", {
                "output_index": 0, "item": message,
            }))
            output.append(message)

        for index in sorted(self.tool_calls):
            call = self.tool_calls[index]
            item = {
                "id": _new_id("fc_"), "type": "function_call", "status": "completed",
                "call_id": call["id"] or _new_id("call_"),
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"] or "{}",
            }
            out.append(self._event("response.output_item.added", {
                "output_index": len(output), "item": item,
            }))
            out.append(self._event("response.output_item.done", {
                "output_index": len(output), "item": item,
            }))
            output.append(item)

        response = build_response(self.model, self.response_id, self.created_at,
                                  output, self.usage)
        out.append(self._event("response.completed", {"response": response}))
        return b"".join(out)
