"""Anthropic Messages API -> OpenAI Chat Completions bridge (standard library only).

Workers are Claude Code processes, and Claude Code speaks only the Anthropic Messages API.
Some providers (e.g. Hive) serve DeepSeek only through an OpenAI-compatible
/chat/completions endpoint. For those, `multiagents run` starts this bridge on 127.0.0.1 for
the length of one worker run: the worker talks Anthropic to the bridge, the bridge talks
OpenAI to the provider.

Security properties:
- the bridge alone holds the provider key; the worker authenticates to the bridge with a
  random per-run token, so the key never enters the worker's process or settings;
- upstream requests are built from scratch: no incoming header is forwarded, so no credential
  Claude Code might attach can reach the provider;
- the bridge listens on loopback only and dies with the run.

Liveness: Claude Code abandons a response that carries no events for five minutes, and a
connection that carries no bytes for a few. So everything the model produces is streamed on
as it arrives (reasoning included), and SSE pings fill the gaps between events. When the
worker hangs up, the provider's generation is abandoned instead of being paid for to the end.
"""
from __future__ import annotations

import http.client
import http.server
import json
import os
import re
import secrets
import select
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

STOP_REASONS = {"stop": "end_turn", "eos": "end_turn", "end_turn": "end_turn", "stop_sequence": "end_turn",
                "length": "max_tokens", "max_tokens": "max_tokens", "tool_calls": "tool_use",
                "function_call": "tool_use", "tool_use": "tool_use", "content_filter": "refusal"}
# A finish_reason such as DeepSeek's "insufficient_system_resource": the provider cut the answer
# short. It is reported as a retryable error instead of being passed off as a complete answer.
CUT_SHORT = re.compile(r"resource|error|abort|cancel|timeout|overload", re.I)
# A 400 that blames the (non-standard) reasoning_content field.
REFUSED_FIELD = re.compile(r"reasoning|extra|unknown|unrecogni[sz]ed|additional|not permitted", re.I)
ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
               404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error",
               500: "api_error", 502: "api_error", 503: "api_error", 529: "overloaded_error"}
PING_EVERY = 15.0       # seconds without output before an SSE ping (Claude Code's byte watchdog)
KEEPALIVE_EVERY = 30.0  # seconds without an event before an empty delta (its event watchdog)
IMAGE_TOKENS = 1600   # count_tokens estimate per image
REASONING_KEEP = 500  # reasoning cache entries (tool_use id -> reasoning) kept per run


def valid_key(key: str) -> bool:
    """Printable ASCII without spaces: what API keys look like, and what a header can carry."""
    return bool(key) and all("!" <= c <= "~" for c in key)


# ----------------------------------------------------------------------------- request

def _text_of(value) -> str:
    """System prompt / text content: a string, or a list of {"type": "text"} blocks."""
    if isinstance(value, str):
        return value
    return "\n\n".join(b.get("text", "") for b in value or []
                       if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))


def _blocks(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return [b for b in content or [] if isinstance(b, dict)]


def _merge_assistant(history: list) -> list:
    """Consecutive assistant messages as one, the way the Anthropic API combines them."""
    out: list[dict] = []
    for m in history:
        if out and m.get("role") == "assistant" and out[-1].get("role") == "assistant":
            out[-1] = {"role": "assistant", "content": _blocks(out[-1].get("content")) + _blocks(m.get("content"))}
        else:
            out.append(m)
    return out


def _attach_note(msgs: list, text: str) -> None:
    """Put a note (a mid-conversation system message, or text that came along with tool
    results) on the tool result or user message before it, rather than in a user message of
    its own: DeepSeek's template takes every user message for a new turn, and drops the
    reasoning handed back for everything before it."""
    last = msgs[-1] if msgs else None
    if last and last["role"] in ("tool", "user") and isinstance(last.get("content"), str):
        last["content"] = f"{last['content']}\n\n{text}" if last["content"] else text
    elif last and last["role"] == "user" and isinstance(last.get("content"), list):
        last["content"].append({"type": "text", "text": text})
    else:
        msgs.append({"role": "user", "content": text})


def _image_part(block: dict) -> dict | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        url = f"data:{src.get('media_type') or 'image/png'};base64,{src['data']}"
    elif src.get("type") == "url" and src.get("url"):
        url = src["url"]
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _tool_result(block: dict) -> tuple[str, list]:
    """(text for the OpenAI tool message, image parts). OpenAI tool messages carry text only,
    so images a tool returned (e.g. a screenshot the worker Read) travel in a user message."""
    content, images = block.get("content"), []
    if isinstance(content, list):
        texts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                texts.append(b.get("text", ""))
            elif b.get("type") == "image":
                part = _image_part(b)
                if part:
                    images.append(part)
        text = "\n".join(t for t in texts if t)
    else:
        text = "" if content is None else str(content)
    if block.get("is_error"):
        text = "[tool error] " + text
    return text or ("(the tool returned the image below)" if images else "(no output)"), images


def _starts_turn(msg: dict) -> bool:
    """A user message that is not a tool-result continuation."""
    content = msg.get("content")
    if isinstance(content, str):
        return True
    return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content or [])


def _schema(schema) -> dict:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    return {k: v for k, v in schema.items() if k != "$schema"}


def to_openai(req: dict, reasoning: dict | None = None, max_tokens_cap: int | None = None,
              extra_body: dict | None = None, keep_reasoning: bool = True) -> dict:
    """Translate an Anthropic /v1/messages request body into a /chat/completions body.

    DeepSeek expects its reasoning back while it is still working through tool calls within
    one turn (older turns don't need it). It comes from the thinking blocks Claude Code sends
    back, or else from `reasoning`: tool_use id -> the reasoning that led to that call."""
    msgs: list[dict] = []
    system = _text_of(req.get("system") or "")
    if system:
        msgs.append({"role": "system", "content": system})
    history = _merge_assistant([m for m in req.get("messages") or [] if isinstance(m, dict)])
    turn_start = max((i for i, m in enumerate(history) if m.get("role") == "user" and _starts_turn(m)),
                     default=-1)
    for i, m in enumerate(history):
        role, content = m.get("role"), m.get("content")
        if role == "system":
            # Claude Code also sends system messages mid-conversation (environment info, and a
            # token budget note after every tool result). DeepSeek's chat template hoists every
            # system message to the top of the prompt, which would change the prefix on each
            # request and defeat prompt caching, so they stay in place, as notes.
            text = _text_of(content)
            if text:
                _attach_note(msgs, text if text.lstrip().startswith("<")
                             else f"<system-reminder>\n{text}\n</system-reminder>")
            continue
        if role == "assistant":
            blocks = _blocks(content)
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            thought = "".join(b["thinking"] for b in blocks
                              if b.get("type") == "thinking" and isinstance(b.get("thinking"), str))
            calls = [{"id": b.get("id"), "type": "function",
                      "function": {"name": b.get("name", ""),
                                   "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                     for b in blocks if b.get("type") == "tool_use"]
            if not text and not calls:
                continue  # e.g. an answer that was all reasoning
            out: dict = {"role": "assistant", "content": text or None}
            if calls:
                out["tool_calls"] = calls
                if keep_reasoning and i > turn_start:
                    known = reasoning or {}
                    thought = thought or next((known[c["id"]] for c in calls if c["id"] in known), "")
                    if thought:
                        out["reasoning_content"] = thought
            msgs.append(out)
            continue
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        parts: list[dict] = []
        tool_images: list[dict] = []
        had_tools = False
        for b in _blocks(content):
            kind = b.get("type")
            if kind == "tool_result":
                had_tools = True
                text, images = _tool_result(b)
                msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"), "content": text})
                tool_images += images
            elif kind == "text" and b.get("text"):
                parts.append({"type": "text", "text": b["text"]})
            elif kind == "image":
                part = _image_part(b)
                if part:
                    parts.append(part)
            elif kind == "document":
                parts.append({"type": "text", "text": "[a document was attached here; this provider "
                                                      "cannot read documents]"})
        if tool_images:
            parts = [{"type": "text", "text": "Images returned by the tool call(s) above:"}] + tool_images + parts
        if not parts:
            continue
        if all(p["type"] == "text" for p in parts):
            text = "\n\n".join(p["text"] for p in parts)
            if had_tools:
                _attach_note(msgs, text)  # e.g. reminders after tool results: same turn
            else:
                msgs.append({"role": "user", "content": text})
        else:
            msgs.append({"role": "user", "content": parts})  # images need a user message

    body: dict = {"model": req.get("model"), "messages": msgs, "stream": True,
                  "stream_options": {"include_usage": True}}
    if req.get("max_tokens"):
        mt = int(req["max_tokens"])
        body["max_tokens"] = min(mt, int(max_tokens_cap)) if max_tokens_cap else mt
    for key in ("temperature", "top_p", "top_k"):
        if req.get(key) is not None:
            body[key] = req[key]
    if req.get("stop_sequences"):
        body["stop"] = req["stop_sequences"]
    tools = [{"type": "function",
              "function": {"name": t["name"], "description": t.get("description") or "",
                           "parameters": _schema(t.get("input_schema"))}}
             for t in req.get("tools") or [] if isinstance(t, dict) and t.get("name") and "input_schema" in t]
    if tools:
        body["tools"] = tools
        choice = req.get("tool_choice") or {}
        kind = choice.get("type")
        if kind == "any":
            body["tool_choice"] = "required"
        elif kind == "tool" and choice.get("name"):
            body["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif kind == "none":
            body["tool_choice"] = "none"
        if choice.get("disable_parallel_tool_use"):
            body["parallel_tool_calls"] = False
    if extra_body:
        body.update(extra_body)
    return body


def estimate_tokens(req: dict) -> int:
    """For /v1/messages/count_tokens, which OpenAI-compatible APIs don't offer: ~4 characters
    per token, and a flat IMAGE_TOKENS per image (its base64 size says little about tokens)."""
    images = 0

    def strip(v):
        nonlocal images
        if isinstance(v, dict):
            if v.get("type") in ("image", "document"):
                images += v.get("type") == "image"
                return None
            return {k: strip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    text = json.dumps(strip({k: req.get(k) for k in ("system", "messages", "tools")}), ensure_ascii=False)
    return max(1, len(text) // 4 + images * IMAGE_TOKENS)


# ----------------------------------------------------------------------------- response

def _parse_args(raw: str) -> dict:
    raw = (raw or "").strip() or "{}"
    try:
        value = json.loads(raw)
    except ValueError:
        # Hand the model something it can see and correct, instead of breaking the answer.
        return {"_unparsed_arguments": raw}
    return value if isinstance(value, dict) else {"value": value}


THINKING_BLOCK = {"type": "thinking", "thinking": "", "signature": ""}
EMPTY_DELTAS = {"thinking": {"type": "thinking_delta", "thinking": ""}, "text": {"type": "text_delta", "text": ""},
                "tool": {"type": "input_json_delta", "partial_json": ""}}


class StreamTranslator:
    """Turns OpenAI chat.completion.chunk objects into Anthropic stream events as they arrive.

    Reasoning becomes a thinking block (as Anthropic-API providers of DeepSeek return it), text a
    text block, and each tool call a tool_use block whose argument JSON streams as
    input_json_delta; Claude Code parses it when the block ends, and tells the model when it
    can't. One block is open at a time, as in the Anthropic API: text or reasoning that arrives
    while a tool call is open waits until that block closes, and servers stream parallel calls
    one after the other (when one doesn't, `late` is set: the stream cannot be continued)."""

    def __init__(self, model: str, input_estimate: int = 0):
        self.model = model
        self.id = "msg_" + uuid.uuid4().hex[:24]
        self.input_estimate = input_estimate
        self.index = -1
        self.open: str | None = None  # the open block: "thinking", "text" or "tool:<n>"
        self.pending: list[tuple[str, str]] = []  # (kind, text) held back while a tool call is open
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict] = {}  # in the order they began
        self._by_index: dict[int, int] = {}  # the server's call index -> our call
        self.late = False
        self.finish_reason: str | None = None
        self.usage: dict = {}

    def start(self) -> list[tuple[str, dict]]:
        return [("message_start", {"type": "message_start", "message": {
            "id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": self.input_estimate, "output_tokens": 0}}})]

    def _open(self, kind: str, block: dict, events: list) -> None:
        if self.open == kind:
            return
        self._close(events)
        self.index += 1
        self.open = kind
        events.append(("content_block_start", {"type": "content_block_start", "index": self.index,
                                               "content_block": block}))

    def _close(self, events: list) -> None:
        if self.open is not None:
            events.append(("content_block_stop", {"type": "content_block_stop", "index": self.index}))
            self.open = None

    def _delta(self, delta: dict) -> tuple[str, dict]:
        return ("content_block_delta", {"type": "content_block_delta", "index": self.index, "delta": delta})

    def _tool_open(self) -> bool:
        return bool(self.open) and self.open.startswith("tool:")

    def _emit(self, kind: str, piece: str, events: list) -> None:
        """Reasoning or text: on the open block of its kind, or held back behind a tool call."""
        if self._tool_open():
            self.pending.append((kind, piece))
            return
        self._open(kind, THINKING_BLOCK if kind == "thinking" else {"type": "text", "text": ""}, events)
        events.append(self._delta({"type": "thinking_delta", "thinking": piece} if kind == "thinking"
                                  else {"type": "text_delta", "text": piece}))

    def _flush(self, events: list) -> None:
        """What was held back while a tool call was open (runs of whitespace are dropped)."""
        runs: list[list] = []
        for kind, piece in self.pending:
            if runs and runs[-1][0] == kind:
                runs[-1][1] += piece
            else:
                runs.append([kind, piece])
        self.pending = []
        for kind, piece in runs:
            if piece.strip():
                self._emit(kind, piece, events)

    def _start_call(self, n: int, events: list) -> None:
        slot = self.calls[n]
        if self._tool_open():  # the previous call is complete: what waited behind it goes first
            self._close(events)
            self._flush(events)
        self._open(f"tool:{n}", {"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": {}}, events)
        slot["started"] = True
        if slot["args"]:
            events.append(self._delta({"type": "input_json_delta", "partial_json": slot["args"]}))

    def keepalive(self) -> list[tuple[str, dict]]:
        """An event that changes nothing, for Claude Code's watchdog (5 minutes without events):
        an empty delta on the open block, after opening an empty thinking block if none is."""
        events: list[tuple[str, dict]] = []
        if self.open is None:
            self._open("thinking", THINKING_BLOCK, events)
        events.append(self._delta(EMPTY_DELTAS["tool" if self._tool_open() else self.open]))
        return events

    def feed(self, chunk: dict) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        if isinstance(chunk.get("usage"), dict):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            delta = delta if isinstance(delta, dict) else {}
            thought = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(thought, str) and thought:
                self.reasoning.append(thought)
                self._emit("thinking", thought, events)
            text = delta.get("content")
            if isinstance(text, str) and text:
                self.text.append(text)
                self._emit("text", text, events)
            for call in delta.get("tool_calls") or []:
                if isinstance(call, dict):
                    self._feed_call(call, events)
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
        return events

    def _call_for(self, call: dict) -> int:
        """Which call a tool_calls delta continues, or a new one: a new id starts a new call even
        at an index already used (some servers give every call index 0), and a delta without
        an index continues the latest call."""
        idx, cid = call.get("index"), call.get("id") or None
        n = self._by_index.get(idx) if isinstance(idx, int) else (len(self.calls) - 1 if self.calls else None)
        if n is not None:
            slot = self.calls[n]
            if not cid or slot["upstream_id"] in (None, cid):
                slot["upstream_id"] = slot["upstream_id"] or cid
                return n
        n = len(self.calls)
        # Our own id: servers may number calls per response ("call_0"), and ids must be unique
        # across the whole conversation.
        self.calls[n] = {"id": "toolu_" + uuid.uuid4().hex[:24], "upstream_id": cid,
                         "name": "", "args": "", "started": False}
        if isinstance(idx, int):
            self._by_index[idx] = n
        return n

    def _feed_call(self, call: dict, events: list) -> None:
        n = self._call_for(call)
        slot = self.calls[n]
        fn = call.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and name and not slot["started"]:
            # Normally sent once; tolerate servers that repeat it or send it in pieces.
            slot["name"] = name if (not slot["name"] or name.startswith(slot["name"])) else slot["name"] + name
        args = fn.get("arguments")
        piece = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else args if isinstance(args, str) else ""
        if not piece:
            return
        slot["args"] += piece
        if not slot["started"]:
            if slot["name"]:
                self._start_call(n, events)  # sends the arguments so far, this piece included
        elif self.open == f"tool:{n}":
            events.append(self._delta({"type": "input_json_delta", "partial_json": piece}))
        else:
            self.late = True  # for a call whose block is closed: stream=false still gets it whole

    def has_output(self) -> bool:
        return bool(self.text or self.reasoning or self.calls)

    def stop_reason(self) -> str:
        if self.finish_reason in ("length", "max_tokens"):
            return "max_tokens"
        if self.calls:
            return "tool_use"
        return STOP_REASONS.get(self.finish_reason or "stop", "end_turn")

    def anthropic_usage(self) -> dict:
        u = self.usage or {}
        prompt = int(u.get("prompt_tokens") or 0)
        details = u.get("prompt_tokens_details") or {}
        cached = int((details.get("cached_tokens") if isinstance(details, dict) else 0)
                     or u.get("prompt_cache_hit_tokens") or 0)
        return {"input_tokens": max(prompt - cached, 0), "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": 0, "output_tokens": int(u.get("completion_tokens") or 0)}

    def end(self) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        for n in sorted(self.calls):
            if not self.calls[n]["started"]:
                self._start_call(n, events)
        if self._tool_open():
            self._close(events)
        self._flush(events)
        self._close(events)
        events.append(("message_delta", {"type": "message_delta",
                                         "delta": {"stop_reason": self.stop_reason(), "stop_sequence": None},
                                         "usage": self.anthropic_usage()}))
        events.append(("message_stop", {"type": "message_stop"}))
        return events

    def message(self) -> dict:
        """The whole response as one Anthropic message (for stream=false requests)."""
        content: list[dict] = []
        if self.reasoning:
            content.append({"type": "thinking", "thinking": "".join(self.reasoning), "signature": ""})
        text = "".join(self.text)
        if text:
            content.append({"type": "text", "text": text})
        for n in sorted(self.calls):
            slot = self.calls[n]
            content.append({"type": "tool_use", "id": slot["id"], "name": slot["name"],
                            "input": _parse_args(slot["args"])})
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": content,
                "stop_reason": self.stop_reason(), "stop_sequence": None, "usage": self.anthropic_usage()}

    def reasoning_entry(self) -> dict:
        """{tool_use id: reasoning} for each call this response made after reasoning."""
        if not (self.calls and self.reasoning):
            return {}
        thought = "".join(self.reasoning)
        return {slot["id"]: thought for slot in self.calls.values()}


# ----------------------------------------------------------------------------- upstream

class UpstreamError(Exception):
    """`status` is what the worker is told; `upstream_status` what the provider said."""

    def __init__(self, status: int, message: str, upstream_status: int | None = None):
        super().__init__(message)
        self.status, self.message = status, message
        self.upstream_status = status if upstream_status is None else upstream_status


def _client_status(upstream: int) -> int:
    """What to tell Claude Code: keep retryable classes retryable, everything else is final."""
    if upstream in (401, 403, 404, 408, 409, 413, 429) or upstream >= 500:
        return upstream
    return 400  # includes e.g. Hive's 405 "out of balance"


HINTS = {401: "check the API key stored for this provider",
         405: "Hive answers 405 when the organization is paused, usually for lack of credit",
         429: "rate limited; Claude Code retries with backoff"}


def _error_text(raw) -> str:
    """The message inside an error body (JSON or not)."""
    text = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw)
    try:
        body = json.loads(text)
        err = body.get("error") if isinstance(body, dict) else None
        text = (err.get("message") if isinstance(err, dict) else err) or body.get("message") or text
    except (ValueError, AttributeError):
        pass
    return str(text)[:500]


def _error_message(status: int, raw) -> str:
    hint = f" ({HINTS[status]})" if status in HINTS else ""
    return f"provider returned HTTP {status}{hint}: {_error_text(raw)}"


def _error_in(obj) -> UpstreamError | None:
    """An error object where a chunk or a response was expected (some servers answer
    HTTP 200 and put the error in the body)."""
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if not err and not ("status_code" in obj and "message" in obj and "choices" not in obj):
        return None
    code = obj.get("status_code")
    if not isinstance(code, int) and isinstance(err, dict):
        code = err.get("code") if isinstance(err.get("code"), int) else err.get("status")
    code = code if isinstance(code, int) and 400 <= code < 600 else None
    hint = f" ({HINTS[code]})" if code in HINTS else ""
    status = _client_status(code) if code else 502
    return UpstreamError(status, f"provider error{f' {code}' if code else ''}{hint}: "
                                 f"{_error_text(json.dumps(obj))}", code or 502)


class _CurlResponse:
    """Streaming POST through curl, for Pythons without CA certificates. Headers (including
    the key) go through curl's stdin config, never its argv."""

    def __init__(self, curl: str, url: str, headers: dict, data: bytes, timeout: int):
        fd, self.body_path = tempfile.mkstemp(prefix="ma-bridge-", suffix=".json")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        conf = "".join('header = "%s: %s"\n' % (k, str(v).replace("\\", "\\\\").replace('"', '\\"'))
                       for k, v in headers.items())
        conf += 'url = "%s"\n' % url
        self.err = tempfile.TemporaryFile()  # a file, not a pipe nobody drains while streaming
        try:
            # -q first: no ~/.curlrc. No overall time limit (answers can take many minutes), but
            # give up on a stalled one. A proxy's "200 Connection established" is not the answer.
            self.proc = subprocess.Popen([curl, "-q", "-sS", "-N", "-i", "--suppress-connect-headers",
                                          "-X", "POST", "--data-binary", "@" + self.body_path,
                                          "--connect-timeout", "30", "--speed-limit", "1",
                                          "--speed-time", str(timeout), "-K", "-"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err)
        except OSError as e:
            self.err.close()
            os.unlink(self.body_path)
            raise UpstreamError(502, f"provider unreachable: could not start curl ({e})")
        self.proc.stdin.write(conf.encode())
        self.proc.stdin.close()
        self.status = 0
        while True:  # status line + headers; skip interim 1xx responses
            line = self.proc.stdout.readline()
            if not line:
                break
            if line.startswith(b"HTTP/"):
                parts = line.split()
                self.status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                while True:
                    h = self.proc.stdout.readline()
                    if not h or h.strip() == b"":
                        break
                if not 100 <= self.status < 200:
                    break
        if not self.status:
            err = self.failure()
            self.close()
            raise UpstreamError(502, f"provider unreachable (curl): {err}")

    def readline(self) -> bytes:
        return self.proc.stdout.readline()

    def read(self) -> bytes:
        return self.proc.stdout.read()

    def failure(self) -> str:
        """curl's error once it has exited with one (the connection broke, stalled...)."""
        try:
            code = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return ""
        if code == 0:
            return ""
        try:
            self.err.seek(0)
            text = self.err.read().decode("utf-8", "replace").strip()
        except (OSError, ValueError):
            text = ""
        return text[:300] or f"curl exit code {code}"

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=10)
        except Exception:
            pass
        for done in (self.err.close, lambda: os.unlink(self.body_path)):
            try:
                done()
            except OSError:
                pass


class _UrllibResponse:
    def __init__(self, resp):
        self.resp, self.status = resp, resp.status

    def readline(self) -> bytes:
        return self.resp.readline()

    def read(self) -> bytes:
        return self.resp.read()

    def failure(self) -> str:
        return ""

    def close(self) -> None:
        try:
            self.resp.close()
        except Exception:
            pass


READ_ERRORS = (OSError, http.client.HTTPException, ValueError)  # ValueError: read after close


class Upstream:
    """POST JSON to <base_url>/chat/completions and stream the answer back line by line."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 600, curl: str | None = None):
        if not valid_key(api_key):  # never quote the key itself
            raise ValueError("the provider API key is empty or contains spaces, line breaks or "
                             "non-ASCII characters")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key = api_key
        self.timeout = timeout  # seconds the provider may stay silent
        self.curl = curl  # for Pythons without CA certificates
        self.use_curl = False

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                "Accept": "text/event-stream", "User-Agent": "multiagents-bridge"}

    def open(self, body: dict):
        # "replace": a lone UTF-16 surrogate (half an emoji) cannot be sent as UTF-8.
        data = json.dumps(body, ensure_ascii=False).encode("utf-8", "replace")
        if not self.use_curl:
            req = urllib.request.Request(self.url, data=data, method="POST", headers=self.headers())
            try:
                return _UrllibResponse(urllib.request.urlopen(req, timeout=self.timeout))
            except urllib.error.HTTPError as e:
                try:
                    raw = e.read()
                except READ_ERRORS:
                    raw = b""
                raise UpstreamError(_client_status(e.code), _error_message(e.code, raw), e.code)
            except urllib.error.URLError as e:
                if "CERTIFICATE" not in str(e) or not self.curl:
                    raise UpstreamError(502, f"provider unreachable: {e.reason}")
                self.use_curl = True  # this Python has no CA certificates; curl does
            except (socket.timeout, TimeoutError, ssl.SSLError, OSError, http.client.HTTPException) as e:
                raise UpstreamError(502, f"provider unreachable: {type(e).__name__}: {e}")
            except ValueError as e:  # e.g. a header value it refuses: don't quote it
                raise UpstreamError(500, f"could not send the request ({type(e).__name__})")
        resp = _CurlResponse(self.curl, self.url, self.headers(), data, self.timeout)
        if resp.status >= 400:
            raw = resp.read()
            resp.close()
            raise UpstreamError(_client_status(resp.status), _error_message(resp.status, raw), resp.status)
        return resp


def iter_chunks(resp, seen: dict | None = None):
    """Yield parsed chunk objects from an OpenAI SSE stream (or a plain JSON body). Sets
    seen["done"] at [DONE]. Raises UpstreamError for error objects and broken connections."""
    seen = {} if seen is None else seen
    first = True
    while True:
        try:
            raw = resp.readline()
        except READ_ERRORS as e:
            raise UpstreamError(502, f"the provider's stream broke off: {type(e).__name__}: {e}")
        if not raw:
            return
        line = raw.decode("utf-8", "replace").strip()
        if first and line.startswith("{"):  # a server that ignored stream=true
            try:
                rest = resp.read()
            except READ_ERRORS as e:
                raise UpstreamError(502, f"the provider's answer broke off: {type(e).__name__}: {e}")
            try:
                obj = json.loads(line + rest.decode("utf-8", "replace"))
            except ValueError:
                raise UpstreamError(502, "the provider's answer is neither an event stream nor JSON")
            err = _error_in(obj)
            if err:
                raise err
            seen["done"] = True
            if isinstance(obj, dict):
                yield obj
            return
        if line:
            first = False
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            seen["done"] = True
            return
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        err = _error_in(chunk)
        if err:
            raise err
        if isinstance(chunk, dict):
            yield chunk


# ----------------------------------------------------------------------------- server

class ClientGone(Exception):
    """The worker hung up: its request was aborted, or its process ended."""


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    bridge: "Bridge"

    def setup(self) -> None:
        super().setup()
        self._wlock = threading.Lock()  # bytes to the worker: from this thread and the watcher
        self._tlock = threading.Lock()  # the translator: fed here, kept alive by the watcher
        self._tr: StreamTranslator | None = None
        self._gone = False
        self._streaming = False
        self._finished = False  # no more events from the translator (under _tlock)
        self._answered = False
        self._ended = False  # the final chunk is out: not one more byte
        self._last_write = self._last_event = time.monotonic()

    def log_message(self, *_args) -> None:
        pass

    def _authorized(self) -> bool:
        token = self.headers.get("x-api-key") or ""
        auth = self.headers.get("authorization") or ""
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:]
        return bool(token) and secrets.compare_digest(token.strip().encode(), self.bridge.token.encode())

    def _body(self) -> bytes:
        length = self.headers.get("content-length")
        if length:
            return self.rfile.read(int(length))
        if "chunked" in (self.headers.get("transfer-encoding") or "").lower():
            data = b""
            while True:
                size = int((self.rfile.readline().split(b";")[0].strip() or b"0"), 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return b""

    # --- writing: every byte to the worker goes through _write, under the lock

    def _write(self, data: bytes, *, ping: bool = False, last: bool = False) -> None:
        with self._wlock:
            if self._ended or (ping and (not self._streaming
                                         or time.monotonic() - self._last_write < PING_EVERY)):
                return
            if last:
                self._streaming = False
                self._ended = True  # nothing may follow the final chunk
            if self._gone:
                raise ClientGone()
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except OSError as e:
                self._gone = True
                raise ClientGone() from e
            self._last_write = time.monotonic()

    def _head(self, status: int, headers: dict) -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.close_connection = True
        with self._wlock:
            try:
                self.end_headers()
            except OSError as e:
                self._gone = True
                raise ClientGone() from e
            self._last_write = time.monotonic()

    def _json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self._head(status, {"content-type": "application/json", "content-length": str(len(data)),
                            "connection": "close"})
        self._write(data)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"type": "error", "error": {"type": ERROR_TYPES.get(status, "api_error"),
                                                       "message": message}})

    def _sse(self, event: str, data: dict) -> None:
        # ASCII JSON: a surrogate pair split across two deltas stays two \u escapes, which the
        # worker's JavaScript joins back together (UTF-8 cannot carry half of one).
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
        self._write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n", ping=event == "ping")
        if event != "ping":
            self._last_event = time.monotonic()

    def _watch(self, done: threading.Event) -> None:
        """While an answer is pending: notice the worker hanging up (the main loop then drops
        the provider's stream), and keep a quiet stream alive: pings for the bytes, and
        every KEEPALIVE_EVERY seconds an event that changes nothing (pings are not events)."""
        while not done.wait(1.0):
            if self._peer_closed():
                self._gone = True
                return
            try:
                if self._streaming and time.monotonic() - self._last_event >= KEEPALIVE_EVERY:
                    with self._tlock:
                        if self._tr is not None and not self._finished:
                            for ev in self._tr.keepalive():
                                self._sse(*ev)
                self._sse("ping", {"type": "ping"})  # no-op unless streaming and quiet
            except ClientGone:
                return

    def _peer_closed(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    # --- routes

    def do_GET(self) -> None:
        try:
            if not self._authorized():
                return self._error(401, "multiagents bridge: missing or wrong token")
            if self.path.split("?")[0].rstrip("/") == "/v1/models":
                models = [{"type": "model", "id": m, "display_name": m, "created_at": "2026-01-01T00:00:00Z"}
                          for m in self.bridge.models]
                return self._json(200, {"data": models, "has_more": False,
                                        "first_id": models[0]["id"] if models else None,
                                        "last_id": models[-1]["id"] if models else None})
            self._error(404, f"multiagents bridge: no route for GET {self.path}")
        except ClientGone:
            pass

    def do_POST(self) -> None:
        try:
            body = self._body()
            if not self._authorized():
                return self._error(401, "multiagents bridge: missing or wrong token")
            path = self.path.split("?")[0].rstrip("/")
            try:
                req = json.loads(body or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._error(400, "multiagents bridge: request body is not a JSON object")
            if path == "/v1/messages/count_tokens":
                return self._json(200, {"input_tokens": estimate_tokens(req)})
            if path == "/v1/messages":
                return self._messages(req)
            self._error(404, f"multiagents bridge: no route for POST {path}")
        except ClientGone:
            pass

    def _messages(self, req: dict) -> None:
        bridge = self.bridge
        with bridge.lock:
            bridge.stats["requests"] += 1
            seq = bridge.stats["requests"]
        try:
            with bridge.lock:
                body = to_openai(req, bridge.reasoning, bridge.max_tokens_cap, bridge.extra_body,
                                 keep_reasoning=bridge.send_reasoning)
        except Exception as e:  # a request shape this translator does not handle
            bridge.failed(f"#{seq} 400 could not translate the request: {type(e).__name__}: {e}")
            return self._fail(400, f"multiagents bridge could not translate this request "
                                   f"({type(e).__name__}: {e})")
        bridge.dump(seq, req, body)
        tr = self._tr = StreamTranslator(req.get("model") or "", estimate_tokens(req))
        stream, t0 = bool(req.get("stream")), time.time()
        done = threading.Event()
        threading.Thread(target=self._watch, args=(done,), name="multiagents-bridge-watch", daemon=True).start()
        resp = None
        try:
            resp = self._open_upstream(body, seq)
            if stream:
                self._head(200, {"content-type": "text/event-stream", "cache-control": "no-cache",
                                 "transfer-encoding": "chunked", "connection": "close"})
                for ev in tr.start():
                    self._sse(*ev)
                self._streaming = True
            seen: dict = {}
            for chunk in iter_chunks(resp, seen):
                if self._gone:
                    raise ClientGone()
                with self._tlock:
                    events = tr.feed(chunk)
                    if stream and tr.late:
                        raise UpstreamError(529, "the provider interleaved the arguments of parallel "
                                                 "tool calls, which cannot be streamed on")
                    if stream:
                        for ev in events:
                            self._sse(*ev)
            with self._tlock:
                self._finished = True
            if not seen.get("done") and tr.finish_reason is None:
                detail = resp.failure()
                raise UpstreamError(502, "the provider's stream ended before the answer was complete"
                                         + (f" ({detail})" if detail else ""))
            fr = tr.finish_reason
            if fr and fr not in STOP_REASONS:
                if CUT_SHORT.search(fr):
                    raise UpstreamError(529, f"the provider cut the answer short (finish_reason {fr!r})")
                bridge.note(f"#{seq} unknown finish_reason {fr!r}, passed on as end_turn")
            if not tr.has_output() and tr.stop_reason() != "max_tokens":
                raise UpstreamError(502, "the provider returned an empty answer")
            # Before the answer completes: the worker's next request may follow at once.
            bridge.remember(tr.reasoning_entry())
            if stream:
                for ev in tr.end():
                    self._sse(*ev)
                self._write(b"0\r\n\r\n", last=True)
            else:
                self._json(200, tr.message())
            self._answered = True
            usage = tr.anthropic_usage()
            bridge.note(f"#{seq} 200 {time.time() - t0:.1f}s in={usage['input_tokens']} cached="
                        f"{usage['cache_read_input_tokens']} out={usage['output_tokens']} "
                        f"stop={tr.stop_reason()} tools={len(tr.calls)}")
        except ClientGone:
            bridge.note(f"#{seq} the worker hung up after {time.time() - t0:.1f}s; answer abandoned")
        except UpstreamError as e:
            bridge.failed(f"#{seq} {e.status} {e.message}")
            self._fail(e.status, e.message)
        except Exception as e:  # a bug in the bridge: still answer, so the worker isn't left hanging
            bridge.failed(f"#{seq} 500 bridge error: {type(e).__name__}: {e}")
            self._fail(500, f"multiagents bridge error: {type(e).__name__}: {e}")
        finally:
            done.set()
            if resp is not None:
                resp.close()

    def _open_upstream(self, body: dict, seq: int):
        bridge = self.bridge
        try:
            return bridge.upstream.open(body)
        except UpstreamError as e:
            # Reasoning goes back in a non-standard field, which a strict server may refuse.
            # Then try without it, and if that works, leave it out for the rest of the run.
            if not (e.upstream_status in (400, 422) and REFUSED_FIELD.search(e.message)
                    and any("reasoning_content" in m for m in body["messages"])):
                raise
            refusal = e.message
        resp = bridge.upstream.open(dict(body, messages=[{k: v for k, v in m.items() if k != "reasoning_content"}
                                                         for m in body["messages"]]))
        with bridge.lock:
            bridge.send_reasoning = False
        bridge.note(f"#{seq} the provider refused reasoning_content ({refusal[:200]}); "
                    "not sending it any more")
        return resp

    def _fail(self, status: int, message: str) -> None:
        """Report an error the way the worker can still take it: as the HTTP status while no
        answer has started, else as an error event (Claude Code retries a mid-stream error
        only when its type is overloaded_error). Never after a complete answer, and never
        with the key in it (a provider may quote the key it was sent)."""
        if self._answered:
            return
        self._answered = True
        message = self.bridge.redact(message)
        try:
            with self._tlock:  # no keepalive event between the error and the end
                self._finished = True
                if self._streaming:
                    kind = ("overloaded_error" if status == 429 or status >= 500
                            else ERROR_TYPES.get(status, "api_error"))
                    self._sse("error", {"type": "error", "error": {"type": kind, "message": message}})
                    self._write(b"0\r\n\r\n", last=True)
                else:
                    self._error(status, message)
        except ClientGone:
            pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    bridge: "Bridge"

    def handle_error(self, request, client_address) -> None:
        # To the bridge log, not the worker run's terminal as a traceback.
        exc = sys.exc_info()[1]
        self.bridge.note(f"connection error: {type(exc).__name__}: {str(exc)[:200]}")


class Bridge:
    """A loopback Anthropic-API endpoint that forwards to an OpenAI-compatible provider."""

    def __init__(self, base_url: str, api_key: str, models: list[str] | None = None,
                 max_tokens_cap: int | None = None, extra_body: dict | None = None,
                 log_path: str | None = None, timeout: int = 600, curl: str | None = None):
        self.upstream = Upstream(base_url, api_key, timeout, curl if curl is not None else shutil.which("curl"))
        self.token = secrets.token_urlsafe(32)
        self.models = list(models or [])
        self.max_tokens_cap = max_tokens_cap
        self.extra_body = dict(extra_body or {})
        self.reasoning: dict[str, str] = {}
        self.send_reasoning = True
        # Debugging aid: MULTIAGENTS_BRIDGE_DUMP=<dir> saves every translated request body
        # (conversation content, never the key) to see exactly what the provider got.
        self.dump_dir = os.environ.get("MULTIAGENTS_BRIDGE_DUMP") or None
        self.lock = threading.Lock()
        self._log_lock = threading.Lock()
        self.stats = {"requests": 0, "errors": 0}
        self.log_path = log_path
        self.server: _Server | None = None
        self.url = ""

    def redact(self, text: str) -> str:
        return str(text).replace(self.upstream.key, "[redacted]")

    def note(self, text: str) -> None:
        """A line in the bridge log (the key blanked out). Never raises: logging must not
        turn a good answer into a failed one."""
        if not self.log_path:
            return
        try:
            with self._log_lock, open(self.log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {self.redact(text)}\n")
        except OSError:
            pass

    def dump(self, seq: int, req: dict, body: dict) -> None:
        if not self.dump_dir:
            return
        try:
            os.makedirs(self.dump_dir, exist_ok=True)
            with open(os.path.join(self.dump_dir, f"{os.getpid()}-{seq:04d}.json"), "w",
                      encoding="utf-8", errors="replace") as f:
                json.dump({"anthropic": req, "openai": body}, f, ensure_ascii=False, indent=1)
        except (OSError, ValueError) as e:
            self.note(f"#{seq} could not save the request dump: {e}")

    def failed(self, text: str) -> None:
        with self.lock:
            self.stats["errors"] += 1
        self.note(text)

    def remember(self, entry: dict) -> None:
        if not entry:
            return
        with self.lock:
            self.reasoning.update(entry)
            for old in list(self.reasoning)[:max(0, len(self.reasoning) - REASONING_KEEP)]:
                del self.reasoning[old]

    def start(self) -> str:
        handler = type("BridgeHandler", (_Handler,), {"bridge": self})
        self.server = _Server(("127.0.0.1", 0), handler)
        self.server.bridge = self
        threading.Thread(target=self.server.serve_forever, name="multiagents-bridge", daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.note(f"listening on {self.url} -> {self.upstream.url}")
        return self.url

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
