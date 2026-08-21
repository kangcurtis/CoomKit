#!/usr/bin/env python3
"""Live scenario forge against K3: suggest -> brainstorm -> refine -> launch."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import urllib.request

import testkit

BASE = "http://127.0.0.1:3939"
OR = "https://openrouter.ai/api/v1"
MODEL = "moonshotai/kimi-k3"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


# A FIXTURE character, never rows[0] — see test_scenarios.
cid = testkit.ensure_character()

# suggestions WITH memory (there are user+character facts from the unit test)
r = call("POST", "/api/scenarios/suggest", {
    "character_id": cid, "backend": OR, "model": MODEL,
    "count": 3, "use_memory": True,
    "brief": "one of them should be a slow burn, one should be filthy immediately"})
assert r.get("ok"), r
print(f"suggested {len(r['scenarios'])} scenarios "
      f"(memory: {r['memory_count']} facts)\n")
for i, s in enumerate(r["scenarios"], 1):
    print(f"{i}. {s['title']}  [{', '.join(s['tags'])}]")
    print(f"   setting: {s['setting']}")
    print(f"   premise: {s['premise'][:150]}")
    print(f"   hook:    {s['hook'][:120]}")
    print(f"   opens:   {s['opening'][:150]}\n")

titles = [s["title"] for s in r["scenarios"]]
assert len(set(titles)) == len(titles), f"duplicate scenarios: {titles}"
assert all(s["opening"] for s in r["scenarios"]), "a scenario has no opening"

# brainstorm: revise one
target = r["scenarios"][0]
rev = call("POST", "/api/scenarios/refine", {
    "character_id": cid, "backend": OR, "model": MODEL,
    "scenario": target,
    "instruction": "make it rainier, move it outdoors, and she should be the "
                   "one who initiates"})
assert rev.get("ok"), rev
s2 = rev["scenario"]
print("--- refined ---")
print(f"{target['title']}  ->  {s2['title']}")
print(f"setting: {s2['setting']}")
print(f"opens:   {s2['opening'][:200]}\n")
assert s2["opening"] and s2["premise"]

# launch it
launched = call("POST", "/api/chats/new",
                {"character_id": cid, "scenario": s2})
assert launched["ok"], launched
chat = launched["chat_id"]
msgs = call("GET", f"/api/chats/{chat}")["messages"]
assert len(msgs) == 1 and msgs[0]["content"].strip() == s2["opening"].strip()
print("launched chat", chat, "seeded with the refined opening")

# and the scene is in the real prompt
prev = call("POST", "/api/chats/preview", {
    "chat_id": chat, "backend": OR, "model": MODEL,
    "text": "what happens next?", "tools": False})
assert s2["title"] in prev["rendered"], "refined scenario missing from prompt"
print(f"prompt carries the scene ({prev['stats']['approx_tokens']} tokens total)")

# without memory: should still work, and report it used none
r2 = call("POST", "/api/scenarios/suggest", {
    "character_id": cid, "backend": OR, "model": MODEL,
    "count": 1, "use_memory": False})
assert r2.get("ok") and r2["memory_count"] == 0, r2
print("clean-slate mode works, 0 memories used")

print("\nLIVE SCENARIO FORGE PASS")
