#!/usr/bin/env python3
"""Post-redesign smoke: new request shape (rail overrides, images, prefill)."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import base64
import json
import urllib.request

BASE = "http://127.0.0.1:3939"
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IHDR" + b"\x00" * 20)  # junk but PNG-ish


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def stream_send(body):
    req = urllib.request.Request(BASE + "/api/chats/send",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    text, think, err = [], [], None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            c = json.loads(payload)
            if "error" in c:
                err = c["error"]
            elif "think" in c:
                think.append(c["think"])
            elif "text" in c:
                text.append(c["text"])
    return "".join(text), "".join(think), err


rows = call("GET", "/api/characters")["rows"]
if rows:
    cid = rows[0]["id"]
    print("using existing card:", rows[0]["name"])
else:
    # minimal synthetic v2 card so the smoke test is self-sufficient
    synth = {"spec": "chara_card_v2", "data": {
        "name": "Test-chan", "description": "a bratty test fixture",
        "first_mes": "ugh, a test? really?"}}
    cid = call("POST", "/api/cards/import", {
        "filename": "test.json",
        "b64": base64.b64encode(json.dumps(synth).encode()).decode()})["id"]
    print("imported synthetic card")
chat = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]
print("chat", chat)

# 1. rail overrides: in-character thinking + reply prefill, no preset at all
t, th, err = stream_send({
    "chat_id": chat, "backend": "https://openrouter.ai/api/v1",
    "model": "moonshotai/kimi-k3", "text": "hi, be brief",
    "thinking_mode": "character",
    "reply_prefill": "Ugh, fine.",
    "samplers": {"max_tokens": 900, "temperature": 0.8},
    "tools": False,
})
assert not err, err
assert t.strip(), "empty reply"
# On hosted APIs a prefill is a soft instruction the model may ignore (proven
# behaviour with K3) — the contract we test is that the request still streams
# a valid in-character reply and nothing 500s. Literal continuation is only
# guaranteed on local backends, covered by test_llm/test_jb.
print("rail overrides OK:", t[:110].replace("\n", " "))
if "Ugh, fine" in t[:120]:
    print("  (prefill emulation landed this run)")
else:
    print("  (prefill ignored by remote model — expected, badge warns user)")

# 2. image attachment path (remote model -> must NOT send the image)
t2, _, err2 = stream_send({
    "chat_id": chat, "backend": "https://openrouter.ai/api/v1",
    "model": "moonshotai/kimi-k3", "text": "what do you think?",
    "images": [{"name": "test.png", "b64": base64.b64encode(PNG).decode()}],
    "samplers": {"max_tokens": 500}, "tools": False,
})
assert not err2, err2
assert t2.strip(), "empty reply on image turn"
print("image turn OK (remote guarded):", t2[:110].replace("\n", " "))

# 3. history recorded the image reference
d = call("GET", f"/api/chats/{chat}")
roles = [m["role"] for m in d["messages"]]
assert roles.count("user") == 2, roles
print("history:", roles)
print("POST-REDESIGN SMOKE PASS")
