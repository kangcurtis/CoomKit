#!/usr/bin/env python3
"""End-to-end chat engine test vs live LM Studio.

Flow: import Gemma-chan card -> new chat -> send user msg -> streamed reply
persisted -> regenerate (swipe) -> switch swipes -> verify persistence.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import base64
import json
import urllib.request

import testkit

BASE = "http://127.0.0.1:3939"
LLM = "http://127.0.0.1:1234/v1"
MODEL = json.loads(urllib.request.urlopen(LLM + "/models").read())["data"][0]["id"]
print("model:", MODEL)


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def send(chat_id, text=None, regenerate=False, swipe_message_id=None):
    body = {"chat_id": chat_id, "backend": LLM, "model": MODEL,
            "regenerate": regenerate,
            "samplers": {"max_tokens": 2048, "temperature": 0.9}}
    if text:
        body["text"] = text
    if swipe_message_id:
        body["swipe_message_id"] = swipe_message_id
    req = urllib.request.Request(BASE + "/api/chats/send",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    think, final = [], None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if "error" in chunk:
                raise SystemExit("STREAM ERROR: " + chunk["error"])
            if chunk.get("done"):
                final = chunk
            elif "think" in chunk:
                think.append(chunk["think"])
    return final, "".join(think)


# 1. import card (generated fixture, no files on disk needed)
cid = testkit.ensure_character()
print("card imported:", call("GET", f"/api/characters/{cid}")["name"])

# 2. new chat — greeting seeded
chat_id = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]
detail = call("GET", f"/api/chats/{chat_id}")
assert detail["messages"] and detail["messages"][0]["role"] == "assistant"
print("chat created, greeting:", detail["messages"][0]["content"][:80].replace("\n", " "))

# 3. send a message
final, think = send(chat_id, "hey gemma-chan! what's 6*7? and don't be smug about it")
assert final and final["full"].strip()
first_take = final["full"].strip()
print("reply:", first_take[:200].replace("\n", " "))
if think:
    print("think:", think[:120].replace("\n", " "))

# 4. regenerate -> swipe
final2, _ = send(chat_id, regenerate=True)
assert final2 and final2["full"].strip()
print("regen swipe:", final2["full"][:200].replace("\n", " "))

detail = call("GET", f"/api/chats/{chat_id}")
last = detail["messages"][-1]
# `swipes` is the TOTAL number of takes, seeded with the original. It used to
# count only the extras, so after one regen the list had length 1, both arrows
# clamped to the same entry, and the first take was unreachable forever —
# which is what "the swipe arrow does nothing" actually was.
assert last["swipes"] == 2 and last["swipe_index"] == 1, last

back = call("POST", f"/api/messages/{last['id']}/swipe", {"index": 0})
assert back["ok"] and back["index"] == 0 and back["total"] == 2, back
assert back["content"].strip() == first_take, \
    "swipe 0 must be the ORIGINAL take, not a copy of the regen"
fwd = call("POST", f"/api/messages/{last['id']}/swipe", {"index": 1})
assert fwd["ok"] and fwd["index"] == 1 and fwd["content"].strip() == final2["full"].strip()
# out of range clamps and REPORTS where it landed, so the counter cannot lie
edge = call("POST", f"/api/messages/{last['id']}/swipe", {"index": 99})
assert edge["index"] == 1 and edge["total"] == 2, edge
assert "{{user}}" not in back["content"], "swipes render through macros too"
print("swipes: original reachable, index/total reported, out-of-range clamps")

# 5. re-roll an OLDER message, not just the last one
greeting = detail["messages"][0]
assert greeting["role"] == "assistant"
final3, _ = send(chat_id, regenerate=True, swipe_message_id=greeting["id"])
assert final3 and final3["full"].strip()
detail = call("GET", f"/api/chats/{chat_id}")
assert detail["messages"][0]["swipes"] == 2, detail["messages"][0]
assert len(detail["messages"]) == 3, "re-rolling an older reply must not truncate"
print("re-roll of an older reply: stored as a swipe, history intact")

# 5. persistence: all messages in db
detail = call("GET", f"/api/chats/{chat_id}")
roles = [m["role"] for m in detail["messages"]]
assert roles == ["assistant", "user", "assistant"], roles
print("history roles:", roles)
print("ALL CHAT ENGINE TESTS PASS")
