#!/usr/bin/env python3
"""The baton: who speaks next, resolved without a round trip.

Free, and offline apart from the last section. `pick_speaker`, `cast_fairness`
and `trim_cast_leak` are pure functions over data already in memory, so every
case is a literal — no server, no model, no GPU. The stop-sequence half is not
pure (the cap depends on whether the backend is a configured remote), so it
goes through `/api/chats/preview`, which builds the real payload and sends it
nowhere.

The rules are worth stating once, because the test order follows them:

  you            an explicit pick beats everything
  same again     a re-roll keeps the take's own speaker
  asked directly the text names exactly one present character
  still answering the last speaker keeps the floor unless starved out
  her turn       least recently spoken
  lead           first turn of the scene
"""

import _bootstrap  # noqa: F401  — repo root on sys.path

import engine

fails = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def member(cid, name, ord_=0, lead=False, **fields):
    return {"character_id": cid, "present": True, "ord": ord_, "lead": lead,
            "note": "", "char": {"id": cid, "name": name,
                                 "data": {"fields": {"name": name, **fields}}}}


MIKA = member(1, "Mika", ord_=-1, lead=True)
RIN = member(2, "Rin", ord_=0)
YUKI = member(3, "Yuki", ord_=1)
HERE = [MIKA, RIN, YUKI]


def said(cid, text="..."):
    return {"role": "assistant", "content": text, "data": {"speaker": cid}}


def unstamped(text="..."):
    return {"role": "assistant", "content": text, "data": {}}


def asked(text):
    return {"role": "user", "content": text, "data": {}}


def who(*a, **kw):
    m, r = engine.pick_speaker(*a, **kw)
    return ((m or {}).get("character_id"), r)


# ── 1. an explicit pick wins, always ─────────────────────────────────────
print("the human always wins")
check("a forced id beats every other rule",
      who(HERE, [said(2)], "Yuki, come here", forced_id=1) == (1, "you"))
check("a forced id that is not present is ignored",
      who(HERE, [], "", forced_id=99)[1] != "you")
check("an empty scene resolves to nobody rather than raising",
      engine.pick_speaker([], [], "hi") == (None, ""))

# ── 2. the first turn of a scene ─────────────────────────────────────────
print("\nan empty scene")
check("nothing to go on falls to the lead", who(HERE, [], "hello") == (1, "lead"))
check("...and the lead is the one flagged, not merely index 0",
      who([RIN, MIKA], [], "hello") == (1, "lead"))

# ── 3. being addressed by name ───────────────────────────────────────────
print("\nasked directly")
check("one name in the text hands her the turn",
      who(HERE, [said(1)], "Yuki, what do you think?") == (3, "asked directly"))
check("it is case-insensitive", who(HERE, [said(1)], "oi, YUKI") [0] == 3)
check("two names is ambiguous and falls through",
      who(HERE, [said(1)], "Rin and Yuki, stop it")[1] != "asked directly")
check("zero names falls through",
      who(HERE, [said(1)], "so anyway")[1] != "asked directly")
# The measured failure mode of a substring matcher: `Rem` inside `remember`.
REM = [MIKA, member(4, "Rem")]
check("a name inside a longer word is NOT a match",
      who(REM, [said(1)], "remember the thing")[1] != "asked directly")
check("...but the same name standing alone is",
      who(REM, [said(1)], "Rem, remember the thing") == (4, "asked directly"))
check("an apostrophe/possessive still matches",
      who(HERE, [said(1)], "that's Yuki's problem")[0] == 3)

print("\n  names that break a naive matcher")
PAREN = [MIKA, member(5, "Rin (twin)")]
check("a name with regex characters matches and does not raise",
      who(PAREN, [said(1)], "Rin (twin), hello") == (5, "asked directly"))
check("...and the bare stem still finds her",
      who(PAREN, [said(1)], "Rin, hello") == (5, "asked directly"))
CJK = [MIKA, member(6, "王")]
# `\b` never matches between two CJK characters — they are all `\w` — so a
# whole-word pattern would make every such name permanently unmatchable.
check("a CJK name matches inside CJK text (the whole-word carve-out)",
      who(CJK, [said(1)], "国王说话")[0] == 6)
FULL = [MIKA, member(7, "Rin Tohsaka")]
check("a first name finds a two-part name",
      who(FULL, [said(1)], "Rin, hello") == (7, "asked directly"))

print("\n  the persona is not a character")
SHARED = [MIKA, member(8, "anon")]
check("a character sharing the player's handle is skipped",
      who(SHARED, [said(1)], "anon, look at this",
          persona_name="anon")[1] != "asked directly")
check("...and with a different persona she matches normally",
      who(SHARED, [said(1)], "anon, look at this",
          persona_name="eric") == (8, "asked directly"))

# ── 4. holding the floor ─────────────────────────────────────────────────
print("\nstill answering")
check("the last speaker keeps the floor",
      who(HERE, [said(2)], "go on") == (2, "still answering"))
check("an unstamped last turn cannot hold the floor",
      who(HERE, [unstamped()], "go on")[1] != "still answering")
check("a direct address takes the floor off her",
      who(HERE, [said(2)], "Yuki?") == (3, "asked directly"))

# ── 5. fairness ──────────────────────────────────────────────────────────
print("\nthe fairness guard")
hog = [said(2), said(2), said(2)]
check("three in a row while someone has never spoken forces a swap",
      who(HERE, hog, "go on")[1] == "her turn")
check("...and it swaps to someone who has not spoken",
      who(HERE, hog, "go on")[0] in (1, 3))
check("two in a row is not a hog",
      who(HERE, [said(2), said(2)], "go on") == (2, "still answering"))
two = [MIKA, RIN]
check("a two-hander going back and forth is a conversation, not unfairness",
      who(two, [said(1), said(2), said(2), said(2)], "go on")
      == (2, "still answering"),
      "nobody present is starved, so the guard must not fire")
check("cast_fairness needs BOTH halves — a streak alone is not enough",
      engine.cast_fairness(2, two, [said(1)] + hog) is False,
      "Mika spoke inside the window, so nobody is starved")
check("...and fires when the streak really has starved someone",
      engine.cast_fairness(2, HERE, hog) is True)
check("a streak shorter than CAST_STREAK never fires",
      engine.cast_fairness(2, HERE, [said(2), said(2)]) is False)
check("fairness never overrides an explicit pick",
      who(HERE, hog, "go on", forced_id=2) == (2, "you"))
check("fairness never overrides a direct address",
      who(HERE, hog, "Rin, again") == (2, "asked directly"))

# ── 6. taking turns ──────────────────────────────────────────────────────
print("\nher turn")
# Rule 4 is only reached when the floor is taken away, so each case here has
# to starve somebody first — a hog nobody is starved by is a conversation.
check("a long silence outranks a recent one",
      who(HERE, [said(3)] + [said(1)] * 3, "go on") == (2, "her turn"),
      "Rin has never spoken; Yuki spoke, but long ago")
check("...and the winner need not be the lead",
      who(HERE, [said(1), said(1), said(1)], "go on") == (2, "her turn"),
      "Rin is ord 0, Yuki is ord 1, so ord breaks the tie deterministically")
check("nobody starved means the floor simply stays put",
      who(HERE, [said(3), said(1), said(2), said(2), said(2)], "go on")
      == (2, "still answering"),
      "all three are inside the window, so the guard must not fire")

# ── 7. the re-roll ───────────────────────────────────────────────────────
print("\nre-rolling a take")
# The regenerate branch truncates history above the take being replaced, so
# the last assistant turn is the one BEFORE it. Without `holding_id`, a
# re-roll silently reassigns the bubble to whoever spoke previously.
before = [said(1), asked("go on")]
check("without the take's own speaker the floor goes to the wrong woman",
      who(HERE, before, "") == (1, "still answering"))
check("holding the take keeps it hers",
      who(HERE, before, "", holding_id=2) == (2, "same again"))
check("an explicit re-roll-as still beats it",
      who(HERE, before, "", forced_id=3, holding_id=2) == (3, "you"))

print("\n  and the prompting message is still readable on a re-roll")
check("an empty text falls back to the last user turn",
      who(HERE, [said(1), asked("Yuki, thoughts?")], "") == (3, "asked directly"),
      "on a re-roll body['text'] is empty; the naming message is in history")
check("a supplied text wins over the history scan",
      who(HERE, [said(1), asked("Yuki, thoughts?")], "Rin?") == (2, "asked directly"))

# ── 8. reading a take through its swipes ─────────────────────────────────
print("\ntake_speaker")
# add_swipe seeds swipes[0] with content/think/director ONLY, so take 0's
# speaker lives on the message and later takes carry their own.
rerolled = {"role": "assistant", "content": "first",
            "data": {"speaker": 2, "swipe_index": 1,
                     "swipes": [{"content": "first"},
                                {"content": "second", "speaker": 3}]}}
check("an active swipe reports its own speaker",
      engine.take_speaker(rerolled) == 3)
back = {**rerolled, "data": {**rerolled["data"], "swipe_index": 0}}
check("take 0 falls back to the message, because add_swipe did not copy it",
      engine.take_speaker(back) == 2)
check("a user turn has no speaker",
      engine.take_speaker(asked("hi")) is None)
check("an unstamped assistant turn has no speaker",
      engine.take_speaker(unstamped()) is None)

# ── 9. trimming a leaked line ────────────────────────────────────────────
print("\ntrim_cast_leak")
OTHERS = ["Rin", "Yuki"]
kept, name = engine.trim_cast_leak("She shrugs.\n\nRin: don't look at me.",
                                   OTHERS)
check("a line that starts with someone else's name is cut", kept == "She shrugs.")
check("...and the trim names her", name == "Rin")
check("the remainder is not returned, so it cannot be stored by accident",
      "don't look at me" not in kept)
check("a name inside prose is left alone — that is the dossier working",
      engine.trim_cast_leak("Rin nodded, so she went on.", OTHERS)
      == ("Rin nodded, so she went on.", ""))
check("a name mid-line is not a leak either",
      engine.trim_cast_leak("she said: Rin: hello", OTHERS)[1] == "")
check("a clean reply is returned untouched",
      engine.trim_cast_leak("just her, talking.", OTHERS)
      == ("just her, talking.", ""))
check("indentation does not hide a leak",
      engine.trim_cast_leak("hm.\n   Yuki: hi", OTHERS)[1] == "Yuki")
check("the EARLIEST leak wins when two names appear",
      engine.trim_cast_leak("a\nYuki: x\nRin: y", OTHERS) == ("a", "Yuki"))
# A reply that is ENTIRELY somebody else's is a misrouted turn to re-roll,
# not a leak to repair — trimming it would store an empty message.
check("a leak at position 0 is left alone rather than emptied",
      engine.trim_cast_leak("Rin: all of it", OTHERS)
      == ("Rin: all of it", ""))
check("no others means nothing is ever trimmed",
      engine.trim_cast_leak("Rin: hello", [])[1] == "")
check("a name with regex characters does not raise",
      engine.trim_cast_leak("a\nRin (twin): x", ["Rin (twin)"])[1]
      == "Rin (twin)")

# ── 10. the prefix that comes back off ───────────────────────────────────
print("\nstrip_speaker_prefix")
check("a leading name is removed",
      engine.strip_speaker_prefix("Rin: hello there", ["Rin"]) == "hello there")
check("leading whitespace does not hide it",
      engine.strip_speaker_prefix("  Rin:  hello", ["Rin"]) == "hello")
check("a name inside the prose is left alone",
      engine.strip_speaker_prefix("she said Rin: hello", ["Rin"])
      == "she said Rin: hello")
check("a clock is not a name",
      engine.strip_speaker_prefix("12:30 and she still isn't up", ["Rin"])
      == "12:30 and she still isn't up")
check("an unknown name is left alone — only KNOWN names are stripped",
      engine.strip_speaker_prefix("Yuki: hello", ["Rin"]) == "Yuki: hello")
check("only one prefix comes off, not a cascade",
      engine.strip_speaker_prefix("Rin: Rin: hello", ["Rin"]) == "Rin: hello")
check("a name with regex characters does not raise",
      engine.strip_speaker_prefix("Rin (twin): hi", ["Rin (twin)"]) == "hi")
check("empty input is safe", engine.strip_speaker_prefix("", ["Rin"]) == "")

# ── 11. name-prefixed history and its gate ───────────────────────────────
print("\nname-prefixed history")
import blocks  # noqa: E402

CHAR = {"id": 1, "name": "Mika", "data": {"fields": {
    "name": "Mika", "description": "a smug lab assistant",
    "first_mes": "Ugh, you again?"}}}
GUEST = {"id": 2, "name": "Rin", "data": {"fields": {
    "name": "Rin", "description": "the quiet one"}}}
PERSONA = {"id": 1, "name": "anon", "data": {"description": "a tired sysadmin"}}
PRESET = {"id": 1, "name": "t", "data": {"samplers": {"max_tokens": 512}}}
CHAT = {"id": 1, "mode": "rp", "data": {}}
FULL = [{"character_id": 1, "present": True, "lead": True, "note": "",
         "char": CHAR},
        {"character_id": 2, "present": True, "lead": False, "note": "",
         "char": GUEST}]
NAMES = {"cast_present": "[several here]", "cast_turn": "[reply as X]",
         "cast_names": "[the labels are bookkeeping]"}


def build(hist, cast=None, speaker=1, layers=None):
    tr = {}
    msgs, pre = engine.assemble_blocks(
        CHAT, CHAR, PERSONA, PRESET, blocks.default_blocks(), [], list(hist),
        context_tokens=8192, layers=layers if layers is not None else NAMES,
        cast=cast, speaker_id=speaker, trace=tr)
    return msgs, pre, tr


def turns_of(msgs):
    return [m["content"] for m in msgs if m["role"] == "assistant"]


STAMPED = [{"id": 1, "role": "assistant", "content": "hi", "data": {"speaker": 1}},
           {"id": 2, "role": "user", "content": "hello", "data": {}},
           {"id": 3, "role": "assistant", "content": "hm", "data": {"speaker": 2}}]
msgs, pre, tr = build(STAMPED, cast=FULL, speaker=2)
check("each retained reply is headed with who wrote it",
      turns_of(msgs)[:2] == ["Mika: hi", "Rin: hm"], str(turns_of(msgs)))
check("the prefill puts the model inside her line", pre == "Rin: ")
check("...and it is handed back separately so a reply_prefill can compose",
      tr.get("speaker_prefix") == "Rin: ")
check("the transcript layer is emitted with the labels",
      "bookkeeping" in "\n".join(m["content"] for m in msgs))
check("still role+content only",
      all(set(m) <= {"role", "content", "name"} for m in msgs))

# The card's greeting is never stamped. Attributing it to the LEAD is safe by
# construction: `speaker` is written only when the scene is multi, so an
# unstamped reply was written when the chat was one-to-one.
GREETED = [{"id": 1, "role": "assistant", "content": "Ugh, you again?",
            "data": {}}] + STAMPED[1:]
msgs2, _, _ = build(GREETED, cast=FULL, speaker=2)
check("an unstamped greeting is the lead's, not a reason to give up",
      turns_of(msgs2)[0] == "Mika: Ugh, you again?", str(turns_of(msgs2)[:1]))

# A stamp naming somebody who is not in the cast at all is unrecoverable.
GONE = [{"id": 1, "role": "assistant", "content": "hi", "data": {"speaker": 99}},
        {"id": 2, "role": "user", "content": "hello", "data": {}}]
msgs3, pre3, _ = build(GONE, cast=FULL, speaker=2)
check("one unnameable turn switches the whole thing off",
      turns_of(msgs3)[0] == "hi", "a half-labelled log is worse than none")
check("...and the prefill goes with it", pre3 == "")

solo_msgs, solo_pre, _ = build(STAMPED, cast=None, speaker=1, layers={})
check("a solo chat is never prefixed",
      turns_of(solo_msgs)[:2] == ["hi", "hm"])
check("...and gets no name in its prefill", solo_pre == "")

# ── 12. entrance cards ───────────────────────────────────────────────────
print("\nentrances")
BIG = {"cast_present": "[several here]", "cast_turn": "[reply as X]",
       "cast_names": "[bookkeeping]",
       "cast_entered": "[New to this scene: {names}. OTHER PEOPLE.]"}
GUEST2 = {"id": 3, "name": "Yuki", "data": {"fields": {
    "name": "Yuki", "description": "the one who never speaks",
    "personality": "watchful"}}}
THREE = FULL + [{"character_id": 3, "present": True, "lead": False,
                 "note": "", "char": GUEST2}]


def blob_of(msgs):
    return "\n".join(m["content"] for m in msgs)


def build2(hist, cast, speaker, ctx, layers=BIG):
    return engine.assemble_blocks(
        CHAT, CHAR, PERSONA, PRESET, blocks.default_blocks(), [], list(hist),
        context_tokens=ctx, layers=layers, cast=cast, speaker_id=speaker)


NEVER = [{"id": 1, "role": "assistant", "content": "hi", "data": {"speaker": 1}},
         {"id": 2, "role": "user", "content": "hello", "data": {}}]

small, _ = build2(NEVER, FULL, 1, 8192)
check("below the context floor nobody is promoted at all",
      "the quiet one" in blob_of(small)
      and "New to this scene" not in blob_of(small),
      "a 12B handed half a character sheet is the incoherence this prevents")

big, _ = build2(NEVER, FULL, 1, 20000)
bb = blob_of(big)
check("a character the model has never seen speak gets a real card",
      "New to this scene: Rin" in bb)
check("...and loses her dossier, so she is not in the prompt twice",
      "- Rin: the quiet one" not in bb,
      "carrying both is exactly the append-mode merge")
check("the header comes before her card",
      bb.index("New to this scene") < bb.index("[Rin]"))
check("the speaker's card is still LAST",
      bb.index("[Rin]") < bb.index("[Mika]"),
      "prefix caching: only the tail changes when the turn passes")
check("still role+content only",
      all(set(m) <= {"role", "content", "name"} for m in big))

SPOKE = NEVER + [{"id": 3, "role": "assistant", "content": "hm",
                  "data": {"speaker": 2}}]
after, _ = build2(SPOKE, FULL, 1, 20000)
ab = blob_of(after)
check("once she has spoken she reverts to a dossier",
      "- Rin: the quiet one" in ab and "New to this scene" not in ab)

# Three present, two new, CAST_ENTRY_MAX is 2 — the third keeps her dossier
# and the header must name only the two it actually introduced.
three, _ = build2(NEVER, THREE, 1, 40000)
tb = blob_of(three)
check("at most CAST_ENTRY_MAX are promoted in one turn",
      tb.count("New to this scene") == 1)
check("the header names only who it actually introduced",
      "New to this scene: Rin, Yuki" in tb, tb[tb.find("New to"):][:80])

# A card far larger than the allowance is trimmed, not dropped.
FAT = {"id": 4, "name": "Fat", "data": {"fields": {
    "name": "Fat", "description": ("She is very complicated. " * 400)}}}
FATC = [FULL[0], {"character_id": 4, "present": True, "lead": False,
                  "note": "", "char": FAT}]
fat, _ = build2(NEVER, FATC, 1, 13000)
fb = blob_of(fat)
check("an oversize card is TRIMMED rather than silently dropped",
      "[Fat]" in fb and "description trimmed" in fb)
check("...and the trim respects the allowance",
      engine.rough_tokens(fb[fb.index("[Fat]"):]) < 13000 * 0.10,
      "8% of 13000 is 1040 tokens")

check("clip_sentence leaves a short card alone",
      engine.clip_sentence("Short. Enough.", 500) == "Short. Enough.")
check("clip_sentence cuts at a sentence boundary",
      engine.clip_sentence("One. Two. " * 60, 20).endswith("[description trimmed]"))

# ── 13. the stop stack, through the real assembly path ───────────────────
# The rules above are pure; this half is not, so it goes through the server.
# Local-only and free — the preview builds the payload and sends nothing.
print("\nstop sequences on the wire")
import base64  # noqa: E402
import testkit as T  # noqa: E402

LEAD = T.ensure_character()
GUESTS = []
for nm in ("Macro-chan", "Edited-chan", "Gemma-chan"):
    GUESTS.append(T.call("POST", "/api/cards/import", {
        "filename": "guest.png",
        "b64": base64.b64encode(T.card_png({
            "spec": "chara_card_v3", "spec_version": "3.0",
            "data": {"name": nm, "description": "a guest",
                     "first_mes": "...hi"}})).decode()})["id"])

chat = T.call("POST", "/api/chats/new",
              {"character_id": LEAD, "mode": "rp"})["chat_id"]
for g in GUESTS:
    T.call("POST", f"/api/chats/{chat}/cast", {"op": "add", "character_id": g})
# The user's OWN two stops, so the truncation rule has something to protect.
preset = T.call("POST", "/api/presets", {
    "name": "Fixture-stops",
    "data": {"samplers": {"max_tokens": 256, "stop": ["###", "<<END>>"]}}})
pid = preset.get("id") or (preset.get("row") or {}).get("id")
ask = {"chat_id": chat, "model": "probe", "tools": False, "text": "hi",
       "preset_id": pid}

local = T.call("POST", "/api/chats/preview",
               {**ask, "backend": "http://127.0.0.1:1234/v1"})["wire"]
check("a local backend takes every cast stop", len(local.get("stop") or []) == 5,
      str(local.get("stop")))
check("...with the user's own stops first and intact",
      (local.get("stop") or [])[:2] == ["###", "<<END>>"])
check("...and one stop per OTHER character, none for the speaker",
      sorted(local["stop"][2:]) == ["\nEdited-chan:", "\nGemma-chan:",
                                    "\nMacro-chan:"])

remote = T.call("POST", "/api/chats/preview",
                {**ask, "backend": "https://openrouter.ai/api/v1",
                 "model": "z-ai/glm-5.3"})["wire"]
check("a remote backend is capped at four", len(remote.get("stop") or []) == 4,
      str(remote.get("stop")))
check("the user's stops are NEVER the ones dropped",
      (remote.get("stop") or [])[:2] == ["###", "<<END>>"],
      "a power user's sampler losing to an automation is the wrong inversion")
check("the cast stops fill what is left",
      all(s.startswith("\n") for s in (remote.get("stop") or [])[2:]))

solo_chat = T.call("POST", "/api/chats/new",
                   {"character_id": LEAD, "mode": "rp"})["chat_id"]
solo_wire = T.call("POST", "/api/chats/preview",
                   {**ask, "chat_id": solo_chat,
                    "backend": "http://127.0.0.1:1234/v1"})["wire"]
check("a solo chat gets the user's stops and nothing added",
      (solo_wire.get("stop") or []) == ["###", "<<END>>"],
      "every cast branch must stay inside `if multi`")

T.call("DELETE", f"/api/presets/{pid}") if pid else None

print()
if fails:
    print(f"BATON TESTS FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("baton ok")
