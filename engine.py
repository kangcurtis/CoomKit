#!/usr/bin/env python3
"""CoomKit chat engine — context assembly, swipes, personas, memory block.

Assembles the outgoing message list from:
  system prompt = [jailbreak] + card system_prompt + description/personality/
                  scenario + persona block + memory block + post-history
  history       = first_mes (or chosen greeting) + message log
  prefill       = from preset
Lorebook (character_book) entries trigger on keyword match against recent
history, capped by token budget.
"""
import json
import re

import blocks
import lore
import sqlite3
import time

import macros


def rough_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# ----------------------------------------------------------------------
# Chat / message persistence
# ----------------------------------------------------------------------


def create_chat(conn, character_id: int | None, persona_id: int | None = None,
                mode: str = "rp", greeting_index: int = 0,
                scenario: dict | None = None, title: str | None = None,
                opening: str | None = None) -> int:
    """Create a chat. When `scenario` is given it overrides the card's
    scenario field and its `opening` seeds the first message instead of
    first_mes — that is how the scenario forge launches a scene.

    `title` names the adventure so it can be found again in her chat list;
    a forged scene takes the scenario's own title by default.

    SMS threads seed NOTHING unless `opening` says otherwise. `mode` was
    written to the row and then never consulted, so every 💬 thread opened
    with the card's `first_mes` — prose narration delivered as a text
    message, sitting in the history fighting the texting-mode layer.
    """
    now = time.time()
    data = {"greeting_index": greeting_index}
    if scenario:
        data["scenario"] = scenario
    if title is None and scenario:
        title = (scenario.get("title") or "").strip() or None
    cur = conn.execute(
        "INSERT INTO chats (character_id, persona_id, mode, title, data,"
        " created, updated) VALUES (?,?,?,?,?,?,?)",
        (character_id, persona_id, mode, title, json.dumps(data), now, now),
    )
    chat_id = cur.lastrowid

    if opening and opening.strip():
        add_message(conn, chat_id, "assistant", opening.strip())
        return chat_id

    if scenario and scenario.get("opening"):
        add_message(conn, chat_id, "assistant", scenario["opening"].strip())
        return chat_id

    if mode == "sms":
        return chat_id

    # A plain chat has no card, so there is no greeting to seed and it opens
    # empty — the same as an SMS thread, and for the same reason.
    if character_id is None:
        return chat_id

    # seed first message from the card greeting
    char = conn.execute("SELECT data FROM characters WHERE id=?",
                        (character_id,)).fetchone()
    if char:
        fields = json.loads(char["data"]).get("fields", {})
        first = fields.get("first_mes", "")
        if greeting_index > 0:
            alts = fields.get("alternate_greetings") or []
            if greeting_index - 1 < len(alts):
                first = alts[greeting_index - 1]
        if first:
            add_message(conn, chat_id, "assistant", first)
    return chat_id


CAST_PRESENT_CAP = 4       # including the lead. See cast_room().


def cast_of(conn, chat_id: int, lead_id: int) -> list:
    """The scene's cast, lead first, then chat_cast rows in `ord` order.

    The lead is chats.character_id and is ALWAYS present and always first —
    she is what the gallery, the chat list and the export footer key off, and
    a scene with nobody in it is not a state worth representing.
    """
    out = [{"character_id": lead_id, "present": True, "ord": -1,
            "lead": True, "note": ""}]
    for r in conn.execute(
            "SELECT character_id, present, ord, data FROM chat_cast"
            " WHERE chat_id=? ORDER BY ord, id", (chat_id,)):
        try:
            d = json.loads(r["data"] or "{}")
        except json.JSONDecodeError:
            d = {}
        if r["character_id"] == lead_id:
            continue          # the unique index should stop this; belt and braces
        out.append({"character_id": r["character_id"],
                    "present": bool(r["present"]), "ord": r["ord"],
                    "lead": False, "note": d.get("note", "")})
    return out


def cast_present(cast: list) -> list:
    return [c for c in cast if c["present"]]


def cast_active(chat: dict, cast: list) -> bool:
    """Is this a scene with more than one person in it?

    ONE predicate, checked once, so the multi-character branches cannot
    disagree with each other about whether they apply. False for a cast of
    one means every existing chat assembles down exactly the path it always
    did — that is what keeps the solo prompt byte-identical.
    """
    if (chat or {}).get("mode") == "sms":
        return False          # the phone is one-to-one by construction
    return len(cast_present(cast)) > 1


# ── who speaks this turn ─────────────────────────────────────────────────
# No round trip and no coin flip. Asking the model to nominate the next
# speaker was measured on a live 31B over twelve turns of a three-hander:
# 4/12 emitted nothing, 2/12 passed, and all 6 that nominated named the FIRST
# name in the list — including on "Mika, tell me about your day". That is
# positional degeneracy, not agreeableness, and it costs ~80 prompt tokens a
# turn to produce a stuck pointer. Six free rules beat it outright.
CAST_STREAK = 3            # consecutive turns before fairness even looks
CAST_STARVE_WINDOW = 2     # × the number present
# How far back a dismissed character's stamped lines keep the cast_absent
# warning alive. Past this, her lines are too far up the log to imitate and
# the warning is stale furniture in every later turn of the chat.
CAST_ABSENT_WINDOW = 30


def take_speaker(msg: dict):
    """Who wrote the take on screen, or None if the turn is unstamped.

    Swipe first, then the message — the same two-step `_chat_detail` uses.
    `add_swipe` seeds swipes[0] with content/think/director ONLY, so for a
    re-rolled message the speaker of take 0 lives on the message and every
    later take carries its own. Reading either half alone is wrong about
    half the takes in any chat where somebody hit re-roll.
    """
    if (msg or {}).get("role") != "assistant":
        return None
    d = msg.get("data") or {}
    return (active_swipe(msg) or {}).get("speaker") or d.get("speaker")


def _aliases(member: dict) -> list:
    """Every name this character answers to, longest first.

    Tolerant of what a card happens to carry and inventing nothing: the
    roster name, the card's own name if it drifted from it, an explicit
    alias list, a nickname, and — only when the name is several words — the
    first word, because people type "Rin" at Rin Tohsaka. Ambiguity is safe
    here by construction: two present characters matching means rule 2 falls
    through rather than guessing.
    """
    char = (member or {}).get("char") or {}
    data = char.get("data") or {}
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    out = [char.get("name") or "", fields.get("name") or "",
           fields.get("nickname") or "", data.get("nickname") or ""]
    for extra in (data.get("aliases"), fields.get("aliases")):
        if isinstance(extra, list):
            out += [str(x) for x in extra]
        elif isinstance(extra, str):
            out += [p for p in re.split(r"[,\n]", extra)]
    for nm in list(out):
        parts = (nm or "").split()
        if len(parts) > 1 and len(parts[0]) >= 3:
            out.append(parts[0])
    seen, clean = set(), []
    for nm in out:
        nm = (nm or "").strip()
        if nm and nm.lower() not in seen:
            seen.add(nm.lower())
            clean.append(nm)
    return sorted(clean, key=len, reverse=True)


def _names_text(text: str, present: list, skip=()) -> list:
    """Which present characters this text names. Zero or several is a miss.

    Whole-word, because the alternative fires `Rem` on "remember" — measured
    on a real tag corpus, that class of false match is constant. The carve-out
    is the same one the lore matcher needs: `\\b` never matches BETWEEN two
    CJK characters (they are all `\\w`), so a whole-word pattern would make
    every Japanese or Chinese name permanently unmatchable. Whole-word applies
    only when the name starts and ends with an ASCII word character.
    """
    low = {s.lower() for s in skip if s}
    found = []
    for c in present:
        for nm in _aliases(c):
            if nm.lower() in low:
                continue
            # re.escape because cards really are named "Rin (twin)", and an
            # unescaped paren is a group rather than a name.
            pat = re.escape(nm)
            edge = nm[:1].isascii() and nm[:1].isalnum() \
                and nm[-1:].isascii() and nm[-1:].isalnum()
            if edge:
                pat = r"\b" + pat + r"\b"
            if re.search(pat, text or "", re.I):
                found.append(c)
                break
    return found


def cast_fairness(candidate, present: list, history: list) -> bool:
    """Should the floor be taken off whoever currently holds it?

    True only when BOTH halves hold: the candidate wrote the last
    CAST_STREAK assistant turns, AND somebody present has written none of the
    last CAST_STARVE_WINDOW x len(present). Two people going back and forth
    is a conversation, not unfairness, and the second half is what tells them
    apart.

    It can only ever force a swap. It never overrides an explicit pick or a
    direct address — that asymmetry is what keeps the human in charge.
    """
    if not candidate or len(present) < 2:
        return False
    ids = [take_speaker(m) for m in history if m.get("role") == "assistant"]
    tail = ids[-CAST_STREAK:]
    if len(tail) < CAST_STREAK or any(s != candidate for s in tail):
        return False
    window = ids[-(CAST_STARVE_WINDOW * len(present)):]
    return any(c["character_id"] not in window for c in present)


def pick_speaker(present: list, history: list, user_text: str = "",
                 forced_id=None, persona_name: str = "",
                 holding_id=None) -> tuple:
    """Who writes this turn, and why. Returns (member, reason).

    Strict priority, every branch named. `auto` that cannot explain itself
    reads as randomness and gets switched off, so the reason is not a debug
    aid — it is the feature. A user reads "asked directly" twice and has
    learnt the rule.

      you           the dropdown is not `auto`. The human always wins.
      same again    a re-roll keeps the take's own speaker. Without this,
                    re-rolling reassigns the bubble to whoever spoke BEFORE
                    it, because the regenerate branch truncates the history
                    above the take being replaced.
      asked directly  the text names exactly one present character.
      still answering the last speaker keeps the floor, unless starved out.
      her turn      least recently spoken, ties by `ord`.
      lead          nothing else applies, i.e. the first turn of a scene.
    """
    if not present:
        return None, ""
    by_id = {c["character_id"]: c for c in present}

    if forced_id and forced_id in by_id:
        return by_id[forced_id], "you"
    if holding_id and holding_id in by_id:
        return by_id[holding_id], "same again"

    # On a re-roll `text` is empty and the message that prompted the take is
    # the last user turn in the (already truncated) history. Reading only the
    # body means rule 2 can never fire on a re-roll even though the naming
    # message is sitting right there.
    text = user_text or ""
    if not text.strip():
        for m in reversed(history or []):
            if m.get("role") == "user":
                text = m.get("content") or ""
                break
    # The persona's name is excluded so a character who shares the player's
    # handle cannot make every single message ambiguous.
    named = _names_text(text, present, skip=(persona_name,))
    if len(named) == 1:
        return named[0], "asked directly"

    last = None
    for m in reversed(history or []):
        if m.get("role") == "assistant":
            last = take_speaker(m)
            break
    if last in by_id and not cast_fairness(last, present, history or []):
        return by_id[last], "still answering"

    spoken = [take_speaker(m) for m in (history or [])
              if m.get("role") == "assistant"]
    if any(spoken):
        def staleness(c):
            cid = c["character_id"]
            # Never spoken sorts first; otherwise longest since she last did.
            where = len(spoken) - 1 - spoken[::-1].index(cid) \
                if cid in spoken else -1
            return (where, c.get("ord", 0), cid)
        return min(present, key=staleness), "her turn"

    lead = next((c for c in present if c.get("lead")), present[0])
    return lead, "lead"


def clip_sentence(text: str, budget_tokens: int) -> str:
    """Cut a card to a token budget at a sentence boundary, and SAY so.

    A card that alone exceeds the entrance budget is truncated rather than
    dropped: half a description of somebody the model has never met still
    beats a one-line dossier, and both beat a silent no-op. The marker is the
    point — an invisible truncation is indistinguishable from a card that was
    always that short.
    """
    if rough_tokens(text) <= budget_tokens:
        return text
    room = max(80, budget_tokens * 4 - 24)
    cut = text[:room]
    stop = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("\n\n"))
    if stop > room // 3:
        cut = cut[:stop + 1]
    return cut.rstrip() + " … [description trimmed]"


def strip_speaker_prefix(text: str, names) -> str:
    """Remove a leading "Name:" that belongs to the transcript, not the prose.

    Name-prefixed history puts "<Speaker>: " into the prefill, and the server
    prepends the prefill to whatever came back — so without this every stored
    reply in a prefixed chat would begin with her own name, and the next turn
    would label it a second time. A model that types the prefix itself lands
    here too.

    Only a leading, KNOWN name is removed, which is what makes "12:30" and a
    line of dialogue containing a colon safe without a single special case.
    """
    t = (text or "").lstrip()
    for nm in names or []:
        if not nm:
            continue
        m = re.match(r"[ \t]*" + re.escape(nm) + r"[ \t]*:[ \t]*", t)
        if m:
            return t[m.end():].lstrip()
    return text


def trim_cast_leak(text: str, others) -> tuple:
    """Cut a reply where it starts writing somebody else's line.

    Returns (kept, name); name is "" when nothing leaked. The remainder is
    discarded by the caller on purpose — it was written with the wrong card in
    the prompt, which is precisely the merged output the cast exists to avoid,
    so keeping it would be keeping the bug.

    Only a name at the START of a line counts. "Rin nodded" is her describing
    Rin, which is the whole point of the dossier; "\\nRin:" is her taking over
    Rin's dialogue. A match at position 0 is left alone: trimming it would
    store an empty message, and a reply that is entirely somebody else's is a
    misrouted turn to re-roll, not a leak to repair.
    """
    best = None
    for nm in others or []:
        if not nm:
            continue
        for m in re.finditer(r"^[ \t]*" + re.escape(nm) + r"[ \t]*:",
                             text or "", re.M):
            if m.start() == 0:
                continue
            if best is None or m.start() < best[0]:
                best = (m.start(), nm)
            break
    if not best:
        return text, ""
    return text[:best[0]].rstrip(), best[1]


def add_message(conn, chat_id: int, role: str, content: str,
                data: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO messages (chat_id, role, content, data, created)"
        " VALUES (?,?,?,?,?)",
        (chat_id, role, content, json.dumps(data or {}), time.time()),
    )
    conn.execute("UPDATE chats SET updated=? WHERE id=?", (time.time(), chat_id))
    return cur.lastrowid


def get_messages(conn, chat_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d.get("data") or "{}")
        out.append(d)
    return out


def active_swipe(msg: dict) -> dict:
    """The take the user is looking at, as a dict with content/think/director.

    `messages.content` stays the canonical first take forever; alternatives
    live in data["swipes"] with data["swipe_index"] pointing at the active
    one. Every reader wants the same three lines, and they were copied in
    four places — one of which (the history budget) measured the original
    while a different one sent the swipe.
    """
    d = msg.get("data") or {}
    idx = d.get("swipe_index")
    swipes = d.get("swipes") or []
    if idx is not None and swipes:
        return swipes[min(idx, len(swipes) - 1)]
    return {"content": msg.get("content", ""),
            "think": d.get("think", ""), "director": d.get("director", "")}


def active_content(msg: dict) -> str:
    return active_swipe(msg).get("content", "")


def add_swipe(conn, message_id: int, content: str, data: dict | None = None) -> int:
    """Store an alternative generation for a message; returns swipe index.

    The FIRST call seeds swipes[0] with the message's original take. Without
    that the original was unreachable from any index forever, and one regen
    left a list of length 1 where both arrows clamped back to the same entry
    — which is what "the swipe arrow does nothing" actually was.

    After this, len(swipes) is the total number of variants, not the count
    of extras.
    """
    row = conn.execute("SELECT content, data FROM messages WHERE id=?",
                       (message_id,)).fetchone()
    d = json.loads(row["data"] or "{}") if row else {}
    swipes = d.setdefault("swipes", [])
    if not swipes and row:
        first = {"content": row["content"]}
        if d.get("think"):
            first["think"] = d["think"]
        if d.get("director"):
            first["director"] = d["director"]
        swipes.append(first)
    swipes.append({"content": content, **(data or {})})
    d["swipe_index"] = len(swipes) - 1
    conn.execute("UPDATE messages SET data=? WHERE id=?",
                 (json.dumps(d), message_id))
    return d["swipe_index"]


def set_swipe(conn, message_id: int, index: int):
    """Switch the active take. Returns (swipe_dict, index, total) or None.

    Returns the index it actually settled on, not the one asked for, so the
    caller can render a counter that matches the text on screen — the old
    version returned bare text and the client counted in an index space the
    backend did not implement, printing "2/2" over unchanged words.
    """
    row = conn.execute("SELECT data, content FROM messages WHERE id=?",
                       (message_id,)).fetchone()
    if not row:
        return None
    d = json.loads(row["data"] or "{}")
    swipes = d.get("swipes") or []
    if not swipes:
        return None
    index = max(0, min(index, len(swipes) - 1))
    d["swipe_index"] = index
    conn.execute("UPDATE messages SET data=? WHERE id=?",
                 (json.dumps(d), message_id))
    return swipes[index], index, len(swipes)


# ----------------------------------------------------------------------
# Context assembly
# ----------------------------------------------------------------------


def _lorebook_entries(fields: dict, history_text: str, budget: int = 1200) -> str:
    """Triggered character_book entries, keyword-matched, budget-capped."""
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


def build_system(fields: dict, persona: dict | None, jailbreak_text: str,
                 memories: list[str], lore_text: str,
                 post_history: str = "", scenario_override: str = "",
                 memory_header: str = "") -> str:
    parts = []
    if jailbreak_text:
        parts.append(jailbreak_text.strip())
    if fields.get("system_prompt"):
        parts.append(fields["system_prompt"].strip())
    if fields.get("description"):
        parts.append(fields["description"].strip())
    if fields.get("personality"):
        parts.append("Personality: " + fields["personality"].strip())
    # a forged scenario replaces the card's static one — that is the whole
    # point of the forge, otherwise the two descriptions fight each other
    if scenario_override:
        parts.append(scenario_override.strip())
    elif fields.get("scenario"):
        parts.append("Scenario: " + fields["scenario"].strip())
    if lore_text:
        parts.append("[World info]\n" + lore_text)
    if persona:
        desc = persona.get("data", {}).get("description", "")
        parts.append(f"[User persona: {persona.get('name','user')}]\n{desc}".strip())
    if memories:
        header = memory_header or (
            "[Memory — what you know about the user and your history together. "
            "(user) facts are about them generally; (character) facts are "
            "things between the two of you; (chat) facts are from this scene.]")
        parts.append(header + "\n" + "\n".join(f"- {m}" for m in memories))
    if post_history:
        parts.append(post_history.strip())
    return "\n\n".join(p for p in parts if p)



# Example dialogue is the strongest style lever a card has and CoomKit
# ignored it entirely: cards.py parsed it, the editor edited it, the forge
# read it, and build_system never once put it in the prompt.
#
# It goes in as *fake turns* rather than as a block of system text, because
# that is measurably stronger — the model sees the pattern in the position it
# is about to imitate. Two consequences have to be managed: it costs real
# tokens that would otherwise hold real history, and examples written for the
# card's original setup will drag her back toward it. Hence a budget cap and
# retirement once the actual scene is established.
EXAMPLE_CAP_TOKENS = 800
EXAMPLE_RETIRE_TOKENS = 3000

_START_RE = re.compile(r"^\s*<\s*start\s*>\s*$", re.I | re.M)


def parse_examples(text: str, char_name: str, user_name: str) -> list:
    """Turn a card's `mes_example` into alternating chat turns.

    The format is a convention rather than a spec — `<START>` separators are
    usual but optional, and the speaker labels may be the raw macros or the
    resolved names depending on when substitution happened. Anything that
    cannot be attributed is treated as her speaking, which is the safer guess
    on a field that exists to demonstrate her voice.
    """
    text = (text or "").strip()
    if not text:
        return []
    char_l, user_l = (char_name or "").lower(), (user_name or "").lower()
    char_keys = {char_l, "{{char}}", "<bot>", "char", "assistant"}
    user_keys = {user_l, "{{user}}", "<user>", "user", "you"}

    out = []
    for block in _START_RE.split(text):
        block = block.strip()
        if not block:
            continue
        role, buf = None, []

        def flush():
            if buf:
                body = "\n".join(buf).strip()
                if body:
                    out.append({"role": role or "assistant", "content": body})

        for line in block.splitlines():
            label, sep, rest = line.partition(":")
            key = label.strip().lower().strip("*_ ")
            if sep and key in char_keys:
                flush()
                role, buf = "assistant", [rest.strip()]
            elif sep and key in user_keys:
                flush()
                role, buf = "user", [rest.strip()]
            else:
                buf.append(line)
        flush()
    return [m for m in out if m["content"]]


def build_examples(text: str, char_name: str, user_name: str, header: str,
                   cap_tokens: int = EXAMPLE_CAP_TOKENS) -> list:
    """Example turns, trimmed to a budget, behind a system note.

    The note matters: without it the model reads the examples as things that
    already happened and answers the last one.
    """
    turns = parse_examples(text, char_name, user_name)
    if not turns:
        return []
    kept, used = [], 0
    for m in turns:
        cost = rough_tokens(m["content"])
        if used + cost > cap_tokens and kept:
            break
        kept.append(m)
        used += cost
    # Never end on the user's line — that reads as a question she left hanging.
    while kept and kept[-1]["role"] == "user":
        kept.pop()
    if not kept:
        return []
    return [{"role": "system", "content": header}] + kept


CAST_LINE_CHARS = 140      # ~35 tokens each
CAST_CAP_TOKENS = 300      # all dossiers together; drop the tail if over

# An entrance: the first time the model meets somebody, she gets a real card
# instead of a 41-token dossier. Derived from the window rather than fixed,
# because the measured cost is not small — card_text is 303-305 tokens against
# dossier_line's 41, and a three-hander with both others new would spend ~1235
# of an 8192 window before jailbreak, persona, examples, memory or lore.
CAST_ENTRY_FRACTION = 0.08     # of context_tokens
CAST_ENTRY_MIN_CONTEXT = 12000  # below this, nobody is promoted at all
CAST_ENTRY_MAX = 2             # per turn


def dossier_line(name: str, fields: dict, note: str = "") -> str:
    """One line for someone in the room who is not the one speaking.

    The hole in swapping cards is that the speaker has never read the other
    character's card and so cannot describe her. A dossier fixes that without
    buying the merge that SillyTavern's append mode causes — its own docs warn
    that concatenating N descriptions produces "merged personalities".
    """
    who = (fields.get("description") or fields.get("personality") or "").strip()
    who = " ".join(who.split())[:CAST_LINE_CHARS].rstrip()
    bits = [b for b in (who, (note or "").strip()) if b]
    return f"- {name}: " + " — ".join(bits)


def card_text(fields: dict, name: str = "") -> str:
    """The card as one block: system prompt, description, personality, scenario."""
    parts = []
    # Headed only when more than one person is in the prompt. A nameless
    # "Personality: bratty, hostile" sitting next to a name-headed dossier
    # binds to the wrong woman. Default "" keeps the solo prompt byte-identical.
    if name:
        parts.append(f"[{name}]")
    for key, prefix in (("system_prompt", ""), ("description", ""),
                        ("personality", "Personality: "),
                        ("scenario", "Scenario: ")):
        val = (fields.get(key) or "").strip()
        if val:
            parts.append(prefix + val)
    return "\n\n".join(parts)


def persona_text(persona) -> str:
    if not persona:
        return ""
    desc = (persona.get("data", {}).get("description") or "").strip()
    return f"[User persona: {persona.get('name', 'user')}]\n{desc}".strip()


def memory_text(memories: list, header: str) -> str:
    if not memories:
        return ""
    return (header or "[Memory]") + "\n" + "\n".join(f"- {m}" for m in memories)


def assemble_blocks(chat: dict, char: dict, persona, preset: dict,
                    block_list: list, memories: list, history: list,
                    layers: dict = None, context_tokens: int = 8192,
                    memory_header: str = "", examples_header: str = "",
                    model: str = "", remote: bool = False,
                    regex=None, trace=None, cast=None, speaker_id=None,
                    books=None, lore_tokens=0) -> tuple:
    """Assemble from an ordered block list. Returns (messages, prefill).

    Pass a dict as `trace` and it comes back holding `segments`: which block
    produced each stretch of the prompt, for the inspector. It is a SIDE
    CHANNEL on purpose. The returned messages must stay reducible to
    role+content, because that is the guarantee the per-message gallery rests
    on and `tests/test_gallery.py` asserts it structurally — attaching
    provenance to the messages themselves broke that test the moment it was
    tried, which is the test doing its job.

    The division of labour: the *server* decides whether a layer has content
    this turn (is the director bar open, is this an SMS chat, are tools on),
    and the *blocks* decide where that content goes and in whose voice. So
    conditional logic stays where it already lived and ordering becomes data.

    An empty layer contributes nothing, exactly as if its block were off.
    """
    layers = layers or {}
    # A cast of one is not a cast. Everything below this branch is the path
    # every existing chat has always taken, unchanged and byte-identical —
    # `multi` is the single switch, decided once, so the branches cannot
    # disagree about whether they apply.
    present = [c for c in (cast or []) if c.get("present") and c.get("char")]
    multi = len(present) > 1
    others = []
    if multi:
        speaker = next((c for c in present
                        if c["character_id"] == speaker_id), present[0])
        others = [c for c in present if c is not speaker]
        # The speaker owns the turn: her card, her name for {{char}}, her
        # post-history instruction, her examples. Everyone else is a dossier.
        char = speaker["char"]

    raw_fields = char["data"].get("fields", {})
    pdata = preset.get("data", {})
    max_reply = pdata.get("samplers", {}).get("max_tokens", 512)

    char_name = char.get("name") or raw_fields.get("name") or "the character"
    user_name = (persona or {}).get("name") or "anon"
    persona_desc = (persona or {}).get("data", {}).get("description", "")
    seed = str(chat.get("id") or "")
    fields = macros.expand_fields(raw_fields, char_name, user_name,
                                  persona_desc, seed)

    def mx(text):
        return macros.expand(text, char_name, user_name, raw_fields,
                             persona_desc, seed)

    # Macros first, then regex, and the result is never written back. Order
    # matters: a rule written against "{{user}}" would never match resolved
    # text, and a rule that matched the stored text would need the stored text
    # rewritten — which is the thing the whole late-resolution design exists
    # to avoid. `regex` is a callable the server passes in (text, depth) so
    # engine keeps no opinion about where the rules live.
    # The active swipe has to win BEFORE macros and regex run, or a
    # regenerated take reaches the model with literal {{user}} in it and the
    # history budget prices a take nobody is looking at.
    history = [{**m, "content": active_content(m)} for m in history]
    history = [{**m, "content": mx(m["content"])} for m in history]
    if regex:
        last = len(history) - 1
        history = [{**m, "content": regex(m["content"], last - i)}
                   for i, m in enumerate(history)]
    history_text = "\n".join(m["content"] for m in history[-20:])

    chat_data = chat.get("data")
    if isinstance(chat_data, str):
        try:
            chat_data = json.loads(chat_data or "{}")
        except json.JSONDecodeError:
            chat_data = {}
    chat_data = chat_data or {}

    # A forged scenario replaces the card's static one — they contradict each
    # other otherwise, which is the whole reason the forge exists.
    scen = chat_data.get("scenario")
    if isinstance(scen, dict):
        lines = []
        for key, prefix in (("title", "Scene: "), ("setting", "Setting: "),
                            ("premise", ""), ("hook", "Tension: ")):
            if scen.get(key):
                lines.append(prefix + scen[key])
        fields = {**fields, "scenario": mx("\n".join(lines))}

    examples = []
    if chat_data.get("examples", True) and fields.get("mes_example"):
        hist_tokens = sum(rough_tokens(m["content"]) for m in history)
        if hist_tokens < EXAMPLE_RETIRE_TOKENS:
            examples = build_examples(
                fields["mes_example"], char_name, user_name,
                examples_header or f"[Example dialogue — how {char_name} speaks.]")

    # The speaker's full card goes LAST on purpose. llama.cpp and LM Studio
    # cache the prompt prefix, and her card is the only part that changes when
    # the turn passes to someone else — everything above the swap point stays
    # cached and only the tail reprocesses. Ordering is free; a second full
    # card is not.
    if multi:
        card_slot = []
        if layers.get("cast_present"):
            card_slot.append({"content": layers["cast_present"],
                              "src": {"id": "card", "name": "who is here",
                                      "builtin": True, "layer": "cast_present",
                                      "marker": "card"}})
        spent = 0
        for c in others:
            oname = c["char"].get("name") or "someone"
            line = dossier_line(oname, c["char"]["data"].get("fields", {}),
                                c.get("note", ""))
            spent += rough_tokens(line)
            if spent > CAST_CAP_TOKENS:
                break
            card_slot.append({"content": line,
                              "src": {"id": "card", "name": f"{oname} (present)",
                                      "builtin": True, "layer": "",
                                      "marker": "card"}})
        card_slot.append({"content": card_text(fields, name=char_name),
                          "src": {"id": "card", "name": f"{char_name}'s card",
                                  "builtin": True, "layer": "", "marker": "card"}})
    # ── lore ──────────────────────────────────────────────────────────
    # `books=None` is EXACTLY today's behaviour: the card's own
    # `character_book` through the legacy adapter, which tests/test_lore.py
    # proves byte-identical against the old matcher. A caller that passes
    # books has resolved them itself and the embedded one is included there.
    lore_books = list(books) if books is not None else [lore.from_card(raw_fields)]
    lore_report = {}
    lore_slot = lore.select(lore_books, [m["content"] for m in history],
                            {"budget": lore_tokens or lore.DEFAULT_BUDGET},
                            expand=mx, report=lore_report)
    if trace is not None:
        trace["lore"] = lore_report
    # The header is gated on a STORED book having fired, not on anything
    # firing. Today lore reaches the model with no header at all, so emitting
    # one unconditionally would add a paragraph to every existing chat with an
    # embedded book — and cast_baseline.json would stay green only because its
    # fixture card has no character_book, which is the test not covering it
    # rather than the change being safe. A legacy-only chat stays byte-identical.
    stored = {b.get("id") for b in lore_books if b.get("id")}
    if lore_slot and layers.get("lore_header") and any(
            str(x["src"]["id"]).split(":")[1:2] and
            int(str(x["src"]["id"]).split(":")[1]) in stored
            for x in lore_slot):
        lore_slot.insert(0, {"content": layers["lore_header"],
                             "src": {"id": "lore", "marker": "lore",
                                     "builtin": True, "layer": "lore_header",
                                     "name": "world info header"}})
    # A list slot collapses into ONE message only when the block's role is
    # system, because `blocks.squash` merges adjacent system messages and
    # nothing else. A user who moved their lore block to role:user would
    # otherwise get N separate user messages where they used to get one, so
    # hand that case a joined string instead. Measured: for role:system the
    # list and the string render byte-identically.
    lore_role = next((b.get("role") for b in block_list
                      if b.get("marker") == "lore"), "system")
    if lore_role != "system":
        lore_slot = "\n\n".join(x["content"] for x in lore_slot)

    slots = {
        "card": card_slot if multi else card_text(fields),
        "persona": persona_text(persona),
        "lore": lore_slot,
        "memory": memory_text(memories, memory_header),
        "examples": examples,
        "tools": layers.get("tools", ""),
        "rp": layers.get("rp", ""),
    }

    # Fill each built-in text block from its layer, so the server's
    # conditional logic stays in one place and this stays pure ordering.
    resolved = []
    for b in block_list:
        key = b.get("layer")
        if key == "__jailbreak__":
            b = {**b, "content": layers.get("jailbreak", "")}
        elif key == "__post_history__":
            b = {**b, "content": fields.get("post_history_instructions", "")}
        elif key:
            b = {**b, "content": layers.get(key, "")}
        resolved.append(b)

    # Budget: everything that is not history, then history gets the rest.
    probe, _ = blocks.render(resolved, {**slots, "history": []}, model, remote)
    overhead = sum(rough_tokens(m["content"]) for m in probe)
    # Entrance cards are chosen from the RETAINED history, which does not
    # exist yet — so the budget is RESERVED here, before history is chosen,
    # and spent afterwards. Reserving the whole allowance and then promoting
    # nobody costs a little history nobody will notice; promoting first and
    # budgeting second would overflow the window by up to two full cards.
    entry_budget = (int(context_tokens * CAST_ENTRY_FRACTION)
                    if multi and context_tokens >= CAST_ENTRY_MIN_CONTEXT
                    else 0)
    hist_budget = max(500, context_tokens - overhead - max_reply - 200
                      - entry_budget)

    # Every character whose lines could be in this log, present or not — an
    # absent guest's turns are still in the history and still hers.
    names_by_id = ({c["character_id"]: (c["char"].get("name") or "")
                    for c in (cast or []) if c.get("char")} if multi else {})
    lead_name = ""
    if multi:
        lead = next((c for c in (cast or []) if c.get("lead")), None)
        lead_name = ((lead or {}).get("char") or {}).get("name") or ""

    def _label(m):
        """Whose name heads this turn in the transcript.

        An UNSTAMPED assistant turn is provably the lead's: `data["speaker"]`
        is written only when the scene is multi, so no stamp means the chat
        was one-to-one when that line was written, and the only person who
        could have written it is the lead. That covers the card's greeting —
        which is never stamped, and which would otherwise keep prefixing
        switched off for the entire life of every chat that has one.

        A stamp naming somebody who is not in the cast at all is different:
        she was removed outright, the name is unrecoverable, and guessing
        would misattribute her lines. That returns None and shuts the gate.
        """
        sid = take_speaker(m)
        if sid is None:
            return lead_name or None
        return names_by_id.get(sid)

    kept, used = [], 0
    for m in reversed(history):
        cost = rough_tokens(m["content"])
        if multi and m["role"] == "assistant":
            # Price the "Name: " we may be about to add. Charging it up front
            # is what stops a long log overflowing by exactly the prefixes;
            # if the gate below then turns prefixing off we have simply kept a
            # few tokens less history, which is invisible.
            cost += rough_tokens((_label(m) or "") + ": ")
        if used + cost > hist_budget and kept:
            break
        kept.append(m)
        used += cost
    kept.reverse()

    # Name-prefixed history — SillyTavern's `names_behavior: content`, and the
    # only way the model can tell two voices apart in a transcript that is
    # otherwise an undifferentiated run of assistant turns.
    #
    # THE GATE: every assistant turn in the RETAINED history must be
    # attributable. A half-labelled log reads as inconsistent to the model and
    # is worse than no labels at all, so one turn nobody can name switches the
    # whole thing off. Measured against `kept` and not the whole log, so a
    # chat carrying one unnameable ancient turn starts working by itself once
    # that turn falls out of the budget.
    said = [m for m in kept if m["role"] == "assistant"]
    prefix_names = bool(multi and said and all(_label(m) for m in said))

    turns = []
    for m in kept:
        role = "assistant" if m["role"] == "assistant" else "user"
        content = m["content"]
        if prefix_names and role == "assistant":
            content = f"{_label(m)}: {content}"
        turns.append({"role": role, "content": content})

    # Who the model has not seen speak yet in the history it is actually
    # being given. Measured against `kept` and not the whole log, so it also
    # covers somebody whose old lines have fallen out of the budget — and it
    # decays by itself, storing nothing, which a `chat_cast.since` column
    # could not do because it cannot see the budget.
    if entry_budget and others:
        seen = {take_speaker(m) for m in kept if m["role"] == "assistant"}
        newcomers = [c for c in others if c["character_id"] not in seen]
        spend, promoted = 0, []
        for c in newcomers[:CAST_ENTRY_MAX]:
            cname = c["char"].get("name") or "someone"
            body = card_text(macros.expand_fields(
                c["char"]["data"].get("fields", {}), cname, user_name,
                persona_desc, seed), name=cname)
            left = entry_budget - spend
            if left <= 0:
                break
            body = clip_sentence(body, left)
            spend += rough_tokens(body)
            promoted.append((c, cname, body))
        if promoted:
            # Rebuilt rather than patched, because a promoted character must
            # lose her dossier — carrying both is the append-mode merge that
            # this whole design exists to avoid.
            up = {c["character_id"] for c, _n, _b in promoted}
            head = []
            if layers.get("cast_present"):
                head.append(card_slot[0])
            if layers.get("cast_entered"):
                # The server leaves {names} unfilled because only this knows
                # who is actually new. Naming everyone present would announce
                # a character the model has been writing all evening.
                intro = layers["cast_entered"].replace(
                    "{names}", ", ".join(n for _c, n, _b in promoted))
                head.append({"content": intro,
                             "src": {"id": "card", "name": "someone new",
                                     "builtin": True, "layer": "cast_entered",
                                     "marker": "card"}})
            for _c, cname, body in promoted:
                head.append({"content": body,
                             "src": {"id": "card", "name": f"{cname} (new here)",
                                     "builtin": True, "layer": "",
                                     "marker": "card"}})
            spent2 = 0
            for c in others:
                if c["character_id"] in up:
                    continue
                oname = c["char"].get("name") or "someone"
                line = dossier_line(oname,
                                    c["char"]["data"].get("fields", {}),
                                    c.get("note", ""))
                spent2 += rough_tokens(line)
                if spent2 > CAST_CAP_TOKENS:
                    break
                head.append({"content": line,
                             "src": {"id": "card",
                                     "name": f"{oname} (present)",
                                     "builtin": True, "layer": "",
                                     "marker": "card"}})
            head.append(card_slot[-1])      # the speaker's card stays LAST
            card_slot = head
            slots["card"] = card_slot

    if prefix_names and layers.get("cast_names"):
        # Placed by the engine and not the server, unusually, because the
        # condition is only knowable after the history budget has run. The
        # server still owns the TEXT; this owns where it goes.
        card_slot.insert(1 if layers.get("cast_present") else 0,
                         {"content": layers["cast_names"],
                          "src": {"id": "card", "name": "reading the log",
                                  "builtin": True, "layer": "cast_names",
                                  "marker": "card"}})

    slots["history"] = turns
    messages, depth_items = blocks.render(resolved, slots, model, remote)
    messages = blocks.apply_depth(messages, depth_items)
    messages = blocks.squash(messages)
    if not messages or messages[0]["role"] != "system":
        messages.insert(0, {"role": "system", "content": ""})

    if trace is not None:
        # Where the CURRENT user turn landed, for the vision inline. Decided
        # here and not in the server, because depth-0 blocks (a card's
        # post_history_instructions, cast_turn, an ST-imported injection)
        # legally render AFTER it — so messages[-1] is a system message for
        # exactly the cards people import, and a backwards role scan from the
        # server can land on an imported block that carries role:user. The
        # history marker is the identity that cannot be faked; example turns
        # are user-role too but carry marker "examples".
        trace["last_user_idx"] = next(
            (i for i in range(len(messages) - 1, -1, -1)
             if messages[i]["role"] == "user"
             and ((messages[i].get("src") or {}).get("marker") == "history"
                  or any(p.get("marker") == "history"
                         for p in messages[i].get("parts") or []))),
            None)
        trace["segments"] = [
            {"role": m["role"],
             "parts": m.get("parts")
                      or ([{**m["src"], "content": m["content"]}]
                          if m.get("src") else [])}
            for m in messages]
    # Off the messages and into the side channel: what goes onward is
    # role+content and nothing else.
    messages = [{k: v for k, v in m.items() if k not in ("src", "parts")}
                for m in messages]
    # Local backends genuinely continue a trailing assistant turn, so putting
    # "Rin: " in the prefill puts the model inside her line before it writes a
    # token. Handed back separately as well, because the server may replace
    # the preset prefill with the user's reply_prefill and the name has to
    # compose AHEAD of whatever wins.
    prefix = f"{char_name}: " if prefix_names else ""
    if trace is not None:
        trace["speaker_prefix"] = prefix
    return messages, prefix + pdata.get("prefill", "")
