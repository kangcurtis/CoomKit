#!/usr/bin/env python3
"""Reproduce the vanish: send -> immediately GET chat detail (like loadChat)."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import base64
import json
import urllib.request

import testkit

BASE = "http://127.0.0.1:3939"

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())

cid = testkit.ensure_character()
chat_id = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]

# stream a send to completion
body = {"chat_id": chat_id, "backend": "https://openrouter.ai/api/v1",
        "model": "moonshotai/kimi-k3", "text": "say hi bratty",
        "samplers": {"max_tokens": 800, "temperature": 0.9}}
req = urllib.request.Request(BASE + "/api/chats/send",
                             data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json"})
done_chunk = None
with urllib.request.urlopen(req, timeout=600) as resp:
    for line in resp:
        line = line.decode().strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if chunk.get("done"):
            done_chunk = chunk

print("done chunk:", done_chunk)
# EXACTLY what loadChat does next:
d = call("GET", f"/api/chats/{chat_id}")
print("messages after send:", [(m["id"], m["role"], m["content"][:40]) for m in d["messages"]])
assert any(m["id"] == done_chunk["message_id"] for m in d["messages"]), \
    "VANISH REPRODUCED: streamed message missing from detail!"
print("message present in detail — persistence OK")
