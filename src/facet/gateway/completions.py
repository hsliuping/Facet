"""Legacy /v1/completions bridge: text completions <-> chat completions.

Pure client-edge adapter (same idea as responses.py): the request's `prompt`
becomes a user message and the whole routing / failover / accounting path is
the chat one; the answer is mapped back into text-completion shape on the way
out. Legacy-only knobs (echo, suffix, logprobs, best_of) are ignored.
"""
from __future__ import annotations

import json
import time

PASSTHROUGH_PARAMS = (
    "temperature", "top_p", "stop", "n", "presence_penalty",
    "frequency_penalty", "seed", "user",
)


def to_chat_request(body: dict) -> dict:
    """Build a chat/completions payload from a legacy completions request."""
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        # Every entry survives, coerced verbatim: dropping non-string items
        # (legacy token ids, odd client payloads) silently rewrote the
        # prompt into a different message.
        text = "\n".join(str(p) for p in prompt)
    elif prompt is None:
        text = ""
    else:
        text = str(prompt)
    chat: dict = {
        "model": str(body.get("model") or "auto"),
        "messages": [{"role": "user", "content": text}],
    }
    for key in PASSTHROUGH_PARAMS:
        if body.get(key) is not None:
            chat[key] = body[key]
    if body.get("max_tokens") is not None:
        chat["max_tokens"] = body["max_tokens"]
    if body.get("stream"):
        chat["stream"] = True
    return chat


def _chunk_payload(model: str, created: int, text: str, finish=None,
                   usage=None) -> dict:
    payload = {
        "id": "cmpl-router",
        "object": "text_completion",
        "created": created,
        "model": model,
        "choices": [{"text": text, "index": 0, "logprobs": None,
                     "finish_reason": finish}],
    }
    if usage:
        payload["usage"] = usage
    return payload


def _data_line(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False,
                                  separators=(",", ":")) + "\n\n").encode("utf-8")


class CompletionsAdapter:
    """Turn chat chunks / responses into legacy text-completion shape.

    Same feed()/finish() contract as ResponsesAdapter so `_relay` can treat
    every client-edge adapter identically.
    """

    def __init__(self, model: str, stream: bool):
        self.model = model
        self.stream = bool(stream)
        self.created = int(time.time())
        self.buffer = b""
        self.usage: dict = {}
        self.finish_reason = None
        self._finished = False

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
        out = b""
        text = delta.get("content")
        if text:
            out = _data_line(_chunk_payload(self.model, self.created, str(text)))
        if choice.get("finish_reason"):
            self.finish_reason = str(choice["finish_reason"])
        return out

    def feed(self, chunk: bytes) -> bytes:
        """Consume raw upstream SSE bytes; return text-completion SSE bytes."""
        if not self.stream:
            return b""
        self.buffer += chunk
        lines = self.buffer.split(b"\n")
        self.buffer = lines.pop()
        return b"".join(self._line(line) for line in lines)

    def _non_stream(self, data: bytes) -> bytes:
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        choice = (obj.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content")
        finish = str(choice.get("finish_reason") or "stop")
        if finish == "tool_calls":
            finish = "stop"  # text completions have no tool-call state
        payload = _chunk_payload(
            self.model, self.created,
            text if isinstance(text, str) else "", finish,
            obj.get("usage") or None)
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def finish(self, data: bytes = b"") -> bytes:
        if self._finished:
            return b""
        self._finished = True
        if not self.stream:
            return self._non_stream(data)
        out = b""
        if self.buffer.strip():
            tail, self.buffer = self.buffer, b""
            out = self._line(tail)
        finish = self.finish_reason or "stop"
        out += _data_line(_chunk_payload(self.model, self.created, "", finish,
                                         self.usage or None))
        return out + b"data: [DONE]\n\n"
