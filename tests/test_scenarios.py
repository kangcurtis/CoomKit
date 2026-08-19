#!/usr/bin/env python3
"""Scenario forge + scoped memory.

Two things must be true or the feature is theatre:
  1. memory scopes route correctly — user facts follow the player everywhere,
     character facts survive a NEW chat, chat facts stay put.
  2. a forged scenario actually lands in the outgoing prompt and replaces the
     card's static scenario, and its opening seeds the first message.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import base64
import json
import sys
import urllib.request
from pathlib import Path


import scenarios  # noqa: E402

BASE = "http://127.0.0.1:3939"
LOCAL = "http://127.0.0.1:1234/v1"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


# ── 0. offline parser tests (no model needed) ────────────────────
noisy = """Sure! Here are some ideas:
```json
{"scenarios": [
 {"title": "Late Lab Night", "premise": "She stayed late.", "setting": "her lab, 2am",
  "hook": "nobody else is in the building", "opening": "You again? At this hour?",
  "tags": ["lab", "alone"]},
 {"title": "Bad Idea", "premise": "A dare.", "setting": "rooftop",
  "hook": "she never backs down", "opening": "Fine. Do it.", "tags": "dare, rooftop"}
]}
```
Hope that helps!"""
got = scenarios.parse_scenarios(noisy)
assert len(got) == 2, got
assert got[0]["title"] == "Late Lab Night"
assert got[1]["tags"] == ["dare", "rooftop"], got[1]["tags"]
# junk in -> empty out, never an exception
assert scenarios.parse_scenarios("no json at all") == []
assert scenarios.parse_scenarios("") == []
# a bare single scenario is tolerated
one = scenarios.parse_one('{"title":"X","premise":"Y","opening":"Z"}')
assert one and one["title"] == "X", one
# incomplete entries are dropped, not returned half-built
assert scenarios.parse_scenarios('{"scenarios":[{"title":"only a title"}]}') == []
print("parser: tolerant of noise, strict about completeness")

# ── 1. fixture card ──────────────────────────────────────────────
rows = call("GET", "/api/characters")["rows"]
if rows:
    cid = rows[0]["id"]
else:
    synth = {"spec": "chara_card_v2", "data": {
        "name": "Fixture-chan", "description": "a smug lab assistant",
        "personality": "bratty", "scenario": "ORIGINAL CARD SCENARIO",
        "first_mes": "the card's canned greeting"}}
    cid = call("POST", "/api/cards/import", {
        "filename": "f.json",
        "b64": base64.b64encode(json.dumps(synth).encode()).decode()})["id"]
print("character:", cid)

# ── 2. memory scope routing ──────────────────────────────────────
chat_a = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]

for scope, content in (("user", "The user is called anon."),
                       ("character", "They kissed in the lab once."),
                       ("chat", "She is currently holding a clipboard.")):
    r = call("POST", "/api/memories",
             {"scope": scope, "content": content, "chat_id": chat_a})
    assert r.get("ok"), r
seen_a = {m["scope"] for m in call("GET", f"/api/chats/{chat_a}/memories")["memories"]}
assert seen_a == {"user", "character", "chat"}, seen_a
print("chat A sees all three scopes")

# a brand new chat with the same character: user + character carry, chat does not
chat_b = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]
mems_b = call("GET", f"/api/chats/{chat_b}/memories")["memories"]
scopes_b = {m["scope"] for m in mems_b}
contents_b = {m["content"] for m in mems_b}
assert "user" in scopes_b and "character" in scopes_b, scopes_b
assert "chat" not in scopes_b, f"scene furniture leaked into a new chat: {mems_b}"
assert "The user is called anon." in contents_b
assert "They kissed in the lab once." in contents_b
assert "She is currently holding a clipboard." not in contents_b
print("new chat inherits user+character memory, not scene detail")

# ── 3. forged scenario reaches the prompt ────────────────────────
forged = {
    "title": "Locked In After Hours",
    "premise": "The building sealed at midnight and they are the only two inside.",
    "setting": "the observatory dome, well past midnight",
    "hook": "neither of them is trying very hard to find the exit",
    "opening": "*she leans against the doorframe* Well. Isn't this inconvenient.",
    "tags": ["locked in", "night"],
}
launched = call("POST", "/api/chats/new",
                {"character_id": cid, "scenario": forged})
assert launched["ok"] and launched["scenario"] == forged["title"], launched
chat_c = launched["chat_id"]

# the opening seeded the first message instead of the card greeting
msgs = call("GET", f"/api/chats/{chat_c}")["messages"]
assert len(msgs) == 1 and msgs[0]["role"] == "assistant", msgs
assert msgs[0]["content"].startswith("*she leans against the doorframe*"), msgs[0]
assert "canned greeting" not in msgs[0]["content"]
print("forged opening seeded the chat:", msgs[0]["content"][:60])

# and the scenario is in the actual outgoing prompt, replacing the card's
prev = call("POST", "/api/chats/preview", {
    "chat_id": chat_c, "backend": LOCAL, "model": "test-model",
    "text": "so what now?", "tools": False})
assert prev.get("ok"), prev
rendered = prev["rendered"]
assert "Locked In After Hours" in rendered, "forged title missing from prompt"
assert "observatory dome" in rendered, "forged setting missing from prompt"
assert "trying very hard to find the exit" in rendered, "hook missing"
assert "Tension:" in rendered, "hook not labelled"
assert "ORIGINAL CARD SCENARIO" not in rendered, \
    "card scenario should have been replaced by the forged one"
# scoped memory is in there too, labelled
assert "(user) The user is called anon." in rendered, "user memory missing"
assert "(character) They kissed in the lab once." in rendered, "history missing"
print("prompt contains forged scenario + scoped memory, card scenario replaced")

# a chat with no forged scenario still uses the card's own fields
char = call("GET", f"/api/characters/{cid}")
card_scenario = (char["data"]["fields"].get("scenario") or "").strip()
plain = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]
prev2 = call("POST", "/api/chats/preview", {
    "chat_id": plain, "backend": LOCAL, "model": "test-model",
    "text": "hi", "tools": False})
assert "Locked In After Hours" not in prev2["rendered"], \
    "forged scenario leaked into an unrelated chat"
if card_scenario:
    assert card_scenario in prev2["rendered"], \
        "cards without a forged scenario must still use their own"
    print("un-forged chats still use the card scenario")
else:
    assert "Scene:" not in prev2["rendered"], \
        "no scenario anywhere, but a Scene: block appeared"
    print("card has no scenario field; no scene block injected (correct)")

print("\nSCENARIO FORGE + SCOPED MEMORY TESTS PASS")
