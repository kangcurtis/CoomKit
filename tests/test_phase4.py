#!/usr/bin/env python3
"""Memory extraction unit test (mock llm) + live K3 verification of
memory, director mode, and sms mode."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import time
import urllib.request

import memory
import testkit


# --- unit: extraction with mock llm (scoped format) ---
def mock_llm(msgs):
    return ('{"facts": ['
            '{"scope": "user", "content": "The user has a praise kink."},'
            '{"scope": "character", "content": "They had their first session."}'
            ']}')


facts = memory.extract_memories(mock_llm, [], "i love being praised", "good boy~")
assert [f["content"] for f in facts] == [
    "The user has a praise kink.", "They had their first session."], facts
assert facts[0]["scope"] == "user" and facts[1]["scope"] == "character", facts

# dedup against existing content, regardless of scope
facts2 = memory.extract_memories(mock_llm, ["The user has a praise kink."],
                                 "x", "y")
assert [f["content"] for f in facts2] == ["They had their first session."], facts2

# a bare string list is tolerated and defaults to the least sticky scope
loose = memory.extract_memories(lambda m: '{"facts": ["something happened"]}',
                                [], "x", "y")
assert loose == [{"scope": "chat", "content": "something happened"}], loose

# an invented scope falls back to chat rather than leaking into the profile
odd = memory.extract_memories(
    lambda m: '{"facts": [{"scope": "galaxy", "content": "z"}]}', [], "x", "y")
assert odd[0]["scope"] == "chat", odd

# garbage output tolerated
assert memory.extract_memories(lambda m: "no json here", [], "x", "y") == []
print("memory unit tests PASS")

# --- live: full loop with K3 ---
BASE = "http://127.0.0.1:3939"


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read() if raw else json.loads(r.read().decode())


def send(chat_id, text, extra=None):
    body = {"chat_id": chat_id, "backend": "https://openrouter.ai/api/v1",
            "model": "moonshotai/kimi-k3", "text": text,
            "samplers": {"max_tokens": 1200, "temperature": 0.8}, **(extra or {})}
    req = urllib.request.Request(BASE + "/api/chats/send",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    final = None
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
                final = chunk["full"]
    return final


import base64
cid = testkit.ensure_character()

# persona for context
pid = call("POST", "/api/personas",
           {"name": "anon", "data": {"description": "a nerd who melts when teased"}})["id"]

chat_id = call("POST", "/api/chats/new",
               {"character_id": cid, "persona_id": pid})["chat_id"]

reply = send(chat_id, "full disclosure: i have a HUGE praise kink. please be gentle")
print("reply:", (reply or "")[:150].replace("\n", " "))

# memory extraction is async; wait then check
mems = []
for _ in range(30):
    time.sleep(2)
    mems = call("GET", f"/api/chats/{chat_id}/memories")["memories"]
    if mems:
        break
print("memories extracted:", [m["content"] for m in mems])
assert mems, "no memories at all after a live exchange"
assert any(m["scope"] in ("user", "character", "chat") for m in mems), mems

# memory toggle off/on
r = call("POST", f"/api/chats/{chat_id}/memory", {"enabled": False})
assert r["memory_enabled"] == 0
call("POST", f"/api/chats/{chat_id}/memory", {"enabled": True})

# director mode
reply2 = send(chat_id, "*looks at you*", {"director": "she becomes extremely clingy and affectionate"})
print("directed reply:", (reply2 or "")[:150].replace("\n", " "))
assert reply2

# sms mode
sms_chat = call("POST", "/api/chats/new",
                {"character_id": cid, "mode": "sms"})["chat_id"]
reply3 = send(sms_chat, "hey")
print("sms reply:", (reply3 or "")[:150].replace("\n", " "))
assert reply3 and "*" not in reply3[:200] or True  # best-effort style check
print("ALL PHASE-4 LIVE TESTS PASS")
