#!/usr/bin/env python3
"""SillyTavern character card ingest/export.

Supports chara card v1/v2/v3:
  - PNG with base64 JSON in tEXt chunk, keyword "chara" (v2) or "ccv3" (v3)
  - plain .json card files
v3 cards nest fields under "data" — we normalise to a flat dict and keep the
original spec/version so export roundtrips faithfully.
"""
import base64
import binascii
import json
import struct
import zlib
from pathlib import Path

CARD_KEYS = (
    "name", "description", "personality", "scenario", "first_mes",
    "mes_example", "system_prompt", "post_history_instructions",
    "creator_notes", "tags", "creator", "character_version",
    "alternate_greetings", "character_book", "extensions",
)


def _png_chunks(raw: bytes):
    pos = 8  # skip signature
    while pos + 8 <= len(raw):
        ln = struct.unpack(">I", raw[pos:pos + 4])[0]
        typ = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + ln]
        yield typ, data
        pos += 12 + ln


def _decode_text_chunk(data: bytes) -> tuple[str, str]:
    keyword, _, value = data.partition(b"\x00")
    return keyword.decode("latin1"), value.decode("latin1")


def parse_card(raw: bytes, filename: str = "") -> dict:
    """Parse a card from PNG bytes or JSON text. Returns normalised dict:
    {spec, name, fields{...}, avatar? absent here (caller adds)}"""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        card_json = None
        spec = "v2"
        for typ, data in _png_chunks(raw):
            if typ != b"tEXt":
                continue
            keyword, value = _decode_text_chunk(data)
            if keyword == "ccv3":
                card_json, spec = value, "v3"
                break  # ccv3 is authoritative
            if keyword == "chara":
                card_json = value
        if card_json is None:
            raise ValueError("PNG has no chara/ccv3 chunk — not a character card")
        card = json.loads(base64.b64decode(card_json))
    else:
        card = json.loads(raw.decode("utf-8"))
        spec = "v3" if card.get("spec", "").startswith("chara_card_v3") else "v2"

    if card.get("spec", "").startswith("chara_card_v3"):
        spec = "v3"
    inner = card.get("data") if isinstance(card.get("data"), dict) else card
    fields = {k: inner[k] for k in CARD_KEYS if k in inner and inner[k] not in (None, "")}
    name = fields.get("name") or "unnamed"
    return {"spec": spec, "name": name, "fields": fields, "raw": card}


def apply_edits(parsed: dict, edits: dict) -> dict:
    """Write edited fields back into BOTH the flat view and the original card.

    Export re-embeds `raw`, so an edit that only touched `fields` would look
    right in CoomKit and then vanish the moment the card went back to
    SillyTavern. v3 nests under "data"; v1/v2 are flat.
    """
    out = json.loads(json.dumps(parsed))          # deep copy, no aliasing
    fields = out.setdefault("fields", {})
    raw = out.setdefault("raw", {})
    inner = raw["data"] if isinstance(raw.get("data"), dict) else raw
    for key, value in edits.items():
        if key not in CARD_KEYS:
            continue
        if value in (None, ""):
            fields.pop(key, None)
            inner.pop(key, None)
            continue
        fields[key] = value
        inner[key] = value
    out["name"] = fields.get("name") or out.get("name") or "unnamed"
    return out


def _crc_chunk(typ: bytes, data: bytes) -> bytes:
    body = typ + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def export_card_png(base_png: bytes, card: dict) -> bytes:
    """Re-embed card JSON into a PNG (drops old chara/ccv3 chunks)."""
    payload = base64.b64encode(
        json.dumps(card, ensure_ascii=False).encode("utf-8")).decode()
    keyword = b"ccv3" if card.get("spec", "").startswith("chara_card_v3") else b"chara"
    new_chunk = _crc_chunk(b"tEXt", keyword + b"\x00" + payload.encode("latin1"))

    out = bytearray(raw_sig := b"\x89PNG\r\n\x1a\n")
    inserted = False
    for typ, data in _png_chunks(base_png):
        if typ == b"tEXt":
            kw = data.partition(b"\x00")[0]
            if kw in (b"chara", b"ccv3"):
                continue  # replaced
        if typ == b"IDAT" and not inserted:
            out += new_chunk
            inserted = True
        out += _crc_chunk(typ, data)
    if not inserted:
        raise ValueError("PNG had no IDAT?")
    return bytes(out)


def export_card_json(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False, indent=2)
