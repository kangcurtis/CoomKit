#!/usr/bin/env python3
"""Editable prompt layers.

The point of this feature is that overriding a layer actually changes what the
model receives. So every assertion here goes through /api/chats/preview — the
real assembly path — rather than trusting the prompts module in isolation.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import sys
import urllib.request
from pathlib import Path

import testkit

HERE = _bootstrap.ROOT


BASE = "http://127.0.0.1:3939"
LOCAL = "http://127.0.0.1:1234/v1"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def prompt_map():
    return {p["key"]: p for p in call("GET", "/api/prompts")["prompts"]}


# ── clean slate ──────────────────────────────────────────────────
call("POST", "/api/prompts/reset", {})

# ── 1. catalog shape ─────────────────────────────────────────────
pm = prompt_map()
expected = {"director", "sms", "thinking_character",
            "thinking_character_prefill", "memory_header",
            "forge_suggest", "forge_refine", "memory_extract", "tools_spec"}
assert expected <= set(pm), f"missing layers: {expected - set(pm)}"
for key, spec in pm.items():
    assert spec["label"] and spec["desc"], f"{key} lacks label/desc"
    assert spec["default"].strip(), f"{key} has an empty default"
    assert spec["customised"] is False, f"{key} should start pristine"
assert "director" in pm["director"]["placeholders"]
assert "char" in pm["thinking_character"]["placeholders"]
print(f"catalog: {len(pm)} layers, all documented, none customised")

groups = {p["group"] for p in pm.values()}
# `cast` joined them: the layers that only exist when more than one
# character is in the room. A solo chat never sees any of it.
assert groups == {"scene", "cast", "forge", "system", "recipes"}, groups
print("groups:", ", ".join(sorted(groups)))

# Every recipe brief that is actually INJECTED has to reach the editor, or the
# studio becomes the one corner of CoomKit where injected text is hidden from
# the user again. A `direct` recipe never reaches the prompt-writer at all
# ("say it out loud" reads words that already exist), so registering a layer
# for it would put a text box in the inspector that changes nothing.
import recipes as _recipes  # noqa: E402
recipe_layers = {k for k, v in pm.items() if v["group"] == "recipes"}
written = {f"recipe_{r}" for r, spec in _recipes.RECIPES.items()
           if not spec.get("direct")}
assert recipe_layers == written, (recipe_layers, written)
assert any(spec.get("direct") for spec in _recipes.RECIPES.values()), \
    "the direct flag is what this exemption rests on — if it is gone, so is it"
for key in recipe_layers:
    assert pm[key]["placeholders"], f"{key} declares no placeholders"
print(f"recipes: {len(recipe_layers)} briefs editable")

# ── 2. fixture chat ──────────────────────────────────────────────
# A FIXTURE character, never rows[0]. Taking whatever the roster held first
# means adopting the user's OWN character on any real install — and this file
# then created an rp chat AND an sms chat on her, every single suite run,
# neither of them swept because they do not belong to a fixture. Measured on
# the dev box: the shipped starter had collected 121 chats that way. Same bug
# testkit.ensure_character exists to end, and the same one already fixed in
# test_scenarios and test_library.
cid = testkit.ensure_character()

chat = call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]
sms = call("POST", "/api/chats/new",
           {"character_id": cid, "mode": "sms"})["chat_id"]
# the name the server will substitute for {char}
char_name = call("GET", f"/api/characters/{cid}")["name"]
print("fixture character:", char_name, f"(id {cid})")


def rendered(chat_id, **extra):
    body = {"chat_id": chat_id, "backend": LOCAL, "model": "m",
            "text": "hi", "tools": False}
    body.update(extra)
    r = call("POST", "/api/chats/preview", body)
    assert r.get("ok"), r
    return r["rendered"]


# ── 3. director override reaches the prompt ──────────────────────
base = rendered(chat, director="she gets bolder")
assert "Director's note" in base and "she gets bolder" in base
assert "stage direction" in base, "shipped director wording missing"

r = call("POST", "/api/prompts", {
    "key": "director",
    "text": "!!!MY OWN WRAPPER!!! do this now: {director} (and {char} stays "
            "oblivious)"})
assert r.get("ok"), r
after = rendered(chat, director="she gets bolder")
assert "!!!MY OWN WRAPPER!!!" in after, "override did not reach the prompt"
assert "she gets bolder" in after, "{director} placeholder not filled"
assert "stage direction" not in after, "default text still present"
# {char} resolved to the actual character name for THIS chat
detail = call("GET", f"/api/chats/{chat}")
resolved_name = detail.get("character", char_name)
assert f"(and {resolved_name} stays oblivious)" in after, \
    f"expected {resolved_name!r}; tail was {after[-200:]!r}"
print("director override lands, placeholders filled with real values")

assert prompt_map()["director"]["customised"] is True
print("catalog reports it as customised")

# ── 4. reset restores shipped behaviour ──────────────────────────
call("POST", "/api/prompts/reset", {"key": "director"})
back = rendered(chat, director="she gets bolder")
assert "!!!MY OWN WRAPPER!!!" not in back and "stage direction" in back
assert prompt_map()["director"]["customised"] is False
print("per-key reset restores the default")

# ── 5. sms + thinking layers ─────────────────────────────────────
assert "Texting mode" in rendered(sms)
call("POST", "/api/prompts", {"key": "sms", "text": "TEXT LIKE A PIRATE"})
assert "TEXT LIKE A PIRATE" in rendered(sms)
assert "Texting mode" not in rendered(sms)
print("sms layer override works")

call("POST", "/api/prompts", {
    "key": "thinking_character", "text": "THINK AS {char} ONLY"})
ic = rendered(chat, thinking_mode="character")
assert f"THINK AS {resolved_name} ONLY" in ic, "thinking hint override missing"
print("in-character thinking hint override works")

# ── 6. an unknown placeholder degrades visibly, never 500s ───────
call("POST", "/api/prompts", {
    "key": "director", "text": "do {director} for {nonexistent_thing}"})
odd = rendered(chat, director="X")
assert "{nonexistent_thing}" in odd, "typo should survive as literal text"
assert "do X for" in odd
print("unknown placeholder degrades to literal text (no crash)")

# ── 7. bad key rejected, forge prompts editable ──────────────────
bad = call("POST", "/api/prompts", {"key": "not_a_real_layer", "text": "x"})
assert "error" in bad, bad
print("unknown key rejected:", bad["error"])

call("POST", "/api/prompts", {
    "key": "forge_suggest",
    "text": pm["forge_suggest"]["default"] + "\n\nEXTRA HOUSE RULE: neon only."})
assert prompt_map()["forge_suggest"]["customised"] is True
print("forge prompt is editable too")

# ── 8. overrides survive a restart (they are on disk) ────────────
over = json.loads((HERE / "data" / "prompts.json").read_text())
assert "forge_suggest" in over and "sms" in over, list(over)
print("overrides persisted to data/prompts.json:", ", ".join(sorted(over)))

# ── cleanup ──────────────────────────────────────────────────────
call("POST", "/api/prompts/reset", {})
assert all(not p["customised"] for p in prompt_map().values())
print("global reset clears everything")

print("\nEDITABLE PROMPT LAYER TESTS PASS")
