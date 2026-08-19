#!/usr/bin/env python3
"""Multi-character scenes: the cast, and the solo prompt that must not move.

Offline and free. No model, no GPU, no HTTP for the assembly half — it calls
engine.assemble_blocks directly so the assertions are about the prompt, not
about a route.

THE FIRST TEST IS THE IMPORTANT ONE. Every branch added for a cast is a chance
to silently change the prompt of every existing single-character chat, and
nobody would ever notice except as "replies got worse". So a solo assembly is
diffed against a baseline recorded on disk, byte for byte. If you meant to
change it, delete tests/cast_baseline.json and re-run to re-record — and then
justify the diff in the commit message.
"""

import json

import _bootstrap  # noqa: F401  — repo root on sys.path
from _bootstrap import ROOT

import blocks
import engine

fails = []
BASELINE = ROOT / "tests" / "cast_baseline.json"


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# ── a fixed scene, built in memory ───────────────────────────────────────
CHAR = {
    "id": 1, "name": "Fixture-chan",
    "data": {"fields": {
        "name": "Fixture-chan",
        "description": "a smug lab assistant with a mesugaki streak",
        "personality": "bratty, brilliant, allergic to sincerity",
        "scenario": "her cluttered lab, late at night",
        "first_mes": "Ugh, you again? Fine, sit down.",
        "mes_example": "",
    }},
}
GUEST = {
    "id": 2, "name": "Rin",
    "data": {"fields": {
        "name": "Rin",
        "description": "the quiet one who actually finishes her work",
        "personality": "dry, patient, unimpressed",
        "scenario": "the same lab, unfortunately",
        "first_mes": "...you're both still here?",
    }},
}
PERSONA = {"id": 1, "name": "anon", "data": {"description": "a tired sysadmin"}}
PRESET = {"id": 1, "name": "t", "data": {"samplers": {"max_tokens": 512}}}
CHAT = {"id": 1, "mode": "rp", "data": {}}
HISTORY = [
    {"id": 1, "role": "assistant", "content": "Ugh, you again? Fine, sit down.", "data": {}},
    {"id": 2, "role": "user", "content": "hello {{char}}", "data": {}},
]


def assemble(**kw):
    return engine.assemble_blocks(
        CHAT, CHAR, PERSONA, PRESET, blocks.default_blocks(),
        [], list(HISTORY), context_tokens=8192, **kw)


# ── 1. the solo prompt does not move ─────────────────────────────────────
print("the prompt of an existing chat")
messages, prefill = assemble()
shot = {"messages": messages, "prefill": prefill}

if not BASELINE.exists():
    BASELINE.write_text(json.dumps(shot, indent=1, sort_keys=True) + "\n")
    print(f"  ..    recorded a new baseline at {BASELINE.name}")
else:
    want = json.loads(BASELINE.read_text())
    same = want == shot
    check("a solo assembly is byte-identical to the recorded baseline", same,
          "delete tests/cast_baseline.json to re-record, then justify the diff")
    if not same:
        for i, (a, b) in enumerate(zip(want["messages"], shot["messages"])):
            if a != b:
                print(f"        first difference at message {i}:")
                print(f"          was: {json.dumps(a)[:220]}")
                print(f"          now: {json.dumps(b)[:220]}")
                break

check("every assembled turn still reduces to role+content",
      all(set(m) <= {"role", "content", "name"} for m in messages),
      "the gallery invariant: provenance and speakers ride side channels")

# ── 2. card_text keeps its solo shape, and heads itself when asked ───────
print("\ncard_text")
solo = engine.card_text(CHAR["data"]["fields"])
named = engine.card_text(CHAR["data"]["fields"], name="Fixture-chan")
check("no name header by default, so the solo prompt is untouched",
      "[Fixture-chan]" not in solo and solo.strip().startswith("a smug lab"))
check("a name header when several people are in the prompt",
      named.startswith("[Fixture-chan]"))
check("heading it changes nothing else",
      named[len("[Fixture-chan]"):].strip() == solo.strip())

# ── 3. the dossier ───────────────────────────────────────────────────────
print("\ndossier lines")
line = engine.dossier_line("Rin", GUEST["data"]["fields"])
check("names her and describes her", line.startswith("- Rin:")
      and "quiet one" in line, line)
check("stays short enough to be worth it",
      engine.rough_tokens(line) <= 40, f"{engine.rough_tokens(line)} tokens")
noted = engine.dossier_line("Rin", GUEST["data"]["fields"],
                            note="on the couch, refusing to look at you")
check("carries the staging note the user typed",
      "refusing to look at you" in noted, noted)
long_fields = {"description": "x" * 900}
check("a chub-sized description is capped, not injected whole",
      len(engine.dossier_line("X", long_fields)) < 200)

# ── 4. the blocks.render splice ──────────────────────────────────────────
# A list slot used to let the spliced dict's role win unconditionally, so a
# user who moved their card block to role:user was silently overridden. The
# dict still wins when it HAS a role, because history turns must keep theirs.
print("\nlist slots respect the block's role")
blist = [dict(b) for b in blocks.default_blocks()]
card_block = next(b for b in blist if b.get("marker") == "card")
card_block["role"] = "user"
msgs, _ = blocks.render(
    blist, {"card": [{"content": "who is here"}, {"content": "[Rin] a card"}],
            "history": [{"role": "assistant", "content": "hi"}]}, "", False)
card_msgs = [m for m in msgs if m.get("content", "").startswith(("who is here", "[Rin]"))]
check("a roleless slot item takes the block's role",
      card_msgs and all(m["role"] == "user" for m in card_msgs),
      str(card_msgs)[:160])
check("a history turn keeps its own role",
      any(m["role"] == "assistant" and m["content"] == "hi" for m in msgs))
check("each slot item can carry its own provenance",
      all(m.get("src") for m in card_msgs))

# ── 5. a cast actually changes the prompt, and only then ─────────────────
print("\nassembly with a cast")
CAST = [
    {"character_id": 1, "present": True, "lead": True, "note": "", "char": CHAR},
    {"character_id": 2, "present": True, "lead": False,
     "note": "at the far bench", "char": GUEST},
]
LAYERS = {"cast_present": "[several people are here: X and Y]",
          "cast_turn": "[reply as X]"}

solo_again, _ = assemble()
check("passing a cast of one changes nothing",
      assemble(cast=[CAST[0]], speaker_id=1)[0] == solo_again)

multi, _ = assemble(cast=CAST, speaker_id=1, layers=LAYERS)
blob = "\n".join(m["content"] for m in multi)
check("the cast header is in the prompt", "several people are here" in blob)
check("the non-speaker gets a dossier, not a card",
      "- Rin: the quiet one" in blob and "unimpressed" not in blob,
      "personality belongs to the speaker only")
check("the speaker's card is headed with her name", "[Fixture-chan]" in blob)
check("the speaker's card comes LAST, after the dossiers",
      blob.index("- Rin:") < blob.index("[Fixture-chan]"),
      "prefix caching: only the tail changes when the turn passes")
check("still role+content only",
      all(set(m) <= {"role", "content", "name"} for m in multi))

# hand the turn over: the cards swap, the dossiers do not
other, _ = assemble(cast=CAST, speaker_id=2, layers=LAYERS)
oblob = "\n".join(m["content"] for m in other)
check("the other speaker gets her own card headed",
      "[Rin]" in oblob and "the quiet one who actually finishes" in oblob)
check("and the first one drops to a dossier",
      "- Fixture-chan: a smug lab assistant" in oblob
      and "allergic to sincerity" not in oblob)

# {{char}} must follow the speaker, or her own greeting names someone else
check("{{char}} resolves to the speaker, not the lead",
      "hello Rin" in oblob and "hello Fixture-chan" in blob)

# Off-stage is genuinely gone. The server only fills cast_present/cast_turn
# when more than one person is present, so the layers here are the ones
# _prepare_request would actually set for this state — cast_absent and
# nothing else.
away = [CAST[0], {**CAST[1], "present": False}]
gone, _ = assemble(cast=away, speaker_id=1, layers={})
gblob = "\n".join(m["content"] for m in gone)
check("an off-stage character contributes nothing to the prompt",
      "Rin" not in gblob, "her card and dossier must both be gone")
check("and the scene assembles as a plain solo chat again",
      gone == solo_again, "byte-identical to no cast at all")

warned, _ = assemble(cast=away, speaker_id=1,
                     layers={"cast_absent": "[Rin has left]"})
check("but the absent warning still lands when the server sets it",
      "[Rin has left]" in "\n".join(m["content"] for m in warned),
      "her lines are still in the history; this is what stops the model "
      "writing her anyway")

print()
if fails:
    print(f"FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("cast ok")
