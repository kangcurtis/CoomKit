#!/usr/bin/env python3
"""The vision inline reaches the CURRENT user turn, wherever it landed.

Offline and free — everything goes through /api/chats/preview, which sends
nothing anywhere. The bug being pinned: the inline used to be gated on
`messages[-1]["role"] == "user"`, but depth-0 blocks (a card's
post_history_instructions, cast_turn, an ST-imported injection) legally
render AFTER the history — so for exactly the cards people import the last
message was system, the picture was silently dropped with no note, and the
model answered "take a look at this" blind. That reads as hallucination,
not as a dropped upload, which is why it survived so long.

The engine now records where the current user turn landed
(trace["last_user_idx"], anchored on the history marker) and the server
inlines there. Three things are pinned:

  1. a card with post_history_instructions still gets the image;
  2. a depth-0 block that carries role:user does NOT steal it — the history
     marker is the identity a backwards role-scan cannot fake;
  3. a regenerate re-attaches the images stored on the user turn the take
     answers, instead of always answering blind.
"""

import base64

import _bootstrap  # noqa: F401  — repo root on sys.path

import engine
import server
import testkit
from testkit import call

# A card that renders a system message AFTER the user's turn — the shape the
# old gate silently dropped pictures on.
PHI = "[Stay in character. Never break the fourth wall.]"
card = dict(testkit.FIXTURE_CARD)
card["data"] = dict(card["data"], post_history_instructions=PHI)
r = call("POST", "/api/cards/import", {
    "filename": "fixture.png",
    "b64": base64.b64encode(testkit.card_png(card)).decode()})
char_id = r["id"]
chat_id = call("POST", "/api/chats/new",
               {"character_id": char_id, "mode": "rp"})["chat_id"]
print("character:", char_id, "chat:", chat_id)

IMG = {"name": "pin.png", "b64": base64.b64encode(testkit.BLANK_PNG).decode()}
BODY = {"chat_id": chat_id, "backend": "http://127.0.0.1:9/v1", "model": "x",
        "mode": "chat"}


def preview_full(**extra):
    r = call("POST", "/api/chats/preview", {**BODY, **extra})
    assert not r.get("error"), r.get("error")
    return r


def preview(**extra):
    return preview_full(**extra)["wire"]["messages"]


def note_holder(msgs):
    """The one message carrying the attached-image note, or None."""
    hits = [m for m in msgs
            if isinstance(m.get("content"), str)
            and "image(s) attached inline" in m["content"]]
    assert len(hits) <= 1, "the note must appear exactly once"
    return hits[0] if hits else None


# ── 1. post_history_instructions does not eat the picture ────────────────
msgs = preview(text="take a look at this", images=[IMG])
assert msgs[-1]["role"] != "user", \
    "the fixture must render a trailing system message or it pins nothing"
assert PHI in msgs[-1]["content"], "the trailing message is the card's PHI"
held = note_holder(msgs)
assert held is not None, "the image note vanished — the old gate is back"
assert held["role"] == "user" and "take a look at this" in held["content"], \
    "the note must ride the user's own turn"
print("PHI card: image rides the user turn, PHI still last")

# ── 2. a depth-0 role:user injection block does not steal it ─────────────
# ST imports keep block roles verbatim, so a backwards scan for role=='user'
# would hand the picture to injection text. The history marker cannot be
# faked by a block.
preset_id = call("POST", "/api/presets", {
    "name": "vision-pin (fixture)",
    "data": {"blocks": [{
        "id": "tst.inject", "name": "tst.inject", "kind": "text",
        "role": "user", "place": "depth", "depth": 0,
        "content": "OOC: keep it brief.", "enabled": True,
    }]},
})["id"]
try:
    msgs = preview(text="look at this one", images=[IMG],
                   preset_id=preset_id)
    assert msgs[-1]["role"] == "user" \
        and "OOC: keep it brief." in msgs[-1]["content"], \
        "the injection block must be the trailing user-role message"
    held = note_holder(msgs)
    assert held is not None, "the image note vanished under a depth-0 block"
    assert "look at this one" in held["content"], \
        "the note must ride the typed turn, not the injection block"
    assert "image(s) attached" not in msgs[-1]["content"], \
        "the injection block stole the picture"
    print("depth-0 user-role block: image stays on the typed turn")
finally:
    call("DELETE", f"/api/presets/{preset_id}")

# ── 3. a regenerate answers with the picture it was written against ──────
with server.get_db() as conn:
    engine.add_message(conn, chat_id, "user", "what do you see?",
                       {"images": ["pin_regen.png"]})
    engine.add_message(conn, chat_id, "assistant", "I see it clearly.")
msgs = preview(regenerate=True)
held = note_holder(msgs)
assert held is not None, "a re-roll of a reply to a picture answered blind"
assert "pin_regen.png" in held["content"], \
    "the re-attached image must be the one stored on the user turn"
assert held["role"] == "user" and "what do you see?" in held["content"]
print("regenerate: stored images re-attached to the turn they belong to")

# ── 4. the per-backend vision flag, and the key-preserving merge ─────────
# A configured remote never receives pictures — unless the user flagged
# THAT backend `vision: true` in settings, the one deliberate exception to
# local-only. This section edits the live remote_backends list, so the
# original list is restored no matter what; restoring the MASKED list from
# GET /api/config is loss-free precisely because of the merge being pinned
# here, which keeps a stored key when the incoming one is empty or the mask.
import json as _json  # noqa: E402

FAKE = "http://vision-pin.invalid/v1"
saved = call("GET", "/api/config").get("remote_backends") or []
try:
    # unflagged remote: the picture is withheld and she is told in-band
    call("POST", "/api/config", {"remote_backends": saved + [
        {"label": "vision-pin", "url": FAKE,
         "key": "sk-pin-full-secret", "vision": False}]})
    r = preview_full(text="see this?", images=[IMG], backend=FAKE)
    assert r["is_remote"] and not r["vision_ok"], \
        "an unflagged remote must report vision_ok: false"
    msgs = r["wire"]["messages"]
    assert note_holder(msgs) is None, \
        "an unflagged remote must never get the attached-image note"
    told = [m for m in msgs if isinstance(m.get("content"), str)
            and "was not sent to this remote model" in m["content"]]
    assert len(told) == 1 and told[0]["role"] == "user", \
        "she must be told in-band, on the user turn"
    print("unflagged remote: picture withheld, said in-band")

    # flip the flag THROUGH the masked list, exactly like the settings
    # toggle does — this is the round-trip that used to clobber keys
    masked = call("GET", "/api/config")["remote_backends"]
    for rb in masked:
        if rb.get("url") == FAKE:
            assert rb.get("key", "").endswith("..."), \
                "the API must mask keys — if this fails, keys leak to the UI"
            rb["vision"] = True
    call("POST", "/api/config", {"remote_backends": masked})

    r = preview_full(text="see this?", images=[IMG], backend=FAKE)
    assert r["is_remote"] and r["vision_ok"], \
        "a flagged remote must report vision_ok: true"
    held = note_holder(r["wire"]["messages"])
    assert held is not None and "see this?" in held["content"], \
        "a flagged remote gets the image like a local backend"
    print("flagged remote: picture sent, still reported as remote")

    disk = _json.loads(
        (_bootstrap.ROOT / "data" / "config.json").read_text())
    mine = [rb for rb in disk.get("remote_backends", [])
            if rb.get("url") == FAKE][0]
    assert mine["key"] == "sk-pin-full-secret", \
        "the masked round-trip clobbered the stored key"
    assert mine["vision"] is True
    print("config merge: the stored key survived the masked round-trip")
finally:
    call("POST", "/api/config", {"remote_backends": saved})

print("\ntest_vision: all sections passed")
