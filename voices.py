#!/usr/bin/env python3
"""CoomKit's shipped voice references.

Voice cloning needs a 3-15 second sample of somebody actually talking, and
most people do not have one lying around for a character they just imported.
Without a default the first "say that out loud" either fails or falls back to
a described voice, which sounds nothing like a person.

So a couple of references ship with CoomKit. They are deliberately plain
readings — a clone inherits whatever performance is in the sample, so a
neutral one is a better base than an emotive one. Anything expressive comes
from the *text* and the emotion vector, not from the reference.

Every bundled clip is public domain or CC BY. See voices/CREDITS.md.

Three of them are archetypes rather than readings — brat, onee-san, ara-ara —
because that is what people actually want a companion to sound like, and no
audiobook narrator has ever delivered that register. They were synthesised
here with OmniVoice's own Voice Design path, which means no third-party audio
and no licence at all. Two natural readings are kept for people who want a
person instead of a type.

Every reference sits above 185 Hz on purpose. Cloning was measured across five
references here: everything at 186 Hz and up held its range, while a 167 Hz
alto reference collapsed an octave to 78 Hz and came out male. Chasing a
warmer default down the pitch scale is exactly how you ship a woman who sounds
like a man — see skills/voice.md.

A character can still carry her own sample; `data.voice.sample` always wins
over `data.voice.preset`. That is the point — these are a floor, not a
ceiling.
"""
from pathlib import Path

VOICE_DIR = Path(__file__).resolve().parent / "voices"

PRESETS = {
    "brat": {
        "label": "Brat", "file": "brat.flac",
        "blurb": "High, sharp, permanently unimpressed with you. Kawaii "
                 "register — the one that says 'ugh, FINE' and means it.",
        "f0_hz": 399, "speed": 0.85,
        "instruct": "female, teenager, high pitch",
        "credit": "Synthesised with OmniVoice Voice Design — no third-party "
                  "audio, no licence.",
    },
    "onee-san": {
        "label": "Onee-san", "file": "onee-san.flac",
        "blurb": "Warm, unhurried, a few years older than you and enjoying "
                 "it. Teasing without the edge.",
        "f0_hz": 265, "speed": 0.82,
        "instruct": "female, young adult, low pitch",
        "credit": "Synthesised with OmniVoice Voice Design — no third-party "
                  "audio, no licence.",
    },
    "mommy": {
        "label": "Ara-ara", "file": "mommy.flac",
        "blurb": "Low, slow, indulgent. The one that calls you sweetheart "
                 "and is not asking.",
        "f0_hz": 269, "speed": 0.82,
        "instruct": "female, middle-aged, low pitch",
        "credit": "Synthesised with OmniVoice Voice Design — no third-party "
                  "audio, no licence.",
    },
    "natural-bright": {
        "label": "Natural — bright", "file": "female-bright.wav",
        "blurb": "A real reading, clear and higher. Neutral base when you "
                 "want a person rather than an archetype.",
        "f0_hz": 210, "speed": 1.0,
        "instruct": "female, young adult, moderate pitch",
        "credit": "LJ Speech (Linda Johnson, LibriVox) — public domain",
    },
    "natural-warm": {
        "label": "Natural — warm", "file": "female-warm.wav",
        "blurb": "A real reading, lower and rounder.",
        "f0_hz": 194, "speed": 1.0,
        "instruct": "female, middle-aged, low pitch",
        "credit": "LibriTTS-R (LibriVox) — CC BY 4.0",
    },
}

DEFAULT = "onee-san"

# Offered in the UI. A clone inherits the *pace* of its reference, so this is
# a second, independent lever applied at generation time.
SPEEDS = [
    (0.80, "much slower"), (0.85, "slower"), (0.90, "a little slower"),
    (0.95, "natural"), (1.00, "default"), (1.05, "a little faster"),
    (1.10, "faster"),
]


def available() -> list:
    """Presets whose audio is actually on disk, for the UI picker."""
    out = []
    for name, spec in PRESETS.items():
        path = VOICE_DIR / spec["file"]
        if not path.exists():
            continue
        out.append({"name": name, "label": spec["label"],
                    "blurb": spec["blurb"], "f0_hz": spec["f0_hz"],
                    "speed": spec.get("speed", 1.0),
                    "instruct": spec["instruct"], "credit": spec["credit"],
                    "url": f"/api/voices/{spec['file']}"})
    return out


def path_for(name: str):
    """Filesystem path of a preset's audio, or None."""
    spec = PRESETS.get(name or "")
    if not spec:
        return None
    path = VOICE_DIR / spec["file"]
    return path if path.exists() else None


def resolve(voice: dict):
    """Work out which reference to clone from.

    Returns (kind, value): ("asset", filename) for the character's own upload,
    ("preset", name) for a shipped one, or ("none", "") when her voice should
    be described in words instead.
    """
    voice = voice or {}
    if voice.get("sample"):
        return "asset", voice["sample"]
    preset = voice.get("preset")
    if preset == "none":
        return "none", ""
    if preset and path_for(preset):
        return "preset", preset
    # No choice made at all: a shipped voice beats a synthetic description,
    # because "female, moderate pitch" renders as nobody in particular.
    if path_for(DEFAULT):
        return "preset", DEFAULT
    return "none", ""
