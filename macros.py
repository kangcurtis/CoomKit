#!/usr/bin/env python3
"""Character-card macro substitution — the {{char}} / {{user}} layer.

Every SillyTavern card is written against these. Without substitution a card
greeting reading "Oh, it's you {{user}}" reaches the model verbatim, the model
copies the style, and the character starts calling you {{user}} to your face.

Handled (case-insensitive, as in ST):

  identity    {{char}} {{bot}} <BOT>      the character's name
              {{user}} <USER>             the active persona's name
              {{persona}}                 the persona description
  card fields {{description}} {{personality}} {{scenario}}
              {{mes_example}} / {{example_dialogue}}
  override    {{original}}                see note below
  random      {{random:a,b,c}} {{random::a::b}}   picked fresh each call
              {{pick:a,b,c}}              stable for a given seed
              {{roll:d20}} {{roll:20}}
  time        {{time}} {{date}} {{weekday}} {{isotime}} {{isodate}}
  layout      {{newline}} {{trim}} {{noop}} {{// comment}} {{comment ...}}

{{original}}: in ST this re-inserts the frontend's own system prompt inside a
card's `system_prompt` / `post_history_instructions`. CoomKit already prepends
its own layers (jailbreak, prompts.py) around the card text in
`engine.build_system`, so re-inserting would duplicate them — it expands to
nothing instead.

Unknown `{{...}}` is left alone on purpose: it is more likely a ComfyUI slot
or something the user typed than a macro we should silently eat.
"""
import hashlib
import random
import re
import time

# {{name}} or {{name:args}} — args run to the closing brace
MACRO_RE = re.compile(r"\{\{\s*([a-zA-Z_/][\w/]*)\s*(?::\s*(.*?))?\s*\}\}",
                      re.DOTALL)
COMMENT_RE = re.compile(r"\{\{\s*(?://|comment\b)\s*.*?\}\}", re.DOTALL)
TRIM_RE = re.compile(r"\s*\{\{\s*trim\s*\}\}\s*", re.IGNORECASE)
MAX_PASSES = 4          # card fields can themselves contain macros


def _split_options(raw: str) -> list[str]:
    """`a,b,c` or `::a::b::c` (ST allows both; :: lets options contain commas)."""
    if "::" in raw:
        return [p.strip() for p in raw.split("::") if p.strip()]
    return [p.strip() for p in raw.split(",") if p.strip()]


def _roll(raw: str) -> str:
    m = re.fullmatch(r"\s*(\d*)\s*[dD]?\s*(\d+)\s*", raw or "")
    if not m:
        return ""
    count = int(m.group(1) or 1)
    sides = int(m.group(2) or 20)
    if sides < 1 or count < 1 or count > 100:
        return ""
    return str(sum(random.randint(1, sides) for _ in range(count)))


def expand(text: str, char: str = "", user: str = "", fields: dict | None = None,
           persona: str = "", seed: str = "") -> str:
    """Substitute card macros in `text`.

    `seed` makes {{pick}} stable — pass something per-chat so a pick does not
    change every time the prompt is rebuilt.
    """
    if not text or "{" not in text and "<" not in text:
        return text or ""
    fields = fields or {}
    char = char or "the character"
    user = user or "anon"

    values = {
        "char": char, "bot": char, "name": char,
        "user": user,
        "persona": persona or "",
        "description": str(fields.get("description") or ""),
        "personality": str(fields.get("personality") or ""),
        "scenario": str(fields.get("scenario") or ""),
        "mes_example": str(fields.get("mes_example") or ""),
        "example_dialogue": str(fields.get("mes_example") or ""),
        "original": "",
        "newline": "\n",
        "noop": "",
    }

    out = text
    for _ in range(MAX_PASSES):
        before = out
        out = COMMENT_RE.sub("", out)

        def repl(m: re.Match) -> str:
            name = m.group(1).lower()
            args = m.group(2)
            if name in values:
                return values[name]
            if name == "random" and args is not None:
                opts = _split_options(args)
                return random.choice(opts) if opts else ""
            if name == "pick" and args is not None:
                opts = _split_options(args)
                if not opts:
                    return ""
                digest = hashlib.sha256((seed + "|" + args).encode()).digest()
                return opts[digest[0] % len(opts)]
            if name == "roll" and args is not None:
                return _roll(args)
            if name in ("time", "isotime"):
                return time.strftime("%H:%M:%S" if name == "isotime" else "%-I:%M %p")
            if name in ("date", "isodate"):
                return time.strftime("%Y-%m-%d" if name == "isodate" else "%B %-d, %Y")
            if name == "weekday":
                return time.strftime("%A")
            return m.group(0)          # unknown: leave it alone

        out = MACRO_RE.sub(repl, out)
        # legacy aliases from v1 cards
        out = out.replace("<BOT>", char).replace("<USER>", user)
        if out == before:
            break

    return TRIM_RE.sub("", out)


def expand_fields(fields: dict, char: str = "", user: str = "",
                  persona: str = "", seed: str = "") -> dict:
    """Expand every string field of a card (and its greeting list)."""
    out = {}
    for key, value in (fields or {}).items():
        if isinstance(value, str):
            out[key] = expand(value, char, user, fields, persona, seed)
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            out[key] = [expand(v, char, user, fields, persona, seed) for v in value]
        else:
            out[key] = value
    return out
