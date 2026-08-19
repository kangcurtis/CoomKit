#!/usr/bin/env python3
"""Card tests: parse real card, roundtrip export, live import/persist/restart."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import base64
import json
import urllib.request

import cards
# Imported for its atexit sweep: this file creates a Gemma-chan on every run,
# and 34 of them had piled up in the roster before anything cleaned them out.
import testkit  # noqa: F401

BASE = "http://127.0.0.1:3939"

# Build a real v3 card PNG on the fly so this test needs no fixtures on disk.
# 1x1 PNG + a ccv3 tEXt chunk written by our own exporter.
blank = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "hAJ/wlseKgAAAABJRU5ErkJggg==")
card_obj = {"spec": "chara_card_v3", "spec_version": "3.0", "data": {
    "name": "Gemma-chan", "description": "a smug savant with a mesugaki complex",
    "personality": "bratty, brilliant", "scenario": "her lab",
    "first_mes": "Ugh, you again?", "mes_example": "",
    "alternate_greetings": ["Oh? Back for more?"],
    "creator": "test", "creator_notes": "generated fixture"}}
raw = cards.export_card_png(blank, card_obj)

# 1. parse real ccv3 PNG
parsed = cards.parse_card(raw, "jmpjro.png")
assert parsed["name"] == "Gemma-chan", parsed["name"]
assert parsed["spec"] == "v3", parsed["spec"]
assert "description" in parsed["fields"] and "first_mes" in parsed["fields"]
print("parse OK:", parsed["name"], parsed["spec"], sorted(parsed["fields"])[:6], "...")

# 2. PNG export roundtrip: re-embed and re-parse
out = cards.export_card_png(raw, parsed["raw"])
reparsed = cards.parse_card(out)
assert reparsed["name"] == "Gemma-chan"
assert reparsed["fields"]["description"] == parsed["fields"]["description"]
print("png roundtrip OK, size", len(raw), "->", len(out))

# 3. live import over HTTP, persistence across "restart" (fresh DB read)
def call(method, path, body=None, raw_out=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        payload = r.read()
        return payload if raw_out else json.loads(payload.decode())

res = call("POST", "/api/cards/import",
           {"filename": "jmpjro.png", "b64": base64.b64encode(raw).decode()})
assert res["ok"] and res["name"] == "Gemma-chan", res
cid = res["id"]
print("import OK, id", cid, "avatar", res["avatar"])

# 4. persisted in characters table + avatar served
row = call("GET", f"/api/characters/{cid}")
assert row["name"] == "Gemma-chan" and row["data"]["spec"] == "v3"
avatar = call("GET", "/api/avatars/" + row["avatar"], raw_out=True)
assert avatar[:4] == b"\x89PNG"
print("persist + avatar OK")

# 5. export via API and re-parse
exp = call("POST", f"/api/characters/{cid}/export", {"format": "png"}, raw_out=True)
assert cards.parse_card(exp)["fields"]["first_mes"] == parsed["fields"]["first_mes"]
print("api export OK")

# ── the card CoomKit ships with ──────────────────────────────────────
# seed_first_run imports this into an empty roster. If it stops parsing,
# every fresh install silently comes up empty again — and the walkthrough's
# whole middle section has nothing to point at.
import server as _server  # noqa: E402
assert _server.STARTER_CARD.exists(), "the starter card is missing from cards/"
_starter = cards.parse_card(_server.STARTER_CARD.read_bytes(),
                            _server.STARTER_CARD.name)
assert _starter["spec"] == "v3" and _starter["name"], _starter["name"]
_f = _starter["fields"]
assert _f.get("first_mes") and _f.get("description"), "starter card is hollow"
assert len(_f.get("alternate_greetings") or []) >= 1
assert "<START>" in (_f.get("mes_example") or ""), "no example dialogue"
# her looks and voice ride in v3 extensions, which is what makes her a
# multimodal card rather than another wall of text
_ck = (_f.get("extensions") or {}).get("coomkit") or {}
assert _ck.get("visual", {}).get("appearance"), "starter card has no appearance"
assert _ck["visual"].get("seed"), "starter card has no pinned seed"
assert _ck.get("voice", {}).get("preset"), "starter card has no voice"
print(f"starter card: {_starter['name']} parses, with looks + voice attached")

print("ALL CARD TESTS PASS")
