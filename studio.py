#!/usr/bin/env python3
"""CoomKit studio — recipe in, media out.

The whole generation path lives here so there is exactly one of it, the same
way `_prepare_request` is the only place a chat turn is assembled. A recipe
(recipes.py) becomes a plan, the plan becomes a draft prompt, the user
approves the draft, and only then does anything reach the GPU.

    plan()      pick the workflow, resolve refs and defaults — no LLM, no GPU
    draft()     brief -> prompt-writer -> the text the user will approve
    run()       broker VRAM, upload refs, queue, fetch, hand back bytes

Nothing is queued without the user seeing the prompt first. That is not a
safety gesture, it is the only way to keep a local model's first draft from
wasting sixty seconds of video render on a misread instruction.

Two output contracts, because the media differ:
  images/video  the writer returns prose or tags — the prompt, and that's it
  voice/music   the writer returns a small JSON object, because a voice needs
                text *and* a delivery, and a song needs a caption *and* lyrics
"""
import hashlib
import json
import os
import re

import comfy
import recipes
import scenarios
import tags
import tools
import voices
import vram
import wfpack

# Which bundled workflow serves each kind when nothing overrides it. Krea2 for
# stills because photoreal is the common case; anima is one click away and
# lives on the character for people whose cards are drawn, not photographed.
DEFAULT_WORKFLOWS = {
    "image": "krea2",
    "image-edit": "klein-edit",
    "video": "h3",
    "tts": "voice-clone",
    "asmr": "voice-design",
    "music": "music",
}

# Recipes whose writer returns JSON rather than a bare prompt.
JSON_KINDS = {"tts", "asmr", "music"}


class StudioError(Exception):
    pass


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def _stamped(stem: str, data: bytes, ext: str) -> str:
    """`stem-<hash><ext>` — a name that changes when the bytes change.

    Uploading new audio under a name that was used before does NOT give you
    the new audio: ComfyUI keeps the old file and OmniVoice caches the
    reference embedding against the filename. Measured here — the same clip
    cloned to 187 Hz under a fresh name and 78 Hz (a man) under a reused one,
    because it was still speaking with the voice of a file replaced an hour
    earlier. Content-addressing the name makes that impossible.
    """
    digest = hashlib.sha1(data).hexdigest()[:10]
    return f"{stem}-{digest}{ext}"


def pick_workflow(kind: str, cfg: dict, character: dict = None) -> str:
    """Resolve the workflow for a kind, most specific setting winning.

    character.data.visual.model > config.studio[kind] > shipped default.
    """
    character = character or {}
    visual = (character.get("data") or {}).get("visual") or {}
    if kind == "image" and visual.get("model") in wfpack.BUNDLED:
        return visual["model"]
    if kind == "video" and visual.get("video_model") in wfpack.BUNDLED:
        return visual["video_model"]

    # Cloning needs a reference clip. She may carry her own; otherwise a
    # shipped one stands in, because a *described* voice renders as nobody in
    # particular. Only an explicit "none" opts out into voice-design.
    if kind == "tts":
        voice = (character.get("data") or {}).get("voice") or {}
        source, _ = voices.resolve(voice)
        if source == "none":
            return "voice-design"
        if voice.get("engine") == "emotion":
            return "voice-emotion"

    # ASMR the same way. Hearing *her* whisper it is most of the point, so the
    # designed-voice graph is the fallback, not the default.
    if kind == "asmr":
        voice = (character.get("data") or {}).get("voice") or {}
        source, _ = voices.resolve(voice)
        return "voice-design" if source == "none" else "asmr-clone"

    chosen = (cfg.get("studio") or {}).get(kind)
    if chosen in wfpack.BUNDLED:
        return chosen
    return DEFAULT_WORKFLOWS.get(kind, "krea2")


def plan(recipe_id: str, opts: dict, ctx: dict, cfg: dict,
         character: dict = None, persona: dict = None,
         workflow: str = "") -> dict:
    """Decide what will be made, before a single token or watt is spent.

    `workflow` overrides the resolved graph for THIS render only. It is how a
    portrait gets re-rolled on a different image model without editing the
    character's saved looks — you are trying to find one you like, not
    committing to it. An unknown name is ignored rather than raising, because
    the settings list and the bundle can drift.
    """
    if recipe_id not in recipes.RECIPES:
        raise StudioError(f"no recipe called {recipe_id!r}")
    recipe = recipes.RECIPES[recipe_id]
    opts = dict(opts or {})
    kind = recipes.target_kind(recipe_id, opts)
    # The override must be for the RIGHT KIND. _fit_slots does not reject
    # values a graph does not declare, it drops them — so an image recipe sent
    # to h3 loses width and height and renders the demo video h3 shipped with,
    # silently, and the caller then files an .mp4 as somebody's avatar.
    # Checked here rather than at the route so _render_portrait, _studio_draft
    # and _tool_via_studio cannot disagree about it.
    ok = (workflow in wfpack.BUNDLED
          and wfpack.BUNDLED[workflow].get("kind") == kind)
    wf_name = workflow if ok else pick_workflow(kind, cfg, character)
    spec = wfpack.BUNDLED[wf_name]

    values = dict(recipe.get("shot") or {})
    values.update((cfg.get("studio") or {}).get("values") or {})

    # A character's pinned seed is what keeps her looking like herself across
    # a gallery. Without it every picture is a different woman.
    visual = ((character or {}).get("data") or {}).get("visual") or {}
    if visual.get("seed"):
        values["seed"] = int(visual["seed"])

    refs = _gather_refs(recipe, opts, spec, character, persona)

    loras = _merge_loras(spec, visual)

    # Artist tags are Anima's strongest style lever and meaningless-to-harmful
    # everywhere else, so they are gated on the workflow's dialect rather than
    # on the character. A character configured with artists simply has no
    # effect when she is rendered by a natural-language model.
    artists = []
    if spec.get("tag_dialect"):
        # Deliberately NOT seeded with values["seed"]. That seed is pinned per
        # character so her face stays hers across a gallery; feeding it to the
        # artist roll made "random — reroll every time" return the identical
        # pair forever, which is the exact opposite of what the mode says on
        # the tin. Style and identity are orthogonal axes. Someone who wants a
        # stable style already has 'pinned' plus the 🎲 button.
        artists = tags.resolve_artists(visual, cfg=cfg)

    # A cloned voice needs the sample on the *ComfyUI* box, not just in
    # CoomKit's asset folder — run() uploads it the same way it uploads
    # reference images.
    voice = ((character or {}).get("data") or {}).get("voice") or {}
    sample = preset_voice = None
    if "ref_audio" in (spec.get("slots") or {}):
        source, which = voices.resolve(voice)
        if source == "asset":
            sample = which
        elif source == "preset":
            preset_voice = which
    if voice.get("ref_text"):
        values.setdefault("ref_text", voice["ref_text"])
    if "instruct" in (spec.get("slots") or {}):
        hint = voice.get("instruct")
        if not hint and preset_voice:
            hint = voices.PRESETS[preset_voice].get("instruct", "")
        if hint:
            values.setdefault("instruct", hint)
    # The ambience bed is the user's choice, not the writer's — the brief
    # tells it the bed is already picked. Its ceiling is a hard model limit:
    # EmptyLatentAudio maxes out at 47.6s, so a longer take is voice-only over
    # a bed that stops.
    slots = spec.get("slots") or {}
    # A song is cut hard at `duration`, so the requested length has to reach
    # the graph — leaving it to the writer's JSON is how a track ends
    # mid-chorus.
    if "duration" in slots and opts.get("seconds"):
        values["duration"] = float(opts["seconds"])
    if "ambience" in slots:
        pick = opts.get("ambience", "breath")
        if pick in recipes.AMBIENCE:
            values["ambience"] = recipes.AMBIENCE[pick]
        want = float(opts.get("seconds") or values.get("ambience_seconds", 47))
        values["ambience_seconds"] = round(min(want, 47.0), 1)

    # Pace: her own setting first, else whatever the shipped voice was
    # rendered at, so a preset sounds the way it did when you auditioned it.
    if "speed" in (spec.get("slots") or {}):
        speed = voice.get("speed")
        if not speed and preset_voice:
            speed = voices.PRESETS[preset_voice].get("speed")
        if speed:
            values.setdefault("speed", float(speed))

    return {
        "recipe": recipe_id, "opts": opts, "kind": kind,
        # Carried so refs_clause can name them rather than saying "her".
        "ctx": {"char": ctx.get("char", "she"), "user": ctx.get("user", "anon")},
        "workflow": wf_name, "skill": spec.get("skill", ""),
        "values": values, "refs": refs, "voice_sample": sample,
        "voice_preset": preset_voice,
        "loras": loras,
        "artists": artists, "tag_dialect": bool(spec.get("tag_dialect")),
        "stages": dict((cfg.get("studio") or {}).get("stages") or {}),
        "vram_gb": spec.get("vram_gb", 8),
        "json_output": kind in JSON_KINDS,
        "character_id": (character or {}).get("id"),
        "chat_id": ctx.get("chat_id"), "message_id": ctx.get("message_id"),
    }


# Kinds a LoRA can meaningfully apply to. A character's LoRA is a *visual*
# setting, and the audio graphs have an attachable loader too — stable-audio
# -open is a CheckpointLoaderSimple and MiniMax Music is a UNETLoader — so
# without this gate her face LoRA was being chained onto the ambience model
# on every ASMR render. Same reasoning as `tag_dialect`: a setting that means
# nothing for this workflow should do nothing, not something.
LORA_KINDS = {"image", "image-edit", "video"}


def _merge_loras(spec: dict, visual: dict) -> list:
    """The workflow's mandatory LoRAs, then the character's own.

    Some of the bundled models are not optional-LoRA situations. Stock Klein
    declines the instruction and base Krea 2 renders a clothed stranger, so
    their LoRAs are declared on the *workflow* rather than left to each
    character to remember — a character forged before you installed one would
    otherwise render wrong forever, with nothing on screen explaining why.

    Order matters: the required one loads first, closest to the base model,
    so the character's own stack composes on top of it. Dedup is on the file
    name and the required entry wins, because chaining the same LoRA twice
    applies its strength twice — which reads as "this LoRA is too strong"
    rather than as the duplicate it is.
    """
    required = [dict(l, required=True) for l in (spec.get("loras") or [])]
    own = (list(visual.get("loras") or [])
           if spec.get("kind") in LORA_KINDS else [])
    taken = {str(l.get("name", "")).strip().lower() for l in required}
    return required + [l for l in own
                       if str(l.get("name", "")).strip().lower() not in taken]


# What each reference label is, in words the prompt-writer can use. The writer
# never sees the pictures — it only ever sees <Picture N> — so if nothing
# tells it which is which, the labels in the prompt it writes are a coin flip.
REF_MEANING = {
    "her": "{char} herself — this is who the woman in the video must look like",
    "cock": "{user}'s cock — this is the one that must be in shot, not an "
            "invented one",
    "body": "{user}'s body, for the parts of him that are in frame",
}


def _gather_refs(recipe: dict, opts: dict, spec: dict,
                 character: dict, persona: dict) -> list:
    """Reference images for a ref2v workflow, in prompt-label order.

    **The prop comes first and she comes second.** Measured preference, not
    symmetry: H3 weights <Picture 1> most heavily, and the anatomy is the
    thing it gets wrong when left to invent — her face it will carry from a
    second reference perfectly well. Inverting these two is the single
    biggest quality difference on this workflow.

    The prop is NOT gated on the POV option any more. It was, and that was
    wrong in a way that only showed up in the output: "she is looking at the
    camera the entire video" is not the first-person framing the pov flag
    injects, so the shot that most needs the reference was the shot that
    never got it. What gates it now is the honest pair — the recipe declaring
    it wants that reference, and the persona actually having one on file.
    """
    if not spec.get("ref_images"):
        return []
    refs = []
    pdata = (persona or {}).get("data") or {}
    for want in recipe.get("wants_refs") or []:
        for r in pdata.get("refs") or []:
            if r.get("kind") == want and r.get("file"):
                refs.append({"label": want, "file": r["file"],
                             "source": "persona"})
                break

    char_data = (character or {}).get("data") or {}
    visual = char_data.get("visual") or {}
    if visual.get("ref"):
        refs.append({"label": "her", "file": visual["ref"], "source": "character"})
    elif (character or {}).get("avatar"):
        refs.append({"label": "her", "file": character["avatar"],
                     "source": "avatar"})
    return refs[:len(spec["ref_images"])]


# --------------------------------------------------------------------------
# Drafting the prompt
# --------------------------------------------------------------------------

def refs_clause(job: dict, ctx: dict = None) -> str:
    """Tell the writer which reference is which, derived from the real list.

    H3 addresses its references as <Picture 1> and <Picture 2>, and the writer
    has to name them in the prompt it produces. It never sees the images —
    vision is not on this path — so without this it is guessing, and a guess
    that comes out backwards produces a video of her face on the wrong thing.

    Generated from `job["refs"]` and never hardcoded, because the list is
    dynamic: with no reference photo on the persona she is <Picture 1>, and a
    fixed mapping would then be wrong in exactly the case that is most common
    on a fresh install.
    """
    refs = job.get("refs") or []
    if len(refs) < 1:
        return ""
    ctx = ctx or {}
    names = {"char": ctx.get("char") or "she", "user": ctx.get("user") or "anon"}
    lines = []
    for i, ref in enumerate(refs, 1):
        meaning = REF_MEANING.get(ref.get("label", ""), ref.get("label", ""))
        lines.append(f"<Picture {i}> is {meaning.format(**names)}")
    return ("\n\nREFERENCES the model has been given:\n" + "\n".join(lines) +
            "\nRefer to them by these exact labels. Define each one where the "
            "format calls for it, and do not describe a reference you were "
            "not given.")


# Curated sets worth spending tokens on. The full fifteen is ~500 tags and
# roughly 1,500 tokens competing with the brief on every single draft, and
# skills/anima.md already carries the synonym table and the negative base
# inline — so this is the subset that is exclusive (the model must pick
# exactly one, which is where it actually goes wrong) rather than merely
# suggestive.
VOCAB_SETS = ("rating", "framing")


def vocab_clause(job: dict) -> str:
    """The curated exclusive tag sets, for the booru-dialect workflow only.

    These shipped for a long time and reached nothing at all — no writer, no
    skill, no UI. They are worth having in the brief because "pick exactly one
    of these" is the instruction a tag model needs and a 12B does not infer:
    stacking `close-up` with `full body` is the single most common way a
    generated tag list fights itself.
    """
    if not job.get("tag_dialect"):
        return ""
    out = []
    for tset in tags.tagsets():
        if tset.get("id") not in VOCAB_SETS:
            continue
        names = ", ".join(tset.get("tags") or [])
        if not names:
            continue
        rule = ("choose exactly one" if tset.get("exclusive")
                else "draw from these")
        out.append(f"{tset.get('label', tset['id']).upper()} — {rule}: {names}")
    if not out:
        return ""
    return ("\n\nVOCABULARY (real Danbooru tags — prefer these exact "
            "strings):\n" + "\n".join(out))


def framing_clause(job: dict) -> str:
    """Offer the writer a framing word — only where it can change anything.

    Skipped entirely for the audio and music graphs, and for any workflow with
    no size slots, so a model is never asked to choose an aspect ratio for a
    voice note. An offer that cannot be acted on is just tokens.
    """
    if job.get("json_output"):
        return ""
    if not wfpack.framing_values(job.get("workflow", ""), "portrait"):
        return ""
    return ("\n\nSHAPE: if one canvas suits this shot better than the "
            "others, put exactly one of `tall`, `wide` or `square` on its own "
            "FIRST line, then the prompt from the next line on. Omit it "
            "entirely and a sensible default is used — do not write any other "
            "word there, and never explain the choice.")


def writer_messages(job: dict, brief: str) -> list:
    """System = core rules + the target model's dialect; user = the brief."""
    core = tools.read_skill("_core.md")
    skill = tools.read_skill(job["skill"]) if job.get("skill") else ""
    system = (core + "\n\n" + skill).strip()
    brief = (brief + refs_clause(job, job.get("ctx"))
             + vocab_clause(job) + framing_clause(job))
    if job.get("artists"):
        # The skill already knows where artist tags go (position 2, right
        # after the quality preamble) and that it must never invent them, so
        # supplying them as data is enough.
        names = ", ".join(a["prompt"] for a in job["artists"])
        brief = (brief + "\n\nARTISTS: " + names +
                 "\nUse these exact artist tags, in the artist position. Do "
                 "not add or substitute any others.")
    return [{"role": "system", "content": system},
            {"role": "user", "content": brief}]


def _escape_artists(prompt: str, artists: list) -> str:
    """Re-escape parentheses inside artist names.

    Booru-lineage samplers read a bare `(campus life)` as an emphasis weight,
    so an artist tag like `kouji_(campus_life)` silently becomes "kouji" plus
    1.1x on two unrelated words. The writer strips the backslashes often
    enough that asking it nicely is not sufficient.
    """
    for a in artists:
        escaped = a.get("prompt", "")
        bare = escaped.replace("\\", "")
        if "(" in bare and bare in prompt and escaped not in prompt:
            prompt = prompt.replace(bare, escaped)
    return prompt


def ensure_artists(job: dict, values: dict) -> dict:
    """Put the artist tags back if the writer dropped them.

    A 12B told to include four things will sometimes include three. Artists
    are the one that changes the image most, so this is worth being
    deterministic about rather than hoping.
    """
    artists = job.get("artists") or []
    prompt = values.get("prompt")
    if not artists or not isinstance(prompt, str) or not prompt.strip():
        return values
    lowered = prompt.lower()
    if any(a["prompt"].lower().replace("\\", "") in lowered.replace("\\", "")
           for a in artists):
        return {**values, "prompt": _escape_artists(prompt, artists)}
    clause = tags.artist_clause(artists)
    parts = [p.strip() for p in prompt.split(",")]
    # Slot it after the quality preamble rather than at the very front, which
    # is where anima.md puts it.
    quality = {"masterpiece", "best quality", "score_7", "score_8", "score_9",
               "safe", "sensitive", "questionable", "explicit",
               "highly detailed", "absurdres"}
    at = 0
    for i, part in enumerate(parts[:6]):
        if part.lower() in quality:
            at = i + 1
    parts.insert(at, clause)
    return {**values, "prompt": ", ".join(p for p in parts if p)}


def parse_writer(job: dict, text: str) -> dict:
    """Turn the writer's reply into slot values.

    Prose models return a prompt; voice and music return JSON. A model that
    was asked for JSON and produced prose is not an error worth failing on —
    the prose is still usable as the spoken line or the caption, so it is
    accepted as such rather than throwing the generation away.
    """
    text = (text or "").strip()
    if not job.get("json_output"):
        return _peel_framing(job, _strip_fence(text))

    obj = scenarios.parse_object(text) or {}
    kind = job["kind"]
    if kind == "music":
        if not obj.get("caption"):
            return {"music_prompt": _strip_fence(text), "lyrics": ""}
        out = {"music_prompt": obj["caption"], "lyrics": obj.get("lyrics", "")}
        if obj.get("duration"):
            out["duration"] = float(obj["duration"])
        return out

    if not obj.get("text"):
        return {"audio_text": _strip_fence(text)}
    out = {"audio_text": obj["text"]}
    # Only keep a key the chosen graph actually has a slot for. skills/voice.md
    # carries worked ASMR examples, and a writer reading it would copy their
    # keys onto a plain clone job — where _fit_slots then dropped them, after
    # the approval card had already offered them as editable fields.
    spec = wfpack.BUNDLED.get(job.get("workflow"), {})
    slots = set(spec.get("slots") or {})
    # An unknown workflow keeps the old permissive behaviour — the gate is
    # here to drop keys a KNOWN graph has no slot for, not to swallow values
    # whenever the job is underspecified.
    known = bool(slots)
    for src, dst in (("voice_instruct", "voice_instruct"),
                     ("instruct", "instruct"), ("speed", "speed"),
                     ("ambience", "ambience"),
                     ("emotion_alpha", "emotion_alpha")):
        if obj.get(src) in (None, ""):
            continue
        if known and dst not in slots:
            continue
        out[dst] = obj[src]
    if isinstance(obj.get("emotions"), dict) and obj["emotions"]:
        if not known or spec.get("emotions_at"):
            out["emotions"] = obj["emotions"]
    return out


_FRAMING_RE = re.compile(
    r"^\s*(?:SHAPE\s*[:\-]\s*)?(tall|wide|square)\s*$", re.I)


def _peel_framing(job: dict, text: str) -> dict:
    """Take an opening framing word off the prompt, if the writer offered one.

    Deliberately forgiving in one direction only: a first line that is not one
    of the three words is left exactly where it is and becomes part of the
    prompt, so a writer that ignores the directive produces byte-identical
    output to before. The failure mode of a *strict* parser here would be
    eating the first sentence of the prompt.
    """
    lines = text.split("\n", 1)
    m = _FRAMING_RE.match(lines[0]) if lines else None
    if not m:
        return {"prompt": text}
    rest = (lines[1] if len(lines) > 1 else "").strip()
    if not rest:                       # it emitted ONLY the word — useless
        return {"prompt": text}
    out = {"prompt": rest}
    out.update(wfpack.framing_values(job.get("workflow", ""), m.group(1)))
    return out


def _strip_fence(text: str) -> str:
    """Drop a wrapping code fence the writer added despite being told not to."""
    t = text.strip()
    if t.startswith("```"):
        body = t.split("\n", 1)[1] if "\n" in t else ""
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        return body.strip()
    return t


def apply_pins(job: dict, values: dict) -> dict:
    """Let explicit settings beat the writer's guesses.

    The writer happily returns `"speed": 1.0` because that is the obvious
    default, which quietly discarded the 0.85 a shipped brat voice was
    rendered at. Anything the plan pinned deliberately wins.
    """
    out = dict(values or {})
    for key in ("speed", "seed", "instruct", "ref_text",
                "ambience", "ambience_seconds", "duration"):
        pinned = (job.get("values") or {}).get(key)
        if pinned not in (None, ""):
            out[key] = pinned
    return out


def review(job: dict, values: dict, installed=None) -> list:
    """Warnings worth showing next to the approve button.

    Cheap, local checks for the mistakes that cost a whole render — a voice
    vocabulary the model will reject, a whisper at a speed that destroys it,
    an ambience bed described as an event.

    Which warnings apply depends on the path. The whisper/speed and
    pitch/whisper findings were measured on Voice *Design*, where those words
    steer the synthesis; on a clone the reference carries the delivery and
    `voice_instruct` is not even a slot, so firing them there is noise the
    user learns to ignore.
    """
    notes = []
    # LoRAs, checked against what ComfyUI actually has. `installed` is
    # optional so this function stays offline and free by default — the route
    # passes the set it already fetched. Worth pre-flighting because a missing
    # file rejects the entire graph, and it does so *after* the user approved
    # and after the chat model was evicted from the GPU to make room.
    if installed is not None:
        for lora in job.get("loras") or []:
            name = str(lora.get("name") or "")
            if not name or name in installed:
                continue
            why = lora.get("why") or ""
            if lora.get("required"):
                notes.append(
                    f"{wfpack.BUNDLED.get(job.get('workflow'), {}).get('label', 'This model')} "
                    f"wants {name} and your ComfyUI does not have it"
                    + (f" — {why.rstrip('.')}" if why else "")
                    + ". It will be skipped; expect a tamer picture than you "
                      "asked for.")
            else:
                notes.append(f"{name} is not installed on your ComfyUI — "
                             f"skipping it.")

    # Video length against a hard VRAM ceiling. Measured on a 32 GB 5090 with
    # the chat model already parked: 10s peaks at 30.9 GB and takes 453s; 15s
    # peaks at 31.2 GB — 99.5% of the card — and takes 876.5s. Neither OOM'd,
    # but there is nothing left above 15s and nothing at all for a smaller
    # card, and the cost of finding out is a quarter of an hour.
    spec = wfpack.BUNDLED.get(job.get("workflow"), {})
    if spec.get("kind") == "video" and "duration" in (spec.get("slots") or {}):
        secs = float(values.get("duration") or spec.get("defaults", {})
                     .get("duration") or 0)
        mp = float(values.get("megapixels") or spec.get("defaults", {})
                   .get("megapixels") or 0)
        if secs >= 15 and mp >= 1.0:
            notes.append(
                f"{secs:g}s at {mp:g} MP is about as far as this goes — "
                f"measured at 31.2 GB of a 32 GB card and roughly 15 minutes. "
                f"It fits on a 5090 and nothing smaller. Drop to 0.7 MP for "
                f"the same framing at half the pixels.")
        elif secs > 10 and mp >= 1.0:
            notes.append(f"{secs:g}s at {mp:g} MP will take several minutes "
                         f"and most of your VRAM.")

    cloning = "ref_audio" in (wfpack.BUNDLED.get(job.get("workflow"), {})
                              .get("slots") or {})
    # Check BOTH, not whichever is truthy first: on a clone graph the `or`
    # validated voice_instruct — which _fit_slots discards — and never looked
    # at `instruct`, which is the one that reaches the node and gets rejected.
    parts, speed = [], float(values.get("speed") or 1.0)
    for key in ("voice_instruct", "instruct"):
        parts += [p.strip() for p in str(values.get(key) or "").split(",")
                  if p.strip()]
    vi = ", ".join(parts)
    if vi:
        bad = [p for p in parts if p.lower() not in VOICE_VOCAB]
        if bad:
            notes.append(
                f"OmniVoice will reject {', '.join(repr(b) for b in bad)} — "
                f"its instruct vocabulary is a closed list. Drop them; put "
                f"that expression in the words instead.")
        if not cloning and "whisper" in vi.lower() and speed < 1.0:
            notes.append("Speed below 1.0 destroys a whisper — hold it at 1.0 "
                         "and pace with shorter sentences.")
        if not cloning and "low pitch" in vi.lower() and "whisper" in vi.lower():
            notes.append("`low pitch` cancels `whisper` — pitch wins. Use "
                         "`moderate pitch` for a whispered take.")
    if cloning and not 0.9 <= speed <= 1.1 and values.get("speed"):
        notes.append(f"Speed {speed} is outside the 0.9–1.1 range cloning was "
                     f"measured safe at. It may still be fine — listen before "
                     f"you trust it.")
    amb = str(values.get("ambience") or "")
    if amb and not any(w in amb.lower() for w in
                       ("steady", "continuous", "constant", "unchanging")):
        notes.append("Ambience reads as an event, not a texture — it will "
                     "come out as sporadic bangs. Add 'steady continuous' "
                     "and 'constant unchanging'.")
    return notes


VOICE_VOCAB = {
    "male", "female",
    "child", "teenager", "young adult", "middle-aged", "elderly",
    "american accent", "british accent", "australian accent",
    "canadian accent", "chinese accent", "indian accent", "japanese accent",
    "korean accent", "portuguese accent", "russian accent",
    "very low pitch", "low pitch", "moderate pitch", "high pitch",
    "very high pitch", "whisper",
}


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------

def run(job: dict, values: dict, cfg: dict, asset_path=None,
        note=None) -> dict:
    """Build, broker the GPU, queue, and return the finished files.

    `asset_path(filename) -> bytes` resolves a stored CoomKit asset so
    reference images can be uploaded to ComfyUI. Returns
    {files, meta, vram, workflow}.
    """
    say = note or (lambda _m: None)
    url = cfg.get("comfyui_url") or ""
    if not url:
        raise StudioError("No ComfyUI address configured — set one in "
                          "settings before she can make you anything.")

    values = dict(job.get("values") or {}, **(values or {}))

    # A shipped preset is read straight off disk, so it must not be gated on
    # asset_path. Missing `voice_preset` from this guard meant nothing was
    # uploaded at all and the graph silently kept its own built-in reference —
    # sample-6.mp3, a man at 77.8 Hz. Every archetype voice came out as him.
    if (job.get("voice_preset")
            or (asset_path and (job.get("refs") or job.get("voice_sample")))):
        client = comfy.ComfyClient(url)
        uploaded = []
        for ref in (job.get("refs") or []) if asset_path else []:
            data = asset_path(ref["file"])
            if not data:
                # Never silently skip. The writer was already told what
                # <Picture 1> and <Picture 2> are, numbered from this exact
                # list — dropping one here slides her into the slot the prompt
                # says is the prop, and the clip comes back wrong with nothing
                # to explain it.
                say(f"⚠ reference '{ref['label']}' is missing from disk — "
                    f"rendering without it, so the picture numbering in the "
                    f"prompt no longer matches.")
                continue
            uploaded.append(client.upload_image(
                data, _stamped(f"coomkit_{ref['label']}", data,
                               os.path.splitext(ref["file"])[1] or ".png")))
        if uploaded:
            values["refs"] = uploaded
            values.setdefault("image", uploaded[0])
        if job.get("voice_sample") or job.get("voice_preset"):
            fell_back = ""
            if job.get("voice_sample") and asset_path:
                data = asset_path(job["voice_sample"])
            else:
                path = voices.path_for(job["voice_preset"])
                data = path.read_bytes() if path else None
            if not data:
                # Leaving ref_audio unset does NOT mean "no clone" — it means
                # the graph keeps its shipped LoadAudio default, which is a
                # 77.8 Hz male sample. A wiped or renamed reference has to
                # degrade to a shipped voice, not to a man.
                path = voices.path_for(voices.DEFAULT)
                if path:
                    data = path.read_bytes()
                    fell_back = voices.DEFAULT
            if data:
                # /upload/image is ComfyUI's only upload route and it drops
                # whatever it is given into input/, which is exactly where
                # LoadAudio looks. Not a misuse; there is no /upload/audio.
                tag = (fell_back or job.get("voice_sample")
                       or job.get("voice_preset"))
                if fell_back:
                    ext = os.path.splitext(voices.PRESETS[fell_back]["file"])[1]
                else:
                    ext = os.path.splitext(
                        job.get("voice_sample")
                        or voices.PRESETS[job["voice_preset"]]["file"])[1]
                values["ref_audio"] = client.upload_image(
                    data, _stamped(f"coomkit_voice_{tag}", data, ext or ".wav"))
                if fell_back:
                    say(f"⚠ her voice reference is missing — falling back to "
                        f"the shipped \"{fell_back}\" voice")
            else:
                say("⚠ no usable voice reference at all — the take will not "
                    "sound like her")

    values = _fit_slots(job["workflow"], values)
    # A LoRA file that is not there rejects the whole graph. A picture without
    # its style LoRA is disappointing; a hard 400 after a VRAM eviction is
    # worse, and the note above already warned them at approval time.
    loras = list(job.get("loras") or [])
    if loras:
        have = _installed_loras(url)
        if have is not None:
            kept = [l for l in loras if str(l.get("name")) in have]
            for gone in [l for l in loras if l not in kept]:
                say(f"⚠ {gone.get('name')} is not installed — rendering "
                    f"without it.")
            loras = kept
    graph, meta = wfpack.build(job["workflow"], values,
                               stages=job.get("stages"),
                               loras=loras)

    report = vram.make_room(cfg, url, meta["vram_gb"], note=say)
    try:
        say(f"🎨 {meta['label']} is rendering…")
        files = comfy.run_workflow(url, graph, {},
                                   timeout_s=int(cfg.get("comfy_timeout", 900)))
    finally:
        vram.give_back(cfg, url, report, note=say)

    return {"files": files, "meta": meta, "vram": report,
            "workflow": job["workflow"], "graph": graph}


# The two voice paths name the same idea differently: the design node calls it
# `voice_instruct`, the clone node calls it `instruct`. A writer that answers
# in the other one's vocabulary is not wrong, so translate rather than drop.
SLOT_ALIASES = {"voice_instruct": "instruct", "instruct": "voice_instruct"}


def _installed_loras(url: str):
    """What LoraLoader offers on this ComfyUI, or None if we could not ask.

    None means "unknown" and is treated as "do not interfere" — a flaky probe
    must never silently strip a LoRA the user does have.
    """
    try:
        info = comfy.ComfyClient(url, timeout=8)._get("/object_info/LoraLoader")
        opts = (info.get("LoraLoader", {}).get("input", {})
                .get("required", {}).get("lora_name", [[]])[0])
        return set(opts) or None
    except Exception:  # noqa: BLE001
        return None


def _fit_slots(workflow: str, values: dict) -> dict:
    """Map values onto the slots this workflow actually has."""
    slots = set((wfpack.BUNDLED[workflow].get("slots") or {}).keys())
    slots |= {"refs", "emotions", "steps", "cfg", "fps", "ref_text"}
    # `aspect`/`megapixels`/`duration` are declared slots on h3 already; this
    # is the belt for any future graph that resolves framing a third way.
    slots |= {"aspect"} if "aspect" in slots else set()
    out = {}
    for key, val in values.items():
        if key in slots:
            out[key] = val
            continue
        alias = SLOT_ALIASES.get(key)
        if alias and alias in slots and alias not in values:
            out[alias] = val
    return out


def speak_values(job: dict, lines: str) -> dict:
    """Values for a recipe that has nothing to invent.

    "Say it out loud" reads words that already exist, so there is no writer
    pass and nothing for a model to hallucinate into the job — which is what
    was putting `ambience: steady continuous fabric rustling…` on a request
    to read one line of dialogue. Pinned character settings still apply.
    """
    return apply_pins(job, {"audio_text": lines})


def dialogue_lines(text: str) -> str:
    """Pull just the spoken words out of a reply, for the speak recipe.

    Roleplay prose interleaves narration with dialogue; feeding the whole
    thing to a TTS model means listening to her read her own stage directions
    aloud. Quotes win when present; otherwise strip *asterisk actions* and
    keep what is left.
    """
    quoted = _between(text or "", '"', '"') + _between(text or "", "“",
                                                       "”")
    if quoted:
        return "\n".join(q.strip() for q in quoted if q.strip())
    out, depth = [], 0
    for ch in text or "":
        if ch == "*":
            depth ^= 1
            continue
        if not depth:
            out.append(ch)
    return " ".join("".join(out).split()).strip()


def _between(text: str, open_ch: str, close_ch: str) -> list:
    found, i = [], 0
    while True:
        a = text.find(open_ch, i)
        if a < 0:
            return found
        b = text.find(close_ch, a + 1)
        if b < 0:
            return found
        found.append(text[a + 1:b])
        i = b + 1
