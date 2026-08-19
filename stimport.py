#!/usr/bin/env python3
"""Import a SillyTavern chat-completion preset as CoomKit blocks.

"Stop sharing 300 KB of JSON" only works if people can bring what they
already have. This reads ST's prompt-manager format and maps it onto
blocks.py, dropping the parts that only exist to work around ST's own UI.

What gets translated:
  prompts[]                  -> blocks, keeping content, role, order
  prompt_order[].order       -> which blocks exist and in what sequence
  marker: true               -> our marker slots, by identifier
  injection_position: 1      -> place="depth" with the given depth
  "(Choose One)" headers     -> real exclusive groups on what follows

What gets dropped, and why:
  empty separator blocks     -> `━━━( TONE )━━━` with no content is grouping
                                faked in a flat list. We have groups. In one
                                sampled preset these were 21 of 64 active
                                blocks.
  disabled blocks            -> optional: a 302-block preset is a library
                                someone posted as a preset.
  forbid_overrides, triggers -> recorded, not honoured yet.
  extensions.regex_scripts   -> counted and handed back, NOT installed here.
                                They were dropped silently for a while, which
                                is the worst option: a preset whose whole
                                visual language is regex imports "cleanly" and
                                then does nothing. Importing them behind the
                                user's back is not right either — they are
                                find/replace rules over everything she says.
                                So: reported, and imported on a second,
                                explicit step. See regexrules.py.

Nothing here raises on a malformed preset. These files are hand-edited by
thousands of people; a missing key should cost one block, not the import.
"""
import json
import re

import blocks
import regexrules

# ST's marker identifiers -> ours. Anything unmapped is dropped with a note
# rather than becoming a mystery empty block.
MARKER_MAP = {
    "charDescription": "card",
    "charPersonality": "card",
    "scenario": "card",
    "personaDescription": "persona",
    "worldInfoBefore": "lore",
    "worldInfoAfter": "lore",
    "dialogueExamples": "examples",
    "chatHistory": "history",
}

# Built-in ST blocks that carry no content of their own.
SKIP_IDENTIFIERS = {"main", "nsfw", "jailbreak", "enhanceDefinitions"}

_CHOOSE_RE = re.compile(r"choose\s+one", re.I)
_ORNAMENT_RE = re.compile(r"[\w']+")

GROUP_HINTS = [
    (re.compile(r"nsfw|erotic|explicit|lewd|smut", re.I), "content"),
    (re.compile(r"violen|dark|death|gore", re.I), "content"),
    (re.compile(r"jailbreak|handshake|uncensor", re.I), "opening"),
    (re.compile(r"anti|slop|repetit|echo|ism\b", re.I), "quality"),
    (re.compile(r"pov|tense|person\b", re.I), "style"),
    (re.compile(r"length|word count|response length", re.I), "style"),
    (re.compile(r"style|prose|dialogue|narrat|format|tone", re.I), "style"),
    (re.compile(r"cot|think|reason|plan", re.I), "steering"),
    (re.compile(r"history|chat|story so far", re.I), "conversation"),
]


def _label(name: str) -> str:
    """Strip the box-drawing and emoji ornament ST headers are wrapped in."""
    words = _ORNAMENT_RE.findall(name or "")
    return " ".join(words).strip()


def _is_separator(p: dict) -> bool:
    return not (p.get("content") or "").strip() and not p.get("marker")


def _group_for(name: str, current: str) -> str:
    for rx, group in GROUP_HINTS:
        if rx.search(name or ""):
            return group
    return current or "style"


def looks_like_st(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("prompts"), list)


def convert(data: dict, keep_disabled: bool = False) -> dict:
    """ST preset -> {blocks, samplers, dropped, notes}."""
    if not looks_like_st(data):
        raise ValueError("not a SillyTavern chat-completion preset")

    by_id = {}
    for p in data.get("prompts") or []:
        if isinstance(p, dict) and p.get("identifier"):
            by_id[p["identifier"]] = p

    # The longest prompt_order is the real one; ST keeps a dummy per
    # character_id and the global default is usually the shorter.
    orders = data.get("prompt_order") or []
    order = max(orders, key=lambda o: len(o.get("order") or []), default=None)
    sequence = (order or {}).get("order") or [
        {"identifier": i, "enabled": True} for i in by_id]

    out, notes = [], []
    dropped = {"separators": 0, "disabled": 0, "unmapped_markers": 0,
               "empty": 0}
    group, exclusive_next = "opening", ""

    for entry in sequence:
        p = by_id.get(entry.get("identifier"))
        if not p:
            continue
        enabled = bool(entry.get("enabled", True))
        name = _label(p.get("name") or p["identifier"])

        # A separator is a group heading. Use it as one, then drop it.
        if _is_separator(p) and not p.get("marker"):
            dropped["separators"] += 1
            if name:
                group = _group_for(name, group)
                exclusive_next = name.lower() if _CHOOSE_RE.search(
                    p.get("name") or "") else ""
            continue

        if not enabled and not keep_disabled:
            dropped["disabled"] += 1
            continue

        if p.get("marker"):
            slot = MARKER_MAP.get(p["identifier"])
            if not slot:
                dropped["unmapped_markers"] += 1
                notes.append(f"no slot for marker {p['identifier']!r}")
                continue
            out.append(blocks.block(
                f"st.{p['identifier']}", name or slot, "conversation"
                if slot == "history" else "character",
                kind="marker", marker=slot, enabled=enabled,
                why="Imported placeholder."))
            continue

        content = (p.get("content") or "").strip()
        if not content:
            dropped["empty"] += 1
            continue
        if p["identifier"] in SKIP_IDENTIFIERS and not content:
            continue

        role = p.get("role") if p.get("role") in blocks.ROLES else "system"
        depth_place = p.get("injection_position") == 1
        out.append(blocks.block(
            f"st.{p['identifier']}", name or p["identifier"],
            _group_for(name, group),
            why="Imported from a SillyTavern preset.",
            content=content, role=role, enabled=enabled,
            place="depth" if depth_place else "order",
            depth=int(p.get("injection_depth") or 0),
            exclusive=exclusive_next))

    samplers = {}
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("top_k", "top_k"), ("min_p", "min_p"),
                     ("repetition_penalty", "repeat_penalty"),
                     ("frequency_penalty", "frequency_penalty"),
                     ("presence_penalty", "presence_penalty"),
                     ("openai_max_tokens", "max_tokens")):
        if isinstance(data.get(src), (int, float)):
            samplers[dst] = data[src]

    if data.get("assistant_prefill"):
        samplers["prefill"] = data["assistant_prefill"]

    # ST records the context the preset was written for, but authors routinely
    # tick `max_context_unlocked` and drag the slider to a million. Trusting
    # that would be worse than ignoring it: the history budget is computed
    # against this number, so a 1,000,000 context means history is never
    # trimmed at all and the first long chat overflows whatever model is
    # actually connected. Take it only when it looks like a real window.
    context = 0
    raw_ctx = data.get("openai_max_context")
    if isinstance(raw_ctx, int) and 1024 <= raw_ctx <= 200000:
        context = raw_ctx
    context_note = ""
    if isinstance(raw_ctx, int) and raw_ctx > 200000:
        context_note = (f"the preset claims a {raw_ctx:,}-token context "
                        f"(ST's unlocked slider) — ignored, set it to your "
                        f"model's real window instead")

    if context_note:
        notes.append(context_note)
    scripts = regexrules.scripts_in(data)
    if scripts:
        notes.append(f"{len(scripts)} regex script(s) in this preset — import "
                     f"them separately from the regex tab if you want the "
                     f"formatting they do")
    return {"blocks": out, "samplers": samplers, "dropped": dropped,
            "notes": notes, "context": context, "regex_scripts": scripts}


def summarise(result: dict, rough_tokens) -> dict:
    """What to show the user before they accept an import."""
    bs = result["blocks"]
    total = sum(rough_tokens(b["content"]) for b in bs if b["kind"] == "text")
    return {
        "regex_scripts": len(result.get("regex_scripts") or []),
        "blocks": len(bs),
        "text_blocks": sum(1 for b in bs if b["kind"] == "text"),
        "markers": sum(1 for b in bs if b["kind"] == "marker"),
        "tokens": total,
        "dropped": result["dropped"],
        "notes": result["notes"][:8],
        "biggest": sorted(
            [{"name": b["name"], "tokens": rough_tokens(b["content"])}
             for b in bs if b["kind"] == "text"],
            key=lambda x: -x["tokens"])[:5],
    }


def load(raw: bytes, keep_disabled: bool = False) -> dict:
    return convert(json.loads(raw.decode("utf-8", "replace")), keep_disabled)
