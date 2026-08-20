#!/usr/bin/env python3
"""CoomKit persistent memory — extraction, scoping, injection.

Three scopes, deliberately separate (the Hermes / Claude-profile model):

  user      — durable facts about the person at the keyboard. Shared across
              every character and every chat. "Prefers being praised", "has a
              cat named Widget". Survives forever until edited.
  character — the relationship with one specific character, across all chats
              with her. "They slept together in the observatory", "she calls
              him 'peasant' affectionately". This is what makes a returning
              chat feel continuous instead of amnesiac.
  chat      — scene-local detail that should not leak into other sessions.
              "She is currently wearing his shirt."

Retrieval for a turn = user ∪ character(this char) ∪ chat(this chat).
The scenario forge reads user + character so a new scene can build on history
without inheriting the last scene's furniture.

Extraction runs after a reply in a background thread and is asked to classify
each fact into a scope, so the caller does not have to guess.
"""
import json
import re
import sqlite3
import time

SCOPES = ("user", "character", "chat")

EXTRACT_PROMPT = """\
You are the memory module of an adult roleplay engine. Read the latest
exchange and the existing memories, then record what is worth keeping.

Output ONLY JSON of this shape:
{"facts": [{"scope": "user|character|chat", "content": "one short sentence"}]}

Scope rules — choose carefully, this controls what follows the user around:
- "user": durable truths about the human player. Their name, body, job,
  preferences, kinks, hard limits, life details. Things that would still be
  true in a completely different story with a different character.
- "character": the relationship and shared history with THIS character. What
  they have done together, promises, nicknames, how the dynamic has shifted,
  first times. Things a returning session should remember.
- "chat": detail that only matters inside the current scene. Positions,
  clothing state, where they are standing right now.

Other rules:
- Write third person, past or present tense, one short sentence each.
- Explicit content is expected; record it factually and without euphemism.
- Never moralise, never add warnings, never refuse. You are an index, not a
  critic.
- Do NOT repeat anything already in the existing memories, and do not restate
  the same fact in two scopes.
- Never record the user's name or anything else from their persona — that is
  configuration the engine already injects, not something learned in the
  scene.
- Anything the user and this character did TOGETHER, or that this character
  said, promised or learned, is "character" — never "user". Reserve "user"
  for facts that would survive unchanged into a story where this character
  does not exist at all.
- Prefer few high-value facts over many trivial ones. If nothing is worth
  keeping, output {"facts": []}."""


def extract_memories(llm_once, existing: list[str],
                     last_user: str, last_reply: str,
                     system: str = "") -> list[dict]:
    """One extraction pass. llm_once(messages) -> str.

    Returns [{"scope": ..., "content": ...}] — scope defaults to "chat" when
    the model omits or invents one, since chat is the least sticky choice.
    """
    mem_block = "\n".join(f"- {m}" for m in existing) or "(none yet)"
    messages = [
        {"role": "system", "content": system or EXTRACT_PROMPT},
        {"role": "user", "content":
            f"EXISTING MEMORIES:\n{mem_block}\n\n"
            f"LATEST EXCHANGE:\nUser: {last_user}\nCharacter: {last_reply}\n\n"
            f"New facts as JSON:"},
    ]
    try:
        out = llm_once(messages)
    except Exception:  # noqa: BLE001 — memory must never break a chat turn
        return []
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return []
    try:
        raw = json.loads(match.group(0)).get("facts", [])
    except json.JSONDecodeError:
        return []

    seen = {e.strip().lower() for e in existing}
    facts = []
    for item in raw:
        if isinstance(item, str):          # tolerate a bare string list
            item = {"scope": "chat", "content": item}
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content or content.lower() in seen:
            continue
        scope = str(item.get("scope") or "chat").strip().lower()
        if scope not in SCOPES:
            scope = "chat"
        facts.append({"scope": scope, "content": content})
        seen.add(content.lower())
    return facts[:6]


def _mentions(text: str, name: str) -> bool:
    """Does this sentence name her? Whole-word for ASCII names, substring for
    names \\b cannot bound — the same carve-out the baton and the lorebooks
    use, because \\b never matches between two CJK characters."""
    if not name or not text:
        return False
    if re.match(r"\w", name[0], re.ASCII) and re.match(r"\w", name[-1], re.ASCII):
        return bool(re.search(r"\b" + re.escape(name) + r"\b", text, re.I))
    return name.lower() in text.lower()


def persona_known(persona_name: str = "", persona_desc: str = "") -> list[str]:
    """What the persona already told the model, phrased as memory lines.

    Handed to the extractor as existing knowledge: the model 'discovers' the
    user's name in a reply because the persona block put it in the prompt, and
    without this it records that discovery as a memory — so a brand-new
    character greets the player by name before any introduction has happened.
    """
    known = []
    if persona_name and persona_name.strip().lower() not in ("anon", "user",
                                                             "you", ""):
        known.append(f"The user's name is {persona_name.strip()}.")
    for sent in re.split(r"(?<=[.!?])\s+", persona_desc or ""):
        sent = sent.strip()
        if len(sent) > 8:
            known.append(sent)
    return known[:12]


# "is called X" / "goes by X" / "X is her name" — the shapes a model reaches
# for when it writes the name fact down. Symmetric Jaccard misses these
# because 'name' and 'called' stem apart, so the name gets its own pattern.
_NAMING = re.compile(r"\b(names?|named|calls?|called|goes by|known as)\b",
                     re.IGNORECASE)


def sanitize_facts(facts: list, persona_name: str = "", persona_desc: str = "",
                   char_name: str = "", threshold: float = 0.6) -> list:
    """Belt to the extractor prompt's braces. Two real failures:

    - A "user" fact that restates the persona (the name, most of all) is
      DROPPED: it was read out of the model's own system prompt, not learned,
      and stored it makes every other character psychic.
    - A "user" fact that names this character is DEMOTED to character scope:
      "the user likes being teased by Mika" following the player into every
      other woman's chat is the leak the scopes exist to prevent.

    Restatement is measured by CONTAINMENT (how much of the fact is already in
    a persona sentence), not symmetric similarity — "the user is a tall
    engineer" is fully contained in a longer persona line yet Jaccard scores
    it 0.5 and keeps it. `threshold` is kept for the caller's config plumbing
    but containment uses its own, stricter bar.
    """
    known = persona_known(persona_name, persona_desc)
    known_words = [_words(k) for k in known]
    out = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("scope") == "user":
            content = f.get("content") or ""
            # The name fact, in any of its phrasings.
            if persona_name and _mentions(content, persona_name) \
                    and _NAMING.search(content):
                continue
            words = _words(content)
            if words and any(kw and len(words & kw) / len(words) >= 0.8
                             for kw in known_words):
                continue
            if char_name and _mentions(content, char_name):
                f = {**f, "scope": "character"}
        out.append(f)
    return out


def attribute_facts(facts: list, members: list) -> tuple:
    """Split character-scope facts among the cast members they unambiguously
    name. Returns ({character_id: [facts]}, leftovers).

    `members` is [(id, name)]. Two names or zero is ambiguity, and ambiguity
    goes to the leftovers for the caller's default (the lead) — the same
    fall-through the baton uses, because guessing wrong files one woman's
    history under another, which is worse than filing it under the lead.
    """
    buckets, rest = {}, []
    for f in facts:
        if isinstance(f, dict) and f.get("scope") == "character":
            hits = {cid for cid, nm in members
                    if nm and _mentions(f.get("content") or "", nm)}
            if len(hits) == 1:
                buckets.setdefault(hits.pop(), []).append(f)
                continue
        rest.append(f)
    return buckets, rest


def rescope_user_facts(conn: sqlite3.Connection, characters: list) -> int:
    """Repair pass for rows written before sanitize_facts existed: a user-scope
    row that is visibly about exactly ONE known character becomes a character
    memory of hers. Two matches is ambiguity, and ambiguity is left alone —
    the same fall-through the baton uses.

    `characters` is [(id, name)]."""
    moved = 0
    for row in conn.execute(
            "SELECT id, content FROM memories WHERE kind='user'").fetchall():
        hits = {cid for cid, nm in characters
                if nm and _mentions(row["content"], nm)}
        if len(hits) == 1:
            conn.execute(
                "UPDATE memories SET kind='character', character_id=?,"
                " chat_id=NULL WHERE id=?", (hits.pop(), row["id"]))
            moved += 1
    return moved


def store_memories(conn: sqlite3.Connection, chat_id: int,
                   character_id: int, facts: list,
                   threshold: float = 0.6) -> int:
    """Persist scoped facts, skipping anything already known.

    The duplicate check happens HERE, against the database, and not against a
    snapshot the caller read earlier. Extraction runs in a background thread
    per turn, so two turns routinely raced: both read the existing set, both
    decided "the user is called anon" was new, both stored it. That is how a
    155-message log ended up with byte-identical memories.

    A repeat is not discarded silently — it bumps the existing row's
    `updated`, which is what keeps a re-confirmed fact ranked above a stale
    one.
    """
    now = time.time()
    count = 0
    for f in facts:
        if isinstance(f, str):
            f = {"scope": "chat", "content": f}
        scope = f.get("scope", "chat")
        if scope not in SCOPES:
            scope = "chat"
        content = (f.get("content") or "").strip()
        if not content:
            continue
        # a user-scope fact is not tied to a chat or a character
        row_chat = None if scope == "user" else chat_id
        row_char = None if scope == "user" else character_id
        if scope == "character":
            row_chat = None          # spans every chat with her

        dupe = find_duplicate(conn, scope, content, chat_id, character_id,
                              threshold)
        if dupe:
            conn.execute("UPDATE memories SET updated=? WHERE id=?",
                         (now, dupe))
            continue
        conn.execute(
            "INSERT INTO memories (chat_id, character_id, kind, content,"
            " created, updated) VALUES (?,?,?,?,?,?)",
            (row_chat, row_char, scope, content, now, now),
        )
        count += 1
    return count


def for_turn(conn: sqlite3.Connection, chat_id: int, character_id: int,
             recent_text: str = "", limits: dict = None) -> list[dict]:
    """What one generation should actually see.

    Everything in scope is user ∪ character(this char) ∪ chat(this chat) — but
    "everything" was the bug. Unbounded, that reached 897 tokens on every
    single turn and grew. Pass `limits` to rank by relevance and stop at a
    ceiling; pass nothing and you get the full set, which is what the memory
    panel and the forge want.
    """
    rows = conn.execute(
        "SELECT * FROM memories WHERE kind='user'"
        " OR (kind='character' AND character_id=?)"
        " OR (kind='chat' AND chat_id=?)"
        " ORDER BY CASE kind WHEN 'user' THEN 0 WHEN 'character' THEN 1"
        " ELSE 2 END, id",
        (character_id, chat_id),
    ).fetchall()
    out = [dict(r) for r in rows]
    if not limits:
        return out
    return budget(rank(out, recent_text),
                  limits.get("max_injected", 20),
                  limits.get("token_budget", 500),
                  limits.get("chat_keep", 6))


def for_scenario(conn: sqlite3.Connection, character_id: int) -> list[dict]:
    """What the scenario forge may draw on: who the user is, and their history
    with this character. Deliberately excludes chat-scope scene furniture."""
    rows = conn.execute(
        "SELECT * FROM memories WHERE kind='user'"
        " OR (kind='character' AND character_id=?)"
        " ORDER BY CASE kind WHEN 'user' THEN 0 ELSE 1 END, id",
        (character_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_memories(conn: sqlite3.Connection, chat_id: int,
                 character_id: int | None = None) -> list[dict]:
    """For the UI panel — same set the next turn will actually see."""
    if character_id is None:
        row = conn.execute("SELECT character_id FROM chats WHERE id=?",
                           (chat_id,)).fetchone()
        character_id = row["character_id"] if row else None
    return for_turn(conn, chat_id, character_id or 0)


def upsert(conn: sqlite3.Connection, mem_id: int | None, scope: str,
           content: str, chat_id: int | None,
           character_id: int | None) -> int:
    """Manual add/edit from the UI. Users own their profile."""
    if scope not in SCOPES:
        scope = "chat"
    now = time.time()
    if scope == "user":
        chat_id = character_id = None
    elif scope == "character":
        chat_id = None
    if mem_id:
        conn.execute(
            "UPDATE memories SET kind=?, content=?, chat_id=?, character_id=?,"
            " updated=? WHERE id=?",
            (scope, content, chat_id, character_id, now, mem_id))
        return mem_id
    # Dedup on the manual path too. store_memories has always done this, and
    # the reason it does — "the duplicate check happens HERE, against the
    # database" — applies just as much to a route anything can POST to.
    # Without it this endpoint could stack byte-identical rows without limit,
    # and because injection is ranked and capped, twenty copies of one fact
    # crowd every other memory out of the prompt entirely.
    dup = find_duplicate(conn, scope, content, chat_id, character_id)
    if dup:
        conn.execute("UPDATE memories SET updated=? WHERE id=?", (now, dup))
        return dup
    cur = conn.execute(
        "INSERT INTO memories (chat_id, character_id, kind, content, created,"
        " updated) VALUES (?,?,?,?,?,?)",
        (chat_id, character_id, scope, content, now, now))
    return cur.lastrowid

# --------------------------------------------------------------------------
# Lifecycle: how often to extract, what to keep, what to inject
# --------------------------------------------------------------------------
# The first version extracted after every single reply, stored whatever came
# back, and injected all of it forever. Measured on a real 155-message log
# that produced 53 memories — 897 tokens injected on every turn, 21
# near-duplicate pairs, and several byte-identical ones.
#
# Three separate faults, fixed separately below:
#   * a *race* — each turn spawned a background extractor that read the
#     existing set before the previous one had written, so two turns happily
#     stored the same sentence. Dedup has to happen at write time, against
#     the database, not against a snapshot read earlier.
#   * no *similarity* check — "she likes praise" and "the user enjoys being
#     praised" are one fact.
#   * no *ceiling* — every memory was injected on every turn regardless of
#     relevance, so the block grew without bound.

DEFAULTS = {
    "every_n_turns": 4,      # extract on every Nth reply, not all of them
    "max_injected": 20,      # hard cap on memories in the prompt
    "token_budget": 500,     # ...and on their combined size
    "chat_keep": 6,          # scene furniture is volatile; keep the freshest
    "dupe_threshold": 0.6,   # Jaccard over content words
    "consolidate_at": 40,    # merge a scope once it gets this fat
}

STOPWORDS = frozenset("""
the a an and or but if then than that this these those is are was were be been
being has have had do does did will would shall should can could may might
must of in on at to for with from by as into about over after before between
their there they them then when while who whom which what her his its it he
she user character not no yes very really just also more most some any each
""".split())


def settings(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update((cfg or {}).get("memory") or {})
    return out


def _stem(word: str) -> str:
    """Crude suffix stripping so `praise`/`praised`/`praising` are one word.

    Not linguistics — just enough that a model restating a fact in a slightly
    different tense is recognised as the same fact, which is the actual
    observed failure.
    """
    for suffix in ("ing", "edly", "ed", "es", "s", "ly"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    # ...and a trailing 'e', so praise/praised collapse to the same stem.
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def _words(text: str) -> frozenset:
    return frozenset(_stem(w) for w in re.findall(r"[a-z']+", (text or "").lower())
                     if len(w) > 2 and w not in STOPWORDS)


def similarity(a: str, b: str) -> float:
    """Jaccard over content words. Cheap, stdlib, good enough for sentences."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 1.0 if a.strip().lower() == b.strip().lower() else 0.0
    return len(wa & wb) / len(wa | wb)


def find_duplicate(conn: sqlite3.Connection, scope: str, content: str,
                   chat_id, character_id, threshold: float = 0.6):
    """An existing row saying the same thing, or None.

    Scoped to the same bucket — the same sentence at user scope and chat
    scope are genuinely different claims about how durable it is.
    """
    if scope == "user":
        rows = conn.execute("SELECT id, content FROM memories WHERE kind='user'")
    elif scope == "character":
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE kind='character'"
            " AND character_id=?", (character_id,))
    else:
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE kind='chat' AND chat_id=?",
            (chat_id,))
    for row in rows.fetchall():
        if similarity(row["content"], content) >= threshold:
            return row["id"]
    return None


def should_extract(turn_index: int, every_n: int = 4) -> bool:
    """Extract on every Nth reply.

    Per-turn extraction is an LLM call per message that mostly produces
    nothing new — but models want to be helpful, so it produces *something*,
    and that something is the clog. A chunk of several turns also yields
    better facts than one exchange seen alone.
    """
    if every_n <= 1:
        return True
    return turn_index > 0 and turn_index % every_n == 0


def rank(memories: list, recent_text: str = "") -> list:
    """Order by usefulness to *this* turn.

    Scope first (who you are outlives what she is wearing), then overlap with
    what is actually being talked about, then recency. No embeddings: word
    overlap is crude but it is stdlib, instant, and enormously better than
    the previous order, which was the primary key.
    """
    recent = _words(recent_text)
    base = {"user": 0, "character": 1, "chat": 2}

    def score(m):
        overlap = 0.0
        if recent:
            mw = _words(m.get("content", ""))
            if mw:
                overlap = len(mw & recent) / len(mw)
        return (base.get(m.get("kind"), 3), -overlap,
                -(m.get("updated") or m.get("created") or 0))

    return sorted(memories, key=score)


def budget(memories: list, max_items: int = 20, token_budget: int = 500,
           chat_keep: int = 6) -> list:
    """Trim a ranked list to what is worth spending context on."""
    out, used, chat_seen = [], 0, 0
    for m in memories:
        if len(out) >= max_items:
            break
        # Scene furniture is the most volatile and the least worth carrying;
        # keep only the freshest few rather than every position she has been in.
        if m.get("kind") == "chat":
            if chat_seen >= chat_keep:
                continue
            chat_seen += 1
        cost = max(1, len(m.get("content", "")) // 4)
        if used + cost > token_budget and out:
            break
        out.append(m)
        used += cost
    return out


CONSOLIDATE_PROMPT = """\
You are compacting the memory index of a roleplay engine. You will be given a
list of remembered facts about one subject. Merge them.

Rules:
- Combine facts that say the same thing, or that are steps in the same story,
  into one denser sentence. "They kissed", "they kissed again", "they slept
  together" becomes one line about how far the relationship has gone.
- Keep every distinct piece of information. You are compressing, not
  forgetting: if a detail appears in only one line, it survives.
- Prefer the most recent version when two facts conflict.
- Third person, one short sentence per fact, no preamble.
- Explicit content is recorded factually and without euphemism.

Output ONLY JSON: {"facts": ["one sentence", "another"]}"""


def consolidate(llm_once, memories: list, system: str = "") -> list:
    """Ask the model to merge a fat scope into fewer, denser facts."""
    if len(memories) < 4:
        return []
    listing = "\n".join(f"- {m['content']}" for m in memories)
    try:
        out = llm_once([
            {"role": "system", "content": system or CONSOLIDATE_PROMPT},
            {"role": "user", "content":
                f"FACTS ({len(memories)}):\n{listing}\n\nMerged JSON:"}])
    except Exception:  # noqa: BLE001
        return []
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return []
    try:
        facts = json.loads(match.group(0)).get("facts", [])
    except json.JSONDecodeError:
        return []
    clean = [str(f).strip() for f in facts if str(f).strip()]
    # Refuse a "compaction" that grew, or that threw most of it away.
    if not clean or len(clean) >= len(memories) or len(clean) < len(memories) // 4:
        return []
    return clean


def replace_scope(conn: sqlite3.Connection, scope: str, contents: list,
                  chat_id, character_id) -> int:
    """Swap a scope's rows for a consolidated set, in one transaction."""
    now = time.time()
    if scope == "user":
        conn.execute("DELETE FROM memories WHERE kind='user'")
        rc, rch = None, None
    elif scope == "character":
        conn.execute("DELETE FROM memories WHERE kind='character'"
                     " AND character_id=?", (character_id,))
        rc, rch = None, character_id
    else:
        conn.execute("DELETE FROM memories WHERE kind='chat' AND chat_id=?",
                     (chat_id,))
        rc, rch = chat_id, character_id
    for text in contents:
        conn.execute(
            "INSERT INTO memories (chat_id, character_id, kind, content,"
            " created, updated) VALUES (?,?,?,?,?,?)",
            (rc, rch, scope, text, now, now))
    return len(contents)


def dedupe_existing(conn: sqlite3.Connection, threshold: float = 0.6) -> dict:
    """Collapse near-duplicates already in the table.

    New writes are deduplicated, but anyone upgrading carries the wreckage of
    the racing extractor — a real log had "The user is called anon." stored
    four times over. Keeps the oldest row of each cluster (it has the longest
    history) but takes the newest `updated`, so a repeatedly re-confirmed fact
    still ranks as fresh.
    """
    removed, kept = 0, 0
    for scope in SCOPES:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE kind=? ORDER BY id", (scope,))]
        # Cluster within the same bucket only — the same sentence about a
        # different character is a different fact.
        buckets = {}
        for r in rows:
            key = (r["character_id"], r["chat_id"])
            buckets.setdefault(key, []).append(r)
        for group in buckets.values():
            survivors = []
            for r in group:
                match = next((s for s in survivors
                              if similarity(s["content"], r["content"]) >= threshold),
                             None)
                if match:
                    match["updated"] = max(match.get("updated") or 0,
                                           r.get("updated") or 0)
                    conn.execute("DELETE FROM memories WHERE id=?", (r["id"],))
                    removed += 1
                else:
                    survivors.append(r)
                    kept += 1
            for sdict in survivors:
                conn.execute("UPDATE memories SET updated=? WHERE id=?",
                             (sdict["updated"], sdict["id"]))
    return {"removed": removed, "kept": kept}
