#!/usr/bin/env python3
"""Many chats per character: list, rename, start-another, and delete.

Offline and free — no model is called. The point of the feature is that an
old adventure survives everything except an explicit delete, so most of this
is about what is still there afterwards.

Historically a chat's identity lived only in the browser's localStorage, so
"restart chat" deleted a JS key and openChat then created a fresh row — the
previous adventure stayed in sqlite, unreachable, which from the outside is
indistinguishable from having been thrown away.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path

import server
import testkit
from testkit import call

char_id = testkit.ensure_character()
print("character:", char_id)


def chats(mode="rp"):
    return call("GET", f"/api/chats?character_id={char_id}&mode={mode}")["chats"]


base = len(chats())
a = call("POST", "/api/chats/new", {"character_id": char_id, "mode": "rp"})["chat_id"]
b = call("POST", "/api/chats/new", {"character_id": char_id, "mode": "rp"})["chat_id"]
c = call("POST", "/api/chats/new", {"character_id": char_id, "mode": "rp"})["chat_id"]
assert a != b != c

rows = chats()
assert len(rows) == base + 3, f"expected {base + 3} chats, got {len(rows)}"
assert [r["id"] for r in rows[:3]] == [c, b, a], "newest first"
print(f"list: {len(rows)} chats, newest first")

# every chat carries a usable label even when nobody named it
assert all(r["title"] for r in rows), "a chat must always have something to call it"
assert not rows[0]["named"], "an unnamed chat reports itself as unnamed"

# rename, and DO NOT reorder — renaming is not activity
call("POST", f"/api/chats/{a}/title", {"title": "the laundromat one"})
rows = chats()
got = [r for r in rows if r["id"] == a][0]
assert got["title"] == "the laundromat one" and got["named"]
assert rows[0]["id"] == c, "renaming an old chat must not bump it to the top"
print("rename: ok, and did not reorder")

# blanking the title restores the derived label rather than leaving it empty
call("POST", f"/api/chats/{a}/title", {"title": "  "})
got = [r for r in chats() if r["id"] == a][0]
assert got["title"] and not got["named"], "blank title falls back to a derived one"
print("rename: blanking restores the derived label")

# starting another leaves the earlier ones completely intact
detail = call("GET", f"/api/chats/{a}")
assert not detail.get("error"), "an older chat is still openable"
assert detail.get("title"), "the detail route reports the same label"
print("start-another: earlier chats still openable")

# ── delete is the only thing that destroys anything, and it is scoped ──
with server.get_db() as conn:
    conn.execute("INSERT INTO messages (chat_id, role, content, data, created)"
                 " VALUES (?, 'user', 'hello', '{}', 0)", (a,))
    conn.execute("INSERT INTO memories (chat_id, character_id, kind, content,"
                 " created, updated) VALUES (?,?,'chat','scene furniture',0,0)",
                 (a, char_id))
    conn.execute("INSERT INTO memories (chat_id, character_id, kind, content,"
                 " created, updated) VALUES (NULL,?,'character','she likes it',0,0)",
                 (char_id,))
    conn.execute("INSERT INTO memories (chat_id, character_id, kind, content,"
                 " created, updated) VALUES (NULL,NULL,'user','anon is a nerd',0,0)")
    conn.execute("INSERT INTO assets (chat_id, message_id, character_id, kind,"
                 " path, data, created) VALUES (?,NULL,?,'image','t.png','{}',0)",
                 (a, char_id))
    asset_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

out = call("DELETE", f"/api/chats/{a}")
assert out.get("ok") and out["memories"] == 1, out

with server.get_db() as conn:
    n = lambda q, *p: conn.execute(q, p).fetchone()[0]  # noqa: E731
    assert n("SELECT COUNT(*) FROM chats WHERE id=?", a) == 0
    assert n("SELECT COUNT(*) FROM messages WHERE chat_id=?", a) == 0
    assert n("SELECT COUNT(*) FROM memories WHERE chat_id=?", a) == 0
    # user and character memories deliberately outlive any single chat — that
    # scoping is the whole reason a returning chat is not amnesiac
    assert n("SELECT COUNT(*) FROM memories WHERE character_id=? AND kind='character'",
             char_id) >= 1, "character-scope memory must survive a chat delete"
    assert n("SELECT COUNT(*) FROM memories WHERE kind='user'") >= 1, \
        "user-scope memory must survive a chat delete"
    # the gallery is keyed on character_id and must survive: unlink, never delete
    row = conn.execute("SELECT chat_id, character_id FROM assets WHERE id=?",
                       (asset_id,)).fetchone()
    assert row is not None, "a chat delete must not delete asset rows"
    assert row["chat_id"] is None and row["character_id"] == char_id
    conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    conn.execute("DELETE FROM memories WHERE content IN"
                 " ('she likes it','anon is a nerd')")

assert a not in [r["id"] for r in chats()]
assert {b, c} <= set(r["id"] for r in chats()), "the other chats are untouched"
print("delete: messages + chat-scope memory gone; user/character memory and "
      "the gallery survive")

assert call("DELETE", f"/api/chats/{a}").get("error"), "double delete 404s"

for leftover in (b, c):
    call("DELETE", f"/api/chats/{leftover}")

# ── sms threads start blank ──────────────────────────────────────────
sms = call("POST", "/api/chats/new", {"character_id": char_id, "mode": "sms"})["chat_id"]
assert call("GET", f"/api/chats/{sms}")["messages"] == [], \
    "an sms thread must not inherit first_mes — that is prose narration " \
    "delivered as a text message"
opened = call("POST", "/api/chats/new", {"character_id": char_id, "mode": "sms",
                                         "opening": "u up"})["chat_id"]
msgs = call("GET", f"/api/chats/{opened}")["messages"]
assert len(msgs) == 1 and msgs[0]["content"] == "u up", msgs
print("sms: blank by default, seeded only when asked")

# an opening can also be written into a thread that already exists — which is
# the common case, because the phone caches one sms chat per character
blank = call("POST", "/api/chats/new", {"character_id": char_id, "mode": "sms"})["chat_id"]
out = call("POST", f"/api/chats/{blank}/opening", {"text": "hey. you awake?"})
assert out.get("ok") and out["message_id"], out
msgs = call("GET", f"/api/chats/{blank}")["messages"]
assert len(msgs) == 1 and msgs[0]["role"] == "assistant"
# an opening is only an opening while the thread is empty
again = call("POST", f"/api/chats/{blank}/opening", {"text": "hey again"})
assert again.get("error") and "already started" in again["error"], again
assert call("POST", f"/api/chats/{blank}/opening", {"text": "  "}).get("error")
print("sms: an opening can be written once, into a blank thread only")
call("DELETE", f"/api/chats/{blank}")
for leftover in (sms, opened):
    call("DELETE", f"/api/chats/{leftover}")

# ── the roster's own decorations ─────────────────────────────────────
rows = call("GET", "/api/characters")["rows"]
me = [r for r in rows if r["id"] == char_id][0]
assert "fav" in me and "last_seen" in me and "chat_count" in me, sorted(me)
call("POST", f"/api/characters/{char_id}/fav", {"on": True})
me = [r for r in call("GET", "/api/characters")["rows"] if r["id"] == char_id][0]
assert me["fav"], "favourite did not stick"
call("POST", f"/api/characters/{char_id}/fav", {"on": False})
print("roster: fav / last_seen / chat_count present, fav round-trips")
# ── a chat with no character at all ──────────────────────────────────────
# Plain chat: preset, jailbreak, samplers and your persona, talking to the
# model as itself. chats.character_id was ALREADY nullable and
# assemble_blocks already degrades to persona + history when the card fields
# are empty, so this is a shell rather than a second prompt path.
print("\nplain chat (no character)")
r = call("POST", "/api/chats/new", {"mode": "rp"})
assert r.get("chat_id"), r
plain = r["chat_id"]
d = call("GET", f"/api/chats/{plain}")
assert d.get("character") is None, d.get("character")
assert d.get("cast_active") is False, "a cardless chat cannot have a cast"
assert d["messages"] == [], "nothing to greet with, so it opens empty"

pv = call("POST", "/api/chats/preview",
          {"chat_id": plain, "backend": "http://127.0.0.1:1234/v1",
           "model": "probe", "tools": False, "text": "hello"})
blob = "\n".join(m["content"] for m in pv["wire"]["messages"])
assert "hello" in blob, "the user turn must reach the prompt"
assert pv["stats"]["approx_tokens"] > 0
# No card means no card layers, and crucially no literal {{char}} left behind.
assert "{{char}}" not in blob and "{{user}}" not in blob, blob[:200]
print("  ok   a cardless chat assembles and sends")

# A character_id that was SUPPLIED and does not exist is still an error —
# "no character" and "wrong character" are different things.
bad = call("POST", "/api/chats/new", {"mode": "rp", "character_id": 99999999})
assert bad.get("error"), bad
print("  ok   a bad character_id is still refused")

call("DELETE", f"/api/chats/{plain}")
print("  ok   and it deletes like any other chat")

print("ALL CHAT LIFECYCLE TESTS PASS")
