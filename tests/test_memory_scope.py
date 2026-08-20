#!/usr/bin/env python3
"""Memory scoping: who a fact belongs to, and who gets to read it.

Two halves. The first is pure — persona_known / sanitize_facts /
rescope_user_facts / _mentions over literals and an in-memory sqlite, no
server. The second goes through /api/chats/preview on the running server
(local-only and free: the preview builds the real payload and sends it
nowhere) to pin the turn-level behaviour:

  * in a cast scene the SPEAKER's character memories are injected, never the
    lead's — handing the writer another woman's relationship history is the
    leak the scopes exist to prevent;
  * cast_absent decays: a dismissed guest stops haunting the prompt once her
    stamped lines have scrolled out of the recent window;
  * a brand-new chat with the same lead carries no cast layer at all;
  * the director layer fires exactly when the request carries direction.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path

import base64
import sqlite3

import memory

fails = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# ── 1. _mentions: whole-word for ASCII, substring for CJK ────────────────
print("who a sentence names")
check("Rem does not fire on 'remember'",
      not memory._mentions("remember the message", "Rem"))
check("Rem fires on 'Rem is her age'",
      memory._mentions("Rem is her age", "Rem"))
check("case-insensitive", memory._mentions("mika waved", "Mika"))
check("a CJK name matches as a substring, unpunctuated",
      memory._mentions("她和小美出去了", "小美"))
check("empty name never matches", not memory._mentions("anything", ""))

# ── 2. persona_known: what the model was already told ────────────────────
print("\nthe persona is configuration, not a discovery")
known = memory.persona_known("Eric", "A tall engineer. Drinks too much coffee.")
check("the name becomes a known fact",
      any("Eric" in k for k in known), str(known))
check("each persona sentence becomes a known fact", len(known) == 3, str(known))
check("'anon' is not a name worth guarding", memory.persona_known("anon") == [])
check("nor is an empty persona", memory.persona_known("", "") == [])

# ── 3. sanitize_facts: drop restatements, demote leaks ───────────────────
print("\nsanitising what the extractor returns")
facts = [
    {"scope": "user", "content": "The user's name is Eric."},
    {"scope": "user", "content": "The user is called Eric."},
    {"scope": "user", "content": "The user loves being teased by Mika."},
    {"scope": "user", "content": "The user has a cat named Widget."},
    {"scope": "character", "content": "Mika bit his ear."},
]
out = memory.sanitize_facts(facts, "Eric", "", "Mika")
contents = [f["content"] for f in out]
check("a user fact restating the persona name is dropped",
      "The user's name is Eric." not in contents
      and "The user is called Eric." not in contents, str(contents))
check("a user fact naming the character is demoted to character scope",
      next((f["scope"] for f in out
            if f["content"].startswith("The user loves")), "") == "character")
check("a genuinely durable user fact survives untouched",
      next((f["scope"] for f in out
            if "Widget" in f["content"]), "") == "user")
check("character-scope facts pass through", "Mika bit his ear." in contents)
check("no persona means nothing to drop",
      len(memory.sanitize_facts(
          [{"scope": "user", "content": "The user's name is Eric."}],
          "", "", "")) == 1)
check("a persona-desc restatement is dropped too",
      memory.sanitize_facts(
          [{"scope": "user", "content": "The user is a tall engineer."}],
          "Eric", "The user is a tall engineer who hates mornings.", "") == [])

# ── 4. rescope_user_facts: repairing rows written before the guard ───────
print("\nrepairing old user-scope leaks")
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, chat_id, "
             "character_id, kind TEXT, content TEXT, created, updated)")
rows = [
    ("user", "The user kissed Mika in the lab."),          # -> Mika's
    ("user", "Mika and Rin both tease the user."),         # ambiguous: stays
    ("user", "The user is left-handed."),                  # nobody's: stays
    ("user", "Remembering things is hard for the user."),  # 'Rem' must NOT hit
]
for kind, content in rows:
    conn.execute("INSERT INTO memories (kind, content) VALUES (?,?)",
                 (kind, content))
chars = [(1, "Mika"), (2, "Rin"), (3, "Rem")]
moved = memory.rescope_user_facts(conn, chars)
check("exactly one row is rescoped", moved == 1, f"moved={moved}")
got = conn.execute("SELECT kind, character_id FROM memories"
                   " WHERE content LIKE 'The user kissed%'").fetchone()
check("...to character scope, hers", (got["kind"], got["character_id"])
      == ("character", 1))
check("two names is ambiguity and ambiguity is left alone",
      conn.execute("SELECT kind FROM memories WHERE content LIKE"
                   " 'Mika and Rin%'").fetchone()["kind"] == "user")
check("a fact naming nobody stays user-scope",
      conn.execute("SELECT kind FROM memories WHERE content LIKE"
                   " '%left-handed%'").fetchone()["kind"] == "user")
check("whole-word matching holds during repair too",
      conn.execute("SELECT kind FROM memories WHERE content LIKE"
                   " 'Remembering%'").fetchone()["kind"] == "user")

# ── 4b. attribute_facts: ♥ remember files facts with who they name ──────
print("\nfiling a scene's facts with the right woman")
buckets, rest = memory.attribute_facts([
    {"scope": "character", "content": "Mika promised to visit."},
    {"scope": "character", "content": "Mika and Rin argued."},
    {"scope": "character", "content": "She seemed tired."},
    {"scope": "user", "content": "The user hates mornings."},
], [(1, "Mika"), (2, "Rin")])
check("a fact naming one woman goes to her bucket",
      [f["content"] for f in buckets.get(1, [])] == ["Mika promised to visit."])
check("two names is ambiguity: to the leftovers",
      any("argued" in f["content"] for f in rest))
check("no name at all: leftovers", any("tired" in f["content"] for f in rest))
check("user-scope facts are never bucketed",
      any(f["scope"] == "user" for f in rest) and 2 not in buckets)

# ── 5. through the real assembly path ────────────────────────────────────
# Local-only and free — the preview builds the payload and sends nothing.
print("\nthe turn only carries the speaker's memories")
import engine  # noqa: E402
import server  # noqa: E402
import testkit as T  # noqa: E402

LEAD = T.ensure_character()
GUEST = T.call("POST", "/api/cards/import", {
    "filename": "guest.png",
    "b64": base64.b64encode(T.card_png({
        "spec": "chara_card_v3", "spec_version": "3.0",
        "data": {"name": "Macro-chan", "description": "a guest",
                 "first_mes": "...hi"}})).decode()})["id"]

chat = T.call("POST", "/api/chats/new",
              {"character_id": LEAD, "mode": "rp"})["chat_id"]
T.call("POST", f"/api/chats/{chat}/cast", {"op": "add", "character_id": GUEST})

with server.get_db() as conn:
    for cid, marker in ((LEAD, "LEAD-MEM-MARKER"), (GUEST, "GUEST-MEM-MARKER")):
        memory.upsert(conn, None, "character",
                      f"She remembers the {marker}.", None, cid)
    memory.upsert(conn, None, "chat", "The CHAT-MEM-MARKER is on the table.",
                  chat, LEAD)

ask = {"chat_id": chat, "model": "probe", "tools": False, "text": "hi",
       "backend": "http://127.0.0.1:1234/v1"}

as_guest = T.call("POST", "/api/chats/preview",
                  {**ask, "speaker_id": GUEST})["rendered"]
check("the guest's own memory is injected on her turn",
      "GUEST-MEM-MARKER" in as_guest)
check("the LEAD's relationship memory is NOT in the guest's turn",
      "LEAD-MEM-MARKER" not in as_guest,
      "memory.for_turn was keyed on the lead, not the speaker")
check("chat-scope memories are shared by everyone in the scene",
      "CHAT-MEM-MARKER" in as_guest)

as_lead = T.call("POST", "/api/chats/preview",
                 {**ask, "speaker_id": LEAD})["rendered"]
check("the lead's turn carries hers", "LEAD-MEM-MARKER" in as_lead)
check("...and not the guest's", "GUEST-MEM-MARKER" not in as_lead)

panel = T.call("GET", f"/api/chats/{chat}/memories")
panel_text = " ".join(m["content"] for m in panel.get("memories", []))
check("the panel shows the whole scene's memory, guests included",
      "GUEST-MEM-MARKER" in panel_text and "LEAD-MEM-MARKER" in panel_text,
      "what the panel cannot show, the user cannot edit")

# Manual write can target a guest, and an edit keeps her attribution.
w = T.call("POST", "/api/memories",
           {"scope": "character", "content": "She owns a MANUAL-MARKER pin.",
            "chat_id": chat, "character_id": GUEST})
with server.get_db() as conn:
    got = conn.execute("SELECT character_id FROM memories WHERE id=?",
                       (w["id"],)).fetchone()
check("a manual memory can be filed under a guest",
      got and got["character_id"] == GUEST)
T.call("POST", "/api/memories",
       {"id": w["id"], "scope": "character",
        "content": "She owns a MANUAL-MARKER pin, silver.", "chat_id": chat})
with server.get_db() as conn:
    got = conn.execute("SELECT character_id FROM memories WHERE id=?",
                       (w["id"],)).fetchone()
check("an edit without character_id keeps her attribution",
      got and got["character_id"] == GUEST,
      "recomputing from the chat's lead refiled guests' rows on every edit")

# ── 6. cast_absent decays with the history ───────────────────────────────
print("\na dismissed guest stops haunting the prompt")
with server.get_db() as conn:
    engine.add_message(conn, chat, "user", "say something, both of you")
    engine.add_message(conn, chat, "assistant", "Macro-chan speaks.",
                       {"speaker": GUEST})
T.call("POST", f"/api/chats/{chat}/cast",
       {"op": "present", "character_id": GUEST, "present": False})

fresh = T.call("POST", "/api/chats/preview", ask)["rendered"]
check("just-dismissed, her lines are recent: the warning fires",
      "No longer in the scene" in fresh)

with server.get_db() as conn:
    for i in range(engine.CAST_ABSENT_WINDOW + 2):
        engine.add_message(conn, chat, "user", f"filler {i}")
        engine.add_message(conn, chat, "assistant", f"reply {i}")

later = T.call("POST", "/api/chats/preview", ask)["rendered"]
check("her lines out of the window: the warning is gone",
      "No longer in the scene" not in later,
      "cast_absent must decay, not haunt the chat forever")

# ── 6b. a removed cast member keeps her name on her old messages ─────────
print("\nremoval leaves a tombstone, not a misattribution")
T.call("POST", f"/api/chats/{chat}/cast",
       {"op": "remove", "character_id": GUEST})
detail = T.call("GET", f"/api/chats/{chat}")
stone = next((c for c in detail.get("cast", [])
              if c["character_id"] == GUEST), None)
check("her stamped lines earn a tombstone entry in the cast payload",
      bool(stone) and stone.get("tombstone") is True,
      "without it her old messages re-attribute to the LEAD's name and face")
check("the tombstone carries her real name",
      (stone or {}).get("name") == "Macro-chan")
check("...and is not presented as present", not (stone or {}).get("present"))

# ── 7. a new chat starts clean ───────────────────────────────────────────
print("\nnothing follows you into a new chat")
solo = T.call("POST", "/api/chats/new",
              {"character_id": LEAD, "mode": "rp"})["chat_id"]
clean = T.call("POST", "/api/chats/preview", {**ask, "chat_id": solo})["rendered"]
check("no cast layer in a fresh chat",
      "No longer in the scene" not in clean
      and "more than one character" not in clean)
check("no director layer without direction in the request",
      "Director's note" not in clean)
directed = T.call("POST", "/api/chats/preview",
                  {**ask, "chat_id": solo, "director": "she gets bolder"})["rendered"]
check("direction in the request is the one thing that fires it",
      "Director's note" in directed and "she gets bolder" in directed)

print()
if fails:
    print(f"MEMORY SCOPE TESTS FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("memory scope ok")
