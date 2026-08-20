#!/usr/bin/env python3
"""CoomKit scenario forge — brainstorm a fresh scene, then launch into it.

The problem this solves: a character card ships one `first_mes` and maybe a
couple of alternate greetings. After a few sessions you have read them all and
the character feels exhausted, even though the character is fine — it is the
*setup* that went stale.

So instead of replaying a canned greeting, we ask the model to propose new
scenarios using everything we actually know:

  * the card (description, personality, scenario, example dialogue)
  * the user's persona
  * optionally what she remembers from previous chats with this user

The user can brainstorm — "darker", "make her my landlord", "morning after" —
and the model revises. When they commit, the chat is created with the agreed
scenario as a context override plus an opening message written in her voice,
so the scene starts already in motion instead of at a cold greeting.

Memory scope is deliberately explicit: continuity is great until you want a
clean slate, and the user should choose which one they are getting.
"""
import json
import re

SUGGEST_SYSTEM = """\
You are a scenario writer for an adult interactive fiction engine. You invent
fresh situations for an established character to be in — you are not playing
her yet, you are pitching scenes.

Given her character sheet, the user's persona, and anything she remembers from
previous sessions, propose distinct scenarios. Each one must be a *situation*,
not a summary of who she is: something is happening, there is a reason these
two are in the same place, and there is immediate tension or momentum.

Rules:
- Every scenario must be genuinely different from the others — different
  setting, different dynamic, different pretext. Do not pitch three variations
  of one idea.
- Respect the character sheet. Her personality does not change; only the
  circumstances do.
- If memories are provided, at least one scenario should build on that shared
  history (a callback, an escalation, a consequence). The others may be fresh.
- Write the opening message in her voice, as she would actually say it, in the
  same style as her example dialogue. Present tense. Drop the reader mid-scene.
  Never open with a greeting like "Hello, how are you?".
- Adult content is expected and fine; these are fictional adults. Set up the
  tension, do not resolve it in the opening line.
- Be economical: premise 2-3 sentences, hook one sentence, opening 2-4
  sentences. Do not pad. The whole JSON object must fit in your reply.

Output ONLY a JSON object of this exact shape, no prose around it:
{"scenarios": [
  {"title": "short punchy label",
   "premise": "2-3 sentences of setup: where, when, why they are together, what is about to happen",
   "setting": "one short phrase - the physical place and time",
   "hook": "the specific tension or question driving the scene",
   "opening": "her first message, in character, 2-4 sentences",
   "tags": ["comma", "separated", "flavour"]}
]}"""

REFINE_SYSTEM = """\
You are revising a single scenario for an adult interactive fiction engine.

The user will give you a scenario and a change request. Apply the change,
keeping everything they did not ask you to touch. Their instruction wins even
if it contradicts the previous draft — do not argue, do not water it down, do
not add warnings.

Keep the character's personality intact; the situation is what changes. Rewrite
the opening message so it matches the revised premise, in her voice, present
tense, mid-scene.

Output ONLY the revised scenario as a JSON object of this exact shape:
{"title": "...", "premise": "...", "setting": "...", "hook": "...",
 "opening": "...", "tags": ["..."]}"""


def _sheet(char: dict, persona: dict | None, memories: list[str]) -> str:
    """The shared context block both prompts are built on."""
    fields = char.get("data", {}).get("fields", {})
    parts = [f"CHARACTER: {fields.get('name', char.get('name', 'unnamed'))}"]
    for label, key in (("Description", "description"),
                       ("Personality", "personality"),
                       ("Default scenario", "scenario"),
                       ("Example dialogue", "mes_example")):
        val = (fields.get(key) or "").strip()
        if val:
            parts.append(f"{label}:\n{val}")
    if persona:
        pdesc = (persona.get("data", {}).get("description") or "").strip()
        parts.append(f"USER PERSONA: {persona.get('name', 'the user')}\n{pdesc}")
    else:
        parts.append("USER PERSONA: not specified — keep the user's role open.")
    if memories:
        parts.append("SHE REMEMBERS FROM PREVIOUS SESSIONS:\n"
                     + "\n".join(f"- {m}" for m in memories))
    else:
        parts.append("SHE REMEMBERS: nothing — this is a fresh start.")
    return "\n\n".join(parts)


def build_suggest_messages(char: dict, persona: dict | None,
                           memories: list[str], brief: str = "",
                           count: int = 3,
                           system: str = "") -> list[dict]:
    ask = [f"Propose {count} scenarios."]
    if brief.strip():
        ask.append(f"The user specifically wants: {brief.strip()}")
        ask.append("Treat that as a hard requirement, not a suggestion.")
    ask.append("Respond with the JSON object only.")
    return [
        {"role": "system", "content": system or SUGGEST_SYSTEM},
        {"role": "user", "content": _sheet(char, persona, memories)
            + "\n\n" + " ".join(ask)},
    ]


def build_refine_messages(char: dict, persona: dict | None,
                          memories: list[str], scenario: dict,
                          instruction: str,
                          system: str = "") -> list[dict]:
    return [
        {"role": "system", "content": system or REFINE_SYSTEM},
        {"role": "user", "content":
            _sheet(char, persona, memories)
            + "\n\nCURRENT SCENARIO:\n" + json.dumps(scenario, indent=1)
            + f"\n\nCHANGE REQUEST: {instruction.strip()}"
            + "\n\nRespond with the revised JSON object only."},
    ]


def _json_slice(text: str, opener: str, closer: str) -> str | None:
    """Extract the first balanced {...} or [...] span from noisy output."""
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


REQUIRED = ("title", "premise", "opening")


def _clean(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None
    out = {}
    for key in ("title", "premise", "setting", "hook", "opening"):
        val = entry.get(key)
        out[key] = val.strip() if isinstance(val, str) else ""
    tags = entry.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    out["tags"] = [str(t).strip() for t in (tags or []) if str(t).strip()][:6]
    if not all(out.get(k) for k in REQUIRED):
        return None
    return out


def _salvage_objects(text: str) -> list[dict]:
    """Pull every complete {...} object out of a possibly-truncated response.

    Thinking models sometimes run out of budget mid-array. Rather than
    discarding three good scenarios because the fourth was cut off, take the
    ones that closed cleanly.
    """
    out = []
    i = 0
    # Stepping in by one on a failed slice (see below) makes this quadratic in
    # the worst case, and the input is model output: `"{" * 12000` took 2.7s
    # measured, in the request thread, with no way to abort it — the same
    # shape of hazard as an imported regex with a nested quantifier. A real
    # reply needs a handful of attempts, so the cap is a backstop that cannot
    # fire in practice rather than a limit on how much is salvaged.
    for _ in range(200):
        start = text.find("{", i)
        if start == -1:
            return out
        blob = _json_slice(text[start:], "{", "}")
        if not blob:
            # Same trap as the JSONDecodeError below, arrived at from the
            # other side: a response truncated mid-array never closes its
            # OUTER brace, so the very first slice fails and returning here
            # threw away every complete object inside it — the exact case
            # this function exists for. Step in and keep looking.
            i = start + 1
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            # Step in by ONE, not past the whole span. Both forges ask for
            # {"characters": [...]} / {"scenarios": [...]}, and brace-matching
            # balances at the OUTER brace regardless of whether the contents
            # are valid — so one malformed entry made the outer object
            # unparseable and skipping it stepped over every good object in
            # the array. Salvage returned nothing for the exact shape it
            # exists to salvage, and had done since it was written; it only
            # ever worked on a bare array, which is not what either forge
            # asks for. Caught by a live character-forge reply with a raw
            # newline inside mes_example.
            i = start + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = start + len(blob)
    return out


def parse_scenarios(text: str) -> list[dict]:
    """Tolerant parse of a suggestion response. Never raises."""
    blob = _json_slice(text or "", "{", "}")
    items = []
    if blob:
        try:
            data = json.loads(blob)
            raw = data.get("scenarios") if isinstance(data, dict) else None
            if isinstance(raw, list):
                items = raw
            elif isinstance(data, dict) and data.get("title"):
                items = [data]  # model returned a single bare scenario
        except json.JSONDecodeError:
            items = []
    if not items:
        arr = _json_slice(text or "", "[", "]")
        if arr:
            try:
                parsed = json.loads(arr)
                if isinstance(parsed, list):
                    items = parsed
            except json.JSONDecodeError:
                items = []
    if not items:
        # last resort: the response was truncated. Keep whatever closed.
        items = [o for o in _salvage_objects(text or "")
                 if o.get("title") or o.get("premise")]
    cleaned = [c for c in (_clean(i) for i in items) if c]
    return cleaned[:6]


def parse_object(text: str) -> dict | None:
    """Tolerant parse of any single JSON object out of a noisy reply.

    The balanced-brace scan is the reusable half of this module — small local
    models wrap JSON in prose, in fences, or in both. `parse_one` below adds
    the forge's own field filtering on top; anything that is not a scenario
    (a voice delivery, a music caption) wants this instead, and got silently
    empty results from `parse_one` until it did.
    """
    blob = _json_slice(text or "", "{", "}")
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_one(text: str) -> dict | None:
    """Tolerant parse of a refine response."""
    blob = _json_slice(text or "", "{", "}")
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("scenarios"), list):
        got = [c for c in (_clean(i) for i in data["scenarios"]) if c]
        return got[0] if got else None
    return _clean(data)


