#!/usr/bin/env python3
"""Shared test helpers. No fixtures on disk required.

The sample card that early tests used (jmpjro.png) is gone from the repo, so
anything needing a character builds one through our own exporter instead.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
from _bootstrap import ROOT
import atexit
import base64
import json
import urllib.error
import urllib.request

import cards

BASE = "http://127.0.0.1:3939"

# 1x1 PNG, valid enough for the card exporter to embed a tEXt chunk into
BLANK_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "hAJ/wlseKgAAAABJRU5ErkJggg==")

FIXTURE_CARD = {
    "spec": "chara_card_v3", "spec_version": "3.0", "data": {
        "name": "Fixture-chan",
        "description": "a smug lab assistant with a mesugaki streak",
        "personality": "bratty, brilliant, allergic to sincerity",
        "scenario": "her cluttered lab, late at night",
        "first_mes": "Ugh, you again? Fine, sit down.",
        "mes_example": "",
        "alternate_greetings": ["Oh? Back for more?"],
        "creator": "coomkit tests", "creator_notes": "generated fixture",
    },
}


def call(method, path, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def card_png(card_obj=None) -> bytes:
    """A real v3 card PNG, built in memory."""
    return cards.export_card_png(BLANK_PNG, card_obj or FIXTURE_CARD)


# Every character a test creates is named one of these, and every one of them
# is swept at exit. A dev box here had accumulated 278 characters — 179
# Fixture-chan, 34 Gemma-chan, 32 each of Edited-chan and Macro-chan — against
# ONE real one, which makes the roster useless and the roster search pointless.
FIXTURE_NAMES = ("Fixture-chan", "Edited-chan", "Macro-chan", "Gemma-chan")


def ensure_character(reuse=True) -> int:
    """Return a character id, importing a generated fixture if needed.

    Reuses only a FIXTURE character, never one of the user's. It used to take
    any character with a `first_mes`, which on a real install means it would
    quietly adopt the shipped starter card and write test chats all over her.
    """
    if reuse:
        rows = call("GET", "/api/characters").get("rows") or []
        for row in rows:
            if row.get("name") not in FIXTURE_NAMES:
                continue
            data = row.get("data") or {}
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            if (inner.get("first_mes") or "").strip():
                return row["id"]
    raw = card_png()
    r = call("POST", "/api/cards/import", {
        "filename": "fixture.png",
        "b64": base64.b64encode(raw).decode()})
    return r["id"]


def sweep_fixtures():
    """Delete every fixture character and everything hanging off it.

    Swept BY NAME rather than by tracking ids, so a fixture left behind by a
    test file that never called ensure_character still goes. Files under
    data/assets/ are deliberately left alone: a render is expensive and a
    stray file is harmless, while a roster full of Fixture-chan is not.
    """
    import sqlite3
    db = ROOT / "data" / "coomkit.sqlite"
    if not db.exists():
        return 0
    try:
        conn = sqlite3.connect(str(db), timeout=5)
        q = ",".join("?" * len(FIXTURE_NAMES))
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM characters WHERE name IN ({q})", FIXTURE_NAMES)]
        if not ids:
            conn.close()
            return 0
        iq = ",".join("?" * len(ids))
        chats = [r[0] for r in conn.execute(
            f"SELECT id FROM chats WHERE character_id IN ({iq})", ids)]
        if chats:
            cq = ",".join("?" * len(chats))
            conn.execute(f"DELETE FROM messages WHERE chat_id IN ({cq})", chats)
        conn.execute(f"DELETE FROM assets WHERE character_id IN ({iq})", ids)
        conn.execute(f"DELETE FROM memories WHERE character_id IN ({iq})", ids)
        conn.execute(f"DELETE FROM chats WHERE character_id IN ({iq})", ids)
        conn.execute(f"DELETE FROM characters WHERE id IN ({iq})", ids)
        conn.commit()
        conn.close()
        return len(ids)
    except sqlite3.Error:
        return 0        # a locked or half-deleted db is not worth failing over


atexit.register(sweep_fixtures)


def local_model():
    """(backend, model) for the first local backend, or (None, None)."""
    for b in call("GET", "/api/backends").get("backends", []):
        if not b.get("remote") and b.get("models"):
            return b["url"], b["models"][0]
    return None, None


def stream_send(body):
    """POST /api/chats/send and collect (text, think, error)."""
    req = urllib.request.Request(
        BASE + "/api/chats/send", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    text, think, err = [], [], None
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                c = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if "error" in c:
                err = c["error"]
            elif "think" in c:
                think.append(c["think"])
            elif "text" in c:
                text.append(c["text"])
    return "".join(text), "".join(think), err
