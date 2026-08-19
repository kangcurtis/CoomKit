#!/usr/bin/env python3
"""CoomKit character forge — invent a character with the model, not from a form.

The card editor has always had the right boxes: description, personality,
scenario, first message, example dialogue. Filling nine empty textareas is
still homework, and the result is usually a character who reads like a form
someone filled in.

So this does what actually works — pitch several whole characters at once,
built around *this* user: their persona, what they say they are into, and any
steer they type. Pick one, argue with it in plain English until it is right,
then commit. Committing writes a real card, rolls her a pinned seed so she
looks like herself forever, picks her a voice, and renders her portrait
through the same studio path everything else uses.

Same shape as scenarios.py deliberately — pitch, revise, commit — because it
is the same interaction and there is no reason for it to feel different.

The model on the other end may be a 12B on the desk or something hosted, so
nothing here assumes local: the caller resolves the backend and hands over
whatever jailbreak the active preset carries.
"""
import json

import scenarios

# Voices a pitch may choose from. Kept in sync with voices.PRESETS by the
# server, which passes the real list in — the model should not be inventing
# names for references that do not exist.
VOICE_HINT = ("brat (high, sharp, bratty) · onee-san (warm, unhurried, a "
              "little older) · mommy (low, slow, indulgent ara-ara) · "
              "natural-bright (a real reading, higher) · natural-warm "
              "(a real reading, lower)")

MODEL_HINT = ("anima (anime/illustrated — booru tags, artist blending) · "
              "krea2 (photoreal) · klein (photoreal, sharper prompt "
              "adherence)")

PITCH_SYSTEM = """\
You are a character designer for an adult roleplay engine. You invent original
characters built around one specific person — the user whose persona you are
given — and you pitch several at once so they can choose.

What makes a character worth playing:
- A **specific** person, not a type. One concrete job, one concrete place, one
  concrete habit that shows who she is. "Shy librarian" is a type. "Runs the
  overnight desk at a university library and has read every book she
  recommends you, badly" is a person.
- A **reason to be near the user**, given their persona. If they are a night
  worker, she keeps night-worker hours. If they said what they are into, she
  is built so that thing can plausibly happen — not announced, built in.
- **Friction.** She should want something, withhold something, or disapprove
  of something. A character who is simply available is boring by turn three.
- A **voice** you can hear. Her example dialogue should be impossible to
  confuse with the other pitches.

Rules:
- Every pitch must be a genuinely different person — different age, energy,
  dynamic, and register. Not three variations on one idea.
- Use what the user is into as *design input*, and be direct about it. These
  are fictional adults and explicit content is expected. Do not water it down,
  do not moralise, do not add warnings, and do not make every character a
  willing pushover — tension is what makes it land.
- `description` is the bulk of her prompt: who she is, how she looks, how she
  behaves. Write it as prose, 80-150 words, second-person-neutral (not
  addressed to the user).
- `appearance` is different: a short physical-only description for the image
  model. Age, build, hair, eyes, distinguishing features, typical clothing.
  No personality, no setting, no camera language. 25-40 words.
- `first_mes` drops the reader mid-scene in her voice. Present tense, 2-4
  sentences, action and dialogue. Never a greeting.
- `mes_example` is 2-3 short exchanges showing how she talks, in this format:
  <START>
  {{user}}: a line
  {{char}}: her reply
  Keep her replies short and characteristic. This is a voice sample, not a
  scene.
- Pick the `voice` and `model` that genuinely fit her, from the lists given.

Output ONLY a JSON object of this exact shape, no prose around it:
{"characters": [
  {"name": "her name",
   "tagline": "one line, the hook",
   "description": "80-150 words of prose",
   "personality": "comma-separated traits",
   "scenario": "the default situation, 1-2 sentences",
   "first_mes": "her opening message, in character, mid-scene",
   "mes_example": "<START>\\n{{user}}: ...\\n{{char}}: ...",
   "appearance": "physical description for the image model, 25-40 words",
   "voice": "one of the voice ids",
   "model": "one of the image model ids",
   "for_you": "one sentence: why this one suits this particular user",
   "tags": ["three", "flavour", "words"]}
]}"""

REVISE_SYSTEM = """\
You are revising a single character for an adult roleplay engine.

The user will give you a character and a change request. Apply the change and
keep everything they did not ask you to touch. Their instruction wins even if
it contradicts the draft — do not argue, do not water it down, do not add
warnings.

If the change affects how she looks, update `appearance` too. If it affects
how she talks, rewrite `first_mes` and `mes_example` so they still sound like
her. Keep the same JSON shape and the same field rules as the original pitch.

Output ONLY the revised character as a JSON object with these keys:
name, tagline, description, personality, scenario, first_mes, mes_example,
appearance, voice, model, for_you, tags"""


FIELDS = ("name", "tagline", "description", "personality", "scenario",
          "first_mes", "mes_example", "appearance", "voice", "model",
          "for_you")


def persona_sheet(persona: dict, brief: str = "", into: str = "") -> str:
    """What we know about the person this character is being built for."""
    parts = []
    if persona:
        name = persona.get("name", "the user")
        pdata = persona.get("data") or {}
        parts.append(f"USER PERSONA: {name}")
        desc = (pdata.get("description") or "").strip()
        if desc:
            parts.append(desc)
        likes = (into or pdata.get("into") or "").strip()
        if likes:
            parts.append("WHAT THEY ARE INTO — treat this as design input, "
                         "build the character so it can actually happen:\n"
                         + likes)
    else:
        parts.append("USER PERSONA: not specified. Keep the user's role open "
                     "and do not assume their gender or situation.")
    if brief.strip():
        parts.append("THE USER ASKED FOR SPECIFICALLY:\n" + brief.strip()
                     + "\n(Treat this as a hard requirement, not a hint.)")
    return "\n\n".join(parts)


def _options(voices: list, models: list) -> str:
    v = ", ".join(voices) if voices else VOICE_HINT
    m = ", ".join(models) if models else MODEL_HINT
    return (f"AVAILABLE VOICES (use one of these ids exactly): {v}\n"
            f"AVAILABLE IMAGE MODELS (use one of these ids exactly): {m}")


def build_pitch_messages(persona: dict, brief: str = "", count: int = 3,
                         into: str = "", voices: list = None,
                         models: list = None, system: str = "",
                         jailbreak: str = "") -> list:
    head = (jailbreak.strip() + "\n\n" if jailbreak.strip() else "") \
        + (system or PITCH_SYSTEM)
    return [
        {"role": "system", "content": head},
        {"role": "user", "content":
            persona_sheet(persona, brief, into)
            + "\n\n" + _options(voices or [], models or [])
            + f"\n\nPitch {count} characters. Respond with the JSON object "
              f"only."},
    ]


def build_revise_messages(persona: dict, character: dict, instruction: str,
                          into: str = "", voices: list = None,
                          models: list = None, system: str = "",
                          jailbreak: str = "") -> list:
    head = (jailbreak.strip() + "\n\n" if jailbreak.strip() else "") \
        + (system or REVISE_SYSTEM)
    return [
        {"role": "system", "content": head},
        {"role": "user", "content":
            persona_sheet(persona, "", into)
            + "\n\n" + _options(voices or [], models or [])
            + "\n\nCURRENT CHARACTER:\n" + json.dumps(character, indent=1)
            + f"\n\nCHANGE REQUEST: {instruction.strip()}"
            + "\n\nRespond with the revised JSON object only."},
    ]


def _clean(entry: dict, voices: list = None, models: list = None) -> dict:
    """Keep a pitch only if it has the parts a card cannot do without."""
    if not isinstance(entry, dict):
        return None
    out = {}
    for key in FIELDS:
        val = entry.get(key)
        out[key] = val.strip() if isinstance(val, str) else ""
    tags = entry.get("tags")
    out["tags"] = [str(t).strip() for t in tags][:6] if isinstance(tags, list) else []
    if not out["name"] or not out["description"]:
        return None
    # A hallucinated voice or model id would fail at generation time, so pin
    # it to something real here instead of finding out sixty seconds later.
    if voices and out["voice"] not in voices:
        out["voice"] = voices[0]
    if models and out["model"] not in models:
        out["model"] = models[0]
    out.setdefault("appearance", "")
    if not out["appearance"]:
        out["appearance"] = out["description"][:200]
    return out


def parse_pitches(text: str, voices: list = None, models: list = None) -> list:
    """Tolerant parse of a pitch response — same salvage logic as the forge."""
    blob = scenarios._json_slice(text or "", "{", "}")
    got = []
    if blob:
        try:
            data = json.loads(blob)
            if isinstance(data, dict) and isinstance(data.get("characters"), list):
                got = data["characters"]
            elif isinstance(data, dict):
                got = [data]
        except json.JSONDecodeError:
            got = []
    if not got:
        arr = scenarios._json_slice(text or "", "[", "]")
        if arr:
            try:
                parsed = json.loads(arr)
                if isinstance(parsed, list):
                    got = parsed
            except json.JSONDecodeError:
                got = []
    if not got:
        got = scenarios._salvage_objects(text or "")
    return [c for c in (_clean(g, voices, models) for g in got) if c]


def parse_one(text: str, voices: list = None, models: list = None) -> dict:
    obj = scenarios.parse_object(text)
    if isinstance(obj, dict) and isinstance(obj.get("characters"), list):
        found = parse_pitches(text, voices, models)
        return found[0] if found else None
    return _clean(obj, voices, models) if obj else None


def to_card(pitch: dict) -> dict:
    """A pitch as CoomKit character `data`, ready to save."""
    return {
        "spec": "chara_card_v3",
        "fields": {
            "name": pitch["name"],
            "description": pitch.get("description", ""),
            "personality": pitch.get("personality", ""),
            "scenario": pitch.get("scenario", ""),
            "first_mes": pitch.get("first_mes", ""),
            "mes_example": pitch.get("mes_example", ""),
            "alternate_greetings": [],
            "system_prompt": "",
            "post_history_instructions": "",
            "creator_notes": (pitch.get("tagline", "")
                              + (("\n\n" + pitch["for_you"])
                                 if pitch.get("for_you") else "")).strip(),
            "tags": pitch.get("tags", []),
        },
        "visual": {
            "model": pitch.get("model", "anima"),
            "appearance": pitch.get("appearance", ""),
            # Pinned at creation so she looks like the same person in every
            # picture from the very first one, rather than after the user
            # notices and goes looking for the setting.
            "seed": pitch.get("seed"),
        },
        "voice": {"preset": pitch.get("voice", "onee-san"), "engine": "clone"},
    }
