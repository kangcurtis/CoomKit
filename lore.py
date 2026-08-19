#!/usr/bin/env python3
"""Lorebooks — world info that is keyed on what was just said.

Two adapters and ONE matcher. That split is the whole design: the embedded
`character_book` on a v2/v3 card has to keep behaving exactly as it does
today, and an imported SillyTavern world has to behave as it did in ST, and
those two are NOT the same semantics. Putting the difference in the adapter
rather than in the matcher means there is one place that decides whether an
entry fires, so the legacy path and the imported path cannot drift.

The compatibility claim is proved rather than asserted: `tests/test_lore.py`
keeps a verbatim copy of the old `engine._lorebook_entries` and diffs this
module's output against it over a battery of entry shapes. If that test is
red, the claim is false and nothing else in here matters.

Deliberately NOT honoured, and each says so at import time rather than
silently doing nothing (regexrules.py's principle): at-depth placement,
recursion, inclusion groups, probability, sticky/cooldown/delay, vectorized
and characterFilter. Everything refused is kept verbatim in the entry's
`data`, so a later phase can honour it and nothing is lost in the meantime.
"""
import json
import re

# ── stop words ───────────────────────────────────────────────────────────
# An entry keyed ONLY on these fires on every line of English roleplay, and
# whole-word matching does not help — `\bthe\b` matches everything. Exactly one
# exists in the 17-book corpus this was measured against: keyed `a`/`and`/`the`,
# 342 tokens, which is 28% of the whole 1200-token ceiling spent on every single
# turn. Such an entry is imported DISABLED with the reason attached.
#
# NOT because it wins a sort race — that claim was checked and is false. Its
# `order` is 100, which is SillyTavern's DEFAULT and the modal value across the
# corpus, and every entry in its own book shares it, so it sorts in the middle
# rather than first. The problem is that it fires unconditionally, not that it
# fires first. Do not build a sort-priority defence against it.
STOP_WORDS = {
    "a", "an", "and", "the", "or", "but", "if", "of", "at", "by", "for",
    "in", "on", "to", "up", "is", "it", "as", "so", "he", "she", "they",
    "you", "i", "we", "me", "my", "his", "her", "their", "your", "this",
    "that", "with", "was", "are", "be", "do", "no", "not", "yes", "there",
}

# ST's selectiveLogic, by value. Only 5 entries in a 281-entry corpus use it,
# but it is ten lines and it is the difference between "supported" and
# "silently wrong".
AND_ANY, NOT_ALL, NOT_ANY, AND_ALL = 0, 1, 2, 3

LEGACY_SCAN_DEPTH = 20     # messages. What the embedded path has always used.
ST_SCAN_DEPTH = 2          # SillyTavern's own default, and what real cards set.
DEFAULT_BUDGET = 1200      # today's number, unchanged.


def rough_tokens(text: str) -> int:
    """Deliberately identical to engine.rough_tokens — the budget must agree."""
    return max(1, len(text) // 4)


# ── the Book ─────────────────────────────────────────────────────────────

def _book(name, source, entries, **flags) -> dict:
    """A book plus the behaviour flags the matcher reads off it."""
    b = {
        "name": name,
        "source": source,
        "entries": entries,
        # Legacy defaults. from_st_world / from_card_book override.
        "keyless_always": True,   # a keyless entry fires unconditionally
        "honour_constant": False,
        "honour_disable": False,  # only `enabled` is read, not `disable`
        "whole_words": False,
        "scan_depth": LEGACY_SCAN_DEPTH,
        "oversize": "skip",       # vs "truncate"
        "ord": 0,
        "id": 0,
        "notes": [],
    }
    b.update(flags)
    return b


def _entry(content, **kw) -> dict:
    e = {
        "content": content,
        "label": "",
        "keys": [],
        "secondary": [],
        "logic": AND_ANY,
        "constant": False,
        "enabled": True,
        "case_sensitive": False,
        "whole_words": None,      # None inherits the book
        "ord": 100,
        "data": {},
        "reason": "",             # why it was imported disabled, if it was
    }
    e.update(kw)
    return e


# ── adapter 1: the embedded character_book (LEGACY, byte-compatible) ─────

def from_card(fields: dict) -> dict:
    """The card's own `character_book`, with TODAY's semantics exactly.

    Every quirk here is deliberate and load-bearing, because changing any of
    them silently changes the prompt of every existing chat with an embedded
    book:

    - a KEYLESS entry always fires (which coincidentally hides the fact that
      `constant` is ignored)
    - `constant` is ignored
    - `disable` is ignored; only `enabled` is read, defaulting to True
    - matching is a lowercase SUBSTRING, not whole-word
    - entries are taken in source order, not sorted by `order`
    - an oversized entry is SKIPPED and smaller later ones still land

    Anyone "fixing" the last one to a break changes which entries appear.
    """
    book = (fields or {}).get("character_book") or {}
    raw = book.get("entries") or []
    if isinstance(raw, dict):                      # tolerate, though cards use a list
        raw = [raw[k] for k in sorted(raw, key=_uid_key)]
    entries = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        # Empty content is filtered HERE and not in the matcher. Six real
        # entries in the corpus have `content: ""`, and an adapter that
        # honours `constant` would otherwise fire one and emit a stray blank
        # paragraph into the prompt.
        if not (e.get("content") or ""):
            continue
        entries.append(_entry(
            e["content"],
            keys=[str(k) for k in (e.get("keys") or e.get("key") or [])],
            enabled=e.get("enabled", True),
            data=e))
    return _book(book.get("name") or "the card's own book", "card", entries)


def _uid_key(k):
    """Sort dict-keyed entries numerically when the keys are stringified ints."""
    try:
        return (0, int(k))
    except (TypeError, ValueError):
        return (1, str(k))


# ── adapter 2: a SillyTavern world, or an embedded book lifted out ───────

def detect(raw):
    """"st-world" | "card" | None. Says what it does not know rather than
    guessing — three untested parsers is three bug reports for formats
    nobody here can reproduce."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("entries"), dict):
        return "st-world"              # standalone: entries keyed by uid
    if isinstance(raw.get("entries"), list):
        return "card"                  # embedded character_book
    inner = raw.get("character_book") or (raw.get("data") or {}).get("character_book")
    if isinstance(inner, dict) and inner.get("entries") is not None:
        return "card"
    return None


def _int(v, default=0):
    """Tolerant int. `token_budget: "2000"` is a string in one real book, and
    half the numeric fields are null rather than absent."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _keys(v) -> list:
    if isinstance(v, str):
        v = [p for p in re.split(r"[,\n]", v)]
    return [str(k).strip() for k in (v or []) if str(k).strip()]


def _promiscuous(keys) -> bool:
    """Would this entry fire on essentially every line the user writes?"""
    return bool(keys) and all(k.lower() in STOP_WORDS for k in keys)


def from_st_world(obj: dict, name: str = "") -> dict:
    """A standalone SillyTavern World Info file.

    `entries` is a DICT keyed by stringified uid in all 17 real books — never
    a list. Read the TOP-LEVEL `entries` and nothing else: 16 of those 17 also
    carry an `originalData` sibling holding a DIFFERENT dialect (snake_case,
    `position` as a string enum or "" or absent, blank strings inside key
    lists, `token_budget` as a string). Falling back to it when a field is
    missing up here — the tolerant instinct — imports the wrong schema.
    """
    raw = obj.get("entries")
    items = ([raw[k] for k in sorted(raw, key=_uid_key)]
             if isinstance(raw, dict) else list(raw or []))
    notes, entries = [], []
    refused = {}
    for e in items:
        if not isinstance(e, dict) or not (e.get("content") or ""):
            continue
        keys = _keys(e.get("key") if e.get("key") is not None else e.get("keys"))
        ext = e.get("extensions") or {}
        # `disable` is inverted and camelCase in the standalone dialect;
        # `enabled` is the embedded one. Honour whichever is present.
        on = not e.get("disable", False)
        if "enabled" in e:
            on = bool(e["enabled"])
        reason = ""
        if _promiscuous(keys):
            on, reason = False, ("keyed only on stop words "
                                 + ", ".join(f'"{k}"' for k in keys[:4])
                                 + " — it would fire on every line you write")
        # probability 0 is not a coin flip, it is a hard OFF, and ST honours
        # it. Four real entries are probability 0 with useProbability true —
        # importing them enabled would make them fire where they never could.
        prob = e.get("probability")
        if prob == 0 and (e.get("useProbability") or ext.get("useProbability")):
            on, reason = False, "its probability is 0, so it never fired in ST either"
        # ONLY position == 4 means at-depth. `depth` is present on 281 of
        # 281 real entries with a default of 4 and `role` on 147 with a
        # meaningful 0, so counting either of them reports every entry in
        # every book as at-depth — which is how the first run of this said
        # "75 of 75" about a book that has none.
        hit = set()
        for field, label in (("position", "at-depth placement"),
                             ("probability", "random firing"),
                             ("excludeRecursion", "recursion"),
                             ("preventRecursion", "recursion"),
                             ("group", "inclusion groups"),
                             ("sticky", "sticky/cooldown/delay"),
                             ("cooldown", "sticky/cooldown/delay"),
                             ("delay", "sticky/cooldown/delay"),
                             ("vectorized", "vector matching"),
                             ("characterFilter", "per-entry character filters")):
            val = e.get(field, ext.get(field))
            if not val:
                continue
            if field == "position" and _int(val) != 4:
                continue          # only 4 means at-depth; 0-3 are before/after
            if field == "probability" and _int(val, 100) == 100:
                continue
            hit.add(label)
        # Per ENTRY, not per field: excludeRecursion and preventRecursion
        # both mean "recursion", and counting hits reported 36 of a 20-entry
        # book.
        for label in hit:
            refused[label] = refused.get(label, 0) + 1
        whole = e.get("matchWholeWords", ext.get("match_whole_words"))
        entries.append(_entry(
            e["content"],
            label=(e.get("comment") or e.get("name") or "").strip(),
            keys=keys,
            secondary=_keys(e.get("keysecondary")
                            if e.get("keysecondary") is not None
                            else e.get("secondary_keys")),
            logic=_int(e.get("selectiveLogic", e.get("selective_logic")), AND_ANY),
            constant=bool(e.get("constant")),
            enabled=on,
            case_sensitive=bool(e.get("caseSensitive")
                                or e.get("case_sensitive")),
            whole_words=None if whole is None else bool(whole),
            ord=_int(e.get("insertion_order", e.get("order")), 100),
            data=e,
            reason=reason))
    for label, n in sorted(refused.items(), key=lambda kv: -kv[1]):
        notes.append({"what": label, "n": n})
    depth = _int(obj.get("scan_depth", obj.get("scanDepth")), 0) or ST_SCAN_DEPTH
    return _book(name or obj.get("name") or "imported book", "st-world",
                 entries, keyless_always=False, honour_constant=True,
                 honour_disable=True, whole_words=True, scan_depth=depth,
                 oversize="truncate", notes=notes)


def from_card_book(obj: dict, name: str = "") -> dict:
    """An embedded `character_book`, LIFTED OUT into a real book.

    Same parser as a standalone world — the dialect differences are absorbed
    by from_st_world's tolerance — but it is worth being explicit that lifting
    out CHANGES BEHAVIOUR: full semantics switch on, so `constant` entries
    start firing and `disable`d ones stop. More entries fire than before, and
    a user can read that as a bug unless the confirm says so.
    """
    book = from_st_world(obj, name or obj.get("name") or "lifted from her card")
    book["source"] = "card-lifted"
    return book


def summarise_import(book: dict) -> dict:
    """The dry run. What comes across, what does not, and what it will cost.

    regexrules.py's principle applied: a rule that does nothing and says
    nothing is worse than one you can see is broken.
    """
    ents = book["entries"]
    off = [e for e in ents if not e["enabled"]]
    tokens = sum(rough_tokens(e["content"]) for e in ents)
    cjk = sum(1 for e in ents for k in e["keys"] if not _ascii_edged(k))
    return {
        "name": book["name"],
        "source": book["source"],
        "entries": len(ents),
        "always_on": sum(1 for e in ents if e["constant"] and e["enabled"]),
        "disabled": len(off),
        "refused_by_us": [e["reason"] for e in off if e["reason"]],
        "tokens": tokens,
        "scan_depth": book["scan_depth"],
        "not_honoured": book["notes"],
        "cjk_keys": cjk,
        # Neither per-entry flag is set by ANY book in the real corpus:
        # caseSensitive is null on all 281 entries and matchWholeWords is
        # never true. Turning whole-word on is therefore a deliberate
        # divergence from ST that no real file asked for, and it has to be
        # said or the first bug report is "entries stopped firing".
        "whole_words_added": bool(book["whole_words"]),
    }


# ── the matcher ──────────────────────────────────────────────────────────

_PATTERNS = {}


def _compile(key: str, whole: bool, case: bool):
    """A key as a pattern, with the CJK carve-out.

    Whole-word matching is right for English — a substring matcher fires the
    corpus's `age`, `ass`, `Rem` and `sex` keys on *message*, *pass*,
    *remember* and *sexy* on nearly every turn. But `re.search(r'\\b王\\b',
    '国王说话')` is FALSE: CJK characters are all `\\w`, so `\\b` never matches
    between two of them, and whole-word would silently kill every key in the
    three Chinese and Korean books in the corpus. So whole-word applies only
    when the key starts AND ends with an ASCII word character.
    """
    ck = (key, whole, case)
    got = _PATTERNS.get(ck)
    if got is None:
        pat = re.escape(key)
        if whole and _ascii_edged(key):
            pat = r"\b" + pat + r"\b"
        # Cached across turns: the corpus has 531 key strings and whole-word
        # is on by default for imports, so several attached books mean
        # hundreds of re.compile calls per turn in the request thread.
        got = _PATTERNS[ck] = re.compile(pat, 0 if case else re.I)
    return got


def _ascii_edged(key: str) -> bool:
    a, z = key[:1], key[-1:]
    return bool(a) and a.isascii() and a.isalnum() \
        and z.isascii() and z.isalnum()


def _hit(keys, text, whole, case) -> bool:
    return any(_compile(k, whole, case).search(text)
               for k in keys if k)


def _fires(entry: dict, book: dict, text: str) -> bool:
    """Does this entry trigger against the scanned text?"""
    if not entry.get("content"):
        return False
    if not entry.get("enabled", True):
        return False
    if book["honour_disable"] and entry.get("data", {}).get("disable"):
        return False

    # KEYLESS-ness is decided by the raw list, not by the usable one. An
    # entry keyed [""] HAS a key list, it just cannot match anything — and
    # today's matcher skips it rather than treating it as keyless. That is a
    # one-character difference in the condition and it changes whether the
    # entry fires on every single turn.
    raw = entry.get("keys") or []
    keys = [k for k in raw if k]
    if not raw:
        # Legacy: keyless means always. Imported: keyless means nothing
        # UNLESS it is constant — which is the semantic the legacy path hides
        # by firing keyless entries unconditionally.
        return bool(book["keyless_always"]
                    or (book["honour_constant"] and entry.get("constant")))
    if not keys:
        return False          # keys present but all blank: cannot match
    if book["honour_constant"] and entry.get("constant"):
        return True

    whole = entry.get("whole_words")
    whole = book["whole_words"] if whole is None else bool(whole)
    case = bool(entry.get("case_sensitive"))
    if not _hit(keys, text, whole, case):
        return False

    sec = [k for k in (entry.get("secondary") or []) if k]
    if not sec:
        return True
    logic = entry.get("logic", AND_ANY)
    got = _hit(sec, text, whole, case)
    all_ = all(_compile(k, whole, case).search(text) for k in sec)
    if logic == AND_ANY:
        return got
    if logic == AND_ALL:
        return all_
    if logic == NOT_ANY:
        return not got
    if logic == NOT_ALL:
        return not all_
    return got


def _clip(text: str, budget: int) -> str:
    """Trim an entry to a budget at a sentence boundary, visibly."""
    if rough_tokens(text) <= budget:
        return text
    room = max(60, budget * 4 - 20)
    cut = text[:room]
    stop = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("\n\n"))
    if stop > room // 3:
        cut = cut[:stop + 1]
    return cut.rstrip() + " … [trimmed]"


def scan_text(history, depth: int) -> str:
    """The last `depth` messages, joined — what keys are matched against.

    Joined with "\\n" and sliced from the end, which is what the embedded path
    has always done. `history` is a list of message contents, oldest first.
    """
    if depth <= 0:
        return ""
    return "\n".join(history[-depth:])


def select(books, history, limits=None, expand=None, report=None) -> list:
    """Which entries fire this turn, in the order they go into the prompt.

    Returns a list of `{content, src}` dicts — the same shape the cast's card
    slot uses, so `blocks.render` and `squash` already know what to do with
    it. `expand` is the caller's macro expander, applied PER ENTRY: macros are
    resolved late everywhere else in this codebase and a lore entry containing
    `{{char}}` must not be the exception.

    ONE global ceiling, fair-shared. With n active books each is guaranteed
    `budget // n` on a first pass and unspent share flows into a second pass in
    the same order, so a fat setting book cannot starve her character book.
    With a single book the share IS the budget and the second pass is a no-op,
    which is what keeps the legacy path byte-identical.
    """
    limits = limits or {}
    budget = int(limits.get("budget") or DEFAULT_BUDGET)
    # Pass a dict as `report` and it comes back with what MATCHED and did not
    # fit. "A lorebook that silently does nothing" is the commonest lorebook
    # complaint anywhere, and half the answer is the entries you cannot see.
    if report is not None:
        report.setdefault("missed", 0)
        report.setdefault("missed_tokens", 0)
    books = [b for b in (books or []) if b and b.get("entries")]
    if not books or budget <= 0:
        return []
    ex = expand or (lambda t: t)

    # Legacy first, then by (ord, id) — a stable, stated order rather than
    # whatever the database happened to return.
    books = sorted(books, key=lambda b: (0 if b["source"] == "card" else 1,
                                         b.get("ord", 0), b.get("id", 0)))
    texts = {}
    for b in books:
        d = int(b.get("scan_depth") or LEGACY_SCAN_DEPTH)
        if d not in texts:
            texts[d] = scan_text(history, d)

    share = budget // len(books)
    out, used, taken = [], 0, set()

    def spend(book, cap):
        nonlocal used
        text = texts[int(book.get("scan_depth") or LEGACY_SCAN_DEPTH)]
        spent = 0
        for i, e in enumerate(_ordered(book)):
            mark = (id(book), i)
            if mark in taken:
                continue
            if not _fires(e, book, text):
                continue
            body = ex(e["content"])
            cost = rough_tokens(body)
            if spent + cost > cap or used + cost > budget:
                if book["oversize"] != "truncate":
                    # Legacy keeps today's `continue`: an oversized entry is
                    # skipped and smaller later ones still land. Changing this
                    # to a break changes which entries appear.
                    if report is not None:
                        report["missed"] += 1
                        report["missed_tokens"] += cost
                    continue
                room = min(cap - spent, budget - used)
                if room <= 0:
                    if report is not None:
                        report["missed"] += 1
                        report["missed_tokens"] += cost
                    continue
                body = _clip(body, room)
                cost = rough_tokens(body)
            taken.add(mark)
            spent += cost
            used += cost
            # The id is deliberately NOT "lore". renderSegments draws a
            # "turn this off" button for any part whose id matches a block,
            # so N fired entries would draw N buttons that all disable the
            # same Lorebook block. Only the header (emitted by the caller
            # with id "lore") gets one.
            out.append({"content": body,
                        "src": {"id": f"lore:{book.get('id', 0)}:{i}",
                                "marker": "lore", "builtin": True,
                                "layer": "", "name": _label(book, e),
                                "tokens": cost}})
        return spent

    for b in books:
        spend(b, share)
    if used < budget:
        for b in books:                    # second pass over the leftover
            spend(b, budget - used)
    return out


def _ordered(book: dict) -> list:
    """Entries in firing order.

    Legacy takes them in SOURCE order — that is what today does and the
    compatibility test pins it. Imported books sort by `order` DESC, which is
    SillyTavern's own sortFn, then by position for stability.
    """
    entries = book["entries"]
    if book["source"] == "card":
        return entries
    return sorted(entries, key=lambda e: -e.get("ord", 100))


def _label(book: dict, entry: dict) -> str:
    """What the inspector calls this entry."""
    who = entry.get("label") or (entry.get("keys") or [""])[0] or "entry"
    return f"{book['name']} · {who}"
