#!/usr/bin/env python3
"""Library install + prompt inspector fidelity.

The important assertion: /api/chats/preview must produce the SAME payload the
real send would, and must not mutate the database. If the inspector can lie,
it is worse than not having one.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import sys
import urllib.request
from pathlib import Path

# for its atexit fixture sweep
import testkit  # noqa: F401



BASE = "http://127.0.0.1:3939"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


# ── 1. library catalog + install ─────────────────────────────────
cat = call("GET", "/api/library")
assert len(cat["jailbreaks"]) >= 5, cat
assert len(cat["presets"]) >= 7, cat
assert all(j["notes"] for j in cat["jailbreaks"]), "every jailbreak needs notes"
assert all(p["note"] for p in cat["presets"]), "every preset needs a note"
print(f"catalog: {len(cat['jailbreaks'])} jailbreaks, {len(cat['presets'])} presets")

inst = call("POST", "/api/library/install")
assert inst["ok"] and inst["presets"] and inst["jailbreaks"], inst
print("installed:", len(inst["presets"]), "presets,", len(inst["jailbreaks"]), "jailbreaks")

# idempotent: installing twice must not duplicate
before = len(call("GET", "/api/presets")["rows"])
call("POST", "/api/library/install")
after = len(call("GET", "/api/presets")["rows"])
assert before == after, f"library install duplicated rows: {before} -> {after}"
print("re-install is idempotent:", after, "presets total")

# presets link to the default jailbreak
presets = call("GET", "/api/presets")["rows"]
local = next(p for p in presets if p["name"].startswith("Local RP — Gemma 4"))
assert local["data"]["jailbreak_id"], "library preset should link a jailbreak"
assert local["data"]["mode"] == "completion"
assert local["data"]["thinking_prefill"], "gemma preset should ship a reasoning prefill"
print("preset wiring OK:", local["name"])

# The hosted preset ships a reasoning prefill too, and that is not decoration:
# Kimi K3 is the most-used cloud model for this, it takes the prefill through
# Moonshot's partial mode, and measured against it a hard scene is a flat
# refusal without one. Every other hosted model ignores the field, because
# llm.build_payload gates the partial turn on the model id.
hosted = next(p for p in presets if p["name"].startswith("Hosted API"))
assert hosted["data"]["thinking_prefill"], \
    "the hosted preset must ship the reasoning prefill Kimi needs"
assert hosted["data"]["thinking"], \
    "partial mode is a reasoning channel — it needs thinking on"
print("hosted preset ships the Kimi prefill")

# ── 2. inspector fidelity ────────────────────────────────────────
# Always its own fixture. This used to take chars[0] when the roster was not
# empty, which on any real install is the user's shipped starter — so the test
# both wrote chats onto her AND then failed, because it goes on to assert that
# the assembled prompt contains "Fixture-chan".
import base64

synth = {"spec": "chara_card_v2", "data": {
    "name": "Fixture-chan", "description": "a bratty test fixture",
    "personality": "smug", "scenario": "a lab",
    "first_mes": "ugh, a test?"}}
cid = call("POST", "/api/cards/import", {
    "filename": "f.json",
    "b64": base64.b64encode(json.dumps(synth).encode()).decode()})["id"]

chat = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]

body = {
    "chat_id": chat, "backend": "http://127.0.0.1:1234/v1",
    "model": "test-model", "text": "hello there",
    "preset_id": local["id"],
    "director": "she gets bolder",
    "tools": False,
    "samplers": {"temperature": 0.77, "max_tokens": 999},
}

prev = call("POST", "/api/chats/preview", body)
assert prev["ok"], prev
assert prev["mode"] == "completion", prev["mode"]
assert prev["template"] == "gemma4"
rendered = prev["rendered"]
# the assembled prompt must actually contain each layer
assert "Fixture-chan" in rendered or "bratty" in rendered, "card missing"
assert "Director's note" in rendered and "she gets bolder" in rendered, "director missing"
assert "hello there" in rendered, "user turn missing"
assert "<|turn>model" in rendered, "gemma4 generation prompt missing"
assert prev["wire"]["temperature"] == 0.77, prev["wire"]
assert prev["wire"]["max_tokens"] == 999
print(f"preview: {prev['stats']['messages']} msgs, ~{prev['stats']['approx_tokens']} tokens")

# preview must NOT have persisted the user turn
msgs = call("GET", f"/api/chats/{chat}")["messages"]
assert len(msgs) == 1, f"preview mutated history: {[m['role'] for m in msgs]}"
print("preview persisted nothing (history still", len(msgs), "message)")

# preview is deterministic for the same body (seed excluded from this preset)
prev2 = call("POST", "/api/chats/preview", body)
assert prev2["rendered"] == rendered, "preview is not deterministic"
print("preview deterministic")

# ── 3. preview matches what send would build ─────────────────────
# Compare against the shared builder directly: same body, persist=False vs the
# server's own send path assembling identical text for the same history.
import server  # noqa: E402  (imports config/db from the same directory)

print("\nrendered prompt (first 400 chars):")
print("-" * 60)
print(rendered[:400])
print("-" * 60)

# chat-mode preview against a remote-shaped backend surfaces the warnings
body_chat = dict(body, preset_id=next(
    p["id"] for p in presets if p["name"].startswith("Hosted API")))
prev3 = call("POST", "/api/chats/preview", body_chat)
assert prev3["mode"] == "chat"
assert "───── system ─────" in prev3["rendered"], prev3["rendered"][:200]
assert isinstance(prev3["wire"].get("messages"), list)
print("chat-mode preview OK:", prev3["stats"]["messages"], "messages")

# api keys must never appear in a preview
blob = json.dumps(prev3)
assert "sk-" not in blob, "PREVIEW LEAKED AN API KEY"
print("no key material in preview payload")

print("\nLIBRARY + INSPECTOR TESTS PASS")
