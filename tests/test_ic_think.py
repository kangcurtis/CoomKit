#!/usr/bin/env python3
"""Live test: in-character thinking mode vs normal."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import base64
import json
import urllib.request

import testkit

BASE = "http://127.0.0.1:3939"
LLM = "http://127.0.0.1:1234/v1"
MODEL = json.loads(urllib.request.urlopen(LLM + "/models").read())["data"][0]["id"]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


# fresh card + chat
cid = testkit.ensure_character()

# preset with in-character thinking
preset = call("POST", "/api/presets", {"name": "ic-think", "data": {
    "mode": "chat", "thinking": True, "thinking_mode": "character",
    "samplers": {"max_tokens": 1200, "temperature": 0.9}}})

chat_id = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]

body = {"chat_id": chat_id, "backend": LLM, "model": MODEL,
        "preset_id": preset["id"], "text": "*I pat your head* you're cute when you compile"}
req = urllib.request.Request(BASE + "/api/chats/send",
                             data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
think, text, notices, errors = [], [], [], []
with urllib.request.urlopen(req, timeout=600) as resp:
    for line in resp:
        line = line.decode().strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if "think" in chunk:
            think.append(chunk["think"])
        elif "text" in chunk:
            text.append(chunk["text"])
        elif "notice" in chunk:
            notices.append(chunk["notice"])
        elif "error" in chunk:
            errors.append(str(chunk["error"]))

print("=== HER INNER VOICE (in-character thinking) ===")
print("".join(think)[:600])
print("=== REPLY ===")
print("".join(text)[:300])
# The ONLY assertion is that a reasoning model produced visible words, and it
# is a live model at temperature, so it does occasionally fail. That is the
# documented ThinkingBudgetExhausted hazard, not a plumbing bug — `_chat_send`
# escalates max_tokens once and streams again when reasoning arrives with no
# text. Report what actually happened so a failure here is diagnosable instead
# of just "empty reply": if the escalation notice is absent the escalation did
# not fire, which WOULD be a bug worth chasing.
if notices:
    print("=== NOTICES ===")
    for n in notices:
        print(" ", n)
if errors:
    print("=== ERRORS ===")
    for e in errors:
        print(" ", e)
assert not errors, f"the turn errored: {errors}"
assert text, (
    "empty reply — she spent the whole budget thinking. "
    + (f"the escalation DID fire ({len(notices)} notice(s)) and still came "
       f"back empty, so this is the model, not the plumbing: {notices}"
       if any("budget" in n for n in notices) else
       "and the escalation did NOT fire, which is a real bug in _chat_send")
    + f" — reasoning was {len(''.join(think))} chars")
print("\nIN-CHARACTER THINKING TEST PASS")
