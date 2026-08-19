#!/usr/bin/env python3
"""Lorebooks: the compatibility oracle first, then everything else.

Offline and free. THE FIRST TEST IS THE IMPORTANT ONE, for the same reason
test_cast.py's is: an embedded `character_book` fires on every turn of every
chat that has one, and changing which entries fire would silently change those
prompts with no symptom except "replies got worse".

So today's `engine._lorebook_entries` is kept here VERBATIM as an oracle and
`lore.from_card` + `lore.select` are diffed against it over a battery of entry
shapes. It is not asserted, it is proved. If this is red the compatibility
claim is false and nothing else in the file matters.
"""

import json

import _bootstrap  # noqa: F401  — repo root on sys.path
from _bootstrap import ROOT

import lore

fails = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# ── the oracle: engine._lorebook_entries as it stood before lore.py ──────
def oracle(fields, history_text, budget=1200):
    """VERBATIM copy. Do not tidy it, do not fix it — it is the spec."""
    def rough_tokens(text):
        return max(1, len(text) // 4)
    book = fields.get("character_book") or {}
    entries = book.get("entries") or []
    hits = []
    used = 0
    low = history_text.lower()
    for e in entries:
        if not e.get("content"):
            continue
        if not e.get("enabled", True):
            continue
        keys = [k.lower() for k in (e.get("keys") or e.get("key") or [])]
        if keys and not any(k and k in low for k in keys):
            continue
        cost = rough_tokens(e["content"])
        if used + cost > budget:
            continue
        hits.append(e["content"])
        used += cost
    return "\n\n".join(hits)


def via_lore(fields, history, budget=1200):
    book = lore.from_card(fields)
    got = lore.select([book], history, {"budget": budget})
    return "\n\n".join(x["content"] for x in got)


def card(*entries):
    return {"character_book": {"name": "b", "entries": list(entries)}}


HIST = ["the gate was sealed", "she mentioned CONCATENATION once",
        "nothing about dragons"]
HTEXT = "\n".join(HIST[-20:])

print("the oracle — today's behaviour, reproduced exactly")
CASES = [
    ("a keyed entry that matches",
     card({"content": "the Sunken Gate is sealed", "keys": ["gate"]})),
    ("a keyed entry that does not",
     card({"content": "dragons are extinct", "keys": ["wyvern"]})),
    ("a KEYLESS entry always fires",
     card({"content": "the world is flat", "keys": []})),
    ("...and so does one with no keys field at all",
     card({"content": "no keys here"})),
    ("constant is IGNORED — an absent key does not fire it",
     card({"content": "constant but unkeyed", "keys": ["absent"],
           "constant": True})),
    ("enabled: false is honoured",
     card({"content": "switched off", "keys": ["gate"], "enabled": False})),
    ("disable: true is IGNORED — only `enabled` is read",
     card({"content": "st-style disable", "keys": ["gate"], "disable": True})),
    ("keys are matched case-insensitively",
     card({"content": "shouty", "keys": ["GATE"]})),
    ("...in both directions",
     card({"content": "shouty text", "keys": ["concatenation"]})),
    ("a key matches INSIDE a word (substring, not whole-word)",
     card({"content": "cat found", "keys": ["cat"]})),
    ("`key` is accepted as well as `keys`",
     card({"content": "singular", "key": ["gate"]})),
    ("empty content is skipped",
     card({"content": "", "keys": ["gate"]},
          {"content": "kept", "keys": ["gate"]})),
    ("an empty-string key does not match everything",
     card({"content": "blank key", "keys": [""]})),
    ("source order is preserved, NOT `order`",
     card({"content": "first", "keys": ["gate"], "order": 1},
          {"content": "second", "keys": ["gate"], "order": 999})),
    ("an oversized entry is SKIPPED and a later small one still lands",
     card({"content": "x" * 8000, "keys": ["gate"]},
          {"content": "small and late", "keys": ["gate"]})),
    ("...and the skip is a continue, so several small ones land",
     card({"content": "y" * 8000, "keys": ["gate"]},
          {"content": "a" * 100, "keys": ["gate"]},
          {"content": "b" * 100, "keys": ["gate"]})),
    ("several entries join with a blank line",
     card({"content": "one", "keys": ["gate"]},
          {"content": "two", "keys": ["gate"]})),
    ("no book at all", {}),
    ("an empty book", {"character_book": {"entries": []}}),
]
for label, fields in CASES:
    want = oracle(fields, HTEXT)
    got = via_lore(fields, HIST)
    check(label, want == got, f"oracle={want[:70]!r}  lore={got[:70]!r}")

# The ONE deliberate divergence, and it only goes one way: today's matcher
# calls .get() on every entry, so a book containing a non-dict entry takes the
# whole assembly down with an AttributeError. lore skips it. Asserting they
# "agree" is impossible because one of them crashes, so assert the difference
# explicitly rather than leaving it undocumented.
print("\n  the one deliberate divergence")
JUNK = {"character_book": {"entries": ["junk", {"content": "ok"}]}}
try:
    oracle(JUNK, HTEXT)
    check("today's matcher raises on a malformed entry", False,
          "it did not — this divergence note is now stale")
except AttributeError:
    check("today's matcher RAISES on a malformed entry, taking the turn down",
          True)
check("...and lore skips it and carries on", via_lore(JUNK, HIST) == "ok")

# The budget boundary is where an off-by-one hides.
print("\n  the budget boundary")
for budget in (1, 4, 25, 100, 1200, 100000):
    fields = card({"content": "q" * 400, "keys": ["gate"]},
                  {"content": "r" * 40, "keys": ["gate"]},
                  {"content": "s" * 4000, "keys": ["gate"]},
                  {"content": "t" * 12, "keys": ["gate"]})
    want, got = oracle(fields, HTEXT, budget), via_lore(fields, HIST, budget)
    check(f"budget {budget} agrees", want == got,
          f"oracle {len(want)} chars, lore {len(got)}")

# ── macros are expanded per entry, late ──────────────────────────────────
print("\nmacros stay late")
fields = card({"content": "{{char}} guards the gate for {{user}}",
               "keys": ["gate"]})
out = lore.select([lore.from_card(fields)], HIST, {"budget": 1200},
                  expand=lambda t: t.replace("{{char}}", "Mika")
                                    .replace("{{user}}", "anon"))
check("an entry's macros are expanded through the caller's expander",
      out and out[0]["content"] == "Mika guards the gate for anon",
      str(out))
check("...and nothing is baked in at import",
      lore.from_card(fields)["entries"][0]["content"].startswith("{{char}}"))

# ── the slot shape ───────────────────────────────────────────────────────
print("\nthe slot")
out = lore.select([lore.from_card(card({"content": "A", "keys": ["gate"]},
                                       {"content": "B", "keys": ["gate"]}))],
                  HIST, {"budget": 1200})
check("every item is content plus a src side channel",
      all(set(x) == {"content", "src"} for x in out), str(out))
check("each src names the entry for the inspector",
      all(x["src"]["name"] and x["src"]["marker"] == "lore" for x in out))
check("...and carries its token cost",
      all(isinstance(x["src"]["tokens"], int) for x in out))

# ── the slot renders the same as the string it replaced ──────────────────
# This is the claim that makes swapping engine's lore slot from a joined
# string to a list safe, so it is measured rather than believed.
print("\nlist slot vs joined string")
import blocks  # noqa: E402
import engine  # noqa: E402

BOOK = {"name": "Fixture-chan", "description": "a smug lab assistant",
        "first_mes": "Ugh, you again?",
        "character_book": {"name": "Ashgrove", "entries": [
            {"content": "The Sunken Gate is sealed.", "keys": ["gate"]},
            {"content": "Dragons are extinct.", "keys": ["dragons"]}]}}
CHAR = {"id": 1, "name": "Fixture-chan", "data": {"fields": BOOK}}
PERSONA = {"id": 1, "name": "anon", "data": {"description": "tired"}}
PRESET = {"id": 1, "name": "t", "data": {"samplers": {"max_tokens": 512}}}
CHAT = {"id": 1, "mode": "rp", "data": {}}
H = [{"id": 1, "role": "user", "content": "the gate and the dragons",
      "data": {}}]


def assemble(block_list):
    return engine.assemble_blocks(CHAT, CHAR, PERSONA, PRESET, block_list,
                                  [], list(H), context_tokens=8192)[0]


sysm, _ = assemble(blocks.default_blocks()), None
joined = "\n".join(m["content"] for m in sysm)
check("both entries fired and are in the prompt",
      "Sunken Gate" in joined and "Dragons are extinct" in joined)
check("...separated by a blank line, as the joined string always was",
      "The Sunken Gate is sealed.\n\nDragons are extinct." in joined,
      joined[joined.find("Sunken") - 40:][:160])
check("the whole assembly is still one system message",
      sum(1 for m in sysm if m["role"] == "system") == 1,
      str([m["role"] for m in sysm]))
check("still role+content only",
      all(set(m) <= {"role", "content", "name"} for m in sysm))

# Move the lore block off role:system and the list must become a string, or
# squash leaves N separate messages where there used to be one.
moved = [dict(b, role="user") if b.get("marker") == "lore" else b
         for b in blocks.default_blocks()]
umsgs = assemble(moved)
users = [m for m in umsgs if m["role"] == "user"
         and "Sunken Gate" in m["content"]]
check("a non-system lore block collapses to ONE message, not two",
      len(users) == 1, str([m["content"][:40] for m in umsgs]))
check("...and it still holds both entries",
      users and "Dragons are extinct" in users[0]["content"])

# ── the importer, against real files where they exist ────────────────────
print("\nimporting a SillyTavern world")
# A real ST install, if this machine has one. Several plausible places
# rather than one hardcoded path — this tree has been relocated twice and
# every absolute path in it broke both times.
def _worlds():
    for base in (ROOT.parent, ROOT.parent.parent, ROOT.parent.parent.parent):
        p = base / "SillyTavern" / "data" / "default-user" / "worlds"
        if p.is_dir():
            return p
    return ROOT / "does-not-exist"


WORLDS = _worlds()


def world(**entries):
    """A standalone book: `entries` is a DICT keyed by stringified uid, which
    is what all 17 real files use. The embedded dialect is a LIST."""
    return {"name": "w", "entries": {str(i): e
                                     for i, e in enumerate(entries.values())}}


check("a dict-of-uids is detected as a standalone world",
      lore.detect(world(a={"content": "x"})) == "st-world")
check("a list of entries is detected as an embedded book",
      lore.detect({"entries": [{"content": "x"}]}) == "card")
check("a whole card with a character_book is detected",
      lore.detect({"data": {"character_book": {"entries": []}}}) == "card")
check("anything else says it does not know",
      lore.detect({"nope": 1}) is None and lore.detect("junk") is None)

W = world(
    keyed={"content": "KEYED", "key": ["gate"], "comment": "the gate"},
    const={"content": "ALWAYS", "key": ["absent"], "constant": True},
    off={"content": "OFF", "key": ["gate"], "disable": True},
    stop={"content": "STOPWORDS", "key": ["a", "and", "the"]},
    sec={"content": "SEC", "key": ["gate"], "keysecondary": ["dragons"],
         "selectiveLogic": 0},
    secall={"content": "SECALL", "key": ["gate"],
            "keysecondary": ["dragons", "absent"], "selectiveLogic": 3},
    secnot={"content": "SECNOT", "key": ["gate"],
            "keysecondary": ["absent"], "selectiveLogic": 2},
)
B = lore.from_st_world(W, "W")
HH = ["talk about the gate", "and the dragons too"]
fired = [x["content"] for x in lore.select([B], HH, {"budget": 99999})]

check("a keyed entry fires", "KEYED" in fired)
check("constant fires with its key ABSENT — the thing legacy hides",
      "ALWAYS" in fired)
check("`disable: true` is honoured on an imported book", "OFF" not in fired)
check("a stop-word-only entry is imported DISABLED", "STOPWORDS" not in fired)
check("...with the reason attached, not silently",
      any("stop words" in e["reason"] for e in B["entries"]))
check("secondary AND_ANY fires when a secondary is present", "SEC" in fired)
check("secondary AND_ALL needs all of them", "SECALL" not in fired)
check("secondary NOT_ANY fires when none are present", "SECNOT" in fired)

WW = world(rem={"content": "REM", "key": ["Rem"]},
           age={"content": "AGE", "key": ["age"]})
BB = lore.from_st_world(WW, "WW")
got = [x["content"] for x in lore.select([BB], ["remember the message"],
                                         {"budget": 99999})]
check("whole-word stops `Rem`/`age` firing on remember/message", got == [], str(got))
got = [x["content"] for x in lore.select([BB], ["Rem is her age"],
                                         {"budget": 99999})]
check("...and they still fire when the word really is there",
      sorted(got) == ["AGE", "REM"], str(got))

# R9: `\b` matches fine next to Chinese punctuation, so a careless test passes
# by accident. The failure only shows in unpunctuated Han.
CJK = world(king={"content": "KING", "key": ["王"]})
BC = lore.from_st_world(CJK, "cjk")
got = [x["content"] for x in lore.select([BC], ["国王说话"], {"budget": 9999})]
check("a CJK key matches inside unpunctuated Han (the carve-out)",
      got == ["KING"], str(got))
import re as _re  # noqa: E402
check("...and a naive whole-word pattern really would have missed it",
      _re.search(r"\b王\b", "国王说话") is None)

print("\n  ordering, budget and truncation")
ORD = world(lo={"content": "LO", "key": ["gate"], "insertion_order": 1},
            hi={"content": "HI", "key": ["gate"], "insertion_order": 999})
got = [x["content"] for x in
       lore.select([lore.from_st_world(ORD, "o")], HH, {"budget": 9999})]
check("imported entries sort by order DESC, unlike the legacy path",
      got == ["HI", "LO"], str(got))

BIG = world(fat={"content": "S" * 8000, "key": ["gate"]})
got = lore.select([lore.from_st_world(BIG, "big")], HH, {"budget": 200})
check("an oversized IMPORTED entry is truncated, not skipped",
      got and "[trimmed]" in got[0]["content"], str(got)[:120])
check("...and it respects the ceiling",
      got and lore.rough_tokens(got[0]["content"]) <= 200)

print("\n  fair share")
b1 = lore.from_st_world(world(**{f"e{i}": {"content": "A" * 400,
                                           "key": ["gate"]}
                                for i in range(10)}), "fat")
b2 = lore.from_st_world(world(small={"content": "B" * 400, "key": ["gate"]}),
                        "thin")
b1["id"], b2["id"] = 1, 2
out = lore.select([b1, b2], HH, {"budget": 600})
names = {x["src"]["name"].split(" · ")[0] for x in out}
check("a fat book cannot starve a thin one", "thin" in names, str(names))
check("...and the ceiling is still respected",
      sum(x["src"]["tokens"] for x in out) <= 600)

print("\n  the import summary")
S = lore.summarise_import(B)
check("it counts what it refused", S["disabled"] >= 1 and S["refused_by_us"])
check("it names what it cannot honour",
      isinstance(S["not_honoured"], list))
check("it admits it turned whole-word on, which ST leaves off",
      S["whole_words_added"] is True,
      "no real book sets matchWholeWords true; this is our divergence")
check("it reports a scan depth", S["scan_depth"] == lore.ST_SCAN_DEPTH)

# The real corpus, when this machine has one. Skips quietly on a bare clone,
# the same way test_regex.py handles st-presets/.
print("\n  the real corpus")
files = sorted(WORLDS.glob("*.json")) if WORLDS.is_dir() else []
if not files:
    print("  ..    no SillyTavern worlds on this machine, skipped")
else:
    bad = []
    total = 0
    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
            if lore.detect(obj) != "st-world":
                bad.append(f"{f.name}: detected {lore.detect(obj)}")
                continue
            bk = lore.from_st_world(obj, f.stem)
            total += len(bk["entries"])
            lore.summarise_import(bk)
            lore.select([bk], ["the gate", "王"], {"budget": 1200})
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{f.name}: {type(exc).__name__}: {exc}")
    check(f"all {len(files)} real books parse, summarise and match",
          not bad, "; ".join(bad[:3]))
    check("...and they carry entries", total > 0, str(total))

# ── what did NOT fit ─────────────────────────────────────────────────────
# Half the answer to "why didn't that fire" is the entries you cannot see.
print("\nthe overflow report")
FAT = world(**{f"e{i}": {"content": "Z" * 800, "key": ["gate"]}
               for i in range(6)})
rep = {}
got = lore.select([lore.from_st_world(FAT, "fat")], HH, {"budget": 300},
                  report=rep)
check("entries that lost the budget race are counted",
      rep["missed"] >= 1, str(rep))
check("...and their cost is reported so the number means something",
      rep["missed_tokens"] > 0, str(rep))
check("nothing missed means nothing to report",
      lore.select([lore.from_card(card({"content": "tiny", "keys": ["gate"]}))],
                  HIST, {"budget": 9999}, report=(r2 := {})) is not None
      and r2["missed"] == 0)

# ── the header gate ──────────────────────────────────────────────────────
# R14: cast_baseline.json is green here for the wrong reason — its fixture
# card has no character_book at all, so it never exercises this. A chat with
# ONLY an embedded book must produce no header and no change.
print("\nthe header gate")
LEG = dict(BOOK)
CH2 = {"id": 1, "name": "Fixture-chan", "data": {"fields": LEG}}
msgs_legacy = engine.assemble_blocks(
    CHAT, CH2, PERSONA, PRESET, blocks.default_blocks(), [], list(H),
    context_tokens=8192, layers={"lore_header": "[World info]"})[0]
blob = "\n".join(m["content"] for m in msgs_legacy)
check("an embedded-only book gets NO header, however loudly it is supplied",
      "[World info]" not in blob, "a legacy chat must not change at all")
check("...and its entries still fire", "Sunken Gate" in blob)

stored = lore.from_st_world(world(g={"content": "STORED", "key": ["gate"]}),
                            "Stored")
stored["id"] = 7
msgs_stored = engine.assemble_blocks(
    CHAT, CH2, PERSONA, PRESET, blocks.default_blocks(), [], list(H),
    context_tokens=8192, layers={"lore_header": "[World info]"},
    books=[stored])[0]
sblob = "\n".join(m["content"] for m in msgs_stored)
check("a STORED book does get the header", "[World info]" in sblob)
check("...before its entries",
      sblob.index("[World info]") < sblob.index("STORED"))

# ── routes, scoping and cleanup ──────────────────────────────────────────
print("\nroutes")
import base64  # noqa: E402
import testkit as T  # noqa: E402

WORLD = {"name": "T", "entries": {
    "0": {"content": "GATE LORE", "key": ["gate"], "comment": "the gate"},
    "1": {"content": "OFF", "key": ["gate"], "disable": True}}}
b64 = base64.b64encode(json.dumps(WORLD).encode()).decode()

dry = T.call("POST", "/api/lorebooks/import",
             {"b64": b64, "name": "Fixture-book", "dry_run": True})
check("a dry run previews and stores nothing",
      dry.get("preview") and dry["summary"]["entries"] == 2)
check("...and says what it switched off", dry["summary"]["disabled"] == 1)
before = len(T.call("GET", "/api/lorebooks")["rows"])
real = T.call("POST", "/api/lorebooks/import", {"b64": b64,
                                                "name": "Fixture-book"})
bid = real.get("id")
check("importing for real stores it", bool(bid))
check("...and it appears in the list",
      len(T.call("GET", "/api/lorebooks")["rows"]) == before + 1)

check("an unknown shape is refused by name, not silently",
      "error" in T.call("POST", "/api/lorebooks/import", {"json": {"no": 1}}))

cid = T.ensure_character()
chid = T.call("POST", "/api/chats/new",
              {"character_id": cid, "mode": "rp"})["chat_id"]


def scope_of(book_id, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    rows = T.call("GET", f"/api/lorebooks?{qs}")["rows"]
    return next((r["scope"] for r in rows if r["id"] == book_id), None)


T.call("POST", "/api/lorebooks/link", {"id": bid, "scope": "character",
                                       "character_id": cid})
check("attaching to a character reads back as hers",
      scope_of(bid, character_id=cid) == "character")
check("...and is invisible to a different character",
      scope_of(bid, character_id=cid + 99999) == "off")

T.call("POST", "/api/lorebooks/link", {"id": bid, "scope": "chat",
                                       "chat_id": chid, "character_id": cid})
check("attaching to a chat reads back as this chat's",
      scope_of(bid, chat_id=chid) == "chat")

T.call("POST", "/api/lorebooks/link", {"id": bid, "scope": "always"})
check("global reads back everywhere",
      scope_of(bid) == "always" and scope_of(bid, character_id=999999) == "always")
check("...and a second global link is refused by the unique index",
      T.call("POST", "/api/lorebooks/link", {"id": bid,
                                             "scope": "always"}).get("ok"),
      "INSERT OR IGNORE, because ON CONFLICT cannot target a COALESCE index")

# Scoped cleanup: a chat-scoped link goes with its chat, hers survives.
T.call("POST", "/api/lorebooks/link", {"id": bid, "scope": "chat",
                                       "chat_id": chid, "character_id": cid})
T.call("DELETE", f"/api/chats/{chid}")
check("deleting a chat takes its lore link with it",
      scope_of(bid, chat_id=chid) == "off")

T.call("POST", "/api/lorebooks/link", {"id": bid, "scope": "character",
                                       "character_id": cid})
check("a character-scoped link survives a chat delete",
      scope_of(bid, character_id=cid) == "character")

T.call("POST", "/api/lorebooks", {"id": bid, "enabled": 0})
rows = T.call("GET", "/api/lorebooks")["rows"]
check("a book can be switched off",
      not next(r["enabled"] for r in rows if r["id"] == bid))

check("deleting a book reports it", T.call("DELETE", f"/api/lorebooks/{bid}").get("ok"))
check("...and it is gone",
      not any(r["id"] == bid for r in T.call("GET", "/api/lorebooks")["rows"]))
check("deleting it twice 404s rather than pretending",
      not T.call("DELETE", f"/api/lorebooks/{bid}").get("ok"))

print()
if fails:
    print(f"LORE TESTS FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("lore ok")
