#!/usr/bin/env python3
"""CoomKit recipes — the shots you actually ask for, one click each.

A raw "generate an image" button is not a feature; it is homework. What people
want mid-scene is *this specific thing* — a selfie, a modelling shot, the
moment that is happening right now, her voice saying the line she just said —
and they want it without leaving the conversation to go and write a prompt.

So a recipe is a small, opinionated brief. It knows what shot it is asking
for, which options make sense for it (clothed or not, POV or not, still or
moving), which workflow family it belongs to, and what to do with the
character's appearance and the current scene. It hands that brief to the
prompt-writer, which rewrites it into the target model's dialect using
skills/, and the result goes to the user for approval before anything runs.

Nothing here talks to ComfyUI. `studio.py` runs it; this module only decides
what should be made.

Every brief is registered as an editable prompt layer (prompts.py), because a
brief is injected text like any other and the whole point of this codebase is
that you can see and change the text being put in your model's mouth.
"""

# --------------------------------------------------------------------------
# Option vocabularies shared across recipes
# --------------------------------------------------------------------------

WARDROBE = {
    "clothed": "fully dressed, in whatever suits the setting",
    "lingerie": "in lingerie",
    "topless": "topless",
    "nude": "completely nude",
}

EXPLICIT = {
    "suggestive": "suggestive but not explicit — implied rather than shown",
    "explicit": "explicit and unambiguous",
    "very-explicit": "as explicit as the model will render; leave nothing "
                     "implied",
}


# Ambience beds, written as *textures*. Stable Audio turns any named event
# into sporadic bangs — measured at ±10 dB across 5s windows versus ±0.3 dB
# for a stationary description — so every one of these says "steady",
# "continuous", "constant" and describes a surface, never a moment.
AMBIENCE = {
    "nails": (
        "steady continuous fingernail tapping on glass, crisp evenly spaced "
        "clicks, constant unchanging rhythm, close mic ASMR, no reverb"),
    "mouth": (
        "steady continuous wet mouth sounds, slow rhythmic licking and "
        "sucking, soft saliva texture, constant unchanging level, extremely "
        "close mic, no speech"),
    "breath": (
        "steady continuous soft breathing very close to a microphone, warm "
        "constant unchanging texture, faint lip and throat sounds, no words"),
    "fabric": (
        "steady continuous fabric rustling against a microphone, slow "
        "constant unchanging texture, close mic, soft cotton and skin"),
    "hair": (
        "steady continuous hair brushing and scalp scratching, fine constant "
        "unchanging texture, close binaural mic"),
    "rain": (
        "steady continuous rain on a window, constant unchanging level, "
        "soft distant hiss, no thunder"),
    "room": (
        "soft continuous room tone, faint constant unchanging hum, close and "
        "quiet, late at night"),
    "none": "",
}

def _opt(kind, label, values=None, default=None, desc=""):
    return {"type": kind, "label": label, "values": values,
            "default": default, "desc": desc}


WARDROBE_OPT = _opt("choice", "Wardrobe", list(WARDROBE), "clothed")
EXPLICIT_OPT = _opt("choice", "How explicit", list(EXPLICIT), "explicit")
POV_OPT = _opt("bool", "POV", None, False,
               "Shot from the user's own eyeline, their body in frame.")
MOVING_OPT = _opt("bool", "Video", None, False,
                  "Generate a short clip with sound instead of a still.")
AMBIENCE_OPT = _opt("choice", "Ambience", list(AMBIENCE), "breath",
                    "The bed under her voice. Tops out at 47.6 seconds.")


# --------------------------------------------------------------------------
# The recipes
# --------------------------------------------------------------------------
# media:   which workflow kinds this can target, best first
# options: what the UI offers
# brief:   the editable text handed to the prompt-writer. {placeholders} are
#          filled from the chat context.

RECIPES = {
    "solo-model": {
        "label": "Modelling photo", "icon": "📸", "group": "photo",
        "media": ["image"],
        "blurb": "A deliberate, posed portrait. She knows the camera is there.",
        "options": {"wardrobe": WARDROBE_OPT,
                    "setting": _opt("text", "Setting", None, "",
                                    "Leave blank to use the current scene.")},
        "brief": (
            "Write a prompt for a single posed modelling photograph of "
            "{char}.\n\n"
            "SUBJECT: {char}. {appearance}\n"
            "WARDROBE: {wardrobe}\n"
            "SETTING: {setting}\n"
            "SHOT: a deliberate portrait or full-length modelling shot. She is "
            "aware of the camera and posing for it — weight on one hip, chin "
            "angled, eye contact. Flattering directional light with real "
            "shadow. One person only, no other figures.\n\n"
            "Make it look like a photograph someone was paid to take. "
            "Composition, lighting and lens matter more than adjectives."
        ),
        "shot": {"width": 832, "height": 1216},
    },

    "solo-lewd": {
        "label": "Filthy solo", "icon": "🔥", "group": "photo",
        "media": ["image"],
        "blurb": "The one she'd only send you.",
        "options": {"explicit": EXPLICIT_OPT,
                    "setting": _opt("text", "Setting", None, "")},
        "brief": (
            "Write a prompt for an explicit solo photograph of {char}.\n\n"
            "SUBJECT: {char}. {appearance}\n"
            "TONE: {explicit}\n"
            "SETTING: {setting}\n"
            "SHOT: she is alone and putting herself on display for the person "
            "holding the camera. Choose one clear, readable pose and commit to "
            "it rather than describing several. Skin, light and texture carry "
            "this — specify the light source and what it does to her.\n"
            "One person only. Nothing in frame that distracts from her."
        ),
        "shot": {"width": 832, "height": 1216},
    },

    "selfie": {
        "label": "Selfie", "icon": "🤳", "group": "photo",
        "media": ["image"],
        "blurb": "Phone camera, her arm's length. The one that lands in a text.",
        "options": {"wardrobe": WARDROBE_OPT,
                    "mirror": _opt("bool", "Mirror shot", None, False)},
        "brief": (
            "Write a prompt for a selfie taken by {char} on her own phone.\n\n"
            "SUBJECT: {char}. {appearance}\n"
            "WARDROBE: {wardrobe}\n"
            "SETTING: {setting}\n"
            "SHOT: {mirror}. Amateur phone photography, NOT a professional "
            "portrait — arm's-length framing, slightly awkward angle, "
            "unflattering-but-real available light, mild sensor noise, a "
            "cluttered ordinary room behind her. Her free hand or the phone "
            "should be visible.\n\n"
            "The realism here comes from imperfection. Do not light it well, "
            "do not compose it well, do not make it look expensive."
        ),
        "shot": {"width": 896, "height": 1152},
    },

    "handjob": {
        "label": "Handjob", "icon": "✋", "group": "act",
        "media": ["image", "video"],
        "blurb": "Optionally from your own eyeline, optionally moving.",
        "options": {"pov": POV_OPT, "moving": MOVING_OPT,
                    "explicit": EXPLICIT_OPT},
        "brief": (
            "Write a prompt showing {char} giving {user} a handjob.\n\n"
            "SUBJECT: {char}. {appearance}\n"
            "TONE: {explicit}\n"
            "FRAMING: {pov}\n"
            "SETTING: {setting}\n"
            "SHOT: her hand and his cock are the subject and must be the "
            "clearest thing in frame — state where both are and what her "
            "fingers are doing. Include her face and where she is looking. "
            "Hands are the hardest thing these models draw, so describe the "
            "grip precisely and keep the rest of the composition simple."
        ),
        "shot": {"width": 1216, "height": 832},
        "wants_refs": ["cock"],
    },

    "blowjob": {
        "label": "Blowjob", "icon": "💋", "group": "act",
        "media": ["image", "video"],
        "blurb": "Optionally from your own eyeline, optionally moving.",
        "options": {"pov": POV_OPT, "moving": MOVING_OPT,
                    "explicit": EXPLICIT_OPT},
        "brief": (
            "Write a prompt showing {char} giving {user} a blowjob.\n\n"
            "SUBJECT: {char}. {appearance}\n"
            "TONE: {explicit}\n"
            "FRAMING: {pov}\n"
            "SETTING: {setting}\n"
            "SHOT: her mouth and his cock are the subject. State her head "
            "position and angle, what her hands are doing, and — critically — "
            "whether she is looking up at him, because that eye line is what "
            "makes the shot. Keep the background simple and the light on her "
            "face."
        ),
        "shot": {"width": 1216, "height": 832},
        "wants_refs": ["cock"],
    },

    "scene": {
        "label": "This moment", "icon": "🎬", "group": "act",
        "media": ["image", "video"],
        "blurb": "Whatever is happening in the chat right now, rendered.",
        "options": {"pov": POV_OPT, "moving": MOVING_OPT},
        "brief": (
            "Render the moment happening right now in this scene.\n\n"
            "SUBJECT: {char}. {appearance}\n"
            "FRAMING: {pov}\n"
            "WHAT IS HAPPENING:\n{scene}\n\n"
            "SHOT: pick the single most striking beat from the above and "
            "compose one image around it — not a summary of the whole scene. "
            "Keep every concrete detail already established: what she is "
            "wearing, where they are, the time of day, who is touching whom. "
            "Do not invent a different location or outfit than the scene "
            "states."
        ),
        "shot": {"width": 1216, "height": 832},
    },

    "asmr": {
        "label": "ASMR", "icon": "🎧", "group": "audio",
        "media": ["asmr"],
        "blurb": "Whispered in your ear, over a bed of texture.",
        "options": {"lewd": _opt("bool", "Lewd", None, False),
                    "ambience": AMBIENCE_OPT,
                    "seconds": _opt("number", "Length (s)", None, 45)},
        "brief": (
            "Write an ASMR performance for {char} to whisper to {user}.\n\n"
            "SPEAKER: {char}. {voice}\n"
            "TONE: {lewd}\n"
            "SCENE SO FAR:\n{scene}\n\n"
            "She is a few inches from their ear — that closeness is the whole "
            "point. Write roughly {seconds} seconds of speech: short "
            "sentences, long pauses, second person, present tense. Say what "
            "she is doing to them and what she wants, in her own words.\n\n"
            "Lean on the non-verbal tags — [sigh], [laughter], [sniff] — and "
            "on punctuation, because the model has no other way to perform "
            "this. Ellipses slow her down, a line break is a beat, a short "
            "fragment lands harder than a sentence. Broken breathing, half-"
            "finished thoughts and repetition read as arousal; adjectives do "
            "not.\n\n"
            "AMBIENCE: {ambience}\n"
            "That bed is already chosen — write as if it is happening around "
            "her (if she is doing her nails, mention them; if it is her "
            "mouth, let the words get wetter and sloppier). Do not describe "
            "the ambience itself, and do not write sound effects into her "
            "lines."
        ),
    },

    "song": {
        "label": "Song", "icon": "🎵", "group": "audio",
        "media": ["music"],
        "blurb": "She writes you one. Lyrics and all.",
        "options": {"lewd": _opt("bool", "Lewd", None, False),
                    "seconds": _opt("number", "Length (s)", None, 210,
                                    "MiniMax Music 3 tops out at 360s. Give "
                                    "it room — a song cut off mid-chorus is "
                                    "worse than a short one."),
                    "brief_text": _opt("text", "Anything specific?", None, "")},
        "brief": (
            "{char} is writing a song for {user}.\n\n"
            "ABOUT HER: {appearance}\n"
            "TONE: {lewd}\n"
            "WHAT THEY HAVE BEEN THROUGH:\n{scene}\n"
            "SPECIFIC REQUEST: {brief_text}\n\n"
            "Write it as her, about him, and make it FIT {seconds} seconds — "
            "the render is cut hard at that length, so a song whose lyrics "
            "run past it ends mid-word. Roughly 2.5 to 3 seconds of audio per "
            "sung line: at 60s that is a verse and a chorus, at 200s a full "
            "song. Count the lines and keep them inside the budget.\n\n"
            "The lyrics should sound like this particular character wrote "
            "them — her vocabulary, her preoccupations, the things she has "
            "actually said. Not a generic love song with her name in it."
        ),
    },

    "speak": {
        "label": "Say it out loud", "icon": "🔊", "group": "audio",
        "media": ["tts"],
        "blurb": "Her last reply, in her voice. Dialogue only — no narration.",
        "options": {},
        "direct": True,       # no prompt-writer pass; see studio.speak()
        "brief": (
            "Choose the delivery for these lines. Do not rewrite the words.\n\n"
            "SPEAKER: {char}. {voice}\n"
            "MOOD RIGHT NOW:\n{scene}\n\n"
            "LINES:\n{lines}"
        ),
    },
    # The escape hatch. Everything above is a shot someone chose for you; this
    # is the one where you say it yourself and she does the translating —
    # which is the whole point of shipping the dialect skills.
    "describe": {
        "label": "Describe it", "icon": "🪄", "group": "custom",
        # media[0] is what target_kind falls back to when no kind arrives.
        "media": ["image", "video", "asmr", "tts", "music"],
        "blurb": "Tell me what you want in plain words. I'll translate it "
                 "into whatever your model actually speaks and go get it.",
        "options": {
            "kind": _opt("choice", "What am I making",
                         ["image", "video", "asmr", "tts", "music"], "image",
                         "image = a picture · video = a clip with sound "
                         "(slow and greedy) · asmr = whispered over a bed · "
                         "tts = a voice note · music = a whole song."),
            "brief_text": _opt("textarea", "What do you want", None, "",
                               "she's on the balcony at 3am in my shirt, "
                               "city behind her, shot from inside"),
            "seconds": _opt("number", "Length (s) — audio and music only",
                            None, 45),
        },
        "requires": ["brief_text"],
        "brief": (
            "{user} asked for this, in their own words:\n\n"
            "{brief_text}\n\n"
            "SUBJECT: {char}, if she is in it. {appearance}\n"
            "HER VOICE: {voice}\n"
            "SETTING, if they did not name one: {setting}\n"
            "SCENE SO FAR:\n{scene}\n\n"
            "Their words are the brief and they outrank everything else "
            "here. Keep every concrete thing they asked for — subject, "
            "action, wardrobe, place, mood — and invent only what they left "
            "open. A place or an outfit they named beats the scene above; if "
            "they named neither, use the scene. Do not add a second person "
            "they did not ask for, do not soften what they asked for, and do "
            "not talk to them: your entire output is the thing itself, in "
            "the target model's own format.\n\n"
            "LENGTH, if this is audio or music: about {seconds} seconds."
        ),
    },

}


# --------------------------------------------------------------------------
# Filling a brief
# --------------------------------------------------------------------------

def _pov_text(opts: dict, user: str) -> str:
    if opts.get("pov"):
        return (f"first-person POV from {user}'s own eyeline — his body in "
                f"the near foreground, the camera where his eyes are, looking "
                f"down at her")
    return "third person, camera observing them both"


def _mirror_text(opts: dict) -> str:
    return ("a mirror selfie — she is photographing her reflection, phone "
            "visible in frame" if opts.get("mirror")
            else "held at arm's length, front camera")


def fill(recipe_id: str, opts: dict, ctx: dict, text: str = "") -> str:
    """Render a recipe brief with the chat's context.

    `ctx` carries char, user, appearance, voice, scene, setting.
    `text` overrides the shipped brief (prompts.py handles the override; this
    stays a pure function so it is testable without config on disk).
    """
    r = RECIPES[recipe_id]
    opts = dict(opts or {})
    fields = {
        "char": ctx.get("char") or "she",
        "user": ctx.get("user") or "anon",
        "appearance": ctx.get("appearance") or
                      "Use the character description already in context.",
        "voice": ctx.get("voice") or "",
        "scene": ctx.get("scene") or "(nothing established yet)",
        "setting": opts.get("setting") or ctx.get("setting") or
                   "wherever the current scene is taking place",
        "lines": ctx.get("lines", ""),
        "seconds": opts.get("seconds",
                            (r.get("options", {}).get("seconds") or {})
                            .get("default", 40)),
        "brief_text": opts.get("brief_text") or "nothing in particular",
        "wardrobe": WARDROBE.get(opts.get("wardrobe", "clothed"), ""),
        "explicit": EXPLICIT.get(opts.get("explicit", "explicit"), ""),
        "pov": _pov_text(opts, ctx.get("user") or "anon"),
        "mirror": _mirror_text(opts),
        "lewd": ("Explicit and filthy. She is not being coy."
                 if opts.get("lewd") else
                 "Warm and intimate, but not explicit."),
        "ambience": (AMBIENCE.get(opts.get("ambience", "breath"), "")
                     or "no bed — her voice alone"),
    }
    out = text or r["brief"]
    for key, val in fields.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def target_kind(recipe_id: str, opts: dict) -> str:
    """Which workflow kind this run should use."""
    r = RECIPES[recipe_id]
    media = r["media"]
    opts = opts or {}
    # A recipe may let the user say outright. Validated against the recipe's
    # own media list, so a stale client or a hallucinated ```tool``` call
    # degrades to media[0] rather than exploding in wfpack — and media[0] is
    # deliberately the cheap mistake: an image is 12s, H3 is 453s and 30.9 GB.
    want = opts.get("kind")
    if want in media:
        return want
    if opts.get("moving") and "video" in media:
        return "video"
    return media[0]


def catalogue() -> list:
    """Everything the UI needs to draw the recipe picker."""
    out = []
    for rid, r in RECIPES.items():
        out.append({
            "id": rid, "label": r["label"], "icon": r["icon"],
            "group": r["group"], "blurb": r["blurb"], "media": r["media"],
            "options": {k: dict(v) for k, v in (r.get("options") or {}).items()},
            "wants_refs": r.get("wants_refs", []),
            # fields the recipe cannot work without, so the client can refuse
            # generically instead of hardcoding another recipe id
            "requires": r.get("requires", []),
        })
    return out


def prompt_layers() -> dict:
    """Recipe briefs as editable prompt layers, for prompts.py to register."""
    layers = {}
    for rid, r in RECIPES.items():
        # A `direct` recipe never reaches the prompt-writer, so registering a
        # brief for it would put a text box in the inspector that changes
        # nothing. The rule is that every brief which IS injected must be an
        # editable layer — not that a layer must exist for a brief nobody uses.
        if r.get("direct"):
            continue
        layers[f"recipe_{rid}"] = {
            "label": f"Recipe — {r['label']}",
            "group": "recipes",
            "desc": r["blurb"] + " This is the brief handed to the "
                                 "prompt-writer before the model's own "
                                 "dialect skill rewrites it.",
            "placeholders": sorted(set(
                p for p in ("char", "user", "appearance", "voice", "scene",
                            "setting", "wardrobe", "explicit", "pov", "mirror",
                            "lewd", "seconds", "brief_text", "lines",
                            "ambience")
                if "{" + p + "}" in r["brief"])),
            "text": r["brief"],
        }
    return layers
