#!/usr/bin/env python3
"""CoomKit — general-purpose NSFW companion harness.

Stdlib-only single-file server: static web UI + JSON API, local/remote
OpenAI-compatible LLM backends, ComfyUI bridge (BYO workflow), sqlite state.
Port 3939 by default. No pip, no build step, no telemetry.
"""
import base64
import binascii
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blocklib  # noqa: E402
import blocks  # noqa: E402
import cards  # noqa: E402
import chargen  # noqa: E402
import comfy  # noqa: E402
import engine  # noqa: E402
import library
import lore  # noqa: E402
import regexrules  # noqa: E402
import llm  # noqa: E402
import macros  # noqa: E402
import memory  # noqa: E402
import prompts  # noqa: E402
import recipes  # noqa: E402
import scenarios  # noqa: E402
import stimport  # noqa: E402
import studio  # noqa: E402
import tags  # noqa: E402
import tools  # noqa: E402
import voiceclip  # noqa: E402
import voices as voices_mod  # noqa: E402
import vram  # noqa: E402
import wfpack  # noqa: E402

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
ASSETS = DATA / "assets"
DB_PATH = DATA / "coomkit.sqlite"
CONFIG_PATH = DATA / "config.json"
VERSION = "0.2.0"

BACKEND_PRESETS = [
    ("LM Studio", "http://127.0.0.1:1234/v1"),
    ("llama.cpp / llama-server", "http://127.0.0.1:8080/v1"),
    ("Ollama", "http://127.0.0.1:11434/v1"),
    ("KoboldCpp", "http://127.0.0.1:5001/v1"),
    ("text-gen-webui / TabbyAPI", "http://127.0.0.1:5000/v1"),
    ("vLLM", "http://127.0.0.1:8000/v1"),
    ("SGLang", "http://127.0.0.1:30000/v1"),
]

# The context the current turn is being budgeted against. _prepare_request
# knows it; the load fixer runs deep inside llm._post with no request in
# scope, and loading at the config default when the user chose 20k silently
# truncates every later chat — the exact hazard the parked-list restore exists
# to avoid, arriving from the other direction.
_ctx_hint = threading.local()


def _llm_load_fixer(backend: str, model: str, detail: str):
    """Registered with llm.set_load_fixer at import.

    A model that will not load is almost always a full card rather than a bad
    model, and until now that surfaced as LM Studio's own `Failed to load
    model "X"` with nothing done about it. Swap out whatever is resident and
    let the request retry once.

    Context comes from config defaults because this hook has no request in
    scope. That is deliberately conservative: loading at SOME chosen context
    beats LM Studio's JIT default, which is the thing that silently truncates
    later chats.
    """
    cfg = load_config()
    ctx = int(getattr(_ctx_hint, "tokens", 0)
              or cfg.get("defaults", {}).get("context_tokens") or 8192)
    try:
        return vram.ensure_model(cfg, backend, model, context_tokens=ctx)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


llm.set_load_fixer(_llm_load_fixer)


def backend_label(url: str) -> str:
    """A human name for a backend URL — the LABEL, never the base URL.

    The export footer prints this. A self-hosted endpoint in a screencap
    posted to 4chan is a doxx, and "http://192.168.1.44:5000/v1" tells a
    reader nothing they wanted to know anyway.
    """
    u = (url or "").rstrip("/")
    for label, purl in BACKEND_PRESETS:
        if purl.rstrip("/") == u:
            return label
    for rb in load_config().get("remote_backends", []):
        if (rb.get("url") or "").rstrip("/") == u:
            return rb.get("label") or "remote"
    return "custom"


def name_scrubber(names, replacement):
    """Whole-word replace of the poster's real name(s), for the image export.

    Binding {{user}} to a pseudonym is exact but incomplete: the model writes
    the persona's name into its prose literally and never emits the macro. So
    the export needs both, and both belong server-side — a client-side pass
    over already-expanded text mangles substrings ("Al" inside "always") and
    cannot be tested. Returns (scrub, counter); scrub is identity when there
    is nothing to do.
    """
    count = [0]
    pats = []
    # longest first, so an alias that contains another name wins
    for n in sorted({(x or "").strip() for x in names if (x or "").strip()},
                    key=len, reverse=True):
        pats.append(re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)", re.I))
    if not pats or not replacement:
        return (lambda t: t), count

    def scrub(text):
        out = text or ""
        for pat in pats:
            out, n = pat.subn(replacement, out)
            count[0] += n
        return out

    return scrub, count


# The largest context figure worth *budgeting against* when a backend reports
# a capability rather than a measured load. Same number and same reasoning as
# stimport.py:189, which refuses ST's unlocked slider: the history budget is
# computed from this, so a million-token claim means history is never trimmed
# at all. See _context_probe.
CONTEXT_TRUST_CEILING = 200000

# OpenAI-compatible APIs reject more than four stop strings. Local backends
# have no such limit, so this is applied only to a configured remote.
REMOTE_STOP_CAP = 4

DEFAULT_CONFIG = {
    "port": 3939,
    "comfyui_url": "",
    # [{"label": "OpenRouter", "url": "https://openrouter.ai/api/v1",
    #   "key": "...", "vision": false}]
    # `vision: true` is the ONE exception to vision-local-only: the user has
    # explicitly said pictures may be sent to this backend (their own LM
    # Studio on another LAN box, a trusted vision endpoint). Off by default,
    # set per backend, never inferred.
    "remote_backends": [],
    "defaults": {"temperature": 0.9, "top_p": 0.95, "top_k": 40,
                 "max_tokens": 2048, "context_tokens": 8192},
    # Which bundled workflow serves each media kind, and whether the optional
    # quality stages are on. Overridden per character where it matters.
    "studio": {"image": "krea2", "video": "h3", "stages": {}},
    # Off by default: unloading somebody's chat model is not a thing to do
    # uninvited. Anyone on one GPU will want "auto".
    "vram": {"policy": "off", "driver": "none"},
    # Memory lifecycle. Extraction every Nth reply rather than every one, and
    # a hard ceiling on what reaches the prompt.
    "memory": dict(),
    # 1800, not 900. A 15s H3 clip measured 876.5s on a 5090 — 97% of the old
    # default — so a slightly slower card threw away fifteen minutes of real
    # GPU work to a timeout. A generous ceiling costs nothing here because a
    # job that actually *fails* is surfaced the moment ComfyUI records it
    # (comfy._failure), not by waiting this out; the timeout is only a
    # backstop for a genuinely hung server.
    "comfy_timeout": 1800,
}

def url_parts_of(path: str) -> list:
    return [p for p in urllib.parse.urlparse(path).path.split("/") if p]


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
}

# --------------------------------------------------------------------------
# Config & database
# --------------------------------------------------------------------------


def load_config() -> dict:
    DATA.mkdir(exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except Exception:
        cfg = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def chat_label(chat: dict) -> str:
    """What to call a chat that was never explicitly named.

    In order: the title the user gave it, the forged scenario's title, the
    opening words of their first message, then the date. Kept in one place
    so the list and the chat header cannot drift apart.
    """
    if chat.get("title"):
        return chat["title"]
    scen = (chat.get("data") or {}).get("scenario") or {}
    if scen.get("title"):
        return scen["title"]
    first = (chat.get("first_user") or "").strip().replace("\n", " ")
    if first:
        return first[:48] + ("…" if len(first) > 48 else "")
    return time.strftime("%d %b %Y", time.localtime(chat.get("created") or 0))


SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    avatar TEXT DEFAULT '',
    fav INTEGER NOT NULL DEFAULT 0,
    created REAL, updated REAL
);
-- Find/replace rules. Deliberately NOT a named-row `data` blob table: this is
-- an ordered chain that gets read on every turn, so the fields it is filtered
-- and sorted on are real columns. `ord` because `order` is a SQL keyword.
-- character_id NULL means global, mirroring the memory scopes and the gallery.
CREATE TABLE IF NOT EXISTS regex_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    replace TEXT NOT NULL DEFAULT '',
    on_prompt INTEGER NOT NULL DEFAULT 0,
    on_display INTEGER NOT NULL DEFAULT 1,
    min_depth INTEGER, max_depth INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    ord INTEGER NOT NULL DEFAULT 0,
    character_id INTEGER,
    data TEXT NOT NULL DEFAULT '{}',
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    avatar TEXT DEFAULT '',
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER,
    persona_id INTEGER,
    mode TEXT DEFAULT 'rp',
    memory_enabled INTEGER DEFAULT 1,
    title TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}',
    created REAL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    character_id INTEGER,
    kind TEXT DEFAULT 'fact',
    content TEXT NOT NULL,
    created REAL, updated REAL,
    persona_id INTEGER
);
CREATE TABLE IF NOT EXISTS presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    data TEXT NOT NULL DEFAULT '{}',
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS jailbreaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    data TEXT NOT NULL DEFAULT '{}',
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT DEFAULT 'image',
    data TEXT NOT NULL DEFAULT '{}',
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    message_id INTEGER,
    character_id INTEGER,
    recipe TEXT DEFAULT '',
    kind TEXT DEFAULT 'image',
    path TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created REAL
);
CREATE INDEX IF NOT EXISTS assets_by_character ON assets (character_id, created);
CREATE INDEX IF NOT EXISTS chats_by_character ON chats (character_id, updated);
-- Who else is in this scene. chats.character_id stays the LEAD: the gallery,
-- the chat list, _rp_digest and the export footer all key off it and must
-- keep working untouched. This table says who ELSE, and who is actually in
-- the room right now. Presence is a queryable column on purpose — the roster
-- asks "which scenes is she in", and a JSON blob on chats.data cannot answer
-- that without reading every row.
CREATE TABLE IF NOT EXISTS chat_cast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    ord INTEGER NOT NULL DEFAULT 0,
    since REAL,
    data TEXT NOT NULL DEFAULT '{}',
    created REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS chat_cast_uniq ON chat_cast (chat_id, character_id);
CREATE INDEX IF NOT EXISTS chat_cast_by_char ON chat_cast (character_id, chat_id);

-- Lorebooks. Real columns for the same reason regex_rules has them: this is
-- read, filtered and sorted on every turn, and a JSON blob cannot be indexed.
-- Kept OUT of VALID_TABLES like regex_rules and chat_cast — rows_get asserts
-- on anything else with a bare AssertionError and no message.
CREATE TABLE IF NOT EXISTS lorebooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',      -- st-world | card | hand
    enabled INTEGER NOT NULL DEFAULT 1,
    scan_depth INTEGER,                   -- NULL inherits the global default
    ord INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL DEFAULT '{}',      -- import notes, what we refused
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS lore_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',       -- ST `comment`: the human name
    keys TEXT NOT NULL DEFAULT '',        -- newline-joined
    secondary TEXT NOT NULL DEFAULT '',   -- newline-joined
    logic INTEGER NOT NULL DEFAULT 0,     -- AND_ANY|NOT_ALL|NOT_ANY|AND_ALL
    content TEXT NOT NULL,
    constant INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    whole_words INTEGER,                  -- NULL inherits the book default
    case_sensitive INTEGER NOT NULL DEFAULT 0,
    ord INTEGER NOT NULL DEFAULT 100,     -- ST insertion_order, HIGHER first
    data TEXT NOT NULL DEFAULT '{}',      -- every field we do not honour, verbatim
    created REAL, updated REAL
);
-- A book attaches to a character, to a chat, or to everything. Three tables
-- and not a scope column on the book, because the whole point is reuse: one
-- one setting book attached to five characters is one book, five links.
CREATE TABLE IF NOT EXISTS lore_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    character_id INTEGER,
    chat_id INTEGER,
    created REAL
);
CREATE INDEX IF NOT EXISTS lore_entries_by_book ON lore_entries (book_id, ord);
CREATE UNIQUE INDEX IF NOT EXISTS lore_links_uniq
    ON lore_links (book_id, COALESCE(character_id, 0), COALESCE(chat_id, 0));
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS cannot
# retrofit a column onto a table that already exists, so they are applied by
# hand — a gallery keyed on character_id is useless if the column only shows
# up for people who deleted their database.
MIGRATIONS = {
    "assets": [("character_id", "INTEGER"), ("recipe", "TEXT DEFAULT ''")],
    "characters": [("fav", "INTEGER NOT NULL DEFAULT 0")],
    "chats": [("title", "TEXT")],
    # NULL = shared with every persona; that is what every pre-existing row
    # means, so the migration is also the correct backfill.
    "memories": [("persona_id", "INTEGER")],
}


# _migrate() runs from exactly one place — get_db(), and only when the stamped
# user_version differs. A MIGRATIONS entry without a bump here is a silent
# no-op that ships "no such column" to everyone who already has a database.
# Bumped to 5 for the three lorebook tables (4 was chat_cast). There is
# deliberately no MIGRATIONS entry: that mechanism is ALTER TABLE ADD COLUMN
# and nothing else, so it cannot create a table or an index. get_db() re-runs
# the whole SCHEMA on a version mismatch and every statement in it is IF NOT
# EXISTS, so the new tables arrive on an existing database for free — but ONLY
# because this number changed. Forget the bump and every lorebook route 500s
# with "no such table" on every database that already exists, which reads as
# the feature being completely broken while the code is correct.
# 6: memories.persona_id (per-persona memory buckets, NULL = shared).
SCHEMA_VERSION = 6


def get_db() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # The database file can disappear under a running server — someone wipes
    # data/ to reset things. sqlite then happily creates an empty file and
    # every route 500s with "no such table" until a manual restart, which
    # looks exactly like "the app lost all my chats". Stamp a version and
    # re-apply the schema whenever it is missing; CREATE TABLE IF NOT EXISTS
    # makes the normal case a single cheap pragma read.
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that postdate the table they belong to."""
    for table, columns in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def migrate_asset_owners() -> int:
    """Attribute renders that were filed without a character. Runs at startup.

    The gallery is keyed on character_id and never on chat_id, so an asset row
    without one is invisible in every gallery forever — the file is on disk,
    the row is in the table, and nothing will ever show it. Two insert sites
    (/api/comfy/run and the free-form tool save) omitted the column, and on a
    real dev box 45 of 48 assets had ended up unreachable.

    Only the recoverable ones: an asset with a chat_id can be attributed to
    that chat's character. One with neither cannot be attributed to anybody
    and is left alone rather than guessed at.
    """
    try:
        with get_db() as conn:
            cur = conn.execute(
                "UPDATE assets SET character_id = ("
                "  SELECT c.character_id FROM chats c WHERE c.id = assets.chat_id)"
                " WHERE character_id IS NULL AND chat_id IS NOT NULL"
                "   AND EXISTS (SELECT 1 FROM chats c WHERE c.id = assets.chat_id)")
            conn.commit()
            return cur.rowcount or 0
    except sqlite3.Error:
        return 0


def migrate_presets() -> int:
    """Give every stored preset a block list. Runs once at startup.

    Presets predate blocks, so without this the block editor would open empty
    for everyone with an existing library and look broken.
    """
    changed = 0
    try:
        for row in rows_list("presets"):
            data, did = blocks.migrate_preset(row.get("data") or {})
            if did:
                rows_upsert("presets", {"name": row["name"], "data": data},
                            row["id"])
                changed += 1
    except Exception:  # noqa: BLE001 — never block startup over this
        return changed
    return changed


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(SCHEMA)


def regex_rules(character_id: int = None) -> list:
    """Every enabled rule that applies here, global first, then hers.

    Compiled per call rather than cached: the set is small, the compile is
    microseconds, and a cache keyed on nothing in particular is how an edited
    rule appears not to take effect until restart.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM regex_rules WHERE enabled=1"
            " AND (character_id IS NULL OR character_id=?)"
            " ORDER BY character_id IS NOT NULL, ord, id",
            (character_id,)).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        row["trim"] = (json.loads(row.get("data") or "{}") or {}).get("trim") or []
        out.append(row)
    return regexrules.prepare(out)


def lore_books_for(character_ids, chat_id: int = None) -> list:
    """Every enabled book in play: linked to any present cast member, to this
    chat, or global.

    Union of the whole cast rather than only the speaker, deliberately. A
    speaker swap must not change the lore half of the prompt — that would
    invalidate the prefix cache the cast's card ordering exists to protect,
    and it matters more now that `auto` changes the speaker most turns.

    Uncached, for the reason regex_rules() gives: a cache keyed on nothing in
    particular is how an edited book appears not to take effect until restart.
    """
    ids = [int(c) for c in (character_ids or []) if c]
    marks = ",".join("?" * len(ids)) or "NULL"
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT b.* FROM lorebooks b"
            f" JOIN lore_links l ON l.book_id = b.id"
            f" WHERE b.enabled=1 AND ("
            f"   (l.character_id IS NULL AND l.chat_id IS NULL)"
            f"   OR l.chat_id = ?"
            f"   OR l.character_id IN ({marks}))"
            f" ORDER BY b.ord, b.id",
            [chat_id] + ids).fetchall()
        out = []
        for r in rows:
            ents = conn.execute(
                "SELECT * FROM lore_entries WHERE book_id=? AND enabled=1"
                " ORDER BY ord DESC, id", (r["id"],)).fetchall()
            out.append(_book_from_rows(r, ents))
    return out


def _book_from_rows(row, ents) -> dict:
    """A stored book, back into the shape lore.select reads."""
    entries = [lore._entry(
        e["content"],
        label=e["label"],
        keys=[k for k in (e["keys"] or "").split("\n") if k],
        secondary=[k for k in (e["secondary"] or "").split("\n") if k],
        logic=e["logic"], constant=bool(e["constant"]),
        enabled=bool(e["enabled"]),
        case_sensitive=bool(e["case_sensitive"]),
        whole_words=None if e["whole_words"] is None else bool(e["whole_words"]),
        ord=e["ord"]) for e in ents]
    data = json.loads(row["data"] or "{}") or {}
    return lore._book(
        row["name"], row["source"] or "st-world", entries,
        keyless_always=False, honour_constant=True, honour_disable=True,
        whole_words=True, oversize="truncate",
        scan_depth=row["scan_depth"] or lore.ST_SCAN_DEPTH,
        ord=row["ord"], id=row["id"],
        # Which card this was lifted OUT of, if any. The embedded book is
        # then skipped for her, matched on this stored provenance and never
        # on comparing text — a text comparison is the silent near-miss that
        # produces every entry twice.
        from_card_id=int(data.get("from_card_id") or 0))


def _lore_link(conn, book_id: int, scope: str, character_id: int = 0,
               chat_id: int = 0) -> None:
    """Set a book's attachment FOR ONE CONTEXT, leaving every other alone.

    INSERT OR IGNORE, not ON CONFLICT: the uniqueness is enforced by an
    expression index over COALESCE(...), and `ON CONFLICT(book_id,
    character_id, chat_id)` does not match it — sqlite raises OperationalError,
    which the route wrapper turns into a 500 rather than a duplicate-key path.
    """
    if scope == "always":
        conn.execute("DELETE FROM lore_links WHERE book_id=?", (book_id,))
        conn.execute("INSERT OR IGNORE INTO lore_links"
                     " (book_id, character_id, chat_id, created)"
                     " VALUES (?,NULL,NULL,?)", (book_id, time.time()))
        return
    # Clear only the link for THIS context. A book attached to five girls
    # stays attached to the other four.
    conn.execute("DELETE FROM lore_links WHERE book_id=? AND"
                 " character_id IS NULL AND chat_id IS NULL", (book_id,))
    if character_id:
        conn.execute("DELETE FROM lore_links WHERE book_id=? AND character_id=?",
                     (book_id, character_id))
    if chat_id:
        conn.execute("DELETE FROM lore_links WHERE book_id=? AND chat_id=?",
                     (book_id, chat_id))
    if scope == "character" and character_id:
        conn.execute("INSERT OR IGNORE INTO lore_links"
                     " (book_id, character_id, chat_id, created)"
                     " VALUES (?,?,NULL,?)",
                     (book_id, character_id, time.time()))
    elif scope == "chat" and chat_id:
        conn.execute("INSERT OR IGNORE INTO lore_links"
                     " (book_id, character_id, chat_id, created)"
                     " VALUES (?,NULL,?,?)", (book_id, chat_id, time.time()))


def _lore_overflow(req: dict) -> list:
    """One trailing informational segment when the budget bit.

    Not part of the prompt — it is appended to what the INSPECTOR is shown,
    because "why didn't that fire" is the question people open it to answer
    and the entries that lost the budget race are invisible by definition.
    """
    rep = (req.get("trace") or {}).get("lore") or {}
    if not rep.get("missed"):
        return []
    return [{"role": "note", "parts": [{
        "id": "lore-overflow", "marker": "lore", "builtin": True, "layer": "",
        "name": f"{rep['missed']} more matched and did not fit",
        "tokens": rep["missed_tokens"],
        "content": f"{rep['missed']} more lorebook entries matched this turn "
                   f"and did not fit the budget (~{rep['missed_tokens']} "
                   f"tokens). Raise defaults.lore_tokens, or switch some off."}]}]


def _book_store(conn, book: dict, name: str = "") -> int:
    """Write a parsed book and its entries. Returns the book id."""
    now = time.time()
    cur = conn.execute(
        "INSERT INTO lorebooks (name, source, enabled, scan_depth, ord, data,"
        " created, updated) VALUES (?,?,1,?,0,?,?,?)",
        (name or book["name"], book["source"], book["scan_depth"],
         json.dumps({"notes": book["notes"]}), now, now))
    bid = cur.lastrowid
    for e in book["entries"]:
        conn.execute(
            "INSERT INTO lore_entries (book_id, label, keys, secondary, logic,"
            " content, constant, enabled, whole_words, case_sensitive, ord,"
            " data, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, e["label"], "\n".join(e["keys"]), "\n".join(e["secondary"]),
             e["logic"], e["content"], int(bool(e["constant"])),
             int(bool(e["enabled"])),
             None if e["whole_words"] is None else int(e["whole_words"]),
             int(bool(e["case_sensitive"])), e["ord"],
             json.dumps({"raw": e["data"], "reason": e["reason"]}), now, now))
    return bid


def for_display(text: str, rules: list, depth: int = None) -> tuple:
    """Apply the display rules and say whether the result carries markup.

    Returns (text, is_html). The flag matters: the client renders with
    textContent by default — model output is never trusted as markup — and
    only switches to innerHTML for text a rule the *user* installed produced,
    after it has been through the allowlist.
    """
    if not rules:
        return text, False
    out = regexrules.apply(text, rules, "display", depth)
    if out == text or not regexrules.has_markup(out):
        return out, False
    return regexrules.sanitize(out), True


def seed_first_run() -> dict:
    """Put the shipped library and a default persona in an empty database.

    A fresh install used to come up with zero presets and zero jailbreaks,
    waiting for the user to find ⚙ → library → install. That is a bad enough
    empty state on its own, but it also silently broke the wizard: its blocks
    step does `S.presets[0]` and installs the starter set into whatever it
    finds, so with no presets at all the headline step of setup rendered its
    summary and then wrote nothing. Setup appeared to succeed and configured
    nothing.

    Emptiness is the trigger, not absence of a marker file. Someone who has
    deleted every preset on purpose gets them back — that is the cost — but
    someone who renamed or edited them keeps their work, because a non-empty
    table is left alone entirely.
    """
    seeded = {}
    with get_db() as conn:
        empty = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
                 for t in ("presets", "jailbreaks", "personas", "characters")}
    if empty["presets"] and empty["jailbreaks"]:
        added = library.install(rows_upsert)
        seeded["presets"] = len(added.get("presets") or [])
        seeded["jailbreaks"] = len(added.get("jailbreaks") or [])
    if empty["personas"]:
        # `anon` is the documented default name, and engine/macros already
        # fall back to it — but a row has to exist for the persona layer to
        # carry anything, and for a reference photo to have somewhere to live.
        # The h3 recipes read their second reference off persona.data.refs.
        rows_upsert("personas", {"name": "anon", "data": {
            "description": "", "into": "",
            "_note": "Rename me. Whatever goes in `description` is who she "
                     "thinks she is talking to."}})
        seeded["personas"] = 1
    if empty["characters"]:
        seeded["characters"] = _seed_starter_card()
    return seeded


STARTER_CARD = ROOT / "cards" / "mika.png"


def _seed_starter_card() -> int:
    """Put one real character in an empty roster.

    An empty roster is not just a sad first screen — the walkthrough's whole
    middle section is about upgrading a card into something multimodal, and
    with nothing to point at those steps have no target. She ships with her
    appearance, a pinned seed and a voice already set, so "🤳 Selfie" works
    on a fresh install and demonstrates the point in one click.

    Same emptiness rule as the rest of seeding: a roster with anything in it
    is left completely alone.
    """
    if not STARTER_CARD.exists():
        return 0
    try:
        raw = STARTER_CARD.read_bytes()
        parsed = cards.parse_card(raw, STARTER_CARD.name)
    except Exception:  # noqa: BLE001 — a bad asset must not break startup
        return 0
    ext = ((parsed.get("fields") or {}).get("extensions") or {})
    mine = ext.get("coomkit") if isinstance(ext, dict) else None
    if isinstance(mine, dict):
        for key in ("visual", "voice"):
            if isinstance(mine.get(key), dict) and mine[key]:
                parsed.setdefault(key, mine[key])
    ASSETS.mkdir(parents=True, exist_ok=True)
    avatar = "starter_mika.png"
    (ASSETS / avatar).write_bytes(raw)
    now = time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO characters (name, data, avatar, created, updated)"
            " VALUES (?,?,?,?,?)",
            (parsed["name"], json.dumps(parsed), avatar, now, now))
    return 1


# --------------------------------------------------------------------------
# Generic named-row store (presets, jailbreaks, workflows — same shape)
# --------------------------------------------------------------------------

VALID_TABLES = {"presets", "jailbreaks", "workflows", "characters", "personas"}


def rows_list(table: str) -> list[dict]:
    assert table in VALID_TABLES
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY updated DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def rows_get(table: str, row_id: int) -> dict | None:
    assert table in VALID_TABLES
    with get_db() as conn:
        r = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    return _row_to_dict(r) if r else None


def _row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["data"] = json.loads(d.get("data") or "{}")
    except json.JSONDecodeError:
        d["data"] = {}
    return d


# The one size cap for anything the user hands us. Named because the forge
# pitches from a picture and then commits the same picture through a second
# path: two caps meant a file could be dropped from the pitch and accepted at
# commit, and the user would be told neither.
# How many times a turn that spent its whole budget thinking may be retried
# with more room. Each try triples, so from a 1200-token preset this reaches
# 1200 -> 3600 -> 10800 and then the 16000 ceiling. Two is measured, not
# chosen: one escalation still left gemma-4-12b with in-character thinking
# empty in 2 of 3 runs.
MAX_THINK_ESCALATIONS = 2

MAX_UPLOAD = 40 * 1024 * 1024
MAX_UPLOAD_MB = MAX_UPLOAD // (1024 * 1024)


def _store_upload(raw: bytes, filename: str) -> str:
    """Write an uploaded file into data/assets/ and return its stored name.

    One writer for user-supplied files, because there are now two callers —
    the upload route and the character forge committing the picture a card
    was built from — and a second copy of the cap and the extension check is
    a second thing to get wrong. Raises ValueError with a message meant for
    the user.
    """
    if not raw:
        raise ValueError("no file data")
    if len(raw) > MAX_UPLOAD:
        raise ValueError(f"file too big ({MAX_UPLOAD_MB} MB cap)")
    ext = Path(filename or "ref.png").suffix.lower() or ".png"
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
        raise ValueError("unsupported file type")
    fname = f"ref_{int(time.time() * 1000)}{ext}"
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / fname).write_bytes(raw)
    return fname


def rows_upsert(table: str, body: dict, row_id: int | None = None) -> dict:
    assert table in VALID_TABLES
    now = time.time()
    name = (body.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    data = json.dumps(body.get("data", {}))
    extra = {k: body[k] for k in ("kind", "avatar") if k in body}
    with get_db() as conn:
        if not row_id and table in ("presets", "jailbreaks", "workflows",
                                    "personas"):
            # Name-unique tables: upsert by name to stay idempotent. Personas
            # were documented as being in this list and were not, so every
            # "save persona" without a selected row created another one — a
            # dev database here had accumulated fourteen identical people.
            existing = conn.execute(
                f"SELECT id FROM {table} WHERE name=?", (name,)).fetchone()
            if existing:
                row_id = existing["id"]
        if row_id:
            # `extra` has to be carried here too. Without it `avatar` could be
            # set once at insert and never changed again, which quietly broke
            # card export (it embeds the card into the avatar PNG) and any
            # attempt to give an existing character a new face.
            sets = ["name=?", "data=?", "updated=?"]
            vals = [name, data, now]
            for k, v in extra.items():
                sets.append(f"{k}=?")
                vals.append(v)
            conn.execute(
                f"UPDATE {table} SET {', '.join(sets)} WHERE id=?",
                vals + [row_id],
            )
        else:
            cols = ["name", "data", "created", "updated"] + list(extra)
            vals = [name, data, now, now] + list(extra.values())
            cur = conn.execute(
                f"INSERT INTO {table} ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                vals,
            )
            row_id = cur.lastrowid
    return rows_get(table, row_id)


def rows_delete(table: str, row_id: int) -> bool:
    assert table in VALID_TABLES
    with get_db() as conn:
        cur = conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# Backend detection — proven in the field
# --------------------------------------------------------------------------


def normalise_backend(url: str) -> str:
    """Accept whatever the user pasted and produce a usable API base."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    if not re.search(r"/(v\d+|api)$", url):
        url += "/v1"
    return url


def api_call(backend: str, path: str, key: str = "", data: bytes | None = None, timeout: int = 15):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{backend}{path}", data=data, headers=headers, method="POST" if data else "GET"
    )
    return urllib.request.urlopen(request, timeout=timeout)


def list_models(backend: str, key: str = "") -> list[str]:
    with api_call(backend, "/models", key, timeout=8) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    models = []
    for entry in payload.get("data", payload.get("models", [])):
        name = entry.get("id") or entry.get("name") if isinstance(entry, dict) else str(entry)
        if name and "embed" not in name.lower():
            models.append(name)
    return models


def detect_backends(cfg: dict) -> list[dict]:
    """Probe the usual local ports (plus configured remotes) in parallel."""
    found: list[dict] = []
    lock = threading.Lock()

    def probe(label: str, url: str, key: str = "", remote: bool = False) -> None:
        try:
            models = list_models(url, key)
        except Exception:  # noqa: BLE001 — a closed port is the common case
            return
        with lock:
            found.append({"label": label, "url": url, "models": models, "remote": remote})

    seen: set[str] = set()
    threads = []
    targets = [(label, url, "", False) for label, url in BACKEND_PRESETS]
    for rb in cfg.get("remote_backends", []):
        targets.append((rb.get("label", "remote"), normalise_backend(rb.get("url", "")),
                        rb.get("key", ""), True))
    for label, url, key, remote in targets:
        if not url or url in seen:
            continue
        seen.add(url)
        thread = threading.Thread(target=probe, args=(label, url, key, remote), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(timeout=3)
    order = {url: i for i, (_, url) in enumerate(BACKEND_PRESETS)}
    found.sort(key=lambda f: order.get(f["url"], 99))
    return found


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"CoomKit/{VERSION}"

    def log_message(self, fmt, *args):  # quieter logs
        pass

    # -- helpers -----------------------------------------------------------
    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        rel = "index.html" if rel == "" else rel
        target = (WEB / rel).resolve()
        if not str(target).startswith(str(WEB.resolve())):
            self._json({"error": "not found"}, 404)
            return
        self._static_file(target)

    def _static_file(self, target: Path) -> None:
        if not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        if url.path == "/api/health":
            self._json({"ok": True, "version": VERSION, "name": "CoomKit"})
        elif url.path == "/api/backends":
            self._json({"backends": detect_backends(load_config())})
        elif url.path == "/api/config":
            cfg = load_config()
            for rb in cfg.get("remote_backends", []):
                if rb.get("key"):
                    rb["key"] = rb["key"][:6] + "..."  # never leak full keys
            self._json(cfg)
        elif url.path == "/api/datapack":
            self._datapack_get(urllib.parse.parse_qs(url.query))
        elif url.path == "/api/tools/pending":
            # Undelivered approval cards — how a picture she attached to a
            # daemon-sent text reaches the user when they next open the
            # phone. In-memory like the rest of the registry: a restart
            # drops them, and she simply offers again next time.
            q = urllib.parse.parse_qs(url.query)
            want = int((q.get("chat_id") or ["0"])[0] or 0)
            out = []
            for e in tools.pending_all():
                pl = (e.get("call") or {}).get("payload")
                job = (e.get("call") or {}).get("studio") or {}
                if pl and (not want or job.get("chat_id") == want):
                    out.append(pl)
            self._json({"pending": out})
        elif url.path == "/api/chats":
            self._chats_list(urllib.parse.parse_qs(url.query))
        elif url.path == "/api/characters":
            self._characters_list()
        elif len(parts) == 2 and parts[0] == "api" and parts[1] in VALID_TABLES:
            self._json({"rows": rows_list(parts[1])})
        elif (len(parts) == 3 and parts[0] == "api" and parts[1] in VALID_TABLES
              and parts[2].isdigit()):
            row = rows_get(parts[1], int(parts[2]))
            self._json(row if row else {"error": "not found"}, 200 if row else 404)
        elif (len(parts) == 3 and parts[:2] == ["api", "avatars"]
              and re.fullmatch(r"[A-Za-z0-9_.-]+", parts[2])):
            self._static_file(ASSETS / parts[2])
        elif (len(parts) == 3 and parts[:2] == ["api", "voices"]
              and re.fullmatch(r"[A-Za-z0-9_.-]+", parts[2])):
            self._static_file(voices_mod.VOICE_DIR / parts[2])
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "cast"):
            self._cast_list(int(parts[2]))
        elif (len(parts) == 3 and parts[:2] == ["api", "chats"] and parts[2].isdigit()):
            self._chat_detail(int(parts[2]),
                              urllib.parse.parse_qs(url.query))
        elif url.path == "/api/tools/pending":
            self._json({"pending": tools.pending_all()})
        elif url.path == "/api/blocks":
            self._blocks_catalogue()
        elif url.path == "/api/studio":
            self._studio_catalogue()
        elif url.path == "/api/tags":
            self._tags_status()
        elif url.path == "/api/tags/search":
            self._tags_search(urllib.parse.parse_qs(url.query))
        elif url.path == "/api/tags/artists":
            self._tags_artists(urllib.parse.parse_qs(url.query))
        elif url.path == "/api/loras":
            self._loras()
        elif url.path == "/api/vram":
            self._vram_status()
        elif (len(parts) == 3 and parts[:2] == ["api", "gallery"]
              and parts[2].isdigit()):
            self._gallery(int(parts[2]))
        elif url.path == "/api/library":
            self._json(library.catalog())
        elif url.path == "/api/prompts":
            self._json({"prompts": prompts.catalog()})
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "memories"):
            self._memories_list(int(parts[2]))
        elif url.path == "/api/regex":
            self._regex_list(urllib.parse.parse_qs(url.query))
        elif url.path == "/api/lorebooks":
            self._lore_list()
        else:
            self._static(url.path)

    def do_POST(self) -> None:  # noqa: N802
        # An unhandled error in any route used to escape into BaseHTTPRequestHandler,
        # which closes the socket without a response. The browser reports that as a
        # failed fetch, so a one-line bug in one endpoint reads as "the server is
        # down" — and the traceback only exists in coomkit.log, which nobody has open.
        try:
            self._route_post(url_parts_of(self.path), self.path)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _route_post(self, parts: list, path: str) -> None:
        url = urllib.parse.urlparse(path)
        if url.path == "/api/config":
            incoming = self._body()
            cfg = load_config()
            for key in ("comfyui_url", "defaults", "vram", "studio",
                        "comfy_timeout", "setup", "host", "texting"):
                if key in incoming:
                    cfg[key] = incoming[key]
            if "remote_backends" in incoming:
                # The GET route masks keys before they reach the browser, and
                # the browser sends the WHOLE list back on any edit — so an
                # incoming key that is empty or IS the mask means "keep what
                # you have". Stored verbatim, adding a second backend (or
                # flipping the vision toggle) silently replaced every other
                # entry's real key with the "sk-abc..." stub, which then
                # presents as 401s from a provider that worked yesterday.
                # Deliberately keep-on-empty: clearing a key means re-adding
                # the backend with a fresh one, or editing config.json.
                stored = {}
                for rb in cfg.get("remote_backends") or []:
                    if isinstance(rb, dict):
                        for k in (rb.get("url"), rb.get("label")):
                            if k:
                                stored.setdefault(k, rb)
                merged = []
                for rb in incoming["remote_backends"] or []:
                    if isinstance(rb, dict):
                        old = (stored.get(rb.get("url"))
                               or stored.get(rb.get("label")))
                        key = rb.get("key") or ""
                        if old and old.get("key") and (
                                not key or key == old["key"][:6] + "..."):
                            rb = {**rb, "key": old["key"]}
                    merged.append(rb)
                cfg["remote_backends"] = merged
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
            self._json({"ok": True})
        elif url.path == "/api/chat":
            self._chat()
        elif url.path == "/api/chats/new":
            self._chat_new()
        elif url.path == "/api/chats/send":
            self._chat_send()
        elif url.path == "/api/chats/preview":
            self._chat_preview()
        elif url.path == "/api/library/install":
            self._library_install()
        elif url.path == "/api/datapack/pull":
            self._datapack_pull()
        elif (len(parts) == 4 and parts[:2] == ["api", "presets"]
              and parts[2].isdigit() and parts[3] == "blocks"):
            self._blocks_save(int(parts[2]))
        elif (len(parts) == 5 and parts[:2] == ["api", "presets"]
              and parts[2].isdigit() and parts[3:5] == ["blocks", "starter"]):
            self._blocks_starter(int(parts[2]))
        elif url.path == "/api/context/probe":
            self._context_probe()
        elif url.path == "/api/presets/import-st":
            self._preset_import_st()
        elif url.path == "/api/blocks/cost":
            self._blocks_cost()
        elif url.path == "/api/forge/characters":
            self._chargen_pitch()
        elif url.path == "/api/forge/characters/from-image":
            self._chargen_from_image()
        elif url.path == "/api/forge/characters/refine":
            self._chargen_revise()
        elif (len(parts) == 4 and parts[:2] == ["api", "characters"]
              and parts[2].isdigit() and parts[3] == "portrait"):
            self._character_portrait(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "characters"]
              and parts[2].isdigit() and parts[3] == "avatar"):
            self._character_avatar(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "characters"]
              and parts[2].isdigit() and parts[3] == "voice-capture"):
            self._character_voice_capture(int(parts[2]))
        elif url.path == "/api/forge/characters/create":
            self._chargen_create()
        elif url.path == "/api/scenarios/suggest":
            self._scenarios_suggest()
        elif url.path == "/api/scenarios/refine":
            self._scenarios_refine()
        elif url.path == "/api/chats/text-first":
            self._chat_text_first()
        elif url.path == "/api/chats/remember":
            self._chat_remember()
        elif url.path == "/api/memories/tidy":
            self._memory_tidy()
        elif url.path == "/api/memories":
            self._memory_write()
        elif url.path == "/api/regex":
            self._regex_write()
        elif url.path == "/api/regex/import":
            self._regex_import()
        elif url.path == "/api/lorebooks":
            self._lore_write()
        elif url.path == "/api/lorebooks/import":
            self._lore_import()
        elif url.path == "/api/lorebooks/link":
            self._lore_link_route()
        elif url.path == "/api/prompts":
            self._prompts_write()
        elif url.path == "/api/prompts/reset":
            body = self._body()
            prompts.reset(body.get("key"))
            self._json({"ok": True, "prompts": prompts.catalog()})
        elif (len(parts) == 4 and parts[:2] == ["api", "messages"]
              and parts[2].isdigit() and parts[3] == "swipe"):
            self._swipe(int(parts[2]))
        elif (len(parts) == 3 and parts[:2] == ["api", "messages"]
              and parts[2].isdigit()):
            self._message_edit(int(parts[2]))
        elif url.path == "/api/cards/import":
            self._card_import()
        elif url.path == "/api/comfy/ping":
            self._comfy_ping()
        elif url.path == "/api/comfy/slots":
            self._comfy_slots()
        elif url.path == "/api/comfy/run":
            self._comfy_run()
        elif url.path == "/api/studio/draft":
            self._studio_draft()
        elif url.path == "/api/studio/approve":
            self._studio_approve()
        elif url.path == "/api/studio/remake":
            self._studio_remake()
        elif url.path == "/api/studio/reject":
            self._tool_reject()
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "cast"):
            self._cast_edit(int(parts[2]))
        elif (len(parts) == 5 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3:] == ["export", "save"]):
            self._export_save(int(parts[2]))
        elif url.path == "/api/assets/upload":
            self._asset_upload()
        elif url.path == "/api/vram/restore":
            self._vram_restore()
        elif (len(parts) == 3 and parts[:2] == ["api", "tools"] and parts[2] == "approve"):
            self._tool_approve()
        elif (len(parts) == 3 and parts[:2] == ["api", "tools"] and parts[2] == "reject"):
            self._tool_reject()
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "memory"):
            self._chat_memory_toggle(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "examples"):
            self._chat_examples_toggle(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "aware"):
            self._chat_aware_toggle(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "texting"):
            self._chat_texting(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "title"):
            self._chat_title(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "persona"):
            self._chat_persona(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit() and parts[3] == "opening"):
            self._chat_opening(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "characters"]
              and parts[2].isdigit() and parts[3] == "fav"):
            self._card_fav(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "characters"]
              and parts[2].isdigit() and parts[3] == "export"):
            self._card_export(int(parts[2]))
        elif (len(parts) == 4 and parts[:2] == ["api", "characters"]
              and parts[2].isdigit() and parts[3] == "fields"):
            self._card_edit(int(parts[2]))
        elif (len(parts) >= 2 and parts[0] == "api" and parts[1] in VALID_TABLES):
            body = self._body()
            row_id = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else None
            try:
                self._json(rows_upsert(parts[1], body, row_id))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
        if (len(parts) == 3 and parts[:2] == ["api", "memories"] and parts[2].isdigit()):
            self._memory_delete(int(parts[2]))
        elif (len(parts) == 3 and parts[:2] == ["api", "messages"]
              and parts[2].isdigit()):
            self._message_delete(int(parts[2]))
        elif (len(parts) == 3 and parts[:2] == ["api", "assets"]
              and parts[2].isdigit()):
            self._asset_delete(int(parts[2]))
        elif (len(parts) == 3 and parts[:2] == ["api", "chats"]
              and parts[2].isdigit()):
            self._chat_delete(int(parts[2]))
        elif (len(parts) == 3 and parts[:2] == ["api", "regex"]
              and parts[2].isdigit()):
            with get_db() as conn:
                cur = conn.execute("DELETE FROM regex_rules WHERE id=?",
                                   (int(parts[2]),))
            self._json({"ok": bool(cur.rowcount)},
                       200 if cur.rowcount else 404)
        elif (len(parts) == 3 and parts[:2] == ["api", "lorebooks"]
              and parts[2].isdigit()):
            self._lore_delete(int(parts[2]))
        elif (len(parts) == 3 and parts[:2] == ["api", "characters"]
              and parts[2].isdigit()):
            self._character_delete(int(parts[2]))
        elif (len(parts) == 3 and parts[0] == "api" and parts[1] in VALID_TABLES
                and parts[2].isdigit()):
            ok = rows_delete(parts[1], int(parts[2]))
            if ok and parts[1] == "personas":
                # Same reasoning as _character_delete: a deleted persona's
                # memory bucket can never be read again — for_turn filters on
                # a persona id that no longer exists — so leaving the rows is
                # leaving invisible garbage.
                with get_db() as conn:
                    conn.execute("DELETE FROM memories WHERE persona_id=?",
                                 (int(parts[2]),))
            self._json({"ok": ok} if ok else {"error": "not found"},
                       200 if ok else 404)
        else:
            self._json({"error": "not found"}, 404)

    # -- chat --------------------------------------------------------------
    def _chat(self) -> None:
        """POST /api/chat — stream an LLM completion as SSE.

        Body: {backend, key?, model, messages, samplers?, prefill?,
               mode?: "chat"|"completion", template?: "gemma4"|...,
               thinking?: bool, thinking_prefill?: str}
        Streams content deltas as `data: {"text": ...}`, ends `data: [DONE]`.
        """
        body = self._body()
        backend = normalise_backend(body.get("backend", ""))
        model = body.get("model", "")
        messages = body.get("messages") or []
        mode = body.get("mode", "chat")
        key = body.get("key", "")
        if not key:
            for rb in load_config().get("remote_backends", []):
                if normalise_backend(rb.get("url", "")) == backend and rb.get("key"):
                    key = rb["key"]
                    break
        if not (backend and model and messages):
            self._json({"error": "backend, model and messages are required"}, 400)
            return
        samplers = body.get("samplers") or load_config().get("defaults", {})
        if mode == "completion":
            payload = llm.build_completion_payload(
                messages, model, samplers,
                template=body.get("template", "gemma4"),
                prefill=body.get("prefill", ""),
                thinking=body.get("thinking", True),
                thinking_prefill=body.get("thinking_prefill", ""),
                stream=True,
            )
        else:
            payload = llm.build_payload(
                messages, model, samplers,
                prefill=body.get("prefill", ""), stream=True,
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # NEVER send "Connection: keep-alive" on an SSE response:
        # http.server's send_header flips close_connection to False on that
        # exact value, so after [DONE] the server holds the socket open for
        # a next request — and a body with no Content-Length and no chunked
        # framing means the client cannot know it ended. A browser fetch
        # never resolves and the UI freezes on the last status line, which
        # presented as "stuck in the rendering stage even though the render
        # is done". Pinned explicitly so a future protocol_version bump
        # cannot quietly resurrect it.
        self.close_connection = True

        def send(obj) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        try:
            for kind, text in llm.stream(
                    backend, key, payload, mode,
                    in_thought=(mode == "completion" and llm.opens_thought(
                        body.get("template", "gemma4"),
                        body.get("thinking", True),
                        body.get("thinking_prefill", "")))):
                if not send({"think": text} if kind == "think" else {"text": text}):
                    return
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as exc:  # noqa: BLE001 — surface backend errors to UI
            send({"error": str(exc)})

    # -- chat engine ---------------------------------------------------------
    def _chat_new(self) -> None:
        """POST /api/chats/new {character_id, persona_id?, greeting_index?,
        mode?: "rp"|"sms", scenario?}

        When `scenario` is present (from the forge) it overrides the card's
        static scenario and its `opening` becomes the first message.
        """
        body = self._body()
        char_id = body.get("character_id")
        # No character at all is a legitimate chat: preset + jailbreak +
        # samplers + your persona, talking to the model as itself. Only a
        # character_id that was SUPPLIED and does not exist is an error.
        if char_id is not None and not rows_get("characters", int(char_id)):
            self._json({"error": "no such character"}, 400)
            return
        scenario = body.get("scenario")
        if not isinstance(scenario, dict):
            scenario = None
        with get_db() as conn:
            chat_id = engine.create_chat(
                conn, int(char_id) if char_id is not None else None,
                body.get("persona_id"), body.get("mode", "rp"),
                int(body.get("greeting_index", 0)),
                scenario=scenario,
                title=(body.get("title") or "").strip() or None,
                opening=body.get("opening"),
            )
        self._json({"ok": True, "chat_id": chat_id,
                    "scenario": scenario.get("title") if scenario else None})

    def _display_ctx(self, chat_id: int, as_user: str = "", aliases=()):
        """(mx, rules) for turning a stored message into what goes on screen.

        Messages are stored with {{user}} intact so the log stays portable
        and switching persona re-resolves the whole history, which means
        every read path has to expand. Shared by _chat_detail and _swipe so
        a swipe cannot render differently from the reload that follows it.

        `as_user` re-binds {{user}} to a pseudonym for the image export, and
        the scrubber then catches the name the model typed out longhand. Both
        default to off, so the two existing callers are unchanged.
        """
        with get_db() as conn:
            # `chats` is not in VALID_TABLES — rows_get asserts on it.
            chat = conn.execute("SELECT * FROM chats WHERE id=?",
                                (chat_id,)).fetchone()
            char = rows_get("characters", chat["character_id"]) if chat else None
            persona = (rows_get("personas", chat["persona_id"])
                       if chat and chat["persona_id"] else None)
        char_name = (char or {}).get("name") or "the character"
        user_name = (persona or {}).get("name") or "anon"
        persona_desc = (persona or {}).get("data", {}).get("description", "")
        cfields = (char or {}).get("data", {}).get("fields", {})

        scrub, redacted = name_scrubber([user_name] + list(aliases), as_user)
        if as_user:
            user_name = as_user          # bind, do not replace afterwards

        def mx(text):
            return scrub(macros.expand(text, char_name, user_name, cfields,
                                       persona_desc, str(chat_id)))

        mx.redacted = redacted           # a 1-list, read after every mx() call
        mx.scrub = scrub                 # for text that must not be expanded
        return mx, regex_rules((char or {}).get("id"))

    def _chats_list(self, q: dict) -> None:
        """GET /api/chats?character_id=&mode= — her past adventures.

        Which chat you are in used to be a localStorage fact: with no list
        route, `openChat` read a browser-side pointer and, finding none,
        created another row — so clearing site data stranded every previous
        adventure in sqlite, unreachable and indistinguishable from deleted.
        """
        char_id = (q.get("character_id") or [""])[0]
        if not str(char_id).isdigit():
            self._json({"error": "character_id required"}, 400)
            return
        mode = (q.get("mode") or ["rp"])[0]
        # Chats she LEADS, plus chats she is merely IN. A group chat used to
        # be listed only under its lead, so from a guest's side of the roster
        # the scene you were "all together" in simply did not exist — which is
        # how conversations got spread across near-identical-looking chats.
        sql = ("SELECT c.*,"
               " (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) AS n,"
               " (SELECT content FROM messages m WHERE m.chat_id=c.id"
               "   ORDER BY id DESC LIMIT 1) AS last,"
               " (SELECT content FROM messages m WHERE m.chat_id=c.id"
               "   AND m.role='user' ORDER BY id LIMIT 1) AS first_user"
               " FROM chats c WHERE (c.character_id=?"
               "   OR c.id IN (SELECT chat_id FROM chat_cast"
               "               WHERE character_id=? AND present=1))")
        args = [int(char_id), int(char_id)]
        if mode != "all":
            sql += " AND c.mode=?"
            args.append(mode)
        sql += " ORDER BY c.updated DESC, c.id DESC"
        with get_db() as conn:
            rows = [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
            char = rows_get("characters", int(char_id))
        char_name = (char or {}).get("name") or "the character"
        cfields = (char or {}).get("data", {}).get("fields", {})

        out = []
        for r in rows:
            # A cast chat's macros belong to ITS lead, not to the character
            # whose list this is — {{char}} in a snippet must name the woman
            # who actually said it.
            as_cast = r["character_id"] != int(char_id)
            lead = (rows_get("characters", r["character_id"]) or {}
                    ) if as_cast else (char or {})
            lead_name = lead.get("name") or char_name
            lead_fields = lead.get("data", {}).get("fields", {}) \
                if as_cast else cfields

            # Stored with {{user}} intact so the log stays portable — so the
            # display side has to expand, exactly as _chat_detail does.
            def mx(t, cid=r["id"], nm=lead_name, fl=lead_fields):
                return macros.expand(t or "", nm, "anon", fl, "", str(cid))
            out.append({
                "id": r["id"], "mode": r["mode"],
                "title": mx(chat_label(r)),
                "named": bool(r.get("title")),
                "messages": r["n"], "snippet": mx(r.get("last") or "")[:120],
                "persona_id": r["persona_id"],
                "has_scenario": bool((r.get("data") or {}).get("scenario")),
                # She is in it but does not lead it. The client badges these
                # so a group scene is distinguishable from a solo chat.
                "as_cast": as_cast,
                "with": lead_name if as_cast else "",
                "created": r["created"], "updated": r["updated"],
            })
        self._json({"chats": out})

    def _chat_title(self, chat_id: int) -> None:
        """POST /api/chats/{id}/title {title} — rename an adventure.

        Deliberately does NOT bump `updated`: that is the list's ordering
        key, and renaming an old chat is not activity — it would jump to the
        top and look like it had been revived.
        """
        title = (self._body().get("title") or "").strip() or None
        with get_db() as conn:
            row = conn.execute("SELECT id FROM chats WHERE id=?",
                               (chat_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            conn.execute("UPDATE chats SET title=? WHERE id=?",
                         (title, chat_id))
        self._json({"ok": True, "title": title})

    def _chat_persona(self, chat_id: int) -> None:
        """POST /api/chats/{id}/persona {persona_id|null} — rebind who YOU are.

        This is the write path the persona dropdown never had: chats stored
        their persona once at creation and nothing could change it, so
        switching the dropdown mid-chat silently did nothing. Rebinding is
        safe by the oldest design decision in the file — messages are STORED
        with {{user}} intact, so the whole history re-resolves to the new
        name on the next read. Memory follows: the chat now reads and writes
        the new persona's bucket.
        """
        body = self._body()
        pid = body.get("persona_id")
        pid = int(pid) if pid else None
        with get_db() as conn:
            if not conn.execute("SELECT 1 FROM chats WHERE id=?",
                                (chat_id,)).fetchone():
                self._json({"error": "not found"}, 404)
                return
            if pid and not conn.execute("SELECT 1 FROM personas WHERE id=?",
                                        (pid,)).fetchone():
                self._json({"error": "no such persona"}, 400)
                return
            conn.execute("UPDATE chats SET persona_id=? WHERE id=?",
                         (pid, chat_id))
        self._json({"ok": True, "persona_id": pid})

    def _chat_delete(self, chat_id: int) -> None:
        """DELETE /api/chats/{id} — the ONLY thing that destroys a chat.

        Scoped hard: chat-scope memories go, user and character memories
        outlive any single chat by design (that scoping is the whole reason
        a returning chat is not amnesiac). Assets are unlinked, never
        deleted — the gallery is keyed on character_id and must survive.
        """
        with get_db() as conn:
            row = conn.execute("SELECT id FROM chats WHERE id=?",
                               (chat_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            n = conn.execute("DELETE FROM messages WHERE chat_id=?",
                             (chat_id,)).rowcount
            m = conn.execute(
                "DELETE FROM memories WHERE chat_id=? AND kind='chat'",
                (chat_id,)).rowcount
            conn.execute("UPDATE assets SET chat_id=NULL, message_id=NULL"
                         " WHERE chat_id=?", (chat_id,))
            # Scoped cleanup, same discipline as the chat-scope memories
            # above: PRAGMA foreign_keys is off, so a chat-scoped lore link
            # would otherwise outlive its chat forever. Character- and
            # global-scoped links are NOT touched.
            conn.execute("DELETE FROM lore_links WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM chat_cast WHERE chat_id=?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        self._json({"ok": True, "messages": n, "memories": m})

    def _characters_list(self) -> None:
        """GET /api/characters — the roster, plus when she was last talked to.

        `characters.updated` is card mtime: it moves on import and on a card
        edit and never when you actually chat. The clock the roster wants is
        chats.updated, which engine.add_message bumps every turn.

        Row order stays as rows_list had it — several tests take rows[0] and
        expect the most recently imported card. The client sorts.
        """
        with get_db() as conn:
            rows = conn.execute(
                "SELECT c.*,"
                " (SELECT MAX(ch.updated) FROM chats ch"
                "   WHERE ch.character_id = c.id) AS last_seen,"
                " (SELECT COUNT(*) FROM chats ch"
                "   WHERE ch.character_id = c.id) AS chat_count"
                " FROM characters c ORDER BY c.updated DESC").fetchall()
        self._json({"rows": [_row_to_dict(r) for r in rows]})

    def _card_fav(self, char_id: int) -> None:
        """POST /api/characters/{id}/fav {on} — pin her to the top."""
        on = 1 if self._body().get("on", True) else 0
        with get_db() as conn:
            cur = conn.execute("UPDATE characters SET fav=? WHERE id=?",
                               (on, char_id))
        self._json({"ok": True, "fav": bool(on)} if cur.rowcount
                   else {"error": "not found"}, 200 if cur.rowcount else 404)

    def _chat_detail(self, chat_id: int, q=None) -> None:
        # ?user_as= / ?aliases= are the image export asking for a redacted
        # read. Absent, this is the ordinary chat load and nothing changes.
        q = q or {}
        as_user = (q.get("user_as") or [""])[0].strip()[:40]
        aliases = [a.strip() for a in (q.get("aliases") or [""])[0].split(",")
                   if a.strip()][:8]
        with get_db() as conn:
            chat = conn.execute("SELECT * FROM chats WHERE id=?",
                                (chat_id,)).fetchone()
            if not chat:
                self._json({"error": "not found"}, 404)
                return
            msgs = engine.get_messages(conn, chat_id)
            # Media generated for a message travels with it, so reopening a
            # thread shows the photos she sent rather than a gap where they
            # were. The phone needs this; the main chat benefits too.
            assets = {}
            for a in conn.execute(
                    "SELECT id, message_id, kind, path, recipe, data FROM assets"
                    " WHERE chat_id=? AND message_id IS NOT NULL"
                    " ORDER BY id", (chat_id,)).fetchall():
                # sqlite3.Row `data` columns are JSON strings — _row_to_dict
                # is not used on this query, so parse by hand.
                try:
                    ad = json.loads(a["data"] or "{}")
                except json.JSONDecodeError:
                    ad = {}
                assets.setdefault(a["message_id"], []).append(
                    {"id": a["id"], "kind": a["kind"],
                     "url": f"/api/avatars/{a['path']}",
                     "recipe": a["recipe"],
                     # scrubbed, never expanded: {{prompt}} is a ComfyUI slot
                     "prompt": ad.get("prompt", ""),
                     "seed": ad.get("seed"), "workflow": ad.get("workflow"),
                     # the job blob stays server-side: it carries on-disk
                     # reference filenames and LoRA names
                     "can_remake": bool(ad.get("job"))})
            char = rows_get("characters", chat["character_id"])
            persona = (rows_get("personas", chat["persona_id"])
                       if chat["persona_id"] else None)
        # Messages are stored with macros intact so the canonical text stays
        # portable and switching persona re-resolves the whole log. That means
        # the display side has to expand too, or the greeting shows literal
        # {{user}} in the bubble.
        mx, rules = self._display_ctx(chat_id, as_user, tuple(aliases))
        cfields = (char or {}).get("data", {}).get("fields", {})
        last = len(msgs) - 1
        chat_d = _row_to_dict(chat)
        chat_d["first_user"] = next(
            (m["content"] for m in msgs if m["role"] == "user"), "")
        chat_title = mx(chat_label(chat_d))

        out = []
        for pos, m in enumerate(msgs):
            d = m["data"]
            # think and director travel WITH the take, so a regenerated reply
            # stops showing the previous one's reasoning under it.
            active = engine.active_swipe(m)
            content = mx(active.get("content", ""))
            content, is_html = for_display(content, rules, last - pos)
            out.append({"id": m["id"], "role": m["role"], "content": content,
                        "html": is_html,
                        # both for the image export: when it happened, and
                        # what actually wrote it (see engine "gen" stamp)
                        "created": m.get("created"),
                        "gen": active.get("gen") or d.get("gen") or None,
                        # travels with the TAKE, so a re-roll as someone else
                        # carries the right face
                        "speaker": active.get("speaker") or d.get("speaker"),
                        # Both halves, for the same reason as `gen` above:
                        # add_swipe seeds swipes[0] with content/think/
                        # director only, so take 0's reason lives on the
                        # message and would otherwise show a blank chip.
                        "reason": active.get("reason") or d.get("reason") or "",
                        "swipes": len(d.get("swipes") or []),
                        "swipe_index": d.get("swipe_index"),
                        # scrubbed, not expanded: think has never been
                        # macro-expanded, but it IS drawn into the export
                        "think": mx.scrub(active.get("think", "")),
                        "director": active.get("director", ""),
                        "assets": assets.get(m["id"], [])})
        chat_out = dict(chat)
        chat_out["title"] = mx.scrub(chat_out.get("title") or "")
        with get_db() as conn:
            cast_rows = engine.cast_of(conn, chat_id, chat["character_id"])
        cast_out = []
        for c in cast_rows:
            crow = rows_get("characters", c["character_id"]) or {}
            c["char"] = crow
            cast_out.append({**c, "name": crow.get("name") or "(gone)",
                             "avatar": crow.get("avatar", "")})
        # Tombstones: someone whose stamped lines are still in this log but
        # who has no chat_cast row any more (removed outright, or her
        # character deleted). The client resolves each reply's name and face
        # out of this list, so without a tombstone her old messages silently
        # re-attribute to the LEAD on every render — the wrong-name-and-pic
        # bug, in its stored-history form. present=False keeps her out of the
        # chips and the speaker dropdown; only the lookup gains her.
        known_ids = {c["character_id"] for c in cast_rows}
        for sid in {m.get("speaker") for m in out} - {None}:
            if sid in known_ids:
                continue
            crow = rows_get("characters", sid) or {}
            cast_out.append({"character_id": sid, "present": False,
                             "ord": 999, "lead": False, "note": "",
                             "tombstone": True,
                             "name": crow.get("name") or "(gone)",
                             "avatar": crow.get("avatar", "")})
        # Who `auto` would hand the turn to if you sent right now. The SAME
        # pure function the send uses, so the rules cannot drift — but with no
        # typed text yet, so "asked directly" cannot be evaluated and the
        # answer changes the moment you name somebody. That is fine and
        # self-explanatory: the label says Mika, you type "Yuki, ..." and Yuki
        # answers. It is a preview of the default, not a promise.
        nxt = ""
        if engine.cast_active(dict(chat), cast_rows):
            here = engine.cast_present([c for c in cast_rows if c.get("char")])
            spk, _why = engine.pick_speaker(here, msgs, "")
            nxt = (spk or {}).get("char", {}).get("name", "")
        self._json({"chat": chat_out, "title": chat_title,
                    "cast": cast_out,
                    "cast_active": engine.cast_active(dict(chat), cast_rows),
                    "next_speaker": nxt,
                    "redacted": mx.redacted[0], "user_as": as_user,
                    "character": char and char["name"],
                    "avatar": char and char.get("avatar", ""),
                    "memory_enabled": bool(chat["memory_enabled"]),
                    # default on, and only meaningful when the card has any
                    "examples": bool((_row_to_dict(chat).get("data") or {})
                                     .get("examples", True)),
                    "has_examples": bool((cfields.get("mes_example") or "").strip()),
                    "aware": bool((_row_to_dict(chat).get("data") or {})
                                  .get("aware", False)),
                    "texting": (_row_to_dict(chat).get("data") or {})
                               .get("texting") or {},
                    "last_at": (msgs[-1].get("created") if msgs else 0),
                    "unprompted_today": sum(
                        1 for m in msgs
                        if (m.get("data") or {}).get("unprompted")
                        and (time.time() - (m.get("created") or 0)) < 86400),
                    "messages": out})

    def _resolve_llm(self, body: dict) -> tuple[str, str, str, dict, str, str]:
        """Shared lookup: backend/model/preset/jailbreak/prefill.

        Returns (backend, key, model, samplers, mode_template_json, prefill)."""
        cfg = load_config()
        backend = normalise_backend(body.get("backend", ""))
        key = body.get("key", "")
        # auto-attach key for configured remote backends (never exposed to UI)
        if not key:
            for rb in cfg.get("remote_backends", []):
                if normalise_backend(rb.get("url", "")) == backend and rb.get("key"):
                    key = rb["key"]
                    break
        model = body.get("model", "")
        preset = {}
        if body.get("preset_id"):
            preset = rows_get("presets", int(body["preset_id"])) or {}
        pdata = preset.get("data", {})
        samplers = pdata.get("samplers") or cfg.get("defaults", {})
        if body.get("samplers"):
            samplers = {**samplers, **body["samplers"]}
        jailbreak_text = ""
        if pdata.get("jailbreak_id"):
            jb = rows_get("jailbreaks", int(pdata["jailbreak_id"]))
            jailbreak_text = (jb or {}).get("data", {}).get("text", "")
        meta = json.dumps({"mode": pdata.get("mode", "chat"),
                           "template": pdata.get("template", "gemma4"),
                           "thinking": pdata.get("thinking", True),
                           "thinking_mode": pdata.get("thinking_mode", "normal"),
                           "thinking_prefill": pdata.get("thinking_prefill", "")})
        return backend, key, model, samplers, meta, jailbreak_text

    def _prepare_request(self, body: dict, persist: bool) -> dict:
        """Build the exact outgoing payload for a chat turn.

        This is the SINGLE code path used by both /api/chats/send and
        /api/chats/preview — the inspector cannot drift from reality because
        there is nothing to drift from. Returns
        {payload, meta, messages, prefill, backend, key, model, samplers,
         chat_row, swipe_target, stored, is_remote} or {"error", "status"}.

        persist=False builds everything but writes nothing to the database
        (the pending user turn is appended to the in-memory history only).
        """
        chat_id = int(body.get("chat_id", 0))
        backend, key, model, samplers, meta_json, jailbreak_text = \
            self._resolve_llm(body)
        meta = json.loads(meta_json)
        # live overrides from the scene rail beat the preset
        if body.get("thinking_mode") not in (None, ""):
            meta["thinking_mode"] = body["thinking_mode"]
        # An empty prefill is a real choice — it is how you ask the model to
        # open its own thought channel — so "" must reach meta instead of
        # falling through to the preset's, which made the box unclearable.
        if body.get("thinking_prefill") is not None:
            meta["thinking_prefill"] = body["thinking_prefill"]
        if not (chat_id and backend and model):
            return {"error": "chat_id, backend and model are required",
                    "status": 400}

        with get_db() as conn:
            chat_row = conn.execute("SELECT * FROM chats WHERE id=?",
                                    (chat_id,)).fetchone()
            if not chat_row:
                return {"error": "chat not found", "status": 404}
            char = rows_get("characters", chat_row["character_id"])
            # A PLAIN CHAT has no character at all — just the preset, the
            # jailbreak, the samplers and your persona, talking to the model
            # as itself. `chats.character_id` was already nullable, and
            # assemble_blocks already degrades cleanly when the card fields
            # are empty, so this is a shell rather than a branch: every layer
            # that would have drawn on a card contributes nothing, exactly as
            # if it were switched off.
            plain = char is None
            if plain:
                char = {"id": None, "name": "", "data": {"fields": {}}}
            persona = (rows_get("personas", chat_row["persona_id"])
                       if chat_row["persona_id"] else None)
            # Memories are fetched AFTER the speaker is resolved (below) —
            # they are hers, not the lead's.
            mems = []

            regenerate = bool(body.get("regenerate"))
            stored: list[str] = []
            swipe_target = None
            holding_id = None
            if regenerate:
                history = engine.get_messages(conn, chat_id)
                target_id = body.get("swipe_message_id")
                if target_id:
                    # Re-rolling an older reply rewinds the context to just
                    # before it, so she answers the same moment again rather
                    # than the end of the scene.
                    pos = next((i for i, m in enumerate(history)
                                if m["id"] == int(target_id)), None)
                    if pos is None or history[pos]["role"] != "assistant":
                        return {"error": "that message can't be re-rolled",
                                "status": 400}
                else:
                    pos = len(history) - 1
                    if pos < 0 or history[pos]["role"] != "assistant":
                        return {"error": "nothing to regenerate", "status": 400}
                swipe_target = history[pos]["id"]
                # Whoever wrote the take we are replacing. Read BEFORE the
                # truncation, because after it the last assistant turn is the
                # one *before* this bubble — routing off that would silently
                # hand a re-roll to a different character, which is the one
                # thing a re-roll must never do.
                holding_id = engine.take_speaker(history[pos])
                history = history[:pos]
                # A re-roll of a reply to a picture must see the picture.
                # `stored` was only ever populated on the fresh-send branch,
                # so every regenerate answered blind — the take being
                # replaced was written WITH the image in context. The
                # filenames live on the user turn the take answers
                # (messages.data.images, written at store time).
                last_user = next((m for m in reversed(history)
                                  if m["role"] == "user"), None)
                if last_user:
                    stored = list((last_user.get("data") or {})
                                  .get("images") or [])
            else:
                text = (body.get("text") or "").strip()
                images = body.get("images") or []
                if not text and not images:
                    return {"error": "text or images required", "status": 400}
                if images and persist:
                    ASSETS.mkdir(parents=True, exist_ok=True)
                    for i, im in enumerate(images[:4]):
                        try:
                            raw_img = base64.b64decode(im.get("b64", ""))
                        except (binascii.Error, ValueError):
                            continue
                        ext = Path(im.get("name", "img.png")).suffix or ".png"
                        fn = f"up_{int(time.time()*1000)}_{i}{ext}"
                        (ASSETS / fn).write_bytes(raw_img)
                        stored.append(fn)
                elif images:
                    stored = [im.get("name", "img.png") for im in images[:4]]
                user_text = text or "(shows her an image)"
                if persist:
                    engine.add_message(conn, chat_id, "user", user_text,
                                       {"images": stored} if stored else None)
                    history = engine.get_messages(conn, chat_id)
                else:
                    history = engine.get_messages(conn, chat_id)
                    history.append({"id": None, "role": "user",
                                    "content": user_text, "data": {}})

            # `or` the lookup, don't just guard the key: a preset_id can point
            # at a row that no longer exists (deleted preset, wiped database,
            # a browser restoring a saved selection from a previous life) and
            # rows_get returns None. Unguarded that reached engine.assemble as
            # None and took the whole send down with a 500 — the turn simply
            # died. Falling back to defaults keeps the scene playable.
            preset = (rows_get("presets", int(body["preset_id"]))
                      if body.get("preset_id") else None) or {"data": {}}

            remotes = load_config().get("remote_backends", [])
            is_remote = any(
                normalise_backend(rb.get("url", "")) == backend
                for rb in remotes)
            # The local-only rule, with its one deliberate exception: a
            # backend the user has flagged `vision: true` in settings may be
            # shown pictures. Everything ELSE keyed on is_remote — prefill
            # emulation, the stop cap, the export label — stays remote
            # behaviour; only the image-withholding is opted out of.
            vision_ok = not is_remote or any(
                normalise_backend(rb.get("url", "")) == backend
                and rb.get("vision")
                for rb in remotes)

            # Raw /completions is text-only, so a turn carrying an image has
            # to borrow the chat endpoint. Decided here rather than after
            # assembly because the thinking layer depends on the final mode.
            if meta["mode"] == "completion" and stored and vision_ok:
                meta["mode"] = "chat"
                meta["vision_fallback"] = True

            # {{char}} still has to resolve to something — a card macro left
            # unexpanded reaches the model literally. "Assistant" is the
            # honest answer when nobody is being played.
            char_name = char.get("name") or ("Assistant" if plain else "she")
            tmode = (meta.get("thinking_mode")
                     or ("normal" if meta.get("thinking", True) else "off"))
            meta["thinking"] = tmode != "off"
            if tmode == "character":
                if meta["mode"] == "completion":
                    meta["thinking_prefill"] = prompts.get(
                        "thinking_character_prefill", char=char_name,
                        extra=(meta.get("thinking_prefill") or "")).strip()

            # The server decides whether each layer has anything to say this
            # turn; the block order decides where it goes and in whose voice.
            director = (body.get("director") or "").strip()
            layers = {
                "jailbreak": jailbreak_text,
                "tools": (prompts.get("tools_spec")
                          if body.get("tools", True)
                          and load_config().get("comfyui_url") else ""),
                "director": (prompts.get("director", director=director,
                                         char=char_name) if director else ""),
                "director_note": (prompts.get("director_note", char=char_name)
                                  if body.get("director_notes") else ""),
                "sms": (prompts.get("sms", char=char_name)
                        if chat_row["mode"] == "sms" else ""),
                "rp": self._rp_digest(chat_row, char_name),
                "thinking_character": (
                    prompts.get("thinking_character", char=char_name)
                    if tmode == "character" and meta["mode"] != "completion"
                    else ""),
            }

            # Who is in the room, and who is speaking this turn. Resolved
            # once, here, so every cast branch downstream agrees.
            cast = engine.cast_of(conn, chat_id, chat_row["character_id"])
            for c in cast:
                c["char"] = rows_get("characters", c["character_id"])
            cast = [c for c in cast if c["char"]]
            multi = engine.cast_active(dict(chat_row), cast)
            speaker_id = int(body.get("speaker_id") or 0) or None
            here = engine.cast_present(cast)
            if multi:
                # ONE resolution, here, so the preview and the send cannot
                # disagree about who is about to speak. `speaker_id` from the
                # body is the human's explicit pick and always wins; with
                # nothing picked the rules decide and name themselves.
                spk, speaker_reason = engine.pick_speaker(
                    here, history, body.get("text", ""),
                    forced_id=speaker_id, holding_id=holding_id,
                    persona_name=(persona or {}).get("name", ""))
                speaker_id = spk["character_id"]
                speaker_name = spk["char"]["name"]
                others = [c["char"]["name"] for c in here if c is not spk]
                layers["cast_present"] = prompts.get(
                    "cast_present", speaker=speaker_name,
                    others=", ".join(others))
                layers["cast_turn"] = prompts.get("cast_turn",
                                                  speaker=speaker_name)
                # Text only. Whether it fires depends on the whole RETAINED
                # history being stamped, which is not knowable until the
                # budget has run — so assemble_blocks owns that gate.
                layers["cast_names"] = prompts.get("cast_names",
                                                   speaker=speaker_name)
                # Likewise text-only, and {names} is left UNSUBSTITUTED on
                # purpose: only assemble_blocks knows which of the others the
                # model has not met yet, and naming all of them would announce
                # people it has been talking to for an hour. prompts.get
                # leaves an unsupplied placeholder as literal text, which is
                # exactly the hand-off this needs.
                layers["cast_entered"] = prompts.get("cast_entered",
                                                     speaker=speaker_name)
                # Every per-character layer must name the SPEAKER, not the
                # lead — she is the one being asked to write.
                #
                # Re-rendering means re-reading the default and substituting
                # again from scratch (prompts.get), so EVERY placeholder the
                # layer takes has to be supplied a second time. `director`
                # takes two — {char} and {director} — and passing only char
                # left `{director}` in the text as a literal, which silently
                # dropped the user's stage direction in every multi-character
                # scene with the bar open. The other three take {char} alone.
                extra = {"director": {"director": director}}
                for key in ("sms", "thinking_character", "director",
                            "director_note"):
                    if layers.get(key):
                        layers[key] = prompts.get(key, char=speaker_name,
                                                  **extra.get(key, {}))
                meta["speaker_id"] = speaker_id
                meta["speaker"] = speaker_name
                meta["speaker_reason"] = speaker_reason
                # For the SSE speaker frame: the client dresses the live
                # bubble as whoever is writing BEFORE the first token, so a
                # cast reply does not stream in under the lead's face.
                meta["speaker_avatar"] = spk["char"].get("avatar", "")
                meta["cast"] = [c["char"]["name"] for c in here]

                # Stop sequences — the one anti-merge layer that WORKS,
                # because it is not a request. `cast_turn` asks her not to
                # write the others and a 12B often obliges; a stop makes it
                # impossible for the backend to continue past "\nRin:".
                #
                # Merged into a COPY. `samplers` is returned and stamped
                # verbatim into data["gen"]["samplers"], which the image
                # export's footer reads — mutating it in place would write an
                # automation's strings into the user's own record of the turn.
                cast_stops = [f"\n{c['char']['name']}:" for c in here
                              if c["character_id"] != speaker_id]
                if cast_stops:
                    mine = list(samplers.get("stop") or [])
                    # OpenAI-compatible APIs cap `stop` at 4. The USER'S OWN
                    # stops go in first and are never dropped — a power user's
                    # sampler setting losing to an automation they did not
                    # enable is exactly the inversion this project exists to
                    # avoid. Completion mode appends default_stops() AFTER
                    # this, so reserve their slots too or the cap is a lie.
                    if is_remote:
                        reserved = len(mine)
                        if meta["mode"] == "completion":
                            reserved += len(llm.default_stops(meta["template"]))
                        room = max(0, REMOTE_STOP_CAP - reserved)
                    else:
                        room = len(cast_stops)   # local backends have no cap
                    fit, cut = cast_stops[:room], cast_stops[room:]
                    if fit:
                        samplers = {**samplers, "stop": mine + fit}
                    if cut:
                        meta["stop_dropped"] = [s.strip() for s in cut]
                meta["cast_others"] = [c["char"]["name"] for c in here
                                       if c["character_id"] != speaker_id]

            # Deliberately NOT gated on `multi`. Sending the only guest
            # off-stage takes the scene back to one speaker, which is the
            # moment this warning matters most: her lines are still in the
            # history and the model will happily keep writing her. A chat that
            # never had a cast has no absent rows and so sees nothing.
            #
            # But it DECAYS. The warning exists because her lines are still
            # close enough for the model to imitate — so it fires only while
            # a message she wrote (stamped with her id) is inside the recent
            # window. Unconditioned, one dismissed guest haunted the prompt
            # of that chat forever, which read as "casting stays on after I
            # dismissed her".
            recent_speakers = {engine.take_speaker(m)
                               for m in history[-engine.CAST_ABSENT_WINDOW:]}
            gone = [c["char"]["name"] for c in cast
                    if not c["present"]
                    and c["character_id"] in recent_speakers]
            layers["cast_absent"] = (
                prompts.get("cast_absent", absent=", ".join(gone))
                if gone else "")

            if chat_row["memory_enabled"]:
                # Ranked against what is actually being said and capped, so
                # the memory block stops growing without bound. Unbounded it
                # reached 897 tokens per turn on a 155-message log.
                #
                # Keyed on the SPEAKER in a cast scene, which is why this sits
                # below the speaker resolution: memory.for_turn is user ∪
                # character(her) ∪ chat, and handing the turn's writer the
                # LEAD's relationship memories put one woman's history in
                # another's mouth — the exact leak the character scope exists
                # to prevent. Solo chats are unchanged: the speaker IS the
                # lead. `history` already carries the pending user turn, so
                # relevance ranking sees what was just typed too.
                mem_char = speaker_id if multi else chat_row["character_id"]
                recent = " ".join(m["content"] for m in history[-6:])
                mems = [f"({m['kind']}) {m['content']}" for m in
                        memory.for_turn(conn, chat_id, mem_char,
                                        recent_text=recent,
                                        limits=memory.settings(load_config()),
                                        persona_id=chat_row["persona_id"])]

            block_list = blocks.merge((preset.get("data") or {}).get("blocks"))
            # The history budget is computed against this. It was never passed
            # at all, so every chat was trimmed to the 8192 default no matter
            # what the model could actually hold — a 20k-context local model
            # was losing 12k of history for nothing.
            ctx = int((preset.get("data") or {}).get("context")
                      or load_config().get("defaults", {}).get("context_tokens")
                      or 8192)
            # Prompt-scope find/replace, applied to the history on its way
            # into the request and nowhere else. Compiled once per turn.
            # Books in play: linked to anyone present, to this chat, or
            # global. Resolved here, beside the cast, because that is where
            # the character, the chat and the connection are already in hand.
            # The card's own embedded book is appended so it keeps firing
            # exactly as it always has — unless it has been lifted out, which
            # is matched on stored provenance and never on comparing text.
            here_ids = [c["character_id"] for c in here] if multi \
                else [(char or {}).get("id")]
            lore_rows = lore_books_for(here_ids, chat_id)
            lifted = {b.get("from_card_id") for b in lore_rows}
            speaker_fields = ((rows_get("characters", speaker_id) or {})
                              .get("data", {}).get("fields", {})
                              if multi and speaker_id
                              else (char or {}).get("data", {}).get("fields", {}))
            books = list(lore_rows)
            if (char or {}).get("id") not in lifted:
                embedded = lore.from_card(speaker_fields)
                if embedded["entries"]:
                    books.append(embedded)
            layers["lore_header"] = prompts.get("lore_header")

            _rx = regex_rules((char or {}).get("id"))
            _prompt_rx = ((lambda t, d: regexrules.apply(t, _rx, "prompt", d))
                          if any(r.get("on_prompt") for r in _rx) else None)
            trace = {}
            messages, prefill = engine.assemble_blocks(
                dict(chat_row), char, persona, preset, block_list,
                mems, history, layers=layers, context_tokens=ctx,
                memory_header=prompts.get("memory_header"),
                examples_header=prompts.get("examples_header", char=char_name),
                model=model, remote=is_remote, regex=_prompt_rx, trace=trace,
                cast=cast if multi else None, speaker_id=speaker_id,
                books=books if books else None,
                lore_tokens=int(load_config().get("defaults", {})
                                .get("lore_tokens") or 0))
            if body.get("reply_prefill"):
                # AHEAD of it, not instead: the name puts the model inside
                # her line, the user's prefill says what she starts with.
                prefill = (trace.get("speaker_prefix") or "") \
                    + body["reply_prefill"]

            # vision: inline uploaded images into the CURRENT user turn — at
            # the index the engine recorded, NOT messages[-1]. Depth-0 blocks
            # (a card's post_history_instructions, cast_turn, an ST-imported
            # injection) render after the history, so messages[-1] is a
            # system message for exactly the cards people import; gated on it
            # the picture was silently dropped with no note, and the model
            # answered "take a look at this" blind — which reads as
            # hallucination, not as a dropped upload.
            # LOCAL BACKENDS ONLY — a remote provider never receives pictures.
            uidx = trace.get("last_user_idx")
            if stored and uidx is not None:
                if not vision_ok:
                    messages[uidx]["content"] += (
                        "\n[the user showed an image, but it was not sent to "
                        "this remote model — respond in character without "
                        "pretending to see details]")
                elif persist:
                    try:
                        messages[uidx] = llm.vision_message(
                            messages[uidx]["content"],
                            [str(ASSETS / fn) for fn in stored])
                    except Exception:  # noqa: BLE001 — say so, don't vanish
                        # A file missing from data/assets/ must not become a
                        # silent drop — that is the exact failure shape this
                        # block exists to end.
                        messages[uidx]["content"] += (
                            "\n[an image was attached but could not be read "
                            "back from disk — answer without it, without "
                            "pretending to see it]")
                else:
                    messages[uidx]["content"] += (
                        f"\n[+{len(stored)} image(s) attached inline: "
                        f"{', '.join(stored)}]")


        if meta["mode"] == "completion":
            payload = llm.build_completion_payload(
                messages, model, samplers, template=meta["template"],
                prefill=prefill, thinking=meta["thinking"],
                thinking_prefill=meta["thinking_prefill"], stream=True)
            # render_prompt leaves the thought channel open when there is a
            # reasoning prefill, so the stream starts mid-thought and the
            # opening marker never arrives. Carried on meta beside
            # vision_fallback — one assembly path, one place this is decided.
            meta["open_thought"] = llm.opens_thought(
                meta["template"], meta["thinking"],
                meta["thinking_prefill"])
        else:
            # chat mode used to compute meta["thinking"] and then drop it on
            # the floor, which is why the thinking selector did nothing at all
            # against LM Studio. Pass it through so the toggle reaches the
            # server's chat template.
            # thinking_prefill reaches chat mode at last. It used to be read
            # only by render_prompt (completion mode), so on a chat-mode
            # backend the reasoning prefill — the strongest jailbreak vector
            # this project has — was silently never sent at all, and the
            # inspector showed a field that did nothing. Moonshot's partial
            # mode is the one chat-mode wire format that carries it; models
            # that do not take it are unaffected (llm.build_payload gates on
            # the id), so no existing turn changes.
            meta["partial_prefill"] = bool(
                meta["thinking_prefill"] and meta["thinking"]
                and llm.wants_partial_reasoning(model))
            payload = llm.build_payload(messages, model, samplers,
                                        prefill=prefill, stream=True,
                                        force_prefill=is_remote,
                                        thinking=meta["thinking"],
                                        thinking_prefill=meta["thinking_prefill"])

        meta["context_tokens"] = ctx
        # so a model swap loads at the window this turn was budgeted against
        _ctx_hint.tokens = ctx
        return {"payload": payload, "meta": meta, "messages": messages,
                "segments": trace.get("segments", []), "trace": trace,
                "prefill": prefill, "backend": backend, "key": key,
                "model": model, "samplers": samplers, "chat_row": chat_row,
                "swipe_target": swipe_target, "stored": stored,
                "is_remote": is_remote, "vision_ok": vision_ok,
                "regenerate": regenerate}

    def _chat_preview(self) -> None:
        """POST /api/chats/preview — the exact payload, nothing sent anywhere.

        Same body as /api/chats/send. Writes nothing to the database and makes
        no upstream request; returns the literal payload plus a rendered view
        of what the model will read. API keys are never included.
        """
        req = self._prepare_request(self._body(), persist=False)
        if "error" in req:
            self._json({"error": req["error"]}, req.get("status", 400))
            return
        payload = req["payload"]
        meta = req["meta"]

        # a human-readable rendering of the wire format
        if meta["mode"] == "completion":
            rendered = payload.get("prompt", "")
        else:
            parts = []
            for m in payload["messages"]:
                content = m.get("content")
                if isinstance(content, list):  # multimodal
                    bits = []
                    for p in content:
                        if p.get("type") == "text":
                            bits.append(p.get("text", ""))
                        else:
                            bits.append("<image data-url omitted>")
                    content = "\n".join(bits)
                parts.append(f"───── {m['role']} ─────\n{content}")
            rendered = "\n\n".join(parts)

        # Provenance for the rendered view: which block, and which editable
        # prompt layer, produced each stretch of text. Built from the
        # pre-payload messages because build_payload strips the tags on their
        # way to the wire — the inspector is the one consumer that wants them.
        segments = []
        for seg in req.get("segments", []):
            parts = seg.get("parts") or [
                {"id": "", "name": "unattributed", "builtin": False,
                 "layer": "", "marker": "", "content": ""}]
            segments.append({"role": seg["role"], "parts": [
                {"id": p.get("id", ""), "name": p.get("name", ""),
                 "builtin": bool(p.get("builtin")), "layer": p.get("layer", ""),
                 "marker": p.get("marker", ""),
                 "tokens": engine.rough_tokens(p.get("content") or ""),
                 "content": p.get("content") if isinstance(p.get("content"), str)
                            else "<image omitted>"}
                for p in parts]})

        wire = {k: v for k, v in payload.items() if k != "messages"}
        wire_messages = []
        for m in payload.get("messages", []):
            c = m.get("content")
            if isinstance(c, list):
                c = [{**p, "image_url": {"url": "<omitted>"}}
                     if p.get("type") == "image_url" else p for p in c]
            wire_messages.append({**m, "content": c})
        if wire_messages:
            wire["messages"] = wire_messages

        self._json({
            "ok": True,
            "mode": meta["mode"],
            "template": meta.get("template") if meta["mode"] == "completion" else None,
            "backend": req["backend"],
            "model": req["model"],
            "is_remote": req["is_remote"],
            "vision_ok": req["vision_ok"],
            "vision_fallback": bool(meta.get("vision_fallback")),
            "thinking": meta.get("thinking"),
            "thinking_mode": meta.get("thinking_mode"),
            "prefill": req["prefill"],
            # The inspector resolved the speaker and never said so. Same
            # values the send will use, because it is the same call.
            "speaker": meta.get("speaker"),
            "speaker_id": meta.get("speaker_id"),
            "speaker_reason": meta.get("speaker_reason"),
            "rendered": rendered,
            "segments": segments + _lore_overflow(req),
            "wire": wire,
            "stats": {
                "messages": len(payload.get("messages", [])),
                "chars": len(rendered),
                "approx_tokens": max(1, len(rendered) // 4),
                "system_chars": len(req["messages"][0]["content"]) if req["messages"] else 0,
                # The window the history budget was actually computed against,
                # so the inspector and the block editor stop guessing at it.
                "context_tokens": req["meta"].get("context_tokens", 8192),
            },
        })

    def _chat_send(self) -> None:
        """POST /api/chats/send — user message in, streamed reply out (SSE).

        Body: {chat_id, text?, images?, backend, model, key?, preset_id?,
               regenerate?, director?, tools?, samplers?, thinking_mode?,
               thinking_prefill?, reply_prefill?}
        On regenerate no user message is appended; a new swipe is generated
        for the last assistant message.
        """
        body = self._body()
        req = self._prepare_request(body, persist=True)
        if "error" in req:
            self._json({"error": req["error"]}, req.get("status", 400))
            return
        payload = req["payload"]
        meta = req["meta"]
        backend, key, model = req["backend"], req["key"], req["model"]
        chat_row = req["chat_row"]
        chat_id = int(body.get("chat_id", 0))
        swipe_target = req["swipe_target"]
        regenerate = req["regenerate"]
        prefill = req["prefill"]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # This route never sent Connection: keep-alive, which is exactly why
        # chat streaming always terminated cleanly while the studio SSE
        # routes hung (see the note on those). Pinned here too so the four
        # routes cannot drift apart again.
        self.close_connection = True

        def send(obj) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        if meta.get("vision_fallback"):
            send({"notice": "image attached — using the chat endpoint for this "
                            "turn so she can actually see it (raw completion "
                            "mode is text-only)"})

        # Said once, on the turn it applies to, because the consequence is
        # hers writing somebody else's lines and the user needs to know why.
        if meta.get("stop_dropped"):
            send({"notice": "this backend only takes four stop sequences, so "
                            + ", ".join(meta["stop_dropped"])
                            + " did not fit — she may write for them"})

        # Who is writing, announced BEFORE the first token. The live bubble is
        # built client-side with no speaker on it, so without this frame a
        # cast reply streamed in wearing the LEAD's name and face for its
        # whole duration and only snapped right on the reload afterwards.
        if meta.get("speaker_id"):
            send({"speaker": {"id": meta["speaker_id"],
                              "name": meta.get("speaker", ""),
                              "avatar": meta.get("speaker_avatar", ""),
                              "reason": meta.get("speaker_reason", "")}})

        # A prefill is only part of the reply when the backend GENUINELY
        # continues it. Local llama.cpp / LM Studio / TabbyAPI do; a remote
        # is emulated — build_payload asks in-band for a reply beginning with
        # that text, which CLAUDE.md calls "a soft request the model may
        # decline" and the UI badges as emulated. Prepending it anyway
        # fabricated text the model never wrote: measured on kimi-k3, a
        # jailbreak scaffold prefill ("I should continue the story…") was
        # stapled to the front of every stored reply and rendered in the
        # bubble, while the stream showed the prose alone — so it appeared
        # only after a reload. If the model DOES comply the text arrives in
        # reply_parts on its own, where it belongs.
        # …with one exception: Moonshot partial mode is a REAL continuation,
        # not an instruction, so a reply prefill sent that way is genuinely
        # the start of the reply and has to be kept.
        stored_prefill = prefill if (not req["is_remote"]
                                     or meta.get("partial_prefill")) else ""
        reply_parts, think_parts = [], []
        try:
            for kind, chunk_text in llm.stream(
                    backend, key, payload, meta["mode"],
                    in_thought=meta.get("open_thought", False)):
                if kind == "think":
                    think_parts.append(chunk_text)
                    if not send({"think": chunk_text}):
                        return
                else:
                    reply_parts.append(chunk_text)
                    if not send({"text": chunk_text}):
                        return
        except Exception as exc:  # noqa: BLE001
            send({"error": str(exc)})
            return

        reply = (stored_prefill + "".join(reply_parts)).strip()
        # Thinking models can spend the entire budget before the first visible
        # word — measured repeatedly on gemma-4-12b. `llm.once_retry` covers
        # the non-streaming helpers; the streamed turn had nothing, so it just
        # landed as a blank message and read as "she said nothing".
        #
        # Escalating ONCE is not enough, and the reason is worth stating
        # because it is easy to reach for a cleverer formula instead: the
        # failed attempt tells you nothing about how much room the model
        # NEEDS, only that it needs more than it had — its reasoning is
        # capped by the very budget it exhausted, so any arithmetic on
        # "how much it thought" is bounded by the number that already
        # failed. Measured on gemma-4-12b with in-character thinking (the
        # worst case: the persona makes the reasoning discursive), a single
        # 3x escalation from 1200 to 3600 still came back empty in 2 of 3
        # runs. So keep tripling until there is a reply or the ceiling is
        # reached, and say so each time.
        tries = 0
        while not reply and think_parts and tries < MAX_THINK_ESCALATIONS:
            current = int(payload.get("max_tokens") or 1024)
            # A multiplier alone is useless when the original budget was tiny —
            # 2.5x of 120 is still less than this model's reasoning. Jump to a
            # floor that can actually hold reasoning plus a reply.
            bigger = dict(payload, max_tokens=min(16000, max(current * 3, 2048)))
            if bigger["max_tokens"] <= current:
                break                       # already at the ceiling
            tries += 1
            payload = bigger
            send({"notice": "she spent the whole budget thinking — retrying "
                            f"with {bigger['max_tokens']} tokens"})
            # The abandoned attempt's output must not be carried forward. Its
            # reasoning would otherwise be stored alongside the take that
            # replaced it — the thought panel showing the discarded train of
            # thought concatenated with the kept one, thousands of characters
            # of it on a heavy reasoner (measured on kimi-k3: 14k stored for a
            # reply whose own reasoning was a fraction of that).
            think_parts, reply_parts = [], []
            try:
                for kind, chunk_text in llm.stream(
                        backend, key, bigger, meta["mode"],
                        in_thought=meta.get("open_thought", False)):
                    if kind == "think":
                        think_parts.append(chunk_text)
                        if not send({"think": chunk_text}):
                            return
                    else:
                        reply_parts.append(chunk_text)
                        if not send({"text": chunk_text}):
                            return
            except Exception as exc:  # noqa: BLE001
                send({"error": str(exc)})
                return
            reply = (stored_prefill + "".join(reply_parts)).strip()

        # Still nothing, and no reasoning to blame it on. A hosted model that
        # declines by returning an EMPTY completion is a real refusal mode —
        # measured on kimi-k3 with thinking off, which answers explicit
        # prompts with zero tokens and no error. The escalation above cannot
        # help (there is no thinking to give room to), and storing the blank
        # silently is the worst of both: an empty bubble that reads as CoomKit
        # being broken rather than as the model refusing. Say which it was.
        if not reply and not think_parts:
            send({"notice": "the model returned nothing at all — usually a "
                            "quiet refusal on a hosted provider. try a "
                            "jailbreak, a reply prefill, or a different "
                            "model; nothing was saved."})

        think = "".join(think_parts)
        visible, tool_call = tools.split_tool_call(reply)
        visible, note = tools.split_director_note(visible)
        # Belt to the stop sequences' braces. A stop that did not fit, a
        # backend that ignores them, or a model that writes "Rin:" mid-reply
        # all land here. The remainder is dropped rather than stored: it was
        # generated with the wrong card in the prompt.
        # The prefill is prepended to what came back, so a prefixed turn
        # arrives starting with her own name. Strip it before storing or the
        # next turn labels it twice — and the log stops being portable.
        if meta.get("speaker"):
            visible = engine.strip_speaker_prefix(visible, [meta["speaker"]])
        leaked = ""
        if meta.get("cast_others"):
            visible, leaked = engine.trim_cast_leak(visible,
                                                    meta["cast_others"])
        data = {"think": think}
        if note:
            data["director"] = note
        # Nothing recorded what wrote what, so the image export could only
        # describe the user's CURRENT settings and hope. Stamped on the take,
        # so a swipe carries the model that produced that swipe rather than
        # the one that happened to be loaded when you exported.
        # Who said it. Only stamped in a multi-character scene, so a solo
        # message is byte-identical to before and `speaker` stays absent
        # rather than redundantly naming the only character in the chat.
        if meta.get("speaker_id"):
            data["speaker"] = meta["speaker_id"]
            # Why she got the turn, stamped on the TAKE alongside the speaker
            # and `gen`, so a swipe carries the reason that produced *that*
            # swipe. Auto that cannot explain itself reads as randomness.
            data["reason"] = meta.get("speaker_reason", "")
        data["gen"] = {
            "model": model,
            "backend": backend_label(backend),
            "preset": (rows_get("presets", int(body["preset_id"])) or {}).get("name", "")
                      if str(body.get("preset_id") or "").isdigit() else "",
            "mode": meta.get("mode", "chat"),
            "samplers": req.get("samplers") or {},
        }
        # A turn that produced NOTHING — no prose, no reasoning, no tool call,
        # no director note — is not stored. There is nothing in it to read, and
        # storing it costs twice: a blank bubble in the log that reads as
        # CoomKit breaking rather than the model declining, and (on a re-roll)
        # a blank swipe REPLACING a take that was fine. The user's own message
        # is already stored, so the scene is intact and they can simply send
        # again. The notice above says what happened.
        msg_id = None
        empty_turn = not (visible.strip() or think.strip() or tool_call or note)
        if not empty_turn:
            with get_db() as conn:
                if swipe_target is not None:
                    engine.add_swipe(conn, swipe_target, visible, data)
                    msg_id = swipe_target
                else:
                    msg_id = engine.add_message(
                        conn, chat_id, "assistant", visible, data)
                if leaked:
                    # Counted per chat, because one trimmed reply is the system
                    # working and a steady trickle of them is the model telling
                    # you the scene has too many people in it for its size.
                    cd = json.loads(chat_row["data"] or "{}") \
                        if chat_row["data"] else {}
                    cd["cast_leaks"] = int(cd.get("cast_leaks") or 0) + 1
                    conn.execute("UPDATE chats SET data=? WHERE id=?",
                                 (json.dumps(cd), chat_id))
                    send({"notice": f"she started writing {leaked} — trimmed"})
                    if cd["cast_leaks"] == 3:
                        send({"notice": "this model keeps writing everyone. Try "
                                        "two people in the scene instead of four."})
        if note:
            send({"director_note": note})
        send({"done": True, "message_id": msg_id, "full": visible})

        # tool call? -> dialect rewrite -> pending approval
        if tool_call and isinstance(tool_call, dict) \
                and tool_call.get("recipe") in recipes.RECIPES:
            # She named a shot rather than writing a prompt. Run it through the
            # same studio path the buttons use, so she gets the right workflow,
            # her reference images and the pre-flight review — instead of a
            # free-form prompt aimed at whatever workflow happens to be first.
            try:
                self._studio_pending_from_tool(
                    tool_call, chat_id, msg_id, chat_row, body, send)
            except Exception as exc:  # noqa: BLE001
                send({"notice": f"couldn't set that up: {exc}"})
        elif tool_call and isinstance(tool_call, dict):
            draft = tool_call.get("prompt", "")
            rewritten = draft
            if draft:
                try:
                    rw_msgs = tools.rewrite_prompt(
                        draft, tools.DEFAULT_SKILL.get(
                            str(tool_call.get("workflow", "")), "anima.md"))
                    rw_payload = llm.build_payload(
                        rw_msgs, model, {"max_tokens": 800, "temperature": 0.4},
                        stream=False)
                    rewritten = llm.once_retry(backend, key, rw_payload,
                                         meta["mode"]).strip() or draft
                except Exception:  # noqa: BLE001 — fall back to raw draft
                    rewritten = draft
            pid = tools.register({**tool_call, "chat_id": chat_id,
                                  "message_id": msg_id}, rewritten)
            send({"tool_pending": {"id": pid, "call": tool_call,
                                   "prompt": rewritten}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

        # Memory extraction, in the background and only every Nth reply.
        # Per-turn extraction was an LLM call per message that mostly found
        # nothing new — but a helpful model always answers, and that answer
        # was the clog. A chunk of several turns also yields better facts than
        # one exchange read alone.
        if chat_row["memory_enabled"] and not regenerate and visible.strip():
            mem_cfg = memory.settings(load_config())
            with get_db() as conn:
                turns = conn.execute(
                    "SELECT count(*) FROM messages WHERE chat_id=?"
                    " AND role='assistant'", (chat_id,)).fetchone()[0]
            if memory.should_extract(turns, mem_cfg["every_n_turns"]):
                # Attributed to the SPEAKER of the reply being read, not the
                # chat's lead. In a cast scene the lead was credited with
                # every guest's relationship — facts about one woman filed
                # under another, then injected as the second one's own
                # memories on her later turns.
                self._extract_memories_bg(
                    chat_id,
                    meta.get("speaker_id") or chat_row["character_id"],
                    body.get("text", ""),
                    visible, backend, key, model, meta)

    def _extract_memories_bg(self, chat_id, character_id, last_user,
                             last_reply, backend, key, model, meta) -> None:
        def work():
            try:
                with get_db() as conn:
                    # The persona is ALREADY KNOWN — it is injected into every
                    # prompt, so the model "discovering" the user's name in a
                    # reply is it reading its own system prompt back. Handed
                    # to the extractor as existing knowledge so it is never
                    # recorded as a memory; memory.sanitize_facts below is the
                    # belt for an extractor that records it anyway.
                    prow = conn.execute(
                        "SELECT c.persona_id AS pid, p.name, p.data"
                        " FROM chats c LEFT JOIN personas p"
                        " ON p.id = c.persona_id"
                        " WHERE c.id=?", (chat_id,)).fetchone()
                    persona_id = prow["pid"] if prow else None
                    existing = [m["content"] for m in
                                memory.for_turn(conn, chat_id, character_id,
                                                persona_id=persona_id)]
                    crow = conn.execute(
                        "SELECT name FROM characters WHERE id=?",
                        (character_id,)).fetchone()
                persona_name, persona_desc = "", ""
                if prow and prow["name"]:
                    persona_name = prow["name"] or ""
                    try:
                        persona_desc = (json.loads(prow["data"] or "{}")
                                        .get("description") or "")
                    except json.JSONDecodeError:
                        pass
                char_name = (crow["name"] if crow else "") or ""
                known = memory.persona_known(persona_name, persona_desc)
                # With no persona picked, {{user}} still expands to "anon"
                # (the macros fallback) in every prompt — so "the user is
                # called anon" is the model reading its own prompt back, and
                # the naming guard must know the name it was actually shown.
                guard_name = persona_name or "anon"

                def llm_once(msgs):
                    payload = llm.build_payload(
                        msgs, model, {"max_tokens": 900, "temperature": 0.2},
                        stream=False)
                    return llm.once_retry(backend, key, payload, "chat")
                facts = memory.extract_memories(
                    llm_once, known + existing, last_user, last_reply,
                    system=prompts.get("memory_extract"))
                cfg = memory.settings(load_config())
                facts = memory.sanitize_facts(
                    facts, guard_name, persona_desc, char_name,
                    cfg["dupe_threshold"])
                if facts:
                    with get_db() as conn:
                        memory.store_memories(conn, chat_id, character_id,
                                              facts, cfg["dupe_threshold"],
                                              persona_id=persona_id)

                # Once a scope gets fat, merge it rather than letting it grow.
                # Compression, not forgetting — consolidate() refuses a result
                # that grew or that threw most of the detail away. Bucketed by
                # persona: consolidating THIS persona's rows must neither read
                # nor delete another persona's.
                with get_db() as conn:
                    for scope in ("character", "user"):
                        rows = [dict(r) for r in conn.execute(
                            "SELECT * FROM memories WHERE kind=?"
                            " AND persona_id IS ?"
                            + (" AND character_id=?" if scope == "character" else ""),
                            (scope, persona_id, character_id)
                            if scope == "character"
                            else (scope, persona_id)).fetchall()]
                        if len(rows) < cfg["consolidate_at"]:
                            continue
                        merged = memory.consolidate(
                            llm_once, rows,
                            system=prompts.get("memory_consolidate"))
                        if merged:
                            memory.replace_scope(conn, scope, merged,
                                                 chat_id, character_id,
                                                 persona_id=persona_id)
            except Exception:  # noqa: BLE001 — memory must never break chat
                pass
        threading.Thread(target=work, daemon=True).start()

    def _prompts_write(self) -> None:
        """POST /api/prompts {key, text} — override one prompt layer.

        Sending empty text, or text identical to the default, removes the
        override rather than storing a redundant copy.
        """
        body = self._body()
        key = (body.get("key") or "").strip()
        if not prompts.set_one(key, body.get("text") or ""):
            self._json({"error": f"unknown prompt key: {key}"}, 400)
            return
        self._json({"ok": True, "prompts": prompts.catalog()})

    # -- scenario forge ------------------------------------------------------
    def _scenario_context(self, body: dict):
        """Shared lookup for suggest/refine: card, persona, memory scope."""
        char = rows_get("characters", int(body.get("character_id") or 0))
        if not char:
            return None, None, [], {"error": "valid character_id required",
                                    "status": 400}
        persona = (rows_get("personas", int(body["persona_id"]))
                   if body.get("persona_id") else None)
        # continuity is opt-out: the caller says whether she should remember
        mems = []
        if body.get("use_memory", True):
            with get_db() as conn:
                mems = [f"({m['kind']}) {m['content']}"
                        for m in memory.for_scenario(
                            conn, char["id"],
                            persona_id=persona["id"] if persona else None)]
        return char, persona, mems, None

    def _scenarios_suggest(self) -> None:
        """POST /api/scenarios/suggest — pitch fresh scenes for a character.

        Body: {character_id, persona_id?, brief?, count?, use_memory?,
               backend, model, key?}
        """
        body = self._body()
        char, persona, mems, err = self._scenario_context(body)
        if err:
            self._json({"error": err["error"]}, err["status"])
            return
        backend, key, model, _s, _m, _jb = self._resolve_llm(body)
        if not (backend and model):
            self._json({"error": "backend and model are required"}, 400)
            return
        count = max(1, min(int(body.get("count", 3)), 5))
        messages = scenarios.build_suggest_messages(
            char, persona, mems, body.get("brief", ""), count,
            system=prompts.get("forge_suggest"))
        payload = llm.build_payload(
            messages, model,
            {"max_tokens": 10000, "temperature": 1.0, "top_p": 0.95},
            stream=False)
        try:
            raw = llm.once_retry(backend, key, payload, "chat")
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 502)
            return
        found = scenarios.parse_scenarios(raw)
        if not found:
            self._json({"error": "could not parse a scenario out of the reply",
                        "raw": (raw or "")[:600]}, 502)
            return
        self._json({"ok": True, "scenarios": found,
                    "used_memory": bool(mems), "memory_count": len(mems)})

    def _scenarios_refine(self) -> None:
        """POST /api/scenarios/refine — revise one scenario from feedback.

        Body: {character_id, persona_id?, scenario, instruction, use_memory?,
               backend, model, key?}
        """
        body = self._body()
        scenario = body.get("scenario")
        instruction = (body.get("instruction") or "").strip()
        if not isinstance(scenario, dict) or not instruction:
            self._json({"error": "scenario and instruction are required"}, 400)
            return
        char, persona, mems, err = self._scenario_context(body)
        if err:
            self._json({"error": err["error"]}, err["status"])
            return
        backend, key, model, _s, _m, _jb = self._resolve_llm(body)
        if not (backend and model):
            self._json({"error": "backend and model are required"}, 400)
            return
        messages = scenarios.build_refine_messages(
            char, persona, mems, scenario, instruction,
            system=prompts.get("forge_refine"))
        payload = llm.build_payload(
            messages, model,
            {"max_tokens": 4000, "temperature": 0.9}, stream=False)
        try:
            raw = llm.once_retry(backend, key, payload, "chat")
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 502)
            return
        revised = scenarios.parse_one(raw)
        if not revised:
            self._json({"error": "could not parse the revision",
                        "raw": (raw or "")[:600]}, 502)
            return
        self._json({"ok": True, "scenario": revised})

    # -- memories ------------------------------------------------------------
    # -- regex rules ---------------------------------------------------------
    def _regex_row(self, row) -> dict:
        out = dict(row)
        data = json.loads(out.pop("data", "") or "{}") or {}
        out["trim"] = data.get("trim") or []
        out["note"] = data.get("note") or ""
        # A rule that will not compile is worth showing as broken rather than
        # as a rule that mysteriously does nothing.
        try:
            regexrules.compile_js(out["pattern"], out["replace"])
            out["problem"] = ""
        except regexrules.RuleError as exc:
            out["problem"] = str(exc)
        return out

    def _regex_list(self, query: dict) -> None:
        """GET /api/regex[?character_id=N] — the whole chain, in order."""
        cid = (query.get("character_id") or [""])[0]
        with get_db() as conn:
            if cid.isdigit():
                rows = conn.execute(
                    "SELECT * FROM regex_rules WHERE character_id IS NULL"
                    " OR character_id=? ORDER BY character_id IS NOT NULL,"
                    " ord, id", (int(cid),)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM regex_rules ORDER BY"
                    " character_id IS NOT NULL, ord, id").fetchall()
        self._json({"rows": [self._regex_row(r) for r in rows],
                    "allowed_html": sorted(regexrules.ALLOWED)})

    def _regex_write(self) -> None:
        """POST /api/regex — create or update one rule."""
        body = self._body()
        rid = body.get("id")
        try:
            regexrules.compile_js(body.get("pattern", ""),
                                  body.get("replace", ""))
        except regexrules.RuleError as exc:
            self._json({"error": str(exc)}, 400)
            return
        fields = {
            "name": (body.get("name") or "rule").strip()[:80],
            "pattern": body.get("pattern") or "",
            "replace": body.get("replace") or "",
            "on_prompt": int(bool(body.get("on_prompt"))),
            "on_display": int(bool(body.get("on_display", True))),
            "min_depth": body.get("min_depth"),
            "max_depth": body.get("max_depth"),
            "enabled": int(bool(body.get("enabled", True))),
            "ord": int(body.get("ord") or 0),
            "character_id": body.get("character_id"),
            "data": json.dumps({"trim": body.get("trim") or [],
                                "note": body.get("note") or ""}),
            "updated": time.time(),
        }
        with get_db() as conn:
            if rid:
                conn.execute(
                    "UPDATE regex_rules SET " +
                    ", ".join(f"{k}=?" for k in fields) + " WHERE id=?",
                    (*fields.values(), int(rid)))
                new_id = int(rid)
            else:
                fields["created"] = time.time()
                cur = conn.execute(
                    "INSERT INTO regex_rules (" + ",".join(fields) + ")"
                    " VALUES (" + ",".join("?" * len(fields)) + ")",
                    tuple(fields.values()))
                new_id = cur.lastrowid
            row = conn.execute("SELECT * FROM regex_rules WHERE id=?",
                               (new_id,)).fetchone()
        self._json({"ok": True, "rule": self._regex_row(row)})

    # ── lorebooks ────────────────────────────────────────────────────
    def _lore_list(self) -> None:
        """GET /api/lorebooks[?character_id=N&chat_id=M] — books and their
        attachment state FOR THIS CONTEXT."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        cid = int((q.get("character_id") or [0])[0] or 0)
        chid = int((q.get("chat_id") or [0])[0] or 0)
        out = []
        with get_db() as conn:
            for r in conn.execute("SELECT * FROM lorebooks ORDER BY ord, id"):
                n, always = conn.execute(
                    "SELECT count(*), sum(constant AND enabled)"
                    " FROM lore_entries WHERE book_id=?", (r["id"],)).fetchone()
                links = [dict(x) for x in conn.execute(
                    "SELECT character_id, chat_id FROM lore_links"
                    " WHERE book_id=?", (r["id"],))]
                # The select shows the state for the CURRENT context and must
                # never destroy a link belonging to another character or chat,
                # so a setting book on five girls stays on five girls.
                scope = "off"
                if any(l["character_id"] is None and l["chat_id"] is None
                       for l in links):
                    scope = "always"
                elif cid and any(l["character_id"] == cid for l in links):
                    scope = "character"
                elif chid and any(l["chat_id"] == chid for l in links):
                    scope = "chat"
                data = json.loads(r["data"] or "{}")
                out.append({"id": r["id"], "name": r["name"],
                            "source": r["source"], "enabled": bool(r["enabled"]),
                            "entries": n, "always_on": always or 0,
                            "scan_depth": r["scan_depth"], "scope": scope,
                            "links": len(links),
                            "notes": data.get("notes") or [],
                            # so the character viewer can stop offering to
                            # lift a book it has already lifted
                            "from_card_id": data.get("from_card_id") or 0,
                            "problem": data.get("problem") or ""})
        self._json({"rows": out})

    def _lore_write(self) -> None:
        """POST /api/lorebooks — rename, toggle, reorder. Not an entry editor."""
        body = self._body()
        bid = int(body.get("id") or 0)
        if not bid:
            self._json({"error": "id required"}, 400)
            return
        sets, vals = [], []
        for key, col in (("name", "name"), ("enabled", "enabled"),
                         ("ord", "ord"), ("scan_depth", "scan_depth")):
            if key in body:
                sets.append(f"{col}=?")
                vals.append(int(body[key]) if key != "name"
                            else str(body[key])[:120])
        if not sets:
            self._json({"error": "nothing to change"}, 400)
            return
        with get_db() as conn:
            cur = conn.execute(
                f"UPDATE lorebooks SET {','.join(sets)}, updated=? WHERE id=?",
                vals + [time.time(), bid])
        self._json({"ok": bool(cur.rowcount)}, 200 if cur.rowcount else 404)

    def _lore_import(self) -> None:
        """POST /api/lorebooks/import — a world file, or lift one out of a card.

        Two-phase like the regex importer: `dry_run` returns what the import
        WOULD do so the confirm can be honest about what is refused, then the
        client posts again for real.

        Body: {b64 | json | from_character_id, name?, dry_run?, attach?}
        """
        body = self._body()
        raw = body.get("json")
        from_char = int(body.get("from_character_id") or 0)
        name = (body.get("name") or "").strip()[:120]

        if from_char:
            row = rows_get("characters", from_char)
            if not row:
                self._json({"error": "no such character"}, 404)
                return
            fields = (row.get("data") or {}).get("fields") or {}
            raw = fields.get("character_book")
            if not raw or not (raw.get("entries") or []):
                self._json({"error": "that card has no lorebook in it"}, 400)
                return
            name = name or f"{row['name']}'s book"
        elif raw is None and body.get("b64"):
            try:
                raw = json.loads(base64.b64decode(body["b64"])
                                 .decode("utf-8", "replace"))
            except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
                self._json({"error": f"could not read that file: {exc}"}, 400)
                return

        kind = lore.detect(raw)
        if not kind:
            self._json({"error": "I don't know that shape. I can read a "
                                 "SillyTavern World Info file, or a character "
                                 "card that has a lorebook in it."}, 400)
            return
        # A whole card was dropped in: dig the book out of it.
        if kind == "card" and not isinstance(raw.get("entries"), list):
            raw = (raw.get("character_book")
                   or (raw.get("data") or {}).get("character_book"))
        book = (lore.from_card_book(raw, name) if kind == "card"
                else lore.from_st_world(raw, name))
        if not book["entries"]:
            self._json({"error": "nothing importable in that file"}, 400)
            return
        summary = lore.summarise_import(book)
        if body.get("dry_run"):
            self._json({"ok": True, "preview": True, "summary": summary})
            return

        with get_db() as conn:
            bid = _book_store(conn, book, name)
            if from_char:
                # Provenance, so the embedded book is skipped for her from now
                # on. Matched on THIS, never on comparing text — a text
                # comparison is the silent near-miss that produces every entry
                # twice.
                conn.execute(
                    "UPDATE lorebooks SET data=? WHERE id=?",
                    (json.dumps({"notes": book["notes"],
                                 "from_card_id": from_char}), bid))
            scope = body.get("attach") or ("character" if from_char else "")
            if scope:
                _lore_link(conn, bid, scope,
                           from_char or int(body.get("character_id") or 0),
                           int(body.get("chat_id") or 0))
        self._json({"ok": True, "id": bid, "summary": summary})

    def _lore_link_route(self) -> None:
        """POST /api/lorebooks/link {id, scope, character_id?, chat_id?}

        scope is off | chat | character | always, mapping exactly onto rows:
        no link / (NULL, chat) / (character, NULL) / (NULL, NULL).
        """
        body = self._body()
        bid = int(body.get("id") or 0)
        scope = body.get("scope") or "off"
        if not bid or scope not in ("off", "chat", "character", "always"):
            self._json({"error": "id and a valid scope required"}, 400)
            return
        with get_db() as conn:
            if not conn.execute("SELECT 1 FROM lorebooks WHERE id=?",
                                (bid,)).fetchone():
                self._json({"error": "no such book"}, 404)
                return
            _lore_link(conn, bid, scope,
                       int(body.get("character_id") or 0),
                       int(body.get("chat_id") or 0))
        self._json({"ok": True, "scope": scope})

    def _character_delete(self, char_id: int) -> None:
        """DELETE /api/characters/<id> — her, and what only pointed at her.

        Its own handler rather than the generic VALID_TABLES branch, which is
        shared by four other tables and cleans up nothing. PRAGMA foreign_keys
        is off, so a deleted character otherwise leaves lore links and cast
        rows behind forever — the same debt class as the orphaned chats
        already sitting in a dev database here.
        """
        if not rows_get("characters", char_id):
            self._json({"error": "not found"}, 404)
            return
        with get_db() as conn:
            conn.execute("DELETE FROM lore_links WHERE character_id=?",
                         (char_id,))
            conn.execute("DELETE FROM chat_cast WHERE character_id=?",
                         (char_id,))
            # Her relationship record goes with her. Character-scope rows for
            # a deleted character are invisible to every read path and were
            # accumulating forever. Chat-scope rows survive: they belong to
            # the chat, which has its own delete discipline.
            conn.execute("DELETE FROM memories WHERE kind='character'"
                         " AND character_id=?", (char_id,))
        rows_delete("characters", char_id)
        self._json({"ok": True})

    def _lore_delete(self, bid: int) -> None:
        """DELETE /api/lorebooks/<id> — the book, its entries and its links,
        in one transaction. PRAGMA foreign_keys is off, so nothing else will."""
        with get_db() as conn:
            cur = conn.execute("DELETE FROM lorebooks WHERE id=?", (bid,))
            conn.execute("DELETE FROM lore_entries WHERE book_id=?", (bid,))
            conn.execute("DELETE FROM lore_links WHERE book_id=?", (bid,))
        self._json({"ok": bool(cur.rowcount)}, 200 if cur.rowcount else 404)

    def _regex_import(self) -> None:
        """POST /api/regex/import — bring SillyTavern regex scripts across.

        Body: {b64 | json, dry_run?, character_id?}. Accepts a preset, a
        character card, a single exported script, or a bare list of them —
        people export all four shapes and none of them announce which.
        """
        body = self._body()
        raw = body.get("json")
        if raw is None and body.get("b64"):
            try:
                raw = json.loads(base64.b64decode(body["b64"])
                                 .decode("utf-8", "replace"))
            except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
                self._json({"error": f"could not read that file: {exc}"}, 400)
                return
        scripts = regexrules.scripts_in(raw)
        if not scripts:
            self._json({"error": "no regex scripts in that file"}, 400)
            return
        results = [regexrules.from_st(sc) for sc in scripts]
        summary = regexrules.summarise_import(results)
        if body.get("dry_run"):
            self._json({"ok": True, "preview": True, "summary": summary,
                        "rules": [r["rule"] for r in results]})
            return
        cid = body.get("character_id")
        now = time.time()
        with get_db() as conn:
            base = conn.execute(
                "SELECT COALESCE(MAX(ord), 0) FROM regex_rules").fetchone()[0]
            for i, r in enumerate(results, 1):
                rule = r["rule"]
                conn.execute(
                    "INSERT INTO regex_rules (name, pattern, replace,"
                    " on_prompt, on_display, min_depth, max_depth, enabled,"
                    " ord, character_id, data, created, updated)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rule["name"], rule["pattern"], rule["replace"],
                     rule["on_prompt"], rule["on_display"],
                     rule["min_depth"], rule["max_depth"], rule["enabled"],
                     base + i, cid,
                     json.dumps({"trim": rule["trim"],
                                 "note": r["note"] or r["problem"]}),
                     now, now))
        self._json({"ok": True, "summary": summary,
                    "imported": len(results)})

    def _memories_list(self, chat_id: int) -> None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT character_id, persona_id FROM chats WHERE id=?",
                (chat_id,)).fetchone()
            if not row:
                self._json({"error": "chat not found"}, 404)
                return
            mems = memory.for_turn(conn, chat_id, row["character_id"],
                                   persona_id=row["persona_id"])
            # The whole scene's memory, not just the lead's. Each guest keeps
            # her own relationship record now, and what the panel cannot show
            # the user cannot see or edit.
            guests = [r["character_id"] for r in conn.execute(
                "SELECT character_id FROM chat_cast WHERE chat_id=?",
                (chat_id,)) if r["character_id"] != row["character_id"]]
            if guests:
                marks = ",".join("?" * len(guests))
                mems += [dict(r) for r in conn.execute(
                    f"SELECT * FROM memories WHERE kind='character'"
                    f" AND character_id IN ({marks})"
                    f" AND (persona_id IS NULL OR persona_id IS ?)"
                    f" ORDER BY id", (*guests, row["persona_id"]))]
        grouped = {"user": [], "character": [], "chat": []}
        for m in mems:
            grouped.setdefault(m["kind"], []).append(
                {"id": m["id"], "content": m["content"], "scope": m["kind"]})
        self._json({"memories": [
            {"id": m["id"], "content": m["content"], "scope": m["kind"]}
            for m in mems], "grouped": grouped})

    def _memory_write(self) -> None:
        """POST /api/memories — manual add or edit. The user owns their profile.

        Body: {id?, scope: user|character|chat, content, chat_id?,
               character_id?}

        `character_id` may name a GUEST of the chat, so a memory can be
        attached to whoever actually experienced it; it is validated against
        the chat's cast plus its lead. An EDIT without one keeps the row's
        existing attribution — recomputing it from the chat's lead silently
        refiled a guest's memory under the lead on every edit.
        """
        body = self._body()
        content = (body.get("content") or "").strip()
        scope = (body.get("scope") or "chat").strip().lower()
        if not content:
            self._json({"error": "content required"}, 400)
            return
        chat_id = int(body.get("chat_id") or 0) or None
        character_id = int(body.get("character_id") or 0) or None
        with get_db() as conn:
            if character_id and chat_id and scope != "user":
                in_scene = conn.execute(
                    "SELECT 1 FROM chats WHERE id=? AND character_id=?",
                    (chat_id, character_id)).fetchone() or conn.execute(
                    "SELECT 1 FROM chat_cast WHERE chat_id=? AND"
                    " character_id=?", (chat_id, character_id)).fetchone()
                if not in_scene:
                    character_id = None
            if not character_id and scope != "user" and body.get("id"):
                old = conn.execute(
                    "SELECT character_id FROM memories WHERE id=?",
                    (int(body["id"]),)).fetchone()
                character_id = old["character_id"] if old else None
            if not character_id and scope != "user" and chat_id:
                row = conn.execute("SELECT character_id FROM chats WHERE id=?",
                                   (chat_id,)).fetchone()
                character_id = row["character_id"] if row else None
            # A manual write from inside a chat lands in that chat's persona
            # bucket — the panel is showing that bucket, so the row must land
            # where the user can see it.
            persona_id = None
            if chat_id:
                prow = conn.execute(
                    "SELECT persona_id FROM chats WHERE id=?",
                    (chat_id,)).fetchone()
                persona_id = prow["persona_id"] if prow else None
            mem_id = memory.upsert(conn, body.get("id"), scope, content,
                                   chat_id, character_id,
                                   persona_id=persona_id)
        self._json({"ok": True, "id": mem_id, "scope": scope})




    def _rp_digest(self, chat_row, char_name: str) -> str:
        """Recent roleplay with this character, for an sms thread that wants it.

        Texting her about what just happened is most of why a sidechat is
        interesting, and the two threads are separate chat rows, so she
        otherwise has no idea. Opt-in per thread: some people want the phone
        to be a clean slate.
        """
        try:
            data = _row_to_dict(chat_row).get("data") or {}
        except Exception:  # noqa: BLE001
            data = {}
        if chat_row["mode"] != "sms" or not data.get("aware"):
            return ""
        with get_db() as conn:
            rp = conn.execute(
                "SELECT id FROM chats WHERE character_id=? AND mode!='sms'"
                " ORDER BY updated DESC, id DESC LIMIT 1",
                (chat_row["character_id"],)).fetchone()
            if not rp:
                return ""
            msgs = engine.get_messages(conn, rp["id"])
        if not msgs:
            return ""
        lines, used = [], 0
        for m in reversed(msgs):
            who = char_name if m["role"] == "assistant" else "them"
            line = f"{who}: {m['content']}"
            cost = engine.rough_tokens(line)
            if used + cost > 900 and lines:
                break
            lines.append(line)
            used += cost
        lines.reverse()
        return (prompts.get("sms_aware", char=char_name) + "\n"
                + "\n".join(lines))


    def _chat_text_first(self) -> None:
        """POST /api/chats/text-first — she messages you unprompted.

        The whole difficulty is making it feel motivated rather than random.
        A bot that pings you every twenty minutes with "hey :)" is worse than
        silence, so the model is handed a *reason* — how long it has been,
        what time it is, what she remembers, and what was last said — and told
        to text about something real or to say nothing at all.

        Returning nothing is a supported outcome. If she has no reason to
        text, she doesn't.
        """
        body = self._body()
        chat_id = int(body.get("chat_id") or 0)
        with get_db() as conn:
            chat = conn.execute("SELECT * FROM chats WHERE id=?",
                                (chat_id,)).fetchone()
            if not chat:
                self._json({"error": "no such chat"}, 404)
                return
            msgs = engine.get_messages(conn, chat_id)
            mems = [f"({m['kind']}) {m['content']}" for m in
                    memory.for_turn(conn, chat_id, chat["character_id"],
                                    limits=memory.settings(load_config()),
                                    persona_id=chat["persona_id"])]
        char = rows_get("characters", chat["character_id"]) or {}
        persona = (rows_get("personas", chat["persona_id"])
                   if chat["persona_id"] else None)

        last_at = msgs[-1].get("created") if msgs else None
        gap_min = int((time.time() - (last_at or time.time())) / 60)
        hour = time.localtime().tm_hour
        part = ("the middle of the night" if hour < 5 else
                "early morning" if hour < 9 else
                "the middle of the day" if hour < 17 else
                "the evening" if hour < 22 else "late at night")

        backend, key, model, samplers, meta, jailbreak = self._resolve_llm(body)
        if not (backend and model):
            self._json({"error": "backend and model are required"}, 400)
            return

        tail = []
        for m in msgs[-8:]:
            who = char.get("name", "she") if m["role"] == "assistant" else "them"
            tail.append(f"{who}: {m['content'][:400]}")

        # On a blank thread there is no tail and no memory to carry her, so
        # without the card she is being asked to text first knowing nothing
        # but her own name. Structural text only — every INSTRUCTION here is
        # still a prompt layer.
        char_name = char.get("name", "she")
        user_name = (persona or {}).get("name", "them")
        cfields = char.get("data", {}).get("fields", {})

        def mx(t):
            return macros.expand(t, char_name, user_name, cfields,
                                 (persona or {}).get("data", {})
                                 .get("description", ""), str(chat_id))

        system = "\n\n".join(x for x in (
            mx(engine.card_text(cfields)),
            mx(engine.persona_text(persona)),
            prompts.get("sms", char=char_name) if chat["mode"] == "sms" else "",
            # She can attach a picture to an unprompted text — "look what
            # i'm wearing" with the selfie actually riding along. The spec
            # was simply absent from this prompt before, so she could not
            # have known the format existed on this path.
            (prompts.get("tools_spec")
             if body.get("tools", True) and load_config().get("comfyui_url")
             else ""),
            prompts.get("text_first", char=char_name, user=user_name),
        ) if x and x.strip())
        ask = [
            f"IT HAS BEEN: {gap_min} minutes since the last message"
            if last_at else "You have never texted them before.",
            f"IT IS CURRENTLY: {part}",
        ]
        if msgs:
            # Who is leaving whom on read is the single strongest signal for
            # how a real person paces a thread, so it is stated outright
            # rather than left to be inferred from the tail.
            ask.append("THE LAST MESSAGE WAS FROM: "
                       + ("you — it has not been answered"
                          if msgs[-1]["role"] == "assistant" else "them"))
        if mems:
            ask.append("YOU REMEMBER:\n" + "\n".join(f"- {m}" for m in mems[:10]))
        if tail:
            ask.append("THE LAST THING SAID:\n" + "\n".join(tail))
        ask.append("Text them, or output exactly NOTHING if you have no real "
                   "reason to.")

        payload = llm.build_payload(
            [{"role": "system", "content":
              ((jailbreak + "\n\n") if jailbreak else "") + system},
             {"role": "user", "content": "\n\n".join(ask)}],
            model, {"max_tokens": 700, "temperature": 1.0}, stream=False,
            # A two-line text needs no reasoning, and a thinking model will
            # happily spend the entire budget on it and return nothing —
            # which is exactly what a 300-token limit did here.
            thinking=False)
        try:
            raw = llm.once_retry(backend, key, payload, "chat")
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 502)
            return

        text = (raw or "").strip()
        # Her own pacing, stated in character. The NEXT line is stripped
        # before anything is stored — it is scheduling, not dialogue — and
        # it is honoured whether she texted or declined: "not now, maybe
        # this evening" is a real answer with a real clock on it. Clamped
        # to [10 min, 48 h] because a model that answers "2" or "99999" is
        # not expressing personality, it is misreading the form.
        next_min = None
        m = re.search(r"(?mi)^\s*NEXT:\s*(\d+(?:\.\d+)?)\s*$", text)
        if m:
            try:
                next_min = max(10.0, min(2880.0, float(m.group(1))))
            except ValueError:
                pass
            text = (text[:m.start()] + text[m.end():]).strip()
        if next_min:
            with get_db() as conn:
                row = conn.execute("SELECT data FROM chats WHERE id=?",
                                   (chat_id,)).fetchone()
                try:
                    data = json.loads((row["data"] if row else "") or "{}")
                except json.JSONDecodeError:
                    data = {}
                t = dict(data.get("texting") or {})
                t["next_at"] = time.time() + next_min * 60
                data["texting"] = t
                conn.execute("UPDATE chats SET data=? WHERE id=?",
                             (json.dumps(data), chat_id))
        if text.upper().startswith("NOTHING") or len(text) < 2:
            self._json({"ok": True, "sent": False,
                        "why": "she had nothing to say",
                        "next_minutes": next_min})
            return
        text, tool_call = tools.split_tool_call(text)
        text = tools.split_director_note(text)[0].strip()
        if not text:
            self._json({"ok": True, "sent": False,
                        "why": "empty after parsing",
                        "next_minutes": next_min})
            return

        with get_db() as conn:
            mid = engine.add_message(conn, chat_id, "assistant", text,
                                     {"unprompted": True})
        # An attached shot rides as a pending approval, never an auto-render:
        # the approval rule holds even when she texts first. The card waits
        # in the registry until a phone opens (loadPhone lists it), so a
        # daemon text sent to a sleeping phone still delivers its "wait till
        # you see" moment hours later. Recipe calls only on this path — the
        # spec steers her there, and a free-form action call with no one
        # watching has nowhere honest to land.
        pending_payload = None
        if tool_call and isinstance(tool_call, dict) \
                and tool_call.get("recipe") in recipes.RECIPES:
            try:
                collected = {}
                self._studio_pending_from_tool(
                    tool_call, chat_id, mid, chat, body,
                    lambda fr: collected.update(fr))
                pending_payload = collected.get("studio_pending")
            except Exception:  # noqa: BLE001 — a failed draft must not kill the text
                pass
        self._json({"ok": True, "sent": True, "message_id": mid,
                    "text": text, "gap_minutes": gap_min,
                    "next_minutes": next_min,
                    "studio_pending": pending_payload})

    def _chat_remember(self) -> None:
        """POST /api/chats/{id}/remember — capture this chat on purpose.

        The automatic pass reads the last exchange every few turns, which is
        right for keeping up but wrong for "that whole evening mattered". This
        reads the *scene* and is triggered by the user, so it is allowed to be
        slower, larger, and to look at everything rather than a window.
        """
        body = self._body()
        chat_id = int(body.get("chat_id") or 0)
        with get_db() as conn:
            chat = conn.execute("SELECT * FROM chats WHERE id=?",
                                (chat_id,)).fetchone()
            if not chat:
                self._json({"error": "no such chat"}, 404)
                return
            msgs = engine.get_messages(conn, chat_id)
            existing = [m["content"] for m in
                        memory.for_turn(conn, chat_id, chat["character_id"],
                                        persona_id=chat["persona_id"])]
        if not msgs:
            self._json({"error": "nothing has happened yet"}, 400)
            return

        backend, key, model, _s, _m, _jb = self._resolve_llm(body)
        if not (backend and model):
            self._json({"error": "backend and model are required"}, 400)
            return

        char = rows_get("characters", chat["character_id"]) or {}
        # Everyone who is or was in the scene, for two jobs below: labelling
        # the transcript with who actually said each line (a cast scene's
        # transcript that just says "assistant:" makes every guest's deed
        # read as the lead's), and filing each extracted relationship fact
        # with the woman it names instead of unconditionally under the lead.
        with get_db() as conn:
            cast_rows = engine.cast_of(conn, chat_id, chat["character_id"])
        members = []
        for c in cast_rows:
            row = rows_get("characters", c["character_id"]) or {}
            if row.get("name"):
                members.append((c["character_id"], row["name"]))
        member_names = dict(members)

        def who_said(m):
            if m["role"] != "assistant":
                return "user"
            sid = engine.take_speaker(m)
            return member_names.get(sid) or char.get("name") or "her"

        # The whole scene, trimmed from the end — the recent half of a long
        # evening is the half worth keeping.
        transcript, used = [], 0
        for m in reversed(msgs):
            line = f"{who_said(m)}: {m['content']}"
            cost = engine.rough_tokens(line)
            if used + cost > 6000 and transcript:
                break
            transcript.append(line)
            used += cost
        transcript.reverse()

        def llm_once(messages):
            payload = llm.build_payload(
                messages, model, {"max_tokens": 1200, "temperature": 0.3},
                stream=False)
            return llm.once_retry(backend, key, payload, "chat")

        # Same persona guard as the automatic pass: ♥ remember is greedier by
        # design, which makes it MORE likely to file "the user is called X"
        # from the persona block as a discovery.
        persona = (rows_get("personas", chat["persona_id"])
                   if chat["persona_id"] else None) or {}
        known = memory.persona_known(
            persona.get("name", ""),
            persona.get("data", {}).get("description", ""))
        facts = memory.extract_memories(
            llm_once, known + existing, "\n".join(transcript),
            f"(the whole scene between {char.get('name', 'her')} and the user)",
            system=prompts.get("memory_remember"))
        cfg = memory.settings(load_config())
        # Same fallback as the automatic pass: with no persona, the prompt
        # still says "anon", so that is the name the guard must catch.
        facts = memory.sanitize_facts(
            facts, persona.get("name", "") or "anon",
            persona.get("data", {}).get("description", ""),
            char.get("name", ""), cfg["dupe_threshold"])
        found = len(facts)
        with get_db() as conn:
            added = 0
            if len(members) > 1:
                # A cast scene: each relationship fact goes to the woman it
                # names, and only the unattributable remainder to the lead.
                buckets, facts = memory.attribute_facts(facts, members)
                for cid, fs in buckets.items():
                    added += memory.store_memories(
                        conn, chat_id, cid, fs, cfg["dupe_threshold"],
                        persona_id=chat["persona_id"])
            added += memory.store_memories(conn, chat_id,
                                           chat["character_id"],
                                           facts, cfg["dupe_threshold"],
                                           persona_id=chat["persona_id"])
        self._json({"ok": True, "added": added, "found": found,
                    "duplicates": found - added})

    def _memory_tidy(self) -> None:
        """POST /api/memories/tidy — collapse near-duplicates already stored.

        Offered rather than run automatically: it deletes rows, and a user
        who has hand-edited their memory profile should be the one to say go.
        """
        cfg = memory.settings(load_config())
        with get_db() as conn:
            before = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
            # Test residue first: the old suite POSTed its fixture memories
            # through the live API, and a user-scope fixture row was
            # structurally unsweepable (character_id NULL). Purged by exact
            # content, plus orphan chat-scope rows nothing can ever read.
            purged = memory.purge_fixture_residue(conn)
            result = memory.dedupe_existing(conn, cfg["dupe_threshold"])
            # Repair scope leaks written before sanitize_facts existed: a
            # user-scope fact that is visibly about exactly one character
            # becomes hers, instead of following the player into every chat.
            chars = [(r["id"], r["name"]) for r in conn.execute(
                "SELECT id, name FROM characters").fetchall()]
            rescoped = memory.rescope_user_facts(conn, chars)
            after = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        self._json({"ok": True, "before": before, "after": after,
                    "rescoped": rescoped, "purged": purged, **result})

    def _memory_delete(self, mem_id: int) -> None:
        with get_db() as conn:
            cur = conn.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        self._json({"ok": cur.rowcount > 0})

    def _chat_memory_toggle(self, chat_id: int) -> None:
        body = self._body()
        enabled = 1 if body.get("enabled", True) else 0
        with get_db() as conn:
            conn.execute("UPDATE chats SET memory_enabled=? WHERE id=?",
                         (enabled, chat_id))
        self._json({"ok": True, "memory_enabled": enabled})


    def _chat_texting(self, chat_id: int) -> None:
        """POST /api/chats/{id}/texting — may she message you unprompted?

        Off by default. A companion that pings you uninvited is a decision the
        user makes, not one the app makes for them.
        """
        body = self._body()
        with get_db() as conn:
            row = conn.execute("SELECT data FROM chats WHERE id=?",
                               (chat_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            try:
                data = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                data = {}
            cur = dict(data.get("texting") or {})
            if "enabled" in body:
                cur["enabled"] = bool(body["enabled"])
            if body.get("gap_minutes"):
                cur["gap_minutes"] = max(5, int(body["gap_minutes"]))
            if body.get("daily_cap"):
                cur["daily_cap"] = max(1, min(int(body["daily_cap"]), 24))
            data["texting"] = cur
            conn.execute("UPDATE chats SET data=?, updated=? WHERE id=?",
                         (json.dumps(data), time.time(), chat_id))
        self._json({"ok": True, "texting": cur})

    def _chat_aware_toggle(self, chat_id: int) -> None:
        """POST /api/chats/{id}/aware {enabled} — does the phone know the scene?"""
        body = self._body()
        enabled = bool(body.get("enabled", True))
        with get_db() as conn:
            row = conn.execute("SELECT data FROM chats WHERE id=?",
                               (chat_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            try:
                data = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                data = {}
            data["aware"] = enabled
            conn.execute("UPDATE chats SET data=?, updated=? WHERE id=?",
                         (json.dumps(data), time.time(), chat_id))
        self._json({"ok": True, "aware": enabled})

    def _chat_opening(self, chat_id: int) -> None:
        """POST /api/chats/{id}/opening {text} — her first text in a thread.

        An SMS thread starts blank now, and the phone caches one chat per
        character forever, so most threads already exist by the time the user
        wants to write her an opener. 409 on a thread that already has
        messages — an opening is only an opening while there is nothing
        there; after that the user wants the edit endpoint.
        """
        text = (self._body().get("text") or "").strip()
        if not text:
            self._json({"error": "say something"}, 400)
            return
        with get_db() as conn:
            # `chats` is not in VALID_TABLES — rows_get asserts on it.
            row = conn.execute("SELECT id FROM chats WHERE id=?",
                               (chat_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            if engine.get_messages(conn, chat_id):
                self._json({"error": "this thread already started"}, 409)
                return
            # stored with {{user}} intact — macros are resolved late
            mid = engine.add_message(conn, chat_id, "assistant", text,
                                     {"opening": True})
        self._json({"ok": True, "message_id": mid, "content": text})

    def _chat_examples_toggle(self, chat_id: int) -> None:
        """POST /api/chats/{id}/examples {enabled} — per-chat, default on.

        Lives in chats.data rather than a column: it is a per-scene
        preference, not a schema-level fact, and it saves a migration.
        """
        body = self._body()
        enabled = bool(body.get("enabled", True))
        # `chats` is not in VALID_TABLES — rows_get asserts on anything that
        # is not a named-row store, so it has to be read directly.
        with get_db() as conn:
            row = conn.execute("SELECT data FROM chats WHERE id=?",
                               (chat_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            try:
                data = json.loads(row["data"] or "{}")
            except json.JSONDecodeError:
                data = {}
            data["examples"] = enabled
            conn.execute("UPDATE chats SET data=?, updated=? WHERE id=?",
                         (json.dumps(data), time.time(), chat_id))
        self._json({"ok": True, "examples": enabled})

    def _swipe(self, message_id: int) -> None:
        """POST /api/messages/{id}/swipe {index} — switch the active take.

        Returns display-ready text, and the index it actually settled on.
        The old version handed back the raw stored string, so switching a
        swipe showed literal {{user}} and unapplied display rules until the
        next reload — everything else in the app renders through macros and
        the regex layer, and this was the one path that did not.
        """
        body = self._body()
        with get_db() as conn:
            row = conn.execute("SELECT chat_id FROM messages WHERE id=?",
                               (message_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            chat_id = row["chat_id"]
            out = engine.set_swipe(conn, message_id, int(body.get("index", 0)))
            if out is None:
                self._json({"error": "no swipes"}, 404)
                return
            order = [r["id"] for r in conn.execute(
                "SELECT id FROM messages WHERE chat_id=? ORDER BY id",
                (chat_id,)).fetchall()]
        swipe, index, total = out
        mx, rules = self._display_ctx(chat_id)
        depth = (len(order) - 1 - order.index(message_id)
                 if message_id in order else 0)
        content, is_html = for_display(mx(swipe.get("content", "")), rules, depth)
        self._json({"ok": True, "content": content, "html": is_html,
                    "index": index, "total": total,
                    "think": swipe.get("think", ""),
                    "director": swipe.get("director", "")})

    def _card_edit(self, char_id: int) -> None:
        """POST /api/characters/{id}/fields {fields} — edit card contents.

        Goes through cards.apply_edits so the change lands in the embedded
        card too and survives an export back to SillyTavern.
        """
        row = rows_get("characters", char_id)
        if not row:
            self._json({"error": "not found"}, 404)
            return
        fields = self._body().get("fields") or {}
        if not isinstance(fields, dict):
            self._json({"error": "fields must be an object"}, 400)
            return
        updated = cards.apply_edits(row["data"], fields)
        with get_db() as conn:
            conn.execute(
                "UPDATE characters SET name=?, data=?, updated=? WHERE id=?",
                (updated["name"], json.dumps(updated), time.time(), char_id))
        self._json({"ok": True, "name": updated["name"],
                    "fields": updated["fields"]})

    def _message_edit(self, message_id: int) -> None:
        """POST /api/messages/{id} {content} — rewrite a turn in place.

        Rewrites whichever variant is on screen: if the message is showing
        swipe N, that is the text the user is looking at and the one they
        mean to fix. Editing history is the whole point of a roleplay
        frontend — one bad sentence early poisons everything after it.
        """
        body = self._body()
        content = (body.get("content") or "").strip()
        if not content:
            self._json({"error": "content is required"}, 400)
            return
        with get_db() as conn:
            row = conn.execute("SELECT data FROM messages WHERE id=?",
                               (message_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            d = json.loads(row["data"] or "{}")
            swipes = d.get("swipes") or []
            idx = d.get("swipe_index")
            if swipes and idx is not None:
                swipes[min(idx, len(swipes) - 1)]["content"] = content
                conn.execute("UPDATE messages SET data=? WHERE id=?",
                             (json.dumps(d), message_id))
            else:
                conn.execute("UPDATE messages SET content=? WHERE id=?",
                             (content, message_id))
        self._json({"ok": True, "content": content})

    def _message_delete(self, message_id: int) -> None:
        """DELETE /api/messages/{id} — drop a turn entirely."""
        with get_db() as conn:
            cur = conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
        ok = cur.rowcount > 0
        self._json({"ok": True} if ok else {"error": "not found"},
                   200 if ok else 404)

    def _library_install(self) -> None:
        """POST /api/library/install — upsert the shipped presets/jailbreaks."""
        added = library.install(rows_upsert)
        self._json({"ok": True, **added})

    # -- ComfyUI bridge ------------------------------------------------------
    def _comfy_url(self) -> str:
        return load_config().get("comfyui_url", "")

    def _comfy_ping(self) -> None:
        """POST /api/comfy/ping {url?} — check ComfyUI reachability."""
        url = self._body().get("url") or self._comfy_url()
        if not url:
            self._json({"ok": False, "error": "no comfyui_url configured"}, 400)
            return
        try:
            stats = comfy.ComfyClient(url, timeout=8).ping()
            devices = [d.get("name", "?") for d in stats.get("devices", [])]
            self._json({"ok": True, "devices": devices})
        except comfy.ComfyError as exc:
            self._json({"ok": False, "error": str(exc)})

    def _comfy_slots(self) -> None:
        """POST /api/comfy/slots {workflow} — detect {{slots}} in a workflow."""
        wf = self._body().get("workflow")
        if not isinstance(wf, dict):
            self._json({"error": "workflow object required"}, 400)
            return
        self._json({"slots": {k: len(v) for k, v in comfy.find_slots(wf).items()}})

    def _comfy_run(self) -> None:
        """POST /api/comfy/run — run a stored or inline workflow.

        Body: {workflow_id? | workflow?, values?, chat_id?, message_id?,
               image_b64?, image_name?}
        Saves outputs to assets + assets table; returns asset descriptors.
        """
        body = self._body()
        wf = body.get("workflow")
        wf_id = body.get("workflow_id")
        if wf is None and wf_id:
            row = rows_get("workflows", int(wf_id))
            if not row:
                self._json({"error": "workflow not found"}, 404)
                return
            wf = row["data"].get("workflow")
        if not isinstance(wf, dict):
            self._json({"error": "workflow or workflow_id required"}, 400)
            return
        url = body.get("url") or self._comfy_url()
        if not url:
            self._json({"error": "no comfyui_url configured"}, 400)
            return

        values = dict(body.get("values") or {})
        if body.get("image_b64"):
            values["_image_bytes"] = base64.b64decode(body["image_b64"])
            values["_image_name"] = body.get("image_name", "coomkit.png")
        try:
            files = comfy.run_workflow(url, wf, values,
                                       timeout_s=int(body.get("timeout", 600)))
        except (comfy.ComfyError, Exception) as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 502)
            return

        ASSETS.mkdir(parents=True, exist_ok=True)
        saved = []
        now = time.time()
        owner = self._char_of_chat(body.get("chat_id"))
        with get_db() as conn:
            for f in files:
                ext = Path(f["filename"]).suffix or ".bin"
                fname = f"gen_{int(now*1000)}_{len(saved)}{ext}"
                (ASSETS / fname).write_bytes(f["data"])
                cur = conn.execute(
                    "INSERT INTO assets (chat_id, character_id, message_id,"
                    " kind, path, data, created) VALUES (?,?,?,?,?,?,?)",
                    (body.get("chat_id"), owner, body.get("message_id"),
                     f["kind"], fname,
                     json.dumps({"node_id": f["node_id"]}), now),
                )
                saved.append({"id": cur.lastrowid, "kind": f["kind"],
                              "url": f"/api/avatars/{fname}"})
        self._json({"ok": True, "assets": saved})





    def _context_probe(self) -> None:
        """POST /api/context/probe {backend, model} — what can this model hold?

        LM Studio reports the context a model was actually *loaded* at, which
        is the number that matters and is never the same as what the model
        supports. Guessing high silently truncates; guessing low wastes the
        window the user paid VRAM for.

        So there are two different numbers here and only one of them is an
        answer. `loaded_context_length` is measured truth. `max_context_length`
        is a CAPABILITY — what the weights allow — and adopting it as a setting
        is the same mistake `stimport` already refuses for ST's unlocked slider
        (stimport.py:189), arriving from a second direction: the first-run
        wizard writes whatever comes back straight into the preset's context
        (app.js, wizard step "blocks"). Measured here: `google/gemma-4-31b`
        sitting unloaded in LM Studio answers 262,144, and z-ai/glm-5.3 on
        OpenRouter answers 1,048,576 — 84 OpenRouter models now exceed a
        million. Either figure then does two things, neither recoverable:
        `engine.assemble_blocks` budgets history against it, so history is
        never trimmed and the first long chat overflows; and on a local
        backend it reaches `lms load --context-length` through
        `vram.ensure_model`, so CoomKit asks the card for a KV cache no card
        has. `context` is therefore only ever a number worth budgeting
        against, and when there isn't one it is 0 and the note says why.
        """
        body = self._body()
        backend = normalise_backend(body.get("backend", ""))
        model = body.get("model", "")
        if not backend:
            self._json({"error": "backend required"}, 400)
            return
        root = backend[:-3] if backend.endswith("/v1") else backend
        for path in ("/api/v0/models", "/v1/models"):
            try:
                # api_call returns the open response, not bytes.
                with api_call(root, path, body.get("key", ""), timeout=8) as r:
                    data = json.loads(r.read().decode())
            except Exception:  # noqa: BLE001
                continue
            for m in data.get("data", []) or []:
                if m.get("id") != model:
                    continue
                loaded = int(m.get("loaded_context_length") or 0)
                total = int(m.get("max_context_length")
                            or m.get("context_length") or 0)
                # LM Studio's /api/v0 says so outright. Nothing else does, and
                # absence of the key is not evidence of an unloaded model —
                # a remote has no notion of loading at all.
                state = (m.get("state") or "").lower()
                if not (loaded or total):
                    continue
                if loaded:
                    self._json({"ok": True, "context": loaded,
                                "loaded": loaded, "max": total,
                                "loaded_now": True, "note": ""})
                    return
                if state.startswith("not-loaded"):
                    self._json({
                        "ok": True, "context": 0, "loaded": 0, "max": total,
                        "loaded_now": False,
                        "note": (f"{model} isn't loaded, so {total:,} is what "
                                 f"it *supports*, not what it would run at. "
                                 f"Load it and detect again, or set the "
                                 f"window yourself.")})
                    return
                # A remote, or a backend that reports a capability and no
                # state. Usable, but not at face value — budgeting history
                # against a million tokens means never trimming it.
                keep = min(total, CONTEXT_TRUST_CEILING)
                self._json({
                    "ok": True, "context": keep, "loaded": 0, "max": total,
                    "loaded_now": False,
                    "note": ("" if keep == total else
                             f"{model} claims {total:,} tokens — capped at "
                             f"{keep:,} for budgeting. Raise it by hand if "
                             f"you really send windows that big.")})
                return
        self._json({"ok": False,
                    "error": "this backend doesn't report a context size"})

    def _preset_import_st(self) -> None:
        """POST /api/presets/import-st — bring a SillyTavern preset across.

        Two-phase on purpose. `dry_run` returns what the import *would* do —
        how many blocks, how many tokens, what got dropped and why, and the
        five most expensive blocks — because the single most useful thing to
        tell someone importing a 24,000-token preset is that it is a
        24,000-token preset. Then they confirm.

        Body: {b64 | json, name?, dry_run?, keep_disabled?}
        """
        body = self._body()
        raw = body.get("json")
        if raw is None and body.get("b64"):
            try:
                raw = json.loads(base64.b64decode(body["b64"])
                                 .decode("utf-8", "replace"))
            except (binascii.Error, ValueError, json.JSONDecodeError) as exc:
                self._json({"error": f"could not read that file: {exc}"}, 400)
                return
        if not stimport.looks_like_st(raw):
            self._json({"error": "that is not a SillyTavern chat-completion "
                                 "preset — no `prompts` list in it"}, 400)
            return
        try:
            result = stimport.convert(raw, bool(body.get("keep_disabled")))
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
            return

        summary = stimport.summarise(result, engine.rough_tokens)
        if body.get("dry_run"):
            self._json({"ok": True, "preview": True, "summary": summary,
                        "blocks": result["blocks"],
                        "context": result.get("context") or 0,
                        "samplers": result["samplers"]})
            return

        name = (body.get("name") or "Imported preset").strip()[:80]
        data = {
            "mode": "chat", "template": "gemma4", "thinking": True,
            "thinking_mode": "normal",
            "samplers": {**load_config().get("defaults", {}),
                         **result["samplers"]},
            # The imported list is the whole prompt, but it may not carry a
            # history marker (some presets rely on ST's implicit placement).
            # merge() appends anything missing so the conversation cannot
            # vanish.
            "blocks": blocks.merge(result["blocks"]),
            # Falls back to the user's own default, never to the preset
            # author's unlocked slider.
            "context": (result.get("context")
                        or load_config().get("defaults", {})
                        .get("context_tokens") or 8192),
            "imported_from": "sillytavern",
        }
        row = rows_upsert("presets", {"name": name, "data": data})
        self._json({"ok": True, "preset": row, "summary": summary})

    def _blocks_catalogue(self) -> None:
        """GET /api/blocks — the default order, the library, and the groups."""
        cfg = load_config()
        self._json({
            "default": blocks.default_blocks(),
            "library": blocklib.library(),
            "groups": [{"id": g, "label": l, "why": w}
                       for g, l, w in blocks.GROUPS],
            "markers": blocks.MARKERS,
            "roles": list(blocks.ROLES),
            "starters": {k: v for k, v in blocklib.STARTERS.items()},
        })

    def _blocks_save(self, preset_id: int) -> None:
        """POST /api/presets/{id}/blocks {blocks} — reorder / edit / toggle."""
        body = self._body()
        row = rows_get("presets", preset_id)
        if not row:
            self._json({"error": "no such preset"}, 404)
            return
        data = dict(row.get("data") or {})
        # Only replace the list when one was actually sent. An omitted key
        # meant "wipe them", so a caller updating just the context silently
        # destroyed the order — which is exactly what the wizard did.
        if body.get("blocks") is not None:
            data["blocks"] = blocks.normalise(body.get("blocks") or [])
        if body.get("context"):
            data["context"] = max(512, int(body["context"]))
        saved = rows_upsert("presets", {"name": row["name"], "data": data},
                            preset_id)
        self._json({"ok": True, "blocks": saved["data"]["blocks"],
                    "context": saved["data"].get("context")})

    def _blocks_starter(self, preset_id: int) -> None:
        """POST /api/presets/{id}/blocks/starter {kind} — install a starter set.

        Appended to the default order rather than replacing it, and tiered:
        a local model needs about four blocks where a hosted one needs the
        machinery that exists to fight hosted behaviour.
        """
        body = self._body()
        kind = body.get("kind") or "local"
        row = rows_get("presets", preset_id)
        if not row:
            self._json({"error": "no such preset"}, 404)
            return
        data = dict(row.get("data") or {})
        current = blocks.merge(data.get("blocks"))
        have = {b["id"] for b in current}
        added = [b for b in blocklib.starter(kind) if b["id"] not in have]
        # Library blocks go before the history marker so they steer the reply
        # rather than trailing the whole conversation.
        at = next((i for i, b in enumerate(current)
                   if b.get("marker") == "history"), len(current))
        data["blocks"] = current[:at] + added + current[at:]
        saved = rows_upsert("presets", {"name": row["name"], "data": data},
                            preset_id)
        self._json({"ok": True, "added": len(added),
                    "blocks": saved["data"]["blocks"]})

    def _blocks_cost(self) -> None:
        """POST /api/blocks/cost — per-block token cost for a real turn.

        Runs the actual assembly, so the number matches what is sent rather
        than an estimate of it.
        """
        body = self._body()
        prep = self._prepare_request(body, persist=False)
        if prep.get("error"):
            self._json({"error": prep["error"]}, prep.get("status", 400))
            return
        msgs = prep["messages"]
        self._json({"ok": True,
                    "total": sum(engine.rough_tokens(m["content"])
                                 for m in msgs),
                    "messages": [{"role": m["role"],
                                  "tokens": engine.rough_tokens(m["content"])}
                                 for m in msgs]})

    # -- character forge: invent her with the model ---------------------------
    def _chargen_options(self):
        """The voice and image-model ids a pitch is allowed to choose."""
        voices = [v["name"] for v in voices_mod.available()]
        models = [n for n, spec in wfpack.BUNDLED.items()
                  if spec["kind"] == "image"]
        return voices, models

    def _chargen_llm(self, body: dict, messages: list, max_tokens: int = 6000):
        backend, key, model, _s, _m, jailbreak = self._resolve_llm(body)
        if not (backend and model):
            return None, {"error": "backend and model are required"}, 400
        payload = llm.build_payload(
            messages, model,
            {"max_tokens": max_tokens, "temperature": 1.0, "top_p": 0.95},
            stream=False)
        try:
            return llm.once_retry(backend, key, payload, "chat"), None, 200
        except Exception as exc:  # noqa: BLE001
            return None, {"error": str(exc)}, 502

    def _chargen_pitch(self) -> None:
        """POST /api/forge/characters — pitch whole characters for this user.

        Body: {persona_id?, brief?, into?, count?, backend, model, key?,
               preset_id?}
        """
        body = self._body()
        persona = (rows_get("personas", int(body["persona_id"]))
                   if body.get("persona_id") else None)
        voices, models = self._chargen_options()
        _b, _k, _m, _s, _meta, jailbreak = self._resolve_llm(body)
        messages = chargen.build_pitch_messages(
            persona, body.get("brief", ""),
            max(1, min(int(body.get("count", 3)), 5)),
            into=body.get("into", ""), voices=voices, models=models,
            system=prompts.get("chargen_pitch"), jailbreak=jailbreak)
        raw, err, code = self._chargen_llm(body, messages)
        if err:
            self._json(err, code)
            return
        found = chargen.parse_pitches(raw, voices, models)
        if not found:
            self._json({"error": "could not parse a character out of the reply",
                        "raw": (raw or "")[:600]}, 502)
            return
        self._json({"ok": True, "characters": found})

    def _chargen_from_image(self) -> None:
        """POST /api/forge/characters/from-image — a card for that feel.

        You have a picture and you want *her*. A vision model reads it and
        pitches several women who all look like that and are otherwise
        different people; from there it is the ordinary forge — same pitch
        cards, same revise box, same commit button — because it is the same
        interaction wearing a photograph.

        Body: {images: [{name, b64}], persona_id?, brief?, into?, count?,
               backend, model, key?, preset_id?}
        """
        body = self._body()
        images = body.get("images") or []
        if not images:
            self._json({"error": "a picture is required"}, 400)
            return

        backend, _k, model, _s, _meta, jailbreak = self._resolve_llm(body)
        if not (backend and model):
            self._json({"error": "backend and model are required"}, 400)
            return
        # Vision is local-only BY CONSTRUCTION, and this is the one place the
        # usual degrade does not apply. A chat turn against a remote still
        # works — she is told in-band that there was a picture and answers
        # around it. There is no equivalent here: a pitch built from an image
        # nobody saw is just the ordinary forge with extra steps, and it would
        # be indistinguishable from the feature working. So refuse and say so
        # — unless the user has flagged THIS backend `vision: true` in
        # settings, in which case the picture really is sent and the whole
        # indistinguishable-failure argument evaporates with it.
        matches = [rb for rb in load_config().get("remote_backends", [])
                   if rb.get("url")
                   and normalise_backend(rb["url"]) == backend]
        if matches and not any(rb.get("vision") for rb in matches):
            self._json({"error": "vision is local-only — the picture never "
                                 "leaves this machine, so a remote backend "
                                 "cannot be shown it. Point CoomKit at your "
                                 "local model for this one, tick 'send "
                                 "images' on that backend in ⚙ → backends "
                                 "if it is really yours, or describe her "
                                 "in the brief under ☆ a whole character."},
                       400)
            return

        # Encoded straight from the request, never written to disk: a picture
        # the user pitches from and then thinks better of leaves no trace.
        # Only the one they actually commit gets stored.
        #
        # Every rejection is NAMED and given its reason. Sharing one `continue`
        # between "that is not valid base64" and "that is too big" meant a
        # perfectly good 22 MB PNG came back as "could not read that picture",
        # blaming the file's integrity for a file that is fine; and with
        # several pictures the oversized one vanished with no notice at all
        # while the model was told, in build_image_messages, that it had been
        # sent one fewer than the user chose.
        urls, dropped = [], []
        for im in images[:4]:
            name = str(im.get("name") or "image.png")[:60]
            try:
                raw = base64.b64decode(im.get("b64", ""), validate=True)
            except (binascii.Error, ValueError):
                raw = b""
            if not raw:
                dropped.append(f"{name} — could not read it")
            elif len(raw) > MAX_UPLOAD:
                dropped.append(f"{name} — over the {MAX_UPLOAD_MB} MB cap")
            else:
                urls.append(llm.encode_bytes(raw, name))
        if not urls:
            self._json({"error": "no usable picture: " + "; ".join(dropped)},
                       400)
            return
        if len(images) > 4:
            dropped.append(f"{len(images) - 4} more past the 4-picture limit")

        persona = (rows_get("personas", int(body["persona_id"]))
                   if body.get("persona_id") else None)
        voices, models = self._chargen_options()
        messages = chargen.build_image_messages(
            persona, urls, body.get("brief", ""),
            max(1, min(int(body.get("count", 3)), 5)),
            into=body.get("into", ""), voices=voices, models=models,
            system=prompts.get("chargen_image"), jailbreak=jailbreak)
        raw_reply, err, code = self._chargen_llm(body, messages)
        if err:
            self._json(err, code)
            return
        found = chargen.parse_pitches(raw_reply, voices, models)
        if found:
            out = {"ok": True, "characters": found}
            if dropped:
                out["notice"] = "built from %d of %d: %s" % (
                    len(urls), len(images), "; ".join(dropped))
            self._json(out)
            return
        # Checked only AFTER the pitches come back empty, so a normal reply
        # that happens to carry the key is never read as a refusal.
        said = chargen.refusal(raw_reply)
        if said:
            self._json({"error": "she would not build from that picture: "
                                 + said, "refused": True}, 422)
            return
        self._json({"error": "could not parse a character out of the reply. "
                             "If that model has no vision it never saw the "
                             "picture at all — check the model, not the "
                             "image.",
                    "raw": (raw_reply or "")[:600]}, 502)

    def _chargen_revise(self) -> None:
        """POST /api/forge/characters/refine — argue with one pitch."""
        body = self._body()
        persona = (rows_get("personas", int(body["persona_id"]))
                   if body.get("persona_id") else None)
        character = body.get("character") or {}
        if not character.get("name"):
            self._json({"error": "character required"}, 400)
            return
        voices, models = self._chargen_options()
        _b, _k, _m, _s, _meta, jailbreak = self._resolve_llm(body)
        messages = chargen.build_revise_messages(
            persona, character, body.get("instruction", ""),
            into=body.get("into", ""), voices=voices, models=models,
            system=prompts.get("chargen_revise"), jailbreak=jailbreak)
        raw, err, code = self._chargen_llm(body, messages, 4000)
        if err:
            self._json(err, code)
            return
        revised = chargen.parse_one(raw, voices, models)
        if not revised:
            self._json({"error": "could not parse the revision",
                        "raw": (raw or "")[:600]}, 502)
            return
        self._json({"ok": True, "character": revised})

    def _chargen_create(self) -> None:
        """POST /api/forge/characters/create — commit a pitch to the roster.

        Saves the card, rolls her a pinned seed, and renders her portrait
        through the ordinary studio path so her first picture is made exactly
        the way every later one will be.
        Body: {character, persona_id?, portrait?, backend, model, key?,
               image_b64?, image_name?}
        """
        body = self._body()
        pitch = dict(body.get("character") or {})
        if not pitch.get("name"):
            self._json({"error": "character required"}, 400)
            return
        pitch.setdefault("seed", random.randint(1, 2 ** 31 - 1))
        data = chargen.to_card(pitch)
        save = {"name": pitch["name"], "data": data}
        # CFTF: the picture she was forged from becomes her face AND her
        # generation reference. Her face, because a card built from a
        # photograph whose avatar is a fresh render of a *description* of
        # that photograph is not what anyone asked for — and if a portrait
        # was requested it overwrites this a moment later, so a failed render
        # leaves her looking like herself instead of leaving her blank. Her
        # reference, because that is the same file doing the job a ref2v
        # workflow reaches for.
        if body.get("image_b64"):
            try:
                raw = base64.b64decode(body["image_b64"], validate=True)
            except (binascii.Error, ValueError):
                raw = b""
            try:
                fname = _store_upload(raw, body.get("image_name", "ref.png"))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            data["visual"] = {**(data.get("visual") or {}), "ref": fname}
            save["avatar"] = fname
        row = rows_upsert("characters", save)

        result = {"ok": True, "character": row, "portrait": None}
        if not body.get("portrait", True):
            self._json(result)
            return

        # Portrait through the same studio path as everything else — if it
        # fails, she is still created. A missing picture is not a reason to
        # lose the character the user just spent five minutes shaping.
        err = self._render_portrait(row, body, pitch.get("appearance", ""),
                                    pitch.get("scenario", ""), result)
        if err:
            result["portrait_error"] = err
        self._json(result)

    def _character_portrait(self, char_id: int) -> None:
        """POST /api/characters/<id>/portrait — re-roll her picture.

        The forge pins a seed at creation so she looks like herself from her
        FIRST picture. That is the right default and the wrong thing to be
        stuck with when the first roll is ugly, which is what people actually
        reported. `new_seed` rerolls the pin and keeps it, so the rest of her
        gallery follows the face you settled on rather than the one you were
        given.
        Body: {backend, model, key?, persona_id?, new_seed?}
        """
        body = self._body()
        row = rows_get("characters", char_id)
        if not row:
            self._json({"error": "character not found"}, 404)
            return
        data = row.get("data") or {}
        # studio.py reads her looks from data.visual — same key, or a re-roll
        # would silently ignore the appearance and pinned seed she was given.
        look = data.get("visual") or {}
        if body.get("new_seed"):
            look = {**look, "seed": random.randint(1, 2 ** 31 - 1)}
            data = {**data, "visual": look}
            rows_upsert("characters", {"name": row["name"], "data": data,
                                       "avatar": row.get("avatar", "")}, char_id)
            row = rows_get("characters", char_id)
        fields = data.get("fields", {}) if isinstance(data.get("fields"), dict) else {}
        result = {"ok": True, "portrait": None}
        err = self._render_portrait(
            row, body, look.get("appearance", ""), fields.get("scenario", ""),
            result)
        if err:
            result["ok"] = False
            result["error"] = err
        self._json(result)

    def _character_avatar(self, char_id: int) -> None:
        """POST /api/characters/<id>/avatar — promote a gallery image.

        The cheapest fix for an ugly portrait is usually not another render:
        it is one of the pictures she already has. Body: {asset_id}.
        """
        body = self._body()
        row = rows_get("characters", char_id)
        if not row:
            self._json({"error": "character not found"}, 404)
            return
        with get_db() as conn:
            # `assets` is not a named-row table — rows_get asserts on it.
            a = conn.execute(
                "SELECT id, path, kind, character_id FROM assets WHERE id=?",
                (int(body.get("asset_id") or 0),)).fetchone()
        if not a or a["character_id"] != char_id:
            self._json({"error": "that picture is not hers"}, 400)
            return
        if a["kind"] != "image" or not (ASSETS / a["path"]).exists():
            self._json({"error": "not an image"}, 400)
            return
        rows_upsert("characters", {"name": row["name"], "avatar": a["path"],
                                   "data": row.get("data") or {}}, char_id)
        self._json({"ok": True, "avatar": a["path"],
                    "url": "/api/avatars/" + a["path"]})

    # A span longer than this is never a clone reference — it is somebody
    # asking the server to download a film. The verdict already says anything
    # past 15s buys nothing; this is the outer limit on what will be FETCHED,
    # which is a different question and belongs on the route rather than in
    # the CLI (a local user running yt-dlp themselves needs no permission
    # from us).
    MAX_CAPTURE_SECONDS = 120

    def _character_voice_capture(self, char_id: int) -> None:
        """POST /api/characters/<id>/voice-capture {url, start, end} — SSE.

        The UI half of `voiceclip.py`: pull a span out of a video, check it
        against the cloning rules, and install it as her voice sample if it
        passes. Streams because yt-dlp takes tens of seconds and a blocking
        POST behind a static "capturing…" is indistinguishable from a hang —
        the same lesson the studio routes learned.

        Everything that can 4xx is decided BEFORE the stream headers, so the
        content type is an honest signal of which shape the client got.
        """
        body = self._body()
        row = rows_get("characters", char_id)
        if not row:
            self._json({"error": "character not found"}, 404)
            return
        try:
            url = voiceclip.check_url(body.get("url", ""))
            start = voiceclip.parse_timestamp(body.get("start", "0"))
            end = voiceclip.parse_timestamp(body.get("end", ""))
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        if end == float("inf"):
            self._json({"error": "give an end time — a whole video is not a "
                                 "voice reference"}, 400)
            return
        if end <= start:
            self._json({"error": "the end has to come after the start"}, 400)
            return
        if end - start > self.MAX_CAPTURE_SECONDS:
            self._json({"error": f"that is {int(end - start)}s; keep it under "
                                 f"{self.MAX_CAPTURE_SECONDS}s. Cloning uses "
                                 f"3-15."}, 400)
            return
        if not shutil.which("yt-dlp") or not shutil.which("ffmpeg"):
            missing = [t for t in ("yt-dlp", "ffmpeg") if not shutil.which(t)]
            self._json({"error": f"{' and '.join(missing)} not installed on "
                                 f"the machine running CoomKit — capturing "
                                 f"from a link needs {'them' if len(missing) > 1 else 'it'}. "
                                 f"You can still upload a clip."}, 400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # Never "Connection: keep-alive" on SSE — see _studio_approve for the
        # whole story. Pinned so a protocol_version bump cannot resurrect it.
        self.close_connection = True

        def send(obj) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        def done(obj) -> None:
            send(obj)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        send({"note": f"fetching {int(end - start)}s…"})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wav = voiceclip.capture(
                    url, start, end, Path(tmp) / "capture.wav", quiet=True,
                    on_note=lambda n: send({"note": n}))
                send({"note": "checking it…"})
                report = voiceclip.inspect_wav(wav)
                ok, notes = voiceclip.verdict(report)
                if not ok:
                    # Not stored. A reference that fails is not a file you
                    # want quietly sitting on the card being cloned from.
                    done({"ok": False, "report": report, "notes": notes,
                          "error": "that clip will not clone well"})
                    return
                raw = wav.read_bytes()
                fname = _store_upload(raw, "voice.wav")
        except voiceclip.MissingTool as exc:
            done({"error": str(exc)})
            return
        except ValueError as exc:
            done({"error": str(exc)})
            return
        except Exception as exc:                       # noqa: BLE001
            done({"error": str(exc)})
            return

        # The same write the upload route makes, on the same key.
        data = row.get("data") or {}
        voice = dict(data.get("voice") or {})
        voice["sample"] = fname
        if body.get("ref_text"):
            voice["ref_text"] = body["ref_text"]
        data["voice"] = voice
        rows_upsert("characters", {"name": row["name"], "data": data,
                                   "avatar": row.get("avatar") or ""}, char_id)
        # /api/avatars serves out of data/assets and is what the editor's
        # audio player already points at for an uploaded sample. Same URL
        # shape, so the preview works without a second serving route.
        done({"ok": True, "file": fname, "report": report, "notes": notes,
              "url": f"/api/avatars/{fname}"})

    # Image recipes a portrait re-roll may use. Audio and video are excluded:
    # an avatar is a still, and offering "ASMR" in a portrait picker is noise.
    PORTRAIT_RECIPES = ("solo-model", "selfie", "solo-lewd", "scene", "describe")

    def _render_portrait(self, row: dict, body: dict, appearance: str,
                         setting: str, result: dict):
        """Render a character's portrait and make it her avatar.

        Shared by the forge (her first picture) and by "regenerate" on an
        existing character, so a re-roll goes down the identical path rather
        than a private one — same rule as _prepare_request for chat and
        studio.plan/run for media. Returns an error string, or None.
        """
        try:
            cfg = load_config()
            persona = (rows_get("personas", int(body["persona_id"]))
                       if body.get("persona_id") else None)
            ctx = {"char": row["name"], "user": (persona or {}).get("name", "anon"),
                   "appearance": appearance, "scene": "", "setting": setting}
            # Which shot, and on which image model. Both are per-render: you
            # are hunting for a portrait you like, not editing her saved looks.
            rid = body.get("recipe") or "solo-model"
            if rid not in self.PORTRAIT_RECIPES:
                rid = "solo-model"
            opts = {"wardrobe": "clothed"}
            if body.get("prompt"):
                opts["prompt"] = body["prompt"]      # the free-form recipe
            job = studio.plan(rid, opts, ctx, cfg, row, persona,
                              workflow=body.get("workflow", ""))
            job["character_id"] = row["id"]
            brief = recipes.fill(rid, opts, ctx,
                                 text=prompts.get("recipe_" + rid))
            raw, err, _code = self._chargen_llm(
                body, studio.writer_messages(job, brief), 2000)
            if err:
                return err["error"]
            values = studio.ensure_artists(
                job, studio.apply_pins(job, studio.parse_writer(job, raw)))

            def read_asset(name):
                path = ASSETS / name
                return path.read_bytes() if path.exists() else None

            out = studio.run(job, values, cfg, asset_path=read_asset)
            saved = self._save_assets(out["files"], job, values, out.get("meta"))
            if saved:
                result["portrait"] = saved[0]
                # An avatar has to be a PICTURE. comfy.kind_of classifies by
                # extension precisely because SaveVideo files arrive under the
                # history's `images` key, so this is the check that stops an
                # .mp4 being written into characters.avatar and rendering as a
                # broken <img> forever. The render still lands in her gallery.
                if saved[0].get("kind") == "image":
                    fname = saved[0]["url"].rsplit("/", 1)[-1]
                    rows_upsert("characters",
                                {"name": row["name"], "avatar": fname,
                                 "data": row.get("data") or {}}, row["id"])
                    result["character"] = rows_get("characters", row["id"])
                else:
                    result["note"] = ("that made a %s, not a picture — it is in "
                                      "her gallery but it is not her face"
                                      % saved[0].get("kind"))
            return None
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    # -- studio: recipes -> approved prompt -> media -------------------------
    def _tags_status(self) -> None:
        """GET /api/tags — the curated sets, plus whether a tag DB was found.

        The 150k-tag Danbooru corpus is deliberately not bundled (see
        tags.py); this reports where CoomKit found the user's copy so the UI
        can say so plainly instead of silently offering nothing.
        """
        self._json(tags.status(load_config()))

    def _tags_search(self, query: dict) -> None:
        cat = query.get("cat", [""])[0]
        cat_id = {v: k for k, v in tags.CATEGORIES.items()}.get(cat)
        self._json({"results": tags.search(
            query.get("q", [""])[0], cat_id,
            int(query.get("limit", ["25"])[0] or 25), load_config())})

    def _tags_artists(self, query: dict) -> None:
        """GET /api/tags/artists — roll a weighted-random artist blend."""
        picked = tags.random_artists(
            int(query.get("n", ["2"])[0] or 2),
            int(query.get("min_posts", ["500"])[0] or 500),
            cfg=load_config())
        self._json({"artists": picked, "clause": tags.artist_clause(picked)})

    def _loras(self) -> None:
        """GET /api/loras — what the user's ComfyUI actually has installed.

        Asked of ComfyUI rather than guessed from a path, because the GPU box
        is often not this box.
        """
        url = load_config().get("comfyui_url", "")
        if not url:
            self._json({"loras": [], "error": "no comfyui configured"})
            return
        try:
            info = comfy.ComfyClient(url, timeout=15)._get(
                "/object_info/LoraLoader")
            opts = (info.get("LoraLoader", {}).get("input", {})
                    .get("required", {}).get("lora_name", [[]])[0])
            self._json({"loras": list(opts)})
        except Exception as exc:  # noqa: BLE001
            self._json({"loras": [], "error": str(exc)})

    def _studio_catalogue(self) -> None:
        """GET /api/studio — everything the studio pane needs to draw itself."""
        cfg = load_config()
        url = cfg.get("comfyui_url", "")
        available = set()
        if url:
            try:
                available = set(comfy.ComfyClient(url, timeout=8)
                                ._get("/object_info").keys())
            except Exception:  # noqa: BLE001
                available = set()

        workflows = []
        for entry in wfpack.catalogue():
            missing = []
            if available:
                try:
                    graph, _ = wfpack.build(entry["name"], {"prompt": "x"})
                    missing = wfpack.missing(graph, available)
                except Exception:  # noqa: BLE001
                    missing = []
            workflows.append({**entry, "missing": missing,
                              "packs": sorted({wfpack.PACK_OF.get(m, m)
                                               for m in missing})})
        self._json({
            "recipes": recipes.catalogue(),
            "workflows": workflows,
            "defaults": {**studio.DEFAULT_WORKFLOWS,
                         **(cfg.get("studio") or {})},
            "stages": {k: {"label": v["label"], "why": v["why"],
                           "default": v.get("default", False)}
                       for k, v in wfpack.STAGES.items()
                       if k != "preview"},
            "comfy": bool(url),
            "voices": voices_mod.available(),
            # The client used to hardcode a preset id here. When the shipped
            # voices were renamed to archetypes the id died, the picker went
            # blank, and voices.resolve() quietly fell through to DEFAULT —
            # so a button labelled one voice produced a different one.
            "voice_default": voices_mod.DEFAULT,
            "speeds": voices_mod.SPEEDS,
            "vram": vram.status(cfg, url),
        })

    def _installed_loras(self, cfg: dict):
        """LoRA names ComfyUI reports, or None when we could not ask."""
        url = cfg.get("comfyui_url") or ""
        return studio._installed_loras(url) if url else None

    def _studio_context(self, body: dict) -> dict:
        """Character, persona and the live scene, as the briefs expect them."""
        char = (rows_get("characters", int(body["character_id"]))
                if body.get("character_id") else None)
        persona = (rows_get("personas", int(body["persona_id"]))
                   if body.get("persona_id") else None)
        chat_id = int(body.get("chat_id") or 0)
        # Fill each from the chat INDEPENDENTLY. This used to be guarded on
        # `not char`, and the studio pane always sends character_id and never
        # persona_id — so the guard was always false and the persona was
        # never resolved at all. That is not cosmetic: a reference photo of
        # the user lives on the persona, so every ref2v render silently went
        # out with her picture and nothing else.
        # Read `chats` with a direct query: it is not a named-row table, and
        # rows_get() asserts on it with a bare AssertionError.
        if chat_id and (not char or not persona):
            with get_db() as conn:
                row = conn.execute(
                    "SELECT character_id, persona_id FROM chats WHERE id=?",
                    (chat_id,)).fetchone()
            chat = dict(row) if row else {}
            if not char and chat.get("character_id"):
                char = rows_get("characters", int(chat["character_id"]))
            if not persona and chat.get("persona_id"):
                persona = rows_get("personas", int(chat["persona_id"]))

        cdata = (char or {}).get("data") or {}
        visual = cdata.get("visual") or {}
        appearance = visual.get("appearance") or cdata.get("description") or ""
        voice = (cdata.get("voice") or {}).get("instruct", "")

        scene = ""
        if chat_id:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT role, content FROM messages WHERE chat_id=?"
                    " ORDER BY id DESC LIMIT ?",
                    (chat_id, int(body.get("scene_turns", 6)))).fetchall()
            scene = "\n".join(f"{r['role']}: {r['content'][:600]}"
                              for r in reversed(rows))

        ctx = {"char": (char or {}).get("name", "she"),
               "user": (persona or {}).get("name", "anon"),
               "appearance": appearance[:1200], "voice": voice,
               "scene": scene, "chat_id": chat_id or None,
               "message_id": body.get("message_id")}
        return {"ctx": ctx, "character": char, "persona": persona}

    def _studio_draft(self) -> None:
        """POST /api/studio/draft — recipe in, a prompt to approve out.

        Body: {recipe, opts?, chat_id?, character_id?, persona_id?,
               message_id?, backend, model, key?, preset_id?}
        Nothing reaches the GPU here. The user sees the prompt first.
        """
        body = self._body()
        cfg = load_config()
        rid = body.get("recipe", "")
        if rid not in recipes.RECIPES:
            self._json({"error": f"no recipe called {rid!r}"}, 400)
            return
        found = self._studio_context(body)
        ctx, char, persona = found["ctx"], found["character"], found["persona"]

        # A hand-picked "use THIS picture of her" reference must actually be
        # one of HER pictures. Checked here rather than in plan() because the
        # gallery is a server concept — and a filename from someone else's
        # gallery (or from nowhere) riding into a render is exactly the kind
        # of thing that must fail loudly at draft time, not 400 from ComfyUI
        # a minute into an upload.
        opts = body.get("opts") or {}
        if opts.get("her_ref"):
            with get_db() as conn:
                owned = conn.execute(
                    "SELECT 1 FROM assets WHERE path=? AND character_id=?",
                    (opts["her_ref"], (char or {}).get("id"))).fetchone()
            if not owned:
                self._json({"error": "that picture is not in her gallery"},
                           400)
                return

        try:
            job = studio.plan(rid, opts, ctx, cfg, char, persona)
        except studio.StudioError as exc:
            self._json({"error": str(exc)}, 400)
            return

        # "Say it out loud" has nothing to invent — the words already exist.
        # Running a writer over them would rewrite her dialogue, which is the
        # one thing this recipe must not do.
        # A recipe may declare fields it cannot work without. The free-form
        # one does: with nothing typed, fill() substitutes "nothing in
        # particular" and the writer cheerfully invents a shot nobody asked
        # for. Generic on purpose — a second hardcoded recipe id here would
        # be a third place they leak into the server.
        for need in recipes.RECIPES[rid].get("requires") or []:
            if not str((body.get("opts") or {}).get(need) or "").strip():
                self._json({"error": "tell me what you want first"}, 400)
                return

        direct = bool(recipes.RECIPES[rid].get("direct"))
        if direct:
            ctx["lines"] = self._speak_lines(body, ctx)
            if not ctx["lines"]:
                self._json({"error": "no dialogue found in that message"}, 400)
                return

        brief = recipes.fill(rid, body.get("opts") or {}, ctx,
                             text=prompts.get(f"recipe_{rid}"))

        if direct:
            # No writer, no backend, no round trip. This is what the recipe's
            # `direct` flag has always claimed and nothing ever honoured — so
            # a prompt-writer ran over skills/voice.md and copied its ASMR
            # worked-examples in as `ambience` and `emotions`, on a recipe
            # whose entire job is to read words that already exist.
            values = studio.speak_values(job, ctx["lines"])
        else:
            backend, key, model, _s, _m, _jb = self._resolve_llm(body)
            if not (backend and model):
                self._json({"error": "backend and model are required"}, 400)
                return
            messages = studio.writer_messages(job, brief)
            payload = llm.build_payload(
                messages, model,
                {"max_tokens": 4000, "temperature": 0.9, "top_p": 0.95},
                stream=False)
            try:
                raw = llm.once_retry(backend, key, payload, "chat")
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 502)
                return

            values = studio.ensure_artists(
                job, studio.apply_pins(job, studio.parse_writer(job, raw)))
        job["character_id"] = (char or {}).get("id")
        pid = tools.register({"studio": job, "brief": brief},
                             json.dumps(values))
        self._json({"ok": True, "id": pid, "recipe": rid,
                    "kind": job["kind"], "workflow": job["workflow"],
                    "label": wfpack.BUNDLED[job["workflow"]]["label"],
                    "values": values, "brief": brief,
                    "review": studio.review(job, values, self._installed_loras(cfg)),
                    "vram_gb": job["vram_gb"],
                    # label AND file: the approval card shows the actual
                    # pictures riding along, so "which photo is Picture 2"
                    # is visible before a minute of video is spent on it.
                    "refs": [{"label": r["label"], "file": r["file"],
                              "source": r.get("source", "")}
                             for r in job["refs"]]})

    def _speak_lines(self, body: dict, ctx: dict) -> str:
        """The words to read out, in order of specificity.

        `lines`, NOT `text`: the client builds this body with requestBody(),
        whose `text` field is the user's next chat message, and it always
        sets it — to the composer contents or the literal "(preview)". That
        key collision meant the override always won and the message was never
        read, which is why speaking a reply produced a preview of nothing.
        """
        source = (body.get("lines") or "").strip()
        if not source and body.get("message_id"):
            # `messages` is deliberately not in VALID_TABLES (it is not a
            # named-row store), so it has to be read directly.
            with get_db() as conn:
                row = conn.execute(
                    "SELECT content, data FROM messages WHERE id=?",
                    (int(body["message_id"]),)).fetchone()
            source = engine.active_content(_row_to_dict(row)) if row else ""
        if not source and ctx.get("chat_id"):
            with get_db() as conn:
                row = conn.execute(
                    "SELECT content, data FROM messages WHERE chat_id=?"
                    " AND role='assistant' ORDER BY id DESC LIMIT 1",
                    (int(ctx["chat_id"]),)).fetchone()
            source = engine.active_content(_row_to_dict(row)) if row else ""
        return studio.dialogue_lines(source)

    def _studio_pending_from_tool(self, call: dict, chat_id: int, msg_id: int,
                                  chat_row: dict, body: dict, send) -> None:
        """She asked for a shot mid-scene — draft it and queue it for approval.

        Same path as the studio buttons, so a tool call and a button press are
        not two different features that drift apart.
        """
        # chat_row is a sqlite3.Row — it indexes but has no .get().
        req = {"recipe": call["recipe"], "opts": call.get("opts") or {},
               "chat_id": chat_id, "message_id": msg_id,
               "character_id": chat_row["character_id"],
               "persona_id": chat_row["persona_id"]}
        found = self._studio_context(req)
        ctx, char, persona = found["ctx"], found["character"], found["persona"]
        cfg = load_config()
        job = studio.plan(req["recipe"], req["opts"], ctx, cfg, char, persona)
        job["character_id"] = (char or {}).get("id")

        if req["recipe"] == "speak":
            ctx["lines"] = studio.dialogue_lines(
                (call.get("prompt") or "").strip())
        brief = recipes.fill(req["recipe"], req["opts"], ctx,
                             text=prompts.get(f"recipe_{req['recipe']}"))

        backend, key, model, _s, _m, _jb = self._resolve_llm(body)
        payload = llm.build_payload(
            studio.writer_messages(job, brief), model,
            {"max_tokens": 4000, "temperature": 0.9}, stream=False)
        raw = llm.once_retry(backend, key, payload, "chat")
        values = studio.ensure_artists(
            job, studio.apply_pins(job, studio.parse_writer(job, raw)))

        payload = {
            "recipe": req["recipe"], "kind": job["kind"],
            "workflow": job["workflow"],
            "label": wfpack.BUNDLED[job["workflow"]]["label"],
            "values": values,
            "review": studio.review(job, values, self._installed_loras(cfg)),
            "vram_gb": job["vram_gb"],
            "refs": [{"label": r["label"], "file": r["file"],
                      "source": r.get("source", "")} for r in job["refs"]]}
        # The payload rides in the registry beside the job so a pending card
        # can be re-shown later — a daemon-sent text has nobody watching the
        # stream, and without this her "wait till you see" attachment
        # evaporated if no tab was open the moment it was drafted.
        pid = tools.register({"studio": job, "brief": brief,
                              "payload": payload}, json.dumps(values))
        payload["id"] = pid
        send({"studio_pending": payload})

    def _studio_approve(self) -> None:
        """POST /api/studio/approve {id, values?} — run the approved job.

        Streams SSE now: {note} frames as the run narrates itself, {progress}
        frames while ComfyUI works, one final {ok, assets, ...} (or {error})
        frame, then [DONE]. It used to be a single blocking POST whose status
        text was written client-side BEFORE the await and never updated — a
        seven-minute H3 render sat behind "rendering on your box…" with no
        sign of life, which is indistinguishable from a hang. The vram
        brokering notes were buffered the same way and arrived after the job,
        which is the one time they are useless.
        """
        body = self._body()
        pending = tools.pending_pop(int(body.get("id", 0)))
        if not pending:
            self._json({"error": "no such pending job"}, 404)
            return
        job = pending["call"]["studio"]
        try:
            values = json.loads(pending["prompt"])
        except json.JSONDecodeError:
            values = {}
        # The user's edits win — but they arrive as STRINGS (the approval
        # card is textareas), and set_slots writes them into the graph
        # verbatim, so "10" landing on a float node rejects the graph.
        # Coerce each edit back to the drafted value's own type.
        for k, v in (body.get("values") or {}).items():
            cur = values.get(k)
            if isinstance(cur, bool):
                v = v in (True, "true", "on", "1", 1)
            elif isinstance(cur, (int, float)) and isinstance(v, str):
                try:
                    v = int(float(v)) if isinstance(cur, int) else float(v)
                except ValueError:
                    pass
            values[k] = v

        cfg = load_config()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # NEVER send "Connection: keep-alive" on an SSE response:
        # http.server's send_header flips close_connection to False on that
        # exact value, so after [DONE] the server holds the socket open for
        # a next request — and a body with no Content-Length and no chunked
        # framing means the client cannot know it ended. A browser fetch
        # never resolves and the UI freezes on the last status line, which
        # presented as "stuck in the rendering stage even though the render
        # is done". Pinned explicitly so a future protocol_version bump
        # cannot quietly resurrect it.
        self.close_connection = True

        def send(obj) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        def read_asset(name: str):
            path = ASSETS / name
            return path.read_bytes() if path.exists() else None

        try:
            result = studio.run(job, values, cfg, asset_path=read_asset,
                                note=lambda m: send({"note": m}),
                                progress=lambda p: send({"progress": p}))
            saved = self._save_assets(result["files"], job, values,
                                      result.get("meta"))
            send({"ok": True, "assets": saved,
                  "workflow": result["workflow"],
                  "vram": result["vram"].get("steps", [])})
        except (studio.StudioError, comfy.ComfyError) as exc:
            send({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            send({"error": f"{type(exc).__name__}: {exc}"})
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _studio_remake(self) -> None:
        """POST /api/studio/remake {asset_id, prompt?, seed?}

        Run a stored render again — same prompt or an edited one, same seed
        or a fresh one. No LLM: this is the same-prompt path by definition.
        It still goes through studio.run, so there is still exactly one
        generation path.
        """
        body = self._body()
        try:
            asset_id = int(body.get("asset_id") or 0)
        except (TypeError, ValueError):
            asset_id = 0
        with get_db() as conn:
            # `assets` is NOT in VALID_TABLES — rows_get asserts on it.
            row = conn.execute("SELECT * FROM assets WHERE id=?",
                               (asset_id,)).fetchone()
        if not row:
            self._json({"error": "not found"}, 404)
            return
        try:
            d = json.loads(row["data"] or "{}")
        except json.JSONDecodeError:
            d = {}
        if not d.get("job"):
            self._json({"error": "this one predates remakes — draft it "
                                 "again from the studio"}, 400)
            return

        job = dict(d["job"])
        job["chat_id"] = row["chat_id"]
        job["character_id"] = row["character_id"]
        # Pinning message_id is what makes the remake land in the SAME inline
        # strip rather than on whatever message happens to be newest now.
        job["message_id"] = row["message_id"]

        values = dict(d.get("values") or {})
        if body.get("prompt") is not None:
            text = str(body["prompt"])
            if job.get("kind") == "music":
                values["music_prompt"] = text
            elif job.get("json_output"):
                values["audio_text"] = text
            else:
                values["prompt"] = text

        seed = body.get("seed", "same")
        if seed == "new":
            # Must be set EXPLICITLY. studio.run merges job["values"] beneath
            # what it is passed, so deleting the key lets the character's
            # pinned seed win and "new seed" would silently give the old face.
            values["seed"] = random.randint(0, 2 ** 31 - 1)
        elif seed not in ("same", None, ""):
            try:
                values["seed"] = int(seed)
            except (TypeError, ValueError):
                pass

        cfg = load_config()

        # SSE from here, same contract as _studio_approve: {note} frames as
        # the run narrates, {progress} while ComfyUI works, one final result
        # frame, [DONE]. The remake was the last blocking render path — a
        # video ⟳ sat behind a frozen toast for its whole four minutes.
        # Everything that can 4xx has already returned above, so the status
        # line is honest.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # NEVER send "Connection: keep-alive" on an SSE response:
        # http.server's send_header flips close_connection to False on that
        # exact value, so after [DONE] the server holds the socket open for
        # a next request — and a body with no Content-Length and no chunked
        # framing means the client cannot know it ended. A browser fetch
        # never resolves and the UI freezes on the last status line, which
        # presented as "stuck in the rendering stage even though the render
        # is done". Pinned explicitly so a future protocol_version bump
        # cannot quietly resurrect it.
        self.close_connection = True

        def send(obj) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        def read_asset(name: str):
            path = ASSETS / name
            return path.read_bytes() if path.exists() else None

        try:
            result = studio.run(job, values, cfg, asset_path=read_asset,
                                note=lambda m: send({"note": m}),
                                progress=lambda p: send({"progress": p}))
            saved = self._save_assets(result["files"], job, values,
                                      result.get("meta"))
            send({"ok": True, "assets": saved,
                  "workflow": result["workflow"],
                  "seed": (result.get("meta") or {}).get(
                      "values", {}).get("seed"),
                  "vram": result["vram"].get("steps", [])})
        except (studio.StudioError, comfy.ComfyError) as exc:
            send({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            send({"error": f"{type(exc).__name__}: {exc}"})
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # Which keys of the job are needed to run it again. Everything here is
    # plain JSON already — studio.plan returns only primitives, lists and
    # dicts. Deliberately NOT the whole job: chat/character/message ids are
    # real columns, and the browser never sees this blob.
    REMAKE_KEYS = ("recipe", "opts", "kind", "workflow", "skill", "refs",
                   "voice_sample", "voice_preset", "loras", "artists",
                   "tag_dialect", "stages", "vram_gb", "json_output", "ctx")

    def _save_assets(self, files: list, job: dict, values: dict,
                     meta: dict | None = None) -> list:
        ASSETS.mkdir(parents=True, exist_ok=True)
        saved = []
        now = time.time()
        # Anchor loose media to the newest message in the chat. A picture the
        # user asked for from a button has no message of its own, and without
        # this it renders once and then disappears on the next reload —
        # which in a phone thread looks exactly like the photo was deleted.
        message_id = job.get("message_id")
        if not message_id and job.get("chat_id"):
            with get_db() as conn:
                row = conn.execute(
                    "SELECT id FROM messages WHERE chat_id=?"
                    " ORDER BY id DESC LIMIT 1", (job["chat_id"],)).fetchone()
            message_id = row["id"] if row else None
        # The receipt. `meta["values"]` is wfpack.build's post-merge dict and
        # is the ONLY place the rolled seed exists — without it nothing on
        # disk could reconstruct a render, because tools.pending is in memory
        # and popped on approve.
        eff = dict((meta or {}).get("values") or values)
        blurb = eff.get("prompt") or eff.get("audio_text") or \
            eff.get("music_prompt") or ""
        receipt = {
            # not truncated: an exact re-run needs the exact text
            "prompt": blurb,
            "workflow": job.get("workflow"),
            "opts": job.get("opts", {}),
            "seed": eff.get("seed"),
            "values": eff,
            "job": {k: job.get(k) for k in self.REMAKE_KEYS},
        }
        with get_db() as conn:
            for f in files:
                ext = Path(f["filename"]).suffix or ".bin"
                fname = f"gen_{int(now * 1000)}_{len(saved)}{ext}"
                (ASSETS / fname).write_bytes(f["data"])
                cur = conn.execute(
                    "INSERT INTO assets (chat_id, message_id, character_id,"
                    " recipe, kind, path, data, created)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (job.get("chat_id"), message_id,
                     job.get("character_id"), job.get("recipe", ""),
                     f["kind"], fname, json.dumps(receipt), now))
                saved.append({"id": cur.lastrowid, "kind": f["kind"],
                              "url": f"/api/avatars/{fname}"})
        return saved

    # -- datapack: one-shot LAN clone between two installs ----------------
    #
    # A CLONE, not a merge. Every table cross-references AUTOINCREMENT ids —
    # including inside JSON blobs (message speaker stamps, lorebook
    # from_card_id) — so a merging import would have to remap ids through
    # free-form JSON, and one missed reference silently mis-attributes
    # messages: the exact bug class the tombstone machinery exists to
    # prevent. Wholesale replacement is honest; per-object merge can ride
    # the card-export format later.

    def _datapack_get(self, query: dict) -> None:
        """GET /api/datapack[?keys=1] — the whole install as one zip.

        The db snapshot goes through sqlite's backup API (WAL-safe; a plain
        file copy of a live WAL db can be torn). API keys for remote
        backends are STRIPPED unless ?keys=1 — same-owner LAN clones
        usually want them, but they travel only when asked for.
        """
        import shutil as _sh
        import tempfile
        import zipfile
        include_keys = (query.get("keys") or [""])[0] == "1"
        tmpdir = tempfile.mkdtemp(prefix="ck-pack-")
        try:
            snap = os.path.join(tmpdir, "coomkit.sqlite")
            db_file = DATA / "coomkit.sqlite"
            if db_file.exists():
                src = sqlite3.connect(str(db_file))
                dst = sqlite3.connect(snap)
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
            zpath = os.path.join(tmpdir, "pack.zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                if os.path.exists(snap):
                    z.write(snap, "coomkit.sqlite")
                cfg = json.loads(json.dumps(load_config()))
                if not include_keys:
                    for rb in cfg.get("remote_backends") or []:
                        rb.pop("key", None)
                # vram-parked.json is deliberately absent: it is a debt to a
                # GPU the other machine does not have.
                z.writestr("config.json", json.dumps(cfg, indent=2))
                prompts_file = DATA / "prompts.json"
                if prompts_file.exists():
                    z.write(prompts_file, "prompts.json")
                if ASSETS.exists():
                    for f in sorted(ASSETS.iterdir()):
                        if f.is_file():
                            z.write(f, "assets/" + f.name)
            size = os.path.getsize(zpath)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             'attachment; filename="coomkit-datapack.zip"')
            self.end_headers()
            # _static_file reads whole files into memory; a datapack is
            # mostly renders and can be GBs, so this one streams.
            with open(zpath, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _sh.rmtree(tmpdir, ignore_errors=True)

    def _datapack_pull(self) -> None:
        """POST /api/datapack/pull {url, keys?, keep_config?} — become the
        other install.

        Replaces data/ wholesale (the old one survives as data.bak/).
        `keep_config` defaults ON: the puller's own config points at ITS
        backends and ComfyUI — a phone cloning a desktop wants the
        desktop's characters and chats, not a config full of the desktop's
        127.0.0.1 addresses that resolve to the phone itself.
        """
        import shutil as _sh
        import tempfile
        import zipfile
        body = self._body()
        url = (body.get("url") or "").strip().rstrip("/")
        if not url:
            self._json({"error": "url required — the other CoomKit's "
                                 "address, e.g. http://192.168.1.20:3939"}, 400)
            return
        if "://" not in url:
            url = "http://" + url
        fetch = url + "/api/datapack" + ("?keys=1" if body.get("keys") else "")
        tmpdir = tempfile.mkdtemp(prefix="ck-pull-")
        try:
            zpath = os.path.join(tmpdir, "pack.zip")
            try:
                with urllib.request.urlopen(fetch, timeout=1800) as r, \
                        open(zpath, "wb") as out:
                    _sh.copyfileobj(r, out, 1 << 20)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"could not fetch the pack: {exc}"}, 502)
                return
            newdir = os.path.join(tmpdir, "data")
            os.makedirs(os.path.join(newdir, "assets"), exist_ok=True)
            try:
                with zipfile.ZipFile(zpath) as z:
                    names = z.namelist()
                    if "coomkit.sqlite" not in names:
                        self._json({"error": "that is not a CoomKit "
                                             "datapack"}, 400)
                        return
                    for n in names:
                        # zip-slip guard: nothing may escape the target dir
                        if n.startswith("/") or ".." in n:
                            self._json({"error": f"unsafe path in pack: "
                                                 f"{n}"}, 400)
                            return
                    z.extractall(newdir)
            except zipfile.BadZipFile:
                self._json({"error": "that is not a zip at all"}, 400)
                return
            keep_config = body.get("keep_config", True)
            own_config = (DATA / "config.json").read_text() \
                if keep_config and (DATA / "config.json").exists() else None
            bak = ROOT / "data.bak"
            if bak.exists():
                _sh.rmtree(bak)
            if DATA.exists():
                DATA.rename(bak)
            _sh.move(newdir, str(DATA))
            if own_config is not None:
                (DATA / "config.json").write_text(own_config)
            counts = {}
            with get_db() as conn:   # also re-stamps the schema version
                for t in ("characters", "chats", "messages", "assets"):
                    counts[t] = conn.execute(
                        f"SELECT count(*) FROM {t}").fetchone()[0]
            self._json({"ok": True, "backup": "data.bak/", **counts,
                        "kept_local_config": bool(own_config is not None)})
        finally:
            _sh.rmtree(tmpdir, ignore_errors=True)

    def _gallery(self, character_id: int) -> None:
        """GET /api/gallery/<character_id> — everything ever made of her.

        Global to the character, not to the chat. A gallery that resets when
        you start a new scene is a folder, not a memory of the two of you.
        """
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM assets WHERE character_id=?"
                " ORDER BY created DESC LIMIT 500", (character_id,)).fetchall()
        out = []
        for r in rows:
            item = _row_to_dict(r)
            item["url"] = f"/api/avatars/{item['path']}"
            out.append(item)
        self._json({"assets": out, "character_id": character_id})

    def _asset_delete(self, asset_id: int) -> None:
        with get_db() as conn:
            row = conn.execute("SELECT path FROM assets WHERE id=?",
                               (asset_id,)).fetchone()
            if not row:
                self._json({"error": "not found"}, 404)
                return
            conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
        target = ASSETS / row["path"]
        if target.exists():
            target.unlink()
        self._json({"ok": True})

    def _char_of_chat(self, chat_id):
        """The character a chat belongs to, for filing a render in her gallery.

        The gallery is keyed on character_id and NEVER on chat_id, so an asset
        row without one is invisible in every gallery forever — it is on disk,
        it is in the table, and nothing will ever show it. Two insert sites
        omitted it. `chats` is not a named-row table; rows_get asserts on it.
        """
        if not chat_id:
            return None
        with get_db() as conn:
            row = conn.execute("SELECT character_id FROM chats WHERE id=?",
                               (int(chat_id),)).fetchone()
        return row["character_id"] if row else None

    def _cast_read(self, chat_id: int):
        """(chat_row, cast) or (None, None). `chat_cast` is NOT in VALID_TABLES
        — rows_get asserts on it, so every read here is a direct query."""
        with get_db() as conn:
            chat = conn.execute("SELECT * FROM chats WHERE id=?",
                                (chat_id,)).fetchone()
            if not chat:
                return None, None
            return chat, engine.cast_of(conn, chat_id, chat["character_id"])

    def _cast_payload(self, chat, cast) -> dict:
        names = {}
        for c in cast:
            row = rows_get("characters", c["character_id"])
            names[c["character_id"]] = row or {}
        return {"cast": [
            {**c, "name": (names[c["character_id"]] or {}).get("name") or "(gone)",
             "avatar": (names[c["character_id"]] or {}).get("avatar", "")}
            for c in cast],
            "active": engine.cast_active(dict(chat), cast),
            "cap": engine.CAST_PRESENT_CAP}

    def _cast_list(self, chat_id: int) -> None:
        """GET /api/chats/<id>/cast"""
        chat, cast = self._cast_read(chat_id)
        if not chat:
            self._json({"error": "chat not found"}, 404)
            return
        self._json(self._cast_payload(chat, cast))

    def _cast_edit(self, chat_id: int) -> None:
        """POST /api/chats/<id>/cast — add, remove, stage in/out, annotate.

        Body: {op: add|remove|present|note, character_id, present?, note?}
        The lead is not removable and cannot be sent off-stage: she is what
        the gallery and the chat list key off, and a scene with an empty room
        is not a state worth representing.
        """
        body = self._body()
        chat, cast = self._cast_read(chat_id)
        if not chat:
            self._json({"error": "chat not found"}, 404)
            return
        op = body.get("op", "add")
        cid = int(body.get("character_id") or 0)
        if not cid or not rows_get("characters", cid):
            self._json({"error": "no such character"}, 400)
            return
        if cid == chat["character_id"]:
            self._json({"error": "she is the lead — she is always here"}, 400)
            return

        want_present = bool(body.get("present", True))
        if op in ("add", "present") and want_present:
            here = len(engine.cast_present(cast))
            already = any(c["character_id"] == cid and c["present"] for c in cast)
            if not already and here >= engine.CAST_PRESENT_CAP:
                # Refused mechanically rather than scolded. The context holds
                # more than a 12B can keep apart, and a silently-floored
                # history budget is the worse failure.
                self._json({"error": f"that is {here} people in one room "
                                     f"already — someone has to leave first",
                            "cap": engine.CAST_PRESENT_CAP}, 400)
                return

        now = time.time()
        with get_db() as conn:
            if op == "remove":
                conn.execute("DELETE FROM chat_cast WHERE chat_id=? AND character_id=?",
                             (chat_id, cid))
            elif op == "note":
                conn.execute(
                    "INSERT INTO chat_cast (chat_id, character_id, present, ord,"
                    " since, data, created) VALUES (?,?,1,?,?,?,?)"
                    " ON CONFLICT(chat_id, character_id) DO UPDATE SET data=excluded.data",
                    (chat_id, cid, len(cast), now,
                     json.dumps({"note": (body.get("note") or "")[:200]}), now))
            else:
                conn.execute(
                    "INSERT INTO chat_cast (chat_id, character_id, present, ord,"
                    " since, data, created) VALUES (?,?,?,?,?,'{}',?)"
                    " ON CONFLICT(chat_id, character_id) DO UPDATE SET"
                    " present=excluded.present, since=excluded.since",
                    (chat_id, cid, 1 if want_present else 0, len(cast), now, now))
            conn.commit()
        chat, cast = self._cast_read(chat_id)
        self._json(self._cast_payload(chat, cast))

    def _export_save(self, chat_id: int) -> None:
        """POST /api/chats/<id>/export/save — keep a rendered log in her gallery.

        The picture is drawn in the browser; this only files it. The gallery is
        keyed on character_id, never chat_id, so an export survives starting a
        new scene — same reasoning as the rest of the gallery.

        The filename is derived here and never taken from the client, and the
        bytes have to actually be a PNG: this route writes into the directory
        /api/avatars/ serves.
        """
        body = self._body()
        try:
            raw = base64.b64decode(body.get("b64", ""), validate=True)
        except (ValueError, binascii.Error):
            raw = b""
        if not raw:
            self._json({"error": "no image data"}, 400)
            return
        if len(raw) > 8 * 1024 * 1024:
            self._json({"error": "that is not a chat log, that is a payload"}, 400)
            return
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            self._json({"error": "png only"}, 400)
            return
        with get_db() as conn:
            chat = conn.execute("SELECT * FROM chats WHERE id=?",
                                (chat_id,)).fetchone()
            if not chat:
                self._json({"error": "chat not found"}, 404)
                return
            fname = "log_%d_%d_%d.png" % (chat_id, int(time.time()),
                                          int(body.get("part", 1) or 1))
            (ASSETS / fname).write_bytes(raw)
            # `assets` is not in VALID_TABLES — rows_upsert asserts on it.
            conn.execute(
                "INSERT INTO assets (chat_id, character_id, message_id, kind,"
                " path, recipe, data, created) VALUES (?,?,?,?,?,?,?,?)",
                (chat_id, chat["character_id"], None, "image", fname, "chatlog",
                 json.dumps({"prompt": "chat log export",
                             "part": body.get("part", 1),
                             "parts": body.get("parts", 1)}), time.time()))
        self._json({"ok": True, "url": "/api/avatars/" + fname})

    def _asset_upload(self) -> None:
        """POST /api/assets/upload — store a reference image or voice sample.

        Body: {filename, b64, kind: 'persona_ref'|'character_ref'|'voice',
               owner_id, ref_kind?}
        Writes the file and records it on the persona or character so the
        studio can reach for it later.
        """
        body = self._body()
        try:
            fname = _store_upload(base64.b64decode(body.get("b64", "")),
                                  body.get("filename", "ref.png"))
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return
        kind = body.get("kind", "persona_ref")

        owner_id = int(body.get("owner_id") or 0)
        table = "personas" if kind == "persona_ref" else "characters"
        row = rows_get(table, owner_id)
        if not row:
            self._json({"error": f"no {table[:-1]} #{owner_id}"}, 404)
            return
        data = row.get("data") or {}
        if kind == "avatar":
            # Her face on the roster tile and in every bubble. Distinct from
            # character_ref, which is a REFERENCE for generation and does not
            # change what she looks like on screen — uploading one and getting
            # the other is exactly the confusion this branch exists to avoid.
            rows_upsert(table, {"name": row["name"], "data": data,
                                "avatar": fname}, owner_id)
            self._json({"ok": True, "file": fname,
                        "url": f"/api/avatars/{fname}"})
            return
        if kind == "persona_ref":
            refs = [r for r in (data.get("refs") or [])
                    if r.get("kind") != body.get("ref_kind", "body")]
            refs.append({"kind": body.get("ref_kind", "body"), "file": fname})
            data["refs"] = refs
        elif kind == "voice":
            voice = dict(data.get("voice") or {})
            voice["sample"] = fname
            if body.get("ref_text"):
                voice["ref_text"] = body["ref_text"]
            data["voice"] = voice
        else:
            visual = dict(data.get("visual") or {})
            visual["ref"] = fname
            data["visual"] = visual
        rows_upsert(table, {"name": row["name"], "data": data,
                            "avatar": row.get("avatar", "")}, owner_id)
        self._json({"ok": True, "file": fname,
                    "url": f"/api/avatars/{fname}"})

    def _vram_status(self) -> None:
        cfg = load_config()
        self._json(vram.status(cfg, cfg.get("comfyui_url", "")))

    def _vram_restore(self) -> None:
        """POST /api/vram/restore — put back anything we unloaded and lost.

        A job that died between make_room and give_back leaves the backend
        with no model loaded, and the next chat message fails in a way that
        looks like CoomKit broke LM Studio. This is the manual undo.
        """
        self._json(vram.restore_all(load_config()))

    # -- tool approval flow --------------------------------------------------
    def _tool_via_studio(self, pending: dict, call: dict, kind: str,
                         send) -> None:
        """Run an approved ```tool``` call on the shipped workflows.

        The bring-your-own table is the escape hatch, not the floor. A graph
        stored there is driven by `comfy.run_workflow`'s `{{slot}}` markers;
        the bundled graphs have no markers at all — their slots are a
        node/field map in wfpack — so they cannot simply be copied into the
        table. Hence a real second entry point into `studio.run` rather than
        a seeding shortcut.

        The prompt the model wrote is used verbatim: it already went through
        the dialect rewrite in tools.py and through the user's approval, so
        there is nothing left for a writer pass to add.
        """
        cfg = load_config()
        char = (rows_get("characters", int(call["character_id"]))
                if call.get("character_id") else None)
        chat_id = int(call.get("chat_id") or 0)
        persona = None
        if chat_id and not char:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT character_id, persona_id FROM chats WHERE id=?",
                    (chat_id,)).fetchone()
            if row:
                if row["character_id"]:
                    char = rows_get("characters", int(row["character_id"]))
                if row["persona_id"]:
                    persona = rows_get("personas", int(row["persona_id"]))

        wf_name = studio.pick_workflow(kind, cfg, char)
        spec = wfpack.BUNDLED[wf_name]
        visual = ((char or {}).get("data") or {}).get("visual") or {}
        values = {"prompt": pending["prompt"]}
        for k in ("width", "height", "seed", "negative"):
            if k in call:
                values[k] = call[k]
        if call.get("action") == "generate_tts":
            values["audio_text"] = call.get("text", pending["prompt"])
        if call.get("action") == "generate_music":
            values["music_prompt"] = pending["prompt"]
        if visual.get("seed"):
            values.setdefault("seed", int(visual["seed"]))

        job = {"recipe": "tool", "opts": {}, "kind": kind,
               "workflow": wf_name, "skill": spec.get("skill", ""),
               "values": values, "refs": [], "voice_sample": None,
               "voice_preset": None,
               "loras": studio._merge_loras(spec, visual),
               "artists": [], "tag_dialect": bool(spec.get("tag_dialect")),
               "stages": dict((cfg.get("studio") or {}).get("stages") or {}),
               "vram_gb": spec.get("vram_gb", 8),
               "character_id": (char or {}).get("id"),
               "chat_id": call.get("chat_id"),
               "message_id": call.get("message_id")}
        def read_asset(name: str):
            path = ASSETS / name
            return path.read_bytes() if path.exists() else None

        try:
            out = studio.run(job, values, cfg, asset_path=read_asset,
                             note=lambda m: send({"note": m}),
                             progress=lambda pr: send({"progress": pr}))
            saved = self._save_assets(out["files"], job, values,
                                      out.get("meta"))
            send({"ok": True, "assets": saved, "prompt": pending["prompt"],
                  "workflow": spec.get("label", wf_name)})
        except (studio.StudioError, comfy.ComfyError) as exc:
            send({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            send({"error": f"{type(exc).__name__}: {exc}"})

    def _tool_approve(self) -> None:
        """POST /api/tools/approve {id, prompt?} — run a pending tool call.

        The user may have edited the prompt; the edited version wins.
        Streams the same SSE contract as the studio routes — {note} and
        {progress} frames, one result frame, [DONE] — because her own tool
        calls render on the same GPU at the same speeds, and a video she
        asked for deserves the same live clock as one the user drafted.
        """
        body = self._body()
        pid = int(body.get("id", 0))
        p = tools.pending_pop(pid, body.get("prompt"))
        if not p:
            self._json({"error": "no such pending call"}, 404)
            return
        call = p["call"]
        kind = tools.ACTION_TO_KIND.get(call.get("action", ""), "image")

        # find a workflow: by name match first, else first of matching kind
        rows = [_row_to_dict(r) for r in get_db().execute(
            "SELECT * FROM workflows WHERE kind=? ORDER BY updated DESC",
            (kind,)).fetchall()]
        wf = None
        wanted = str(call.get("workflow", "")).lower()
        for r in rows:
            if wanted and wanted in r["name"].lower():
                wf = r
                break
        if wf is None and rows:
            wf = rows[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        # NEVER send "Connection: keep-alive" on an SSE response:
        # http.server's send_header flips close_connection to False on that
        # exact value, so after [DONE] the server holds the socket open for
        # a next request — and a body with no Content-Length and no chunked
        # framing means the client cannot know it ended. A browser fetch
        # never resolves and the UI freezes on the last status line, which
        # presented as "stuck in the rendering stage even though the render
        # is done". Pinned explicitly so a future protocol_version bump
        # cannot quietly resurrect it.
        self.close_connection = True

        def send(obj) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError):
                return False

        def done():
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        if wf is None:
            # Nothing stored is the NORMAL case, not an error: the shipped
            # graphs live in wfpack, not in this table, and nothing ever
            # seeded it. This branch used to 400 with "upload one in the
            # workflows tab first", so on a fresh install every ```tool```
            # block she wrote failed — with fourteen working graphs sitting
            # in the repo. Hand it to the studio path instead, which is the
            # one generation path and brings VRAM brokering, stage splicing
            # and LoRA injection with it.
            self._tool_via_studio(p, call, kind, send)
            done()
            return

        values = {"prompt": p["prompt"]}
        for k in ("width", "height", "seed", "negative"):
            if k in call:
                values[k] = call[k]
        # tts/music specific slots
        if call.get("action") == "generate_tts":
            values["audio_text"] = call.get("text", p["prompt"])
        if call.get("action") == "generate_music":
            values["music_prompt"] = p["prompt"]

        url = load_config().get("comfyui_url", "")
        try:
            files = comfy.run_workflow(url, wf["data"]["workflow"], values,
                                       timeout_s=int(body.get("timeout", 900)),
                                       progress=lambda pr: send(
                                           {"progress": pr}))
        except Exception as exc:  # noqa: BLE001
            send({"error": str(exc)})
            done()
            return

        ASSETS.mkdir(parents=True, exist_ok=True)
        saved = []
        now = time.time()
        owner = self._char_of_chat(call.get("chat_id"))
        with get_db() as conn:
            for f in files:
                ext = Path(f["filename"]).suffix or ".bin"
                fname = f"gen_{int(now*1000)}_{len(saved)}{ext}"
                (ASSETS / fname).write_bytes(f["data"])
                cur = conn.execute(
                    "INSERT INTO assets (chat_id, character_id, message_id,"
                    " kind, path, data, created) VALUES (?,?,?,?,?,?,?)",
                    (call.get("chat_id"), owner, call.get("message_id"),
                     f["kind"], fname,
                     json.dumps({"prompt": p["prompt"]}), now),
                )
                saved.append({"id": cur.lastrowid, "kind": f["kind"],
                              "url": f"/api/avatars/{fname}"})
        send({"ok": True, "assets": saved, "prompt": p["prompt"],
              "workflow": wf["name"]})
        done()

    def _tool_reject(self) -> None:
        body = self._body()
        p = tools.pending_pop(int(body.get("id", 0)))
        self._json({"ok": bool(p)})

    # -- character cards -----------------------------------------------------
    def _card_import(self) -> None:
        """POST /api/cards/import — multipart-free import.

        Body: {"filename": "card.png", "b64": "<base64 file bytes>"}
        Parses v1/v2/v3 card, persists to characters table, saves avatar PNG.
        """
        body = self._body()
        filename = body.get("filename", "card.png")
        b64 = body.get("b64", "")
        if not b64:
            self._json({"error": "b64 file data required"}, 400)
            return
        try:
            raw = base64.b64decode(b64)
            parsed = cards.parse_card(raw, filename)
        except (ValueError, json.JSONDecodeError, binascii.Error) as exc:
            self._json({"error": f"could not parse card: {exc}"}, 400)
            return

        avatar_rel = ""
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            ASSETS.mkdir(parents=True, exist_ok=True)
            avatar_rel = f"char_{int(time.time()*1000)}.png"
            (ASSETS / avatar_rel).write_bytes(raw)

        # …and lift them back out on the way in, so a CoomKit card arrives
        # with its looks and voice already set instead of as plain text.
        ext = ((parsed.get("fields") or {}).get("extensions") or {})
        mine = ext.get("coomkit") if isinstance(ext, dict) else None
        if isinstance(mine, dict):
            for key in ("visual", "voice"):
                if isinstance(mine.get(key), dict) and mine[key]:
                    parsed.setdefault(key, mine[key])

        now = time.time()
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO characters (name, data, avatar, created, updated)"
                " VALUES (?,?,?,?,?)",
                (parsed["name"], json.dumps(parsed), avatar_rel, now, now),
            )
        self._json({"ok": True, "id": cur.lastrowid, "name": parsed["name"],
                    "spec": parsed["spec"], "avatar": avatar_rel,
                    "fields": sorted(parsed["fields"].keys())})

    def _card_export(self, char_id: int) -> None:
        """POST /api/characters/{id}/export — {"format": "png"|"json"}."""
        row = rows_get("characters", char_id)
        if not row:
            self._json({"error": "not found"}, 404)
            return
        parsed = row["data"]
        # Characters made by hand in the UI have `fields` but no `raw` — only
        # imported cards carry the original object. Build a v3 card from the
        # fields so anything in the roster is exportable, not just imports.
        raw = parsed.get("raw")
        if not isinstance(raw, dict) or not raw.get("data"):
            fields = dict(parsed.get("fields") or {})
            fields.setdefault("name", row["name"])
            raw = {"spec": "chara_card_v3", "spec_version": "3.0",
                   "data": fields}
        # Her looks and her voice are the multimodal half of the card and
        # they live beside `raw`, so a plain export dropped them — a card
        # exported and reimported came back as text again. v3 `extensions`
        # is exactly the sanctioned place for this, and every other harness
        # ignores keys it does not know, so the card still works in ST.
        extra = {k: parsed[k] for k in ("visual", "voice")
                 if isinstance(parsed.get(k), dict) and parsed[k]}
        if extra:
            raw = json.loads(json.dumps(raw))       # never mutate the stored row
            ext = raw["data"].setdefault("extensions", {})
            ext["coomkit"] = {**(ext.get("coomkit") or {}), **extra}
        fmt = (self._body().get("format") or "png")
        if fmt == "json":
            out = cards.export_card_json(raw).encode("utf-8")
            mime, ext = "application/json", "json"
        else:
            avatar_path = ASSETS / row["avatar"] if row.get("avatar") else None
            if not avatar_path or not avatar_path.is_file():
                self._json({"error": "no avatar PNG on file to embed into"}, 400)
                return
            out = cards.export_card_png(avatar_path.read_bytes(), raw)
            mime, ext = "image/png", "png"
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", row["name"]) or "card"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition",
                         f'attachment; filename="{safe}.{ext}"')
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _recover_parked_models(cfg: dict) -> None:
    """Hand back any model a previous run unloaded and never restored.

    data/vram-parked.json exists precisely because a crash between make_room
    and give_back loses the debt forever — after which LM Studio JIT-reloads
    at its DEFAULT context instead of the one the user chose, silently
    truncating every later chat. The record survived; nothing ever acted on
    it, and the only cue was a note inside .col-right, which the
    max-width:1180px media query hides entirely.

    On a daemon thread, never inline: load_timeout_s is 300 and restart.sh
    waits on the port binding.
    """
    try:
        debts = vram.parked()
    except Exception:  # noqa: BLE001 — vram must never take the server down
        return
    if not debts:
        return

    def run():
        try:
            out = vram.give_back(cfg, cfg.get("comfyui_url", ""),
                                 {"acted": True, "restore": debts})
            for step in out.get("steps", []):
                print(f"→ vram: {step}")
        except Exception as exc:  # noqa: BLE001
            print(f"→ vram: could not restore parked models: {exc}")

    print(f"→ {len(debts)} model(s) left parked by a previous run — reloading")
    threading.Thread(target=run, daemon=True).start()


def _texting_daemon(port: int) -> None:
    """She texts on her own clock now, not the browser's.

    The browser scheduler only ever ran while a tab was open, which on
    Android means never — the browser is suspended the moment the screen
    blanks, while the Termux server underneath holds itself open. So the
    schedule moved server-side: this thread wakes every couple of minutes,
    finds texting-enabled sms threads whose gap has elapsed, and POSTs the
    ordinary /api/chats/text-first route at itself — one path, exactly the
    call the browser made, so the model still gets its reason and "nothing
    to say" stays a supported outcome.

    Gated on config `texting.server` with a STORED backend and model,
    because a daemon has no browser session to borrow a pick from — that is
    the decision CLAUDE.md said to make deliberately, made: enabling the
    feature captures the picks then in hand, and /api/config carries no key
    material (the route attaches keys server-side as always). Bookkeeping
    (last_attempt, daily count) lives on the chat's own texting block, the
    same place the gap and cap already were. A thread with ZERO messages is
    never texted: who opens is the user's call, same guard the browser
    version had. The browser scheduler stands down entirely while this is
    on, so the two clocks cannot double-text.
    """
    while True:
        time.sleep(120)
        try:
            cfg = load_config()
            tx = cfg.get("texting") or {}
            if not (tx.get("server") and tx.get("backend") and tx.get("model")):
                continue
            with get_db() as conn:
                rows = [(r["id"], r["data"]) for r in conn.execute(
                    "SELECT id, data FROM chats WHERE mode='sms'").fetchall()]
            now = time.time()
            today = time.strftime("%Y-%m-%d")
            for chat_id, raw in rows:
                try:
                    data = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    continue
                t = dict(data.get("texting") or {})
                if not t.get("enabled"):
                    continue
                if t.get("sent_day") == today \
                        and (t.get("sent_today") or 0) >= (t.get("daily_cap") or 6):
                    continue
                with get_db() as conn:
                    last = conn.execute(
                        "SELECT MAX(created), COUNT(*) FROM messages"
                        " WHERE chat_id=?", (chat_id,)).fetchone()
                if not last[1]:
                    continue
                # HER clock first: the text-first call ends with a NEXT line
                # — the character's own guess at when she would reach for the
                # phone again — and the route stores it as next_at. That is
                # what makes the pacing feel like a person instead of a cron
                # job: the needy double-text within the hour, the proud sulk
                # for a day, and nobody texts a silence at the same tempo
                # twice. The daily cap stays the spend guard either way.
                due = t.get("next_at")
                if due:
                    if now < due:
                        continue
                else:
                    # No stated pace yet (first run, or a model that ignored
                    # the form): the configured gap, JITTERED — a fixed
                    # interval reads as a machine the third time it fires.
                    since = now - max(last[0] or 0,
                                      t.get("last_attempt") or 0)
                    gap = (t.get("gap_minutes") or 45) * 60
                    if since < gap * random.uniform(0.8, 2.2):
                        continue
                body = json.dumps({
                    "chat_id": chat_id, "backend": tx["backend"],
                    "model": tx["model"],
                    "preset_id": tx.get("preset_id")}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/chats/text-first",
                    data=body, headers={"Content-Type": "application/json"},
                    method="POST")
                sent = False
                try:
                    with urllib.request.urlopen(req, timeout=300) as resp:
                        sent = bool(json.loads(resp.read().decode())
                                    .get("sent"))
                except Exception:  # noqa: BLE001 — backend down; next tick
                    continue
                # The clock moves even when she had nothing to say — that is
                # a real answer, and without this she is asked again in two
                # minutes forever.
                with get_db() as conn:
                    row = conn.execute("SELECT data FROM chats WHERE id=?",
                                       (chat_id,)).fetchone()
                    if not row:
                        continue
                    try:
                        data = json.loads(row["data"] or "{}")
                    except json.JSONDecodeError:
                        data = {}
                    t = dict(data.get("texting") or {})
                    t["last_attempt"] = now
                    # The route refreshes next_at from the model's NEXT line
                    # during the call. If it is still in the past the model
                    # skipped the form — drop it, or this loop fires again
                    # every two minutes until the daily cap eats itself.
                    if (t.get("next_at") or 0) <= now:
                        t.pop("next_at", None)
                    if sent:
                        if t.get("sent_day") != today:
                            t["sent_day"], t["sent_today"] = today, 0
                        t["sent_today"] = (t.get("sent_today") or 0) + 1
                    data["texting"] = t
                    conn.execute("UPDATE chats SET data=? WHERE id=?",
                                 (json.dumps(data), chat_id))
        except Exception:  # noqa: BLE001 — the daemon must never die
            pass


def main() -> int:
    init_db()
    seeded = seed_first_run()
    if seeded:
        print("→ first run: " + ", ".join(f"{v} {k}" for k, v in seeded.items()))
    moved = migrate_presets()
    owned = migrate_asset_owners()
    if owned:
        print(f"  attributed {owned} orphaned render(s) to a character")
    if moved:
        print(f"→ gave {moved} preset(s) a prompt-block order")
    cfg = load_config()
    port = int(cfg.get("port", 3939))
    # 127.0.0.1 unless the user says otherwise. "host": "0.0.0.0" is how a
    # phone on the same LAN reaches a desktop CoomKit (and how the datapack
    # pull reaches a peer) — but exposing an unauthenticated NSFW harness to
    # a network is a decision, not a default, so it stays opt-in config.
    host = str(cfg.get("host") or "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    (ROOT / ".coomkit.pid").write_text(str(__import__("os").getpid()))
    _recover_parked_models(cfg)
    threading.Thread(target=_texting_daemon, args=(port,),
                     daemon=True).start()
    print(f"CoomKit {VERSION} — f-fine, I'll manage your smut...")
    print(f"→ http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"
          + (" (listening on every interface)" if host == "0.0.0.0" else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
