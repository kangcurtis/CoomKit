#!/usr/bin/env python3
"""The chat's inline gallery: it persists, it can be remade, and it never
reaches the model.

Offline and free. The context exclusion is the load-bearing assertion here —
the whole point of keeping generated media in the `assets` table keyed on
message_id, rather than appending it to the chat as a message, is that a
character with a hundred renders behind her still costs the model nothing.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json

import engine
import server
import testkit
import wfpack
from testkit import call

char_id = testkit.ensure_character()
chat_id = call("POST", "/api/chats/new", {"character_id": char_id})["chat_id"]

MARKER = "ZZQX-should-never-reach-the-model"
with server.get_db() as conn:
    msg_id = engine.add_message(conn, chat_id, "assistant", "Here, look at this.")
    conn.execute(
        "INSERT INTO assets (chat_id, message_id, character_id, recipe, kind,"
        " path, data, created) VALUES (?,?,?,?,?,?,?,?)",
        (chat_id, msg_id, char_id, "selfie", "image", f"{MARKER}.png",
         json.dumps({"prompt": f"a photograph of {MARKER}", "seed": 12345,
                     "workflow": "krea2", "values": {"seed": 12345},
                     "job": {"recipe": "selfie", "workflow": "krea2"}}), 0))

# ── 1. it shows up on the message ────────────────────────────────────
detail = call("GET", f"/api/chats/{chat_id}")
mine = [m for m in detail["messages"] if m["id"] == msg_id][0]
assert len(mine["assets"]) == 1, mine["assets"]
asset = mine["assets"][0]
assert asset["seed"] == 12345 and asset["can_remake"], asset
assert asset["prompt"].endswith(MARKER), asset
# the job blob carries on-disk reference filenames and lora names — the
# browser gets the flag, not the blob
assert "job" not in asset and "values" not in asset, asset
print("inline strip: asset reaches the message with its receipt, not its job")

# ── 2. and it reaches the MODEL nowhere at all ───────────────────────
with server.get_db() as conn:
    history = engine.get_messages(conn, chat_id)
    chat_row = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    char = server.rows_get("characters", char_id)

import blocklib  # noqa: E402
messages, _prefill = engine.assemble_blocks(
    chat=dict(chat_row), char=char, persona=None, preset={},
    block_list=blocklib.starter("local"), memories=[], history=history,
    layers={}, context_tokens=8192)
wire = json.dumps(messages)
assert MARKER not in wire, "generated media leaked into the prompt"
assert "assets" not in wire and "/api/avatars/" not in wire, wire[:400]
# every history turn is reduced to {role, content} and nothing else
for m in messages:
    assert set(m) <= {"role", "content", "name"}, m
print("context: the strip is invisible to the model — history is role+content only")

# ── 3. the receipt is enough to run it again ─────────────────────────
with server.get_db() as conn:
    stored = json.loads(conn.execute(
        "SELECT data FROM assets WHERE path=?", (f"{MARKER}.png",)
    ).fetchone()["data"])
assert stored["job"]["workflow"] in wfpack.BUNDLED
assert stored["seed"] == stored["values"]["seed"], \
    "the rolled seed has to be the one that was actually used"
print("receipt: workflow + seed + values round-trip")

# a row written before receipts existed must refuse politely, not explode
with server.get_db() as conn:
    conn.execute(
        "INSERT INTO assets (chat_id, message_id, character_id, recipe, kind,"
        " path, data, created) VALUES (?,?,?,?,?,?,?,?)",
        (chat_id, msg_id, char_id, "selfie", "image", "old.png",
         json.dumps({"prompt": "from before", "workflow": "krea2"}), 0))
    old_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
out = call("POST", "/api/studio/remake", {"asset_id": old_id})
assert out.get("error") and "predates" in out["error"], out
assert call("POST", "/api/studio/remake", {"asset_id": 99999999}).get("error")
print("remake: a pre-receipt asset refuses with a reason; a missing one 404s")

call("DELETE", f"/api/chats/{chat_id}")
with server.get_db() as conn:
    conn.execute("DELETE FROM assets WHERE path IN (?, ?)", (f"{MARKER}.png", "old.png"))

# ── a render with no character_id is invisible forever ───────────────────
# The gallery is keyed on character_id and NEVER on chat_id, so an asset row
# without one cannot appear in any gallery: the file is on disk, the row is in
# the table, and nothing will ever show it. Two insert sites omitted the
# column and 45 of 48 assets on a real dev box were unreachable.
import time as _time
from server import get_db as _db, migrate_asset_owners as _fix

with _db() as _c:
    _cid = _c.execute("SELECT id FROM characters LIMIT 1").fetchone()["id"]
    _chat = _c.execute("SELECT id FROM chats WHERE character_id=? LIMIT 1",
                       (_cid,)).fetchone()
    _chat = _chat["id"] if _chat else None
if _chat:
    _now = _time.time()
    with _db() as _c:
        _live = _c.execute(
            "INSERT INTO assets (chat_id, message_id, kind, path, data, created)"
            " VALUES (?,?,?,?,?,?)",
            (_chat, None, "image", "probe_live.png", "{}", _now)).lastrowid
        _none = _c.execute(
            "INSERT INTO assets (chat_id, message_id, kind, path, data, created)"
            " VALUES (?,?,?,?,?,?)",
            (None, None, "image", "probe_none.png", "{}", _now)).lastrowid
        _dead = _c.execute(
            "INSERT INTO assets (chat_id, message_id, kind, path, data, created)"
            " VALUES (?,?,?,?,?,?)",
            (99999999, None, "image", "probe_dead.png", "{}", _now)).lastrowid
        _c.commit()
    _fix()
    with _db() as _c:
        _get = lambda i: _c.execute(
            "SELECT character_id FROM assets WHERE id=?", (i,)).fetchone()["character_id"]
        assert _get(_live) == _cid, "an asset with a live chat must be attributed"
        assert _get(_none) is None, "an asset with no chat must NOT be guessed at"
        assert _get(_dead) is None, "an asset whose chat is gone must not be guessed at"
        _c.execute("DELETE FROM assets WHERE path LIKE 'probe_%'")
        _c.commit()
    print("orphaned renders: recoverable ones attributed, the rest left alone")


print("ALL INLINE GALLERY TESTS PASS")
