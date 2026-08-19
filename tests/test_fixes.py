#!/usr/bin/env python3
"""Regressions for the round of bug fixes: schema self-heal, message editing,
card editing, the director round-trip and the thinking toggle.

Offline/local only — no tokens. The live half of the director feature (does a
real model actually emit the block) is exercised by hand; what is pinned here
is that the plumbing on either side of the model is correct.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import atexit
import base64
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

HERE = _bootstrap.ROOT


import cards  # noqa: E402
import llm  # noqa: E402
import macros  # noqa: E402
import testkit as T  # noqa: E402
import tools  # noqa: E402

BASE = "http://127.0.0.1:3939"


def delete(path):
    req = urllib.request.Request(BASE + path, method="DELETE")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


# ── 1. thinking toggle reaches the chat payload ──────────────────
# It used to be computed and dropped, so "off" did nothing on LM Studio.
off = llm.build_payload([{"role": "user", "content": "hi"}], "m", {}, thinking=False)
assert off["chat_template_kwargs"] == {"enable_thinking": False}, off
assert off["reasoning_effort"] == "none", off
# thinking ON must not send the keys at all — asking a non-reasoning model to
# turn reasoning on is a 400 for nothing
on = llm.build_payload([{"role": "user", "content": "hi"}], "m", {}, thinking=True)
assert "chat_template_kwargs" not in on and "reasoning_effort" not in on, on
unset = llm.build_payload([{"role": "user", "content": "hi"}], "m", {})
assert "reasoning_effort" not in unset, unset
print("thinking toggle reaches the payload OK")

# ── 2. inline <think> is split out of chat content ───────────────
# Local models emit reasoning in the content stream; it must not hit the bubble.
ev, tail, in_thought = llm._split_stream("a<think>b</think>c", "", False,
                                         "<think>", "</think>")
assert ev == [("text", "a"), ("think", "b"), ("text", "c")], ev
# ...including when the marker straddles two SSE chunks
ev1, tail, st = llm._split_stream("hello <thi", "", False, "<think>", "</think>")
ev2, tail, st = llm._split_stream(tail + "nk>secret", "", st, "<think>", "</think>")
assert ev1 == [("text", "hello ")], ev1
assert ev2 == [("think", "secret")], ev2
print("inline <think> splitting OK")

# ── 3. director block parses out of the reply ────────────────────
reply = 'She smirks.\n\n```director\nsetting up a fork here\n```'
visible, note = tools.split_director_note(reply)
assert visible == "She smirks.", repr(visible)
assert note == "setting up a fork here", repr(note)
assert tools.split_director_note("no block here") == ("no block here", "")
print("director block parsing OK")

# ── 4. card edits land in the embedded card, not just the flat view ──
parsed = cards.parse_card(T.card_png())
edited = cards.apply_edits(parsed, {"description": "NEW", "name": "Renamed"})
inner = edited["raw"]["data"] if isinstance(edited["raw"].get("data"), dict) else edited["raw"]
assert edited["fields"]["description"] == "NEW"
assert inner["description"] == "NEW", "edit did not reach the embedded card"
assert edited["name"] == "Renamed"
# and the original is untouched — apply_edits must not alias
assert parsed["fields"]["description"] != "NEW"
print("card edit write-through OK")

# ── 5. live: message edit + delete ───────────────────────────────
cid = T.ensure_character()
chat = T.call("POST", "/api/chats/new", {"character_id": cid, "mode": "rp"})["chat_id"]
msgs = T.call("GET", f"/api/chats/{chat}")["messages"]
assert msgs, "new chat should be seeded with a greeting"
mid = msgs[0]["id"]
r = T.call("POST", f"/api/messages/{mid}", {"content": "rewritten by the test"})
assert r.get("ok"), r
again = T.call("GET", f"/api/chats/{chat}")["messages"][0]
assert again["content"] == "rewritten by the test", again
assert "director" in again, "chat detail must expose the director note field"
blank = T.call("POST", f"/api/messages/{mid}", {"content": "   "})
assert blank.get("error"), "empty edit should be refused"
assert delete(f"/api/messages/{mid}").get("ok")
assert not T.call("GET", f"/api/chats/{chat}")["messages"]
print("message edit + delete OK")

# ── 6. live: card edit survives an export round trip ─────────────
T.call("POST", f"/api/characters/{cid}/fields",
       {"fields": {"description": "ROUNDTRIP", "name": "Edited-chan"}})
req = urllib.request.Request(
    f"{BASE}/api/characters/{cid}/export", data=json.dumps({"format": "png"}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req) as resp:
    png = resp.read()
back = cards.parse_card(png)
assert back["fields"]["description"] == "ROUNDTRIP", back["fields"].get("description")
assert back["name"] == "Edited-chan", back["name"]
print("card edit survives export OK")

# ── 7. director channel is opt-in ────────────────────────────────
base = {"chat_id": chat, "backend": "http://127.0.0.1:1234/v1",
        "model": "probe", "text": "hi", "tools": False}
sys_off = T.call("POST", "/api/chats/preview", base)["wire"]["messages"][0]["content"]
sys_on = T.call("POST", "/api/chats/preview",
                {**base, "director_notes": True})["wire"]["messages"][0]["content"]
assert "Director's channel" not in sys_off
assert "Director's channel" in sys_on
print("director channel opt-in OK")

# ── 7b. the director's note survives a multi-character scene ─────
# The multi path re-renders every per-character layer so it names the SPEAKER
# rather than the lead. Re-rendering re-reads the default and substitutes from
# scratch, so a layer taking TWO placeholders needs both supplied again —
# `director` takes {char} and {director}, and passing only char left the
# literal string "{director}" in the prompt. The user's stage direction was
# silently dropped in every multi-character chat with the bar open, which is
# invisible: the note is meant to be invisible to the character anyway, so the
# only symptom is that she ignores it.
NOTE = "she trips over the cat"
dbase = {**base, "director": NOTE}
solo_dir = T.call("POST", "/api/chats/preview", dbase)["wire"]["messages"]
assert NOTE in "\n".join(m["content"] for m in solo_dir), "solo lost the note"

guest = T.call("POST", "/api/cards/import", {
    "filename": "guest.png",
    "b64": base64.b64encode(T.card_png({
        "spec": "chara_card_v3", "spec_version": "3.0",
        "data": {"name": "Macro-chan",
                 "description": "the quiet one who finishes her work",
                 "first_mes": "...you're both still here?"}})).decode()})["id"]
addc = T.call("POST", f"/api/chats/{chat}/cast",
              {"op": "add", "character_id": guest})
assert not addc.get("error"), addc
multi = T.call("POST", "/api/chats/preview", dbase)
blob = "\n".join(m["content"] for m in multi["wire"]["messages"])
assert "{director}" not in blob, "the placeholder survived into the prompt"
assert NOTE in blob, "the stage direction was dropped in a multi scene"
# ...and the scene really was multi, or the assertion above proves nothing:
# the guest's dossier only lands when cast_active is True.
assert "Macro-chan" in blob, "the cast never activated, so this was a solo run"
T.call("POST", f"/api/chats/{chat}/cast", {"op": "remove",
                                           "character_id": guest})
print("director note survives a cast OK")

# ── 8. images force the chat endpoint in completion mode ─────────
# Raw /completions is text-only: render_prompt drops the picture, so an
# attached image used to become "I don't see an image." with no warning.
lib = T.call("POST", "/api/library/install", {})
assert lib.get("ok"), lib
raw_preset = next(p for p in T.call("GET", "/api/presets")["rows"]
                  if p["data"].get("mode") == "completion")
img_chat = T.call("POST", "/api/chats/new", {"character_id": cid})["chat_id"]
img_body = {"chat_id": img_chat, "backend": "http://127.0.0.1:1234/v1",
            "model": "probe", "preset_id": raw_preset["id"], "text": "look",
            "tools": False,
            "images": [{"name": "x.png", "b64": base64.b64encode(T.BLANK_PNG).decode()}]}
with_img = T.call("POST", "/api/chats/preview", img_body)
assert with_img["mode"] == "chat", with_img["mode"]
assert with_img["vision_fallback"] is True, with_img
# ...and a text-only turn on the same preset must stay in completion mode
no_img = T.call("POST", "/api/chats/preview", {**img_body, "images": []})
assert no_img["mode"] == "completion", no_img["mode"]
assert no_img["vision_fallback"] is False, no_img
print("vision falls back to the chat endpoint OK")

# ── 9. card macros resolve against the ACTIVE persona ────────────
# Unsubstituted, a card greeting reaches the model verbatim and it starts
# calling the user "{{user}}" to their face.
assert macros.expand("{{char}} greets {{user}}", "Sophia", "anon") == "Sophia greets anon"
assert macros.expand("<BOT> and <USER>", "Sophia", "anon") == "Sophia and anon"
assert macros.expand("{{CHAR}} {{User}}", "S", "E") == "S E"          # case-insensitive
assert macros.expand("{{// hush}}kept", "S", "E") == "kept"
assert macros.expand("a{{newline}}b", "S", "E") == "a\nb"
# nested: a field macro whose value contains more macros
assert macros.expand("{{description}}", "S", "E",
                     {"description": "{{char}} likes {{user}}"}) == "S likes E"
# self-reference must terminate rather than hang
macros.expand("{{description}}", "S", "E", {"description": "{{description}}"})
# {{pick}} is stable for a seed, so a rebuilt prompt keeps the same choice
assert macros.expand("{{pick:a,b,c}}", seed="s") == macros.expand("{{pick:a,b,c}}", seed="s")
# unknown macros survive — {{prompt}} is a ComfyUI slot, not ours to eat
assert macros.expand("{{prompt}}", "S", "E") == "{{prompt}}"

macro_card = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {
    "name": "Macro-chan",
    "description": "{{char}} calls {{user}} 'test subject'.",
    "first_mes": "Oh, it's you {{user}}."}}
mc_id = T.call("POST", "/api/cards/import", {
    "filename": "macro.png",
    "b64": base64.b64encode(T.card_png(macro_card)).decode()})["id"]
persona_id = T.call("POST", "/api/personas",
                    {"name": "anon", "data": {"description": "a tired sysadmin"}})["id"]
mc_chat = T.call("POST", "/api/chats/new",
                 {"character_id": mc_id, "persona_id": persona_id})["chat_id"]
greeting = T.call("GET", f"/api/chats/{mc_chat}")["messages"][0]["content"]
assert greeting == "Oh, it's you anon.", greeting
mc_prev = T.call("POST", "/api/chats/preview", {
    "chat_id": mc_chat, "backend": "http://127.0.0.1:1234/v1",
    "model": "probe", "text": "hey", "tools": False})
assert "{{" not in json.dumps(mc_prev["wire"]), "macros leaked into the payload"
assert "anon" in mc_prev["wire"]["messages"][0]["content"]
# with no persona the fallback is anon, never a literal macro
bare = T.call("POST", "/api/chats/new", {"character_id": mc_id})["chat_id"]
assert T.call("GET", f"/api/chats/{bare}")["messages"][0]["content"] == "Oh, it's you anon."
print("card macros resolve against the active persona OK")

# ── 10. the schema heals if the database is deleted underneath ───
# Someone wipes data/ to reset things; every route used to 500 with
# "no such table" until the server was restarted.
#
# This test has to delete the LIVE database to prove that, which means it
# deletes the user's characters, chats, memories and gallery index along with
# it. That was tolerable when the only casualties were fixture chats; it is
# not now that a character can carry a forged card, a pinned seed, a voice and
# a gallery of generated media. So: snapshot first, restore after, and hang
# the restore on atexit so a failing assertion further down cannot strand
# somebody's roster in the deleted state.
LIVE_DB = HERE / "data/coomkit.sqlite"
BACKUP_DB = HERE / "data/coomkit.sqlite.testbak"


def _snapshot_db():
    if not LIVE_DB.exists():
        return False
    src = sqlite3.connect(LIVE_DB)
    dst = sqlite3.connect(BACKUP_DB)
    with dst:
        src.backup(dst)          # consistent copy, WAL and all
    src.close()
    dst.close()
    return True


def _restore_db():
    """Restore *through* sqlite, never by swapping the file.

    Copying over a live WAL-mode database while the server holds it open
    leaves every later connection raising `disk I/O error` on
    `PRAGMA journal_mode=WAL` — the server survives, but every request during
    the window 500s. The backup API takes the right locks and writes through
    the existing connection state instead.
    """
    if not BACKUP_DB.exists():
        return
    src = sqlite3.connect(BACKUP_DB)
    dst = sqlite3.connect(LIVE_DB)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()
    BACKUP_DB.unlink()
    print("restored the database that the self-heal test deleted")


had_db = _snapshot_db()
if had_db:
    atexit.register(_restore_db)

for suffix in ("", "-wal", "-shm"):
    p = HERE / f"data/coomkit.sqlite{suffix}"
    if p.exists():
        p.unlink()
rows = T.call("GET", "/api/characters")
assert rows.get("rows") == [], rows
db = sqlite3.connect(HERE / "data/coomkit.sqlite")
tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert {"characters", "chats", "messages", "memories"} <= tables, tables
assert T.ensure_character(reuse=False), "import must work straight after a wipe"
db.close()
print("schema self-heal OK")

# Put the user's data back before the rest of the suite runs against it.
if had_db:
    _restore_db()
    atexit.unregister(_restore_db)
    back = T.call("GET", "/api/characters").get("rows") or []
    print(f"roster restored: {len(back)} character(s) survived the wipe")

# ── 11. a preset_id pointing at nothing must not kill the turn ───
# Exactly the state the wipe above leaves a browser in: it restores a saved
# preset selection whose row is gone. rows_get returns None, and unguarded
# that reached engine.assemble and 500'd the whole send.
ghost_char = T.ensure_character()
ghost_chat = T.call("POST", "/api/chats/new", {"character_id": ghost_char})["chat_id"]
ghost = T.call("POST", "/api/chats/preview", {
    "chat_id": ghost_chat, "backend": "http://127.0.0.1:1234/v1",
    "model": "probe", "text": "hi", "tools": False, "preset_id": 999999})
assert ghost.get("ok"), ghost
assert ghost["mode"] == "chat", ghost["mode"]
print("dangling preset_id falls back to defaults OK")

# Belt and braces: if there was no database to snapshot (a genuinely fresh
# install), the wipe above took the shipped presets and jailbreaks with it and
# a test run should not hand the user an empty library. Install is an upsert by
# name, so this is a no-op when the restore already brought them back.
T.call("POST", "/api/library/install", {})
print("library present")

print("ALL FIX REGRESSIONS PASS")
