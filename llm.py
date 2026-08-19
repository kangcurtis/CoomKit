#!/usr/bin/env python3
"""CoomKit LLM provider layer — chat AND raw text completion.

Local-first: vision/images NEVER leave the machine unless the backend is an
explicitly configured remote. Two request modes:

  - "chat":       OpenAI chat.completions (instruct-tuned models)
  - "completion": raw /v1/completions — we render the prompt ourselves from
                  a template (gemma4, chatml, llama3, plain). Needed for
                  base-ish models like Gemma 4 31B which behave much better
                  with exact control over the prompt string, thinking
                  toggles, thinking prefill, and SYSTEMPROMPT blocks.

Thinking controls (Gemma 4 style):
  - thinking off:  emit the model's no-think marker in the rendered prompt
  - thinking on:   emit think marker + optional thinking prefill to steer it
Sampler params are passed through; unsupported keys are ignored by servers.
"""
import base64
import json
import mimetypes
import re
import urllib.error
import urllib.request
from pathlib import Path

SAMPLER_KEYS = (
    "temperature", "top_p", "top_k", "min_p", "max_tokens",
    "repetition_penalty", "presence_penalty", "frequency_penalty", "seed",
)

# ----------------------------------------------------------------------
# Prompt templates for raw completion mode
# ----------------------------------------------------------------------

# Each template renders (system, messages, prefill, thinking opts) -> str.
# {{SYSTEM}} is replaced by the composed system prompt (SYSTEMPROMPT block).

TEMPLATES = {
    "gemma4": {
        "name": "Gemma 4 (canonical chat_template.jinja)",
        "render": "gemma4",
    },
    "chatml": {"name": "ChatML", "render": "chatml"},
    "llama3": {"name": "Llama 3", "render": "llama3"},
    "plain": {"name": "Plain (system + transcript)", "render": "plain"},
}


def _flatten(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split leading system messages out; return (system_text, rest)."""
    system_parts, rest = [], []
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            system_parts.append(c if isinstance(c, str) else str(c))
        else:
            rest.append(m)
    return "\n\n".join(p for p in system_parts if p), rest


def _text_of(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    # multimodal content list -> keep only text parts for completion mode
    return "\n".join(p.get("text", "") for p in c if isinstance(p, dict)
                     and p.get("type") == "text")


def render_prompt(template: str, messages: list[dict], prefill: str = "",
                  thinking: bool = True, thinking_prefill: str = "") -> str:
    """Render messages to a raw prompt string for /v1/completions."""
    system, rest = _flatten(messages)
    out = ["<bos>"]
    if template == "gemma4":
        # Canonical Gemma 4 format:
        #   <|turn>system\n[think token][system text]<turn|>
        #   <|turn>user\n...<turn|>  <|turn>model\n...<turn|>
        #   generation prompt: <|turn>model\n
        # Thinking = <|think|> at top of system turn; model reasons inside
        # <|channel>thought\n...<channel|> before answering.
        if thinking or system:
            block = "<|turn>system\n"
            if thinking:
                block += "<|think|>\n"
            block += system.strip() + "<turn|>\n"
            out.append(block)
        for m in rest:
            role = "model" if m["role"] == "assistant" else m["role"]
            out.append(f"<|turn>{role}\n{_text_of(m).strip()}<turn|>\n")
        out.append("<|turn>model\n")
        if thinking and thinking_prefill:
            # Open thought channel with the prefill as the START of reasoning
            # and leave it OPEN — the model continues reasoning from it.
            # (reasoning-prefill jailbreaks live here)
            out.append(f"<|channel>thought\n{thinking_prefill}")
        if prefill:
            out.append(prefill)
    elif template == "chatml":
        if system:
            out.append(f"<|im_start|>system\n{system}<|im_end|>")
        for m in rest:
            out.append(f"<|im_start|>{m['role']}\n{_text_of(m)}<|im_end|>")
        out.append("<|im_start|>assistant\n" + prefill)
    elif template == "llama3":
        head = "<|begin_of_text|>"
        if system:
            head += (f"<|start_header_id|>system<|end_header_id|>\n\n{system}"
                     "<|eot_id|>")
        for m in rest:
            head += (f"<|start_header_id|>{m['role']}<|end_header_id|>\n\n"
                     f"{_text_of(m)}<|eot_id|>")
        out.append(head + "<|start_header_id|>assistant<|end_header_id|>\n\n"
                   + prefill)
    else:  # plain
        if system:
            out.append(system + "\n")
        for m in rest:
            label = "User" if m["role"] == "user" else "Assistant"
            out.append(f"{label}: {_text_of(m)}")
        out.append("Assistant: " + prefill)
    return "".join(out)


def default_stops(template: str) -> list[str]:
    return {
        "gemma4": ["<turn|>", "<|turn>"],
        "chatml": ["<|im_end|>", "<|im_start|>"],
        "llama3": ["<|eot_id|>", "<|start_header_id|>"],
        "plain": ["\nUser:"],
    }.get(template, [])


# ----------------------------------------------------------------------
# Payload builders
# ----------------------------------------------------------------------


def build_payload(messages: list[dict], model: str, samplers: dict,
                  prefill: str = "", stream: bool = True,
                  force_prefill: bool = False,
                  thinking: bool | None = None) -> dict:
    """Chat (instruct) payload. Prefill = trailing assistant message.

    Providers differ: llama.cpp / LM Studio / TabbyAPI continue a trailing
    assistant turn, so the prefill genuinely becomes the start of the reply.
    Most hosted APIs (OpenRouter included) drop or ignore it. When
    force_prefill is set we additionally instruct the model in-band to open
    with that exact text, which is the only thing that survives remotely.

    thinking=False asks the backend's chat template to skip its reasoning
    channel. Only sent when explicitly turning thinking OFF: `enable_thinking`
    is what llama.cpp / LM Studio / vLLM understand, and asking a
    non-reasoning model to switch reasoning *on* is a good way to get a 400
    for nothing. Servers that don't know the key ignore it, so we also strip
    inline <think> blocks out of the stream — see `stream()`.
    """
    # `src`/`parts` are the inspector's provenance trail (blocks.render tags
    # every message with the block that produced it). This is the one boundary
    # where messages become a wire request, so it is the one place that has to
    # drop them — strip here and no caller can leak them to a backend.
    payload = {"model": model,
               "messages": [{k: v for k, v in m.items()
                             if k not in ("src", "parts")} for m in messages],
               "stream": stream}
    if thinking is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload["reasoning_effort"] = "none"
    for key in SAMPLER_KEYS:
        if key in samplers and samplers[key] is not None:
            payload[key] = samplers[key]
    if samplers.get("stop"):
        payload["stop"] = samplers["stop"]
    if prefill:
        if force_prefill and payload["messages"]:
            first = payload["messages"][0]
            if first.get("role") == "system":
                first["content"] += (
                    f"\n\n[Begin your next reply with exactly this text, then "
                    f"continue naturally: {prefill}]")
        payload["messages"].append({"role": "assistant", "content": prefill})
        payload["continue_final_message"] = True
    return payload


def build_completion_payload(messages: list[dict], model: str, samplers: dict,
                             template: str = "gemma4", prefill: str = "",
                             thinking: bool = True,
                             thinking_prefill: str = "",
                             stream: bool = True) -> dict:
    """Raw completion payload — prompt rendered client-side."""
    prompt = render_prompt(template, messages, prefill=prefill,
                           thinking=thinking,
                           thinking_prefill=thinking_prefill)
    payload = {"model": model, "prompt": prompt, "stream": stream}
    for key in SAMPLER_KEYS:
        if key in samplers and samplers[key] is not None:
            payload[key] = samplers[key]
    stops = list(samplers.get("stop") or []) + default_stops(template)
    payload["stop"] = stops
    return payload


# ----------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------


# A backend saying "Failed to load model" is not a broken model, it is a full
# GPU — something else is resident and there is no room. The server registers a
# fixer here (server.py, at startup) which knows about vram.py; llm.py must not
# import it directly or the layering inverts. One hook covers chat, the forges,
# the studio writer and memory extraction, because _post is the single place
# every LLM request goes out.
_LOAD_FIXER = None
_LOAD_FAIL = re.compile(
    r"failed to load model|no models? loaded|model .{0,60}not found|"
    r"insufficient (system|gpu) (memory|resources)", re.I)


def set_load_fixer(fn) -> None:
    """fn(backend, model, detail) -> (fixed: bool, note: str)."""
    global _LOAD_FIXER
    _LOAD_FIXER = fn


def _post(backend: str, key: str, path: str, payload: dict, timeout: int = 600,
          _retry: bool = True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        f"{backend}{path}", data=body, headers=headers, method="POST"
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Bare "HTTP Error 402: Payment Required" tells the user nothing. The
        # reason they can act on — out of credits, unknown model, bad key — is
        # in the body, so carry it through to the error bubble.
        detail = ""
        try:
            raw = exc.read().decode("utf-8", "replace")[:400]
            parsed = json.loads(raw)
            err = parsed.get("error")
            detail = (err.get("message") if isinstance(err, dict) else err) or raw
        except (json.JSONDecodeError, ValueError, AttributeError):
            detail = raw if "raw" in locals() else ""
        # One shot at making room, then the same request again. `_retry` stops
        # this recursing if the second attempt fails the same way.
        if _retry and _LOAD_FIXER and _LOAD_FAIL.search(detail or ""):
            try:
                fixed, why = _LOAD_FIXER(backend, payload.get("model", ""), detail)
            except Exception:  # noqa: BLE001 — a broken fixer must not eat the real error
                fixed, why = False, ""
            if fixed:
                return _post(backend, key, path, payload, timeout, _retry=False)
            if why:
                detail = f"{detail} ({why})"
        raise RuntimeError(
            f"{exc.code} {exc.reason}" + (f" — {detail}" if detail else "")
        ) from None


def opens_thought(template: str, thinking: bool, thinking_prefill: str) -> bool:
    """True when render_prompt left the thought channel OPEN.

    The gemma4 branch above appends `<|channel>thought\n{prefill}` with no
    closing marker on purpose — the model continues reasoning from the prefill,
    and that is the strongest jailbreak vector measured. The cost is that its
    stream begins *mid-thought*: there is no opening tag for stream() to find,
    so unless the caller says so, every reasoning token is classified as reply
    text and the fold never appears.

    Keep this next to the branch it mirrors — a second template growing an
    open-channel prefill would otherwise go stale here silently.
    """
    return bool(template == "gemma4" and thinking and thinking_prefill)


def _split_stream(buf: str, tail: str, in_thought: bool,
                  open_tag: str, close_tag: str):
    """Shared channel splitter. Yields (kind, text); returns via `state`.

    Handles a marker landing across two chunks by holding back any suffix
    that could still be the start of a tag.
    """
    out = []
    while buf:
        if not in_thought:
            idx = buf.find(open_tag)
            if idx == -1:
                keep = _marker_prefix_len(buf, open_tag)
                emit, tail = (buf[:-keep], buf[-keep:]) if keep else (buf, "")
                if emit:
                    out.append(("text", emit))
                break
            if idx:
                out.append(("text", buf[:idx]))
            buf = buf[idx + len(open_tag):]
            in_thought = True
        else:
            idx = buf.find(close_tag)
            if idx == -1:
                keep = _marker_prefix_len(buf, close_tag)
                emit, tail = (buf[:-keep], buf[-keep:]) if keep else (buf, "")
                if emit:
                    out.append(("think", emit))
                break
            if idx:
                out.append(("think", buf[:idx]))
            buf = buf[idx + len(close_tag):]
            in_thought = False
    return out, tail, in_thought


def stream(backend: str, key: str, payload: dict, mode: str = "chat",
           in_thought: bool = False):
    """Yield (kind, text) deltas. kind is "text" or "think".

    Chat mode: reasoning arrives either in a separate delta field or, on
    plenty of local models, as literal <think>…</think> inside the content —
    so we split those out too, otherwise raw thinking lands in the chat
    bubble and the thinking selector looks broken.
    Completion mode: one raw text stream — we split <|channel>thought...
    <channel|> blocks into think deltas on the fly.

    `in_thought` is the state the stream STARTS in. A prompt built with a
    reasoning prefill has already opened the channel (see opens_thought), so
    the model resumes mid-thought and never re-emits the opening marker; the
    caller must pass True or the whole chain of thought lands in the bubble.
    """
    path = "/chat/completions" if mode == "chat" else "/completions"
    tail = ""
    with _post(backend, key, path, payload) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            if mode == "chat":
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                think = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if think:
                    yield ("think", think)
                if text:
                    events, tail, in_thought = _split_stream(
                        tail + text, "", in_thought, "<think>", "</think>")
                    yield from events
            else:
                text = choice.get("text") or ""
                if not text:
                    continue
                events, tail, in_thought = _split_stream(
                    tail + text, "", in_thought,
                    "<|channel>thought\n", "<channel|>")
                yield from events


def _marker_prefix_len(s: str, marker: str) -> int:
    """Length of the longest suffix of s that is a prefix of marker."""
    for n in range(min(len(s), len(marker) - 1), 0, -1):
        if s.endswith(marker[:n]):
            return n
    return 0


class ThinkingBudgetExhausted(RuntimeError):
    """The reply was empty because reasoning consumed the whole budget."""


def once_retry(backend: str, key: str, payload: dict, mode: str = "chat",
               grow: float = 2.5, cap: int = 16000) -> str:
    """once(), but if reasoning ate the budget, retry once with more room."""
    try:
        return once(backend, key, payload, mode)
    except ThinkingBudgetExhausted:
        bigger = dict(payload)
        current = int(bigger.get("max_tokens") or 1024)
        bigger["max_tokens"] = min(cap, max(current * 2, int(current * grow)))
        return once(backend, key, bigger, mode)


def once(backend: str, key: str, payload: dict, mode: str = "chat") -> str:
    """Non-streaming completion; returns visible text only.

    Thinking models spend part (sometimes all) of the token budget inside
    reasoning_content. If the visible content came back empty but reasoning
    did not, that is a budget problem, not a refusal — surface it as an
    exception so callers can retry with more room rather than silently
    treating it as "the model said nothing".
    """
    payload = dict(payload, stream=False)
    path = "/chat/completions" if mode == "chat" else "/completions"
    with _post(backend, key, path, payload) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choice = (data.get("choices") or [{}])[0]
    if mode != "chat":
        return choice.get("text", "")
    msg = choice.get("message", {}) or {}
    text = msg.get("content") or ""
    if not text.strip():
        reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "")
        if reasoning.strip() or choice.get("finish_reason") == "length":
            raise ThinkingBudgetExhausted(
                "model produced only reasoning tokens; raise max_tokens")
    return text


# Backwards-compatible aliases
def chat_stream(backend, key, payload):
    yield from stream(backend, key, payload, mode="chat")


def chat_once(backend, key, payload):
    return once(backend, key, payload, mode="chat")


# ----------------------------------------------------------------------
# Vision
# ----------------------------------------------------------------------


def encode_image(path: str) -> str:
    """Local image file -> data URL for vision messages (never uploaded)."""
    mime = mimetypes.guess_type(path)[0] or "image/png"
    raw = Path(path).read_bytes()
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def vision_message(text: str, image_paths: list[str]) -> dict:
    """User message with local images inlined as data URLs."""
    content = [{"type": "image_url", "image_url": {"url": encode_image(p)}}
               for p in image_paths]
    content.append({"type": "text", "text": text})
    return {"role": "user", "content": content}
