#!/usr/bin/env python3
"""CoomKit's shipped ComfyUI workflows, and the surgery that adapts them.

Bring-your-own-workflow (comfy.py) stays the escape hatch, but nobody should
have to build a graph before their character can send a picture. These are
real API-format graphs exported from a working ComfyUI, with `{{slot}}`
placeholders written into the fields CoomKit drives.

Two problems that a static bundle cannot solve on its own, both handled here:

**Not everyone has the same node packs.** The graphs came off a machine with
Impact Pack, rgthree, Ultimate SD Upscale and friends installed. On a bare
ComfyUI those nodes do not exist and the whole graph 400s. So every optional
embellishment is declared as a *stage* that can be spliced out — the node is
removed and whatever fed it is wired straight to whatever consumed it. What
survives is core-node-only and runs anywhere.

**A chat is not a render farm.** Two FaceDetailer passes and a 2x upscale turn
a six-second picture into ninety. Mid-scene that is the difference between a
reply and an interruption, so the heavy stages are off unless asked for. The
same splice machinery does both jobs.

Stages are defined by node *class*, not by node id, so they apply to every
bundled graph and to workflows the user imports later.
"""
import copy
import json
import random
from pathlib import Path

WF_DIR = Path(__file__).resolve().parent / "workflows"


# --------------------------------------------------------------------------
# Stage rules — optional node packs, spliced out by class
# --------------------------------------------------------------------------
# bridge: {output_index: input_name} — references to this node's output N are
# replaced by whatever sits on input `input_name` (a link or a literal).
# drop:   nodes removed outright; nothing consumes them once the bridge is cut.

STAGES = {
    "wildcard": {
        "label": "dynamic prompts",
        "why": "{a|b} and __wildcard__ expansion. Off by default — CoomKit "
               "already wrote the exact prompt and does not want it rerolled.",
        "bridge": {"ImpactWildcardProcessor": {0: "populated_text"},
                   "PromptComposerCustomLists": {0: "text_in_opt"}},
        "default": False,
    },
    "loras": {
        "label": "rgthree LoRA stack",
        "why": "Replaced by CoomKit's own portable LoRA chain.",
        "bridge": {"Power Lora Loader (rgthree)": {0: "model", 1: "clip"}},
        "default": False,
    },
    "detail": {
        "label": "face + hand detailers",
        "why": "adetailer equivalent. Big quality win on faces, ~2x the time.",
        "bridge": {"FaceDetailer": {0: "image"}},
        "drop": ["UltralyticsDetectorProvider", "SAMLoader"],
        "default": False,
    },
    "upscale": {
        "label": "2x upscale",
        "why": "Ultimate SD Upscale at denoise 0.33. Slowest stage by far.",
        "bridge": {"UltimateSDUpscale": {0: "image"}},
        "drop": ["UpscaleModelLoader"],
        "default": False,
    },
    "preview": {
        "label": "preview nodes",
        "why": "Always off — they duplicate every output back at us.",
        "drop": ["PreviewAudio", "PreviewImage", "PreviewVideo"],
        "default": False,
    },
}

# Classes that are not core ComfyUI. Used to explain a missing dependency in
# words rather than letting ComfyUI reject the prompt with a node-type error.
PACK_OF = {
    "ImpactWildcardProcessor": "ComfyUI-Impact-Pack",
    "FaceDetailer": "ComfyUI-Impact-Pack",
    "UltralyticsDetectorProvider": "ComfyUI-Impact-Subpack",
    "SAMLoader": "ComfyUI-Impact-Pack",
    "PromptComposerCustomLists": "comfyui-prompt-composer",
    "Power Lora Loader (rgthree)": "rgthree-comfy",
    "UltimateSDUpscale": "ComfyUI_UltimateSDUpscale",
    "OmniVoiceVoiceCloneTTS": "ComfyUI-OmniVoice",
    "OmniVoiceVoiceDesignTTS": "ComfyUI-OmniVoice",
    "OmniVoiceMultiSpeakerTTS": "ComfyUI-OmniVoice",
    "OmniVoiceWhisperLoader": "ComfyUI-OmniVoice",
    "IndexTTSEngineNode": "TTS-Audio-Suite",
    "IndexTTSEmotionOptionsNode": "TTS-Audio-Suite",
    "CharacterVoicesNode": "TTS-Audio-Suite",
    "UnifiedTTSTextNode": "TTS-Audio-Suite",
    "AudioAdjustVolume": "ComfyUI-Audio-Tools",
    "AudioEqualizer3Band": "ComfyUI-Audio-Tools",
    "AudioMerge": "ComfyUI-Audio-Tools",
}
# NOT in PACK_OF, deliberately: ResolutionSelector, ComfyMathExpression,
# ComfySwitchNode and SeedNode were listed here as "comfy-kitchen", which is a
# pip dependency of ComfyUI and not an installable node pack. All four are core
# (`comfy_extras.nodes_resolution` / `nodes_math` / `nodes_logic` /
# `nodes_seed`), so a user missing them needs to update ComfyUI, not install
# anything — and telling them to go find a pack that does not exist is worse
# than saying nothing.


# --------------------------------------------------------------------------
# The bundle
# --------------------------------------------------------------------------
# `slots` maps a CoomKit slot name to the [node_id, field] pairs it fills.
# `vram_gb` is what the job wants free before it starts — vram.py uses it to
# decide whether the chat model has to step off the GPU first.

BUNDLED = {
    # ---- images ---------------------------------------------------------
    "anima": {
        "file": "01-anima-t2i.json", "kind": "image",
        "label": "Anima", "skill": "anima.md", "vram_gb": 14,
        # The only booru-tag model in the bundle. Artist tags and the curated
        # tag sets apply here and NOWHERE else — krea2, klein and z-image are
        # natural-language, and their own skills say tag salad actively hurts
        # them, so feeding artist tags to those is a downgrade, not a style.
        "tag_dialect": True,
        "blurb": "Anime and illustrated. Danbooru tags, artist blending, "
                 "the most permissive of the image models.",
        "slots": {"prompt": [["6", "wildcard_text"], ["6", "populated_text"]],
                  "negative": [["9", "text"]],
                  "seed": [["10", "seed"]],
                  "width": [["1", "width"]], "height": [["1", "height"]]},
        "defaults": {"width": 832, "height": 1216,
                     "negative": "worst quality, low quality, score_1, "
                                 "score_2, score_3, blurry, jpeg artifacts"},
        "steps_at": [["10", "steps"]], "cfg_at": [["10", "cfg"]],
    },
    "krea2": {
        "file": "03-krea2-turbo-t2i.json", "kind": "image",
        "label": "Krea 2 Turbo", "skill": "krea2.md", "vram_gb": 16,
        "blurb": "Photoreal, natural-language prompting. 8 steps — the "
                 "fastest good-looking option.",
        # Base Krea 2 is a general photography model and will not render what
        # this program exists to render. Not an optional style preference —
        # without it the shot comes back clothed and confused.
        "loras": [{"name": "MysticXXX_KREA2_v3.safetensors", "strength": 1.0,
                   "why": "Krea 2 will not do explicit work without it."}],
        "slots": {"prompt": [["6", "wildcard_text"], ["6", "populated_text"]],
                  "seed": [["10", "seed"]],
                  "width": [["1", "width"]], "height": [["1", "height"]]},
        "defaults": {"width": 832, "height": 1216},
        "steps_at": [["10", "steps"]],
        "note": "CFG 1.0 — the negative prompt is zeroed out and does nothing.",
    },
    "klein": {
        "file": "04-klein-t2i.json", "kind": "image",
        "label": "FLUX.2 Klein 4B", "skill": "klein.md", "vram_gb": 12,
        "blurb": "Distilled Flux. 4 steps, excellent prompt adherence, "
                 "reads long descriptive sentences properly.",
        # Klein is heavily aligned out of the box. Same story as krea2: the
        # LoRA is what makes the model usable here at all.
        "loras": [{"name": "KLEIN-Unchained-V2.safetensors", "strength": 1.0,
                   "why": "Stock Klein refuses or blurs explicit subjects."}],
        "slots": {"prompt": [["5", "wildcard_text"], ["5", "populated_text"]],
                  "seed": [["9", "noise_seed"]],
                  "width": [["8", "width"], ["11", "width"]],
                  "height": [["8", "height"], ["11", "height"]]},
        "defaults": {"width": 832, "height": 1216},
        "steps_at": [["11", "steps"]],
    },
    "klein9b": {
        "file": "08-klein-9b-t2i.json", "kind": "image",
        "label": "FLUX.2 Klein 9B", "skill": "klein.md", "vram_gb": 20,
        "blurb": "The bigger Klein at 8 steps. Slower, better skin and hands.",
        "loras": [{"name": "KLEIN-Unchained-V2.safetensors", "strength": 1.0,
                   "why": "Stock Klein refuses or blurs explicit subjects."}],
        "slots": {"prompt": [["5", "wildcard_text"], ["5", "populated_text"]],
                  "seed": [["9", "noise_seed"]],
                  "width": [["8", "width"], ["11", "width"]],
                  "height": [["8", "height"], ["11", "height"]]},
        "defaults": {"width": 832, "height": 1216},
        "steps_at": [["11", "steps"]],
    },
    "zimage": {
        "file": "02-zimage-turbo-t2i.json", "kind": "image",
        "label": "Z-Image Turbo", "skill": "zimage.md", "vram_gb": 12,
        "blurb": "9 steps, light on VRAM. Good when the chat model is "
                 "staying resident.",
        "slots": {"prompt": [["6", "wildcard_text"], ["6", "populated_text"]],
                  "seed": [["11", "seed"]],
                  "width": [["1", "width"]], "height": [["1", "height"]]},
        "defaults": {"width": 832, "height": 1216},
        "steps_at": [["11", "steps"]],
        "note": "CFG 1.0 — negative prompt is zeroed out.",
    },
    "klein-edit": {
        "file": "07-klein-9b-edit.json", "kind": "image-edit",
        "label": "Klein 9B edit", "skill": "klein-edit.md", "vram_gb": 20,
        "blurb": "Edits an existing picture from an instruction. Outfit "
                 "changes, backgrounds, 'now without the towel'.",
        "loras": [{"name": "KLEIN-Unchained-V2.safetensors", "strength": 1.0,
                   "why": "'now without the towel' is exactly the instruction "
                          "stock Klein declines."}],
        "slots": {"prompt": [["7", "text"]],
                  "seed": [["13", "noise_seed"]],
                  "image": [["4", "image"]]},
        "steps_at": [["15", "steps"]],
        "needs_image": True,
    },

    # ---- video ----------------------------------------------------------
    "h3": {
        "file": "12-minimax-h3-r2v-nvfp4-sage.json", "kind": "video",
        # 26 was measured at the old 480x864 / 5s defaults. At 896x1184 / 10s
        # a real run peaked at 30.4 GB of a 32 GB 5090 — so 26 would have let
        # the broker leave a chat model resident and OOM the render.
        "label": "MiniMax H3", "skill": "h3.md", "vram_gb": 30,
        "blurb": "Video with native synchronised audio, driven by reference "
                 "images. The good one — and the hungry one.",
        "slots": {"prompt": [["138", "value"]],
                  "seed": [["129", "noise_seed"]],
                  "duration": [["132", "value"]],
                  "aspect": [["115", "aspect_ratio"]],
                  "megapixels": [["115", "megapixels"]],
                  "image": [["137", "image"]],
                  "image2": [["139", "image"]]},
        # aspect is a ResolutionSelector combo — these strings must match its
        # option list exactly, spelling and all.
        # Stated preferences, not guesses: 3:4 at 1.0 MP frames a person
        # head-to-thigh without the 9:16 crop cutting hands out of shot, and
        # ten seconds is long enough for a beat to land. ResolutionSelector
        # counts a megapixel as 1024*1024, not 1e6, so this is 896x1184 —
        # 2.56x the old 480x864, and 5.1x the work once the duration doubles.
        #
        # MEASURED on a 5090 + ComfyUI 0.33, chat model parked: 453.3s and a
        # 30.9 GB peak of 31.36 GB — 98.5% of the card, from 63.8s at the old
        # defaults. It fits inside the default 900s `comfy_timeout` with half
        # the budget spare, and it will not fit on a smaller card at all.
        # Anyone short of room or time wants 0.7 MP at 3:4 — 736x992, same
        # framing, roughly half the pixels.
        #
        # There is no headroom left for a longer clip: see the duration note.
        "defaults": {"duration": 10, "aspect": "3:4 (Portrait Standard)",
                     "megapixels": 1.0},
        "aspects": ["1:1 (Square)", "2:3 (Portrait Photo)", "3:2 (Photo)",
                    "3:4 (Portrait Standard)", "4:3 (Standard)",
                    "9:16 (Portrait Widescreen)", "16:9 (Widescreen)",
                    "21:9 (Ultrawide)"],
        # fps_at is wired but must stay unreachable: H3 generates at a fixed
        # 24 fps and node 131 bakes 24 into its frame-count expression, so
        # changing node 130 alone slides the native audio out of sync with
        # the picture. No recipe emits `fps`; keep it that way.
        "steps_at": [["124", "steps"]], "fps_at": [["130", "fps"]],
        "ref_images": [["136", "ref_images.ref_image_0"],
                       ["136", "ref_images.ref_image_1"]],
        "note": "Reference images are addressed in the prompt as "
                "<Picture 1>, <Picture 2> — the prop first, her second, "
                "because H3 weights <Picture 1> hardest and anatomy is what "
                "it invents worst.",
    },
    "wan-i2v": {
        "file": "06-wan22-i2v.json", "kind": "video",
        "label": "Wan 2.2 I2V", "skill": "video-wan-ltx.md", "vram_gb": 20,
        "blurb": "Animates a still. 4 steps, no audio. Cheaper than H3 when "
                 "you already have the picture.",
        "slots": {"prompt": [["9", "wildcard_text"], ["9", "populated_text"]],
                  "negative": [["11", "text"]],
                  "seed": [["14", "noise_seed"], ["15", "noise_seed"]],
                  "image": [["12", "image"]],
                  "width": [["13", "width"]], "height": [["13", "height"]],
                  "length": [["13", "length"]]},
        "defaults": {"width": 640, "height": 640, "length": 81},
        "fps_at": [["17", "fps"]],
        "needs_image": True,
    },
    "wan-t2v": {
        "file": "05-wan22-t2v.json", "kind": "video",
        "label": "Wan 2.2 T2V", "skill": "video-wan-ltx.md", "vram_gb": 20,
        "blurb": "Video from text alone. 4 steps, no audio.",
        "slots": {"prompt": [["9", "wildcard_text"], ["9", "populated_text"]],
                  "negative": [["11", "text"]],
                  "seed": [["13", "noise_seed"], ["14", "noise_seed"]],
                  "width": [["12", "width"]], "height": [["12", "height"]],
                  "length": [["12", "length"]]},
        "defaults": {"width": 640, "height": 640, "length": 81},
        "fps_at": [["16", "fps"]],
    },

    # ---- voice ----------------------------------------------------------
    "voice-clone": {
        "file": "20-omnivoice-roleplay-clone.json", "kind": "tts",
        "label": "OmniVoice clone", "skill": "voice.md", "vram_gb": 8,
        "blurb": "Speaks her line in a voice cloned from a 3-15s sample. "
                 "Needs a voice sample on the character.",
        "slots": {"audio_text": [["3", "text"]],
                  "seed": [["3", "seed"]],
                  "speed": [["3", "speed"]],
                  "instruct": [["3", "instruct"]],
                  "ref_audio": [["2", "audio"]]},
        "defaults": {"speed": 1.0},
        "needs_voice": True,
    },
    "voice-design": {
        "file": "22-omnivoice-asmr.json", "kind": "asmr",
        "label": "ASMR / designed voice", "skill": "voice.md", "vram_gb": 10,
        "blurb": "Whispered performance over a generated ambience bed. No "
                 "voice sample needed — the voice is described in words.",
        "slots": {"audio_text": [["10", "text"]],
                  "voice_instruct": [["10", "voice_instruct"]],
                  "speed": [["10", "speed"]],
                  "seed": [["10", "seed"]],
                  "ambience": [["3", "text"]],
                  "ambience_negative": [["4", "text"]],
                  "ambience_seconds": [["5", "seconds_total"],
                                       ["6", "seconds"]],
                  "ambience_seed": [["7", "seed"]]},
        # voice_instruct is a CLOSED vocabulary — OmniVoice validates against
        # a fixed list and rejects anything else, so no adjectives here.
        # skills/voice.md carries the full permitted set.
        "defaults": {"speed": 1.0, "ambience_seconds": 47,
                     "voice_instruct": "female, young adult, moderate pitch, "
                                       "whisper",
                     "ambience_negative":
                         "music, melody, speech, voices, singing, thunder, "
                         "sudden loud bangs, silence, fade out, clipping"},
        "note": "Keep speed at 1.00 — anything lower destroys a whisper. "
                "Ambience must be described as a texture, never an event.",
    },
    "asmr-clone": {
        "file": "24-omnivoice-asmr-clone.json", "kind": "asmr",
        "label": "ASMR — her own voice", "skill": "voice.md", "vram_gb": 10,
        "blurb": "The same whispered performance over an ambience bed, but "
                 "in her cloned voice instead of a described one.",
        "slots": {"audio_text": [["10", "text"]],
                  "speed": [["10", "speed"]],
                  "seed": [["10", "seed"]],
                  "instruct": [["10", "instruct"]],
                  "ref_audio": [["20", "audio"]],
                  "ref_text": [["10", "ref_text"]],
                  "ambience": [["3", "text"]],
                  "ambience_negative": [["4", "text"]],
                  "ambience_seconds": [["5", "seconds_total"],
                                       ["6", "seconds"]],
                  "ambience_seed": [["7", "seed"]]},
        "defaults": {"speed": 1.0, "ambience_seconds": 47,
                     "ambience_negative":
                         "music, melody, speech, voices, singing, thunder, "
                         "sudden loud bangs, silence, fade out, clipping"},
        "needs_voice": True,
        "note": "The bed tops out at 47.6s — anything longer is voice only.",
    },
    "voice-emotion": {
        "file": "23-indextts2-roleplay-emotion.json", "kind": "tts",
        "label": "IndexTTS-2 (emotion)", "skill": "voice.md", "vram_gb": 10,
        "blurb": "Clones the voice, then sets the performance separately. "
                 "Use when the delivery matters more than the speed.",
        "slots": {"audio_text": [["5", "text"]],
                  "seed": [["5", "seed"]],
                  "ref_audio": [["1", "audio"]],
                  "emotion_alpha": [["4", "emotion_alpha"]]},
        "emotions_at": "3",
        "emotions": ["Happy", "Angry", "Sad", "Surprised", "Afraid",
                     "Disgusted", "Calm", "Melancholic"],
        "defaults": {"emotion_alpha": 1.0},
        "needs_voice": True,
    },

    # ---- music ----------------------------------------------------------
    "music": {
        "file": "30-minimax-music3.json", "kind": "music",
        "label": "MiniMax Music 3", "skill": "music.md", "vram_gb": 14,
        "blurb": "Full songs with vocals from a structured caption plus "
                 "tagged lyrics.",
        "slots": {"music_prompt": [["37:13", "caption"]],
                  "lyrics": [["37:13", "lyrics"]],
                  "duration": [["37:13", "max_duration"], ["37:15", "seconds"]],
                  "seed": [["37:13", "seed"], ["37:38", "seed"]]},
        "defaults": {"duration": 120},
    },
}


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------
# A closed, one-word vocabulary the prompt-writer may choose from, resolved
# here into whatever each workflow actually speaks — pixels for the latent
# graphs, a combo string for H3's ResolutionSelector.
#
# Words, not numbers, on purpose. A 12B asked to remember "832x1216" produces
# 832x1215 often enough to matter, and ComfyUI does not reject an off-grid
# size — it silently floors it, or OOMs at 16384. A closed vocabulary either
# matches or it does not, and "does not" falls back to the recipe's own shot.

# `tall`/`wide`, NOT `portrait`/`landscape`. Anima's curated `framing` tag set
# already uses `portrait` to mean a shot distance — a booru tag, nothing to do
# with the canvas — and both lists land in the same brief. Two vocabularies
# sharing a word is how a model picks the wrong one.
FRAMINGS = ("tall", "wide", "square")

# ~1.0 MP, on the multiple-of-64 grid every one of these graphs is happy with.
SIZES = {
    "tall": (832, 1216),
    "wide": (1216, 832),
    "square": (1024, 1024),
}

# H3 speaks ResolutionSelector combo strings instead, and they must match its
# option list character-for-character.
ASPECT_OF = {
    "tall": "3:4 (Portrait Standard)",
    "wide": "4:3 (Standard)",
    "square": "1:1 (Square)",
}


def framing_values(name: str, framing: str) -> dict:
    """Resolve a framing word for one workflow. {} if it means nothing here."""
    spec = BUNDLED.get(name) or {}
    framing = str(framing or "").strip().lower()
    if framing not in FRAMINGS:
        return {}
    slots = spec.get("slots") or {}
    if "aspect" in slots:
        aspect = ASPECT_OF[framing]
        # Never emit a combo value the graph's own option list does not carry.
        if aspect not in (spec.get("aspects") or [aspect]):
            return {}
        return {"aspect": aspect}
    if "width" in slots and "height" in slots:
        w, h = SIZES[framing]
        return {"width": w, "height": h}
    return {}


# --------------------------------------------------------------------------
# Loading and surgery
# --------------------------------------------------------------------------

def load(name: str) -> dict:
    spec = BUNDLED.get(name)
    if not spec:
        raise KeyError(f"no bundled workflow named {name!r}")
    return json.loads((WF_DIR / spec["file"]).read_text())


def classes(graph: dict) -> set:
    return {n.get("class_type", "") for n in graph.values()}


def _is_link(v) -> bool:
    return (isinstance(v, list) and len(v) == 2
            and isinstance(v[0], str) and isinstance(v[1], int))


def splice(graph: dict, stage: dict) -> dict:
    """Remove a stage's nodes, wiring their consumers to their producers.

    Bridging is what makes this safe: a FaceDetailer taking `image` and
    emitting an image is replaced by its own `image` input, so the picture
    still reaches SaveImage. A node with nothing to bridge is simply dropped
    once nobody references it.
    """
    graph = copy.deepcopy(graph)
    bridge_classes = stage.get("bridge") or {}
    drop_classes = set(stage.get("drop") or [])

    # Resolve each bridged output to the value that should replace it. Chained
    # nodes of the same class (two detailers in a row) need repeated passes.
    replace = {}   # (node_id, out_idx) -> value
    for nid, node in graph.items():
        rule = bridge_classes.get(node.get("class_type", ""))
        if not rule:
            continue
        for out_idx, field in rule.items():
            replace[(nid, int(out_idx))] = node.get("inputs", {}).get(field)

    def resolve(val, depth=0):
        while _is_link(val) and (val[0], val[1]) in replace and depth < 64:
            val = replace[(val[0], val[1])]
            depth += 1
        return val

    for key in list(replace):
        replace[key] = resolve(replace[key])

    doomed = set(nid for (nid, _) in replace)
    doomed |= {nid for nid, n in graph.items()
               if n.get("class_type", "") in drop_classes}

    out = {}
    for nid, node in graph.items():
        if nid in doomed:
            continue
        inputs = {}
        for field, val in (node.get("inputs") or {}).items():
            inputs[field] = resolve(val) if _is_link(val) else val
        out[nid] = {**node, "inputs": inputs}

    # A dropped loader may still be referenced by a node we kept (an
    # UltralyticsDetectorProvider feeding a detailer that survived because its
    # stage was left on). Put those back rather than shipping a broken graph.
    for _ in range(4):
        needed = set()
        for node in out.values():
            for val in (node.get("inputs") or {}).values():
                if _is_link(val) and val[0] not in out:
                    needed.add(val[0])
        if not needed:
            break
        for nid in needed:
            if nid in graph:
                out[nid] = copy.deepcopy(graph[nid])
    return out


def prune_orphans(graph: dict) -> dict:
    """Drop nodes nothing reaches from an output node.

    Splicing can leave a model loader feeding a detailer that is now gone.
    ComfyUI tolerates unreferenced nodes, but it will happily *load the model*
    for one, which on a 32 GB card is not a rounding error.
    """
    outputs = {nid for nid, n in graph.items()
               if n.get("class_type", "").startswith(("Save", "Preview"))}
    if not outputs:
        return graph
    keep, frontier = set(outputs), list(outputs)
    while frontier:
        nid = frontier.pop()
        for val in (graph.get(nid, {}).get("inputs") or {}).values():
            if _is_link(val) and val[0] not in keep:
                keep.add(val[0])
                frontier.append(val[0])
    return {nid: n for nid, n in graph.items() if nid in keep}


def set_slots(graph: dict, spec: dict, values: dict) -> dict:
    """Write `{{slot}}` markers into the fields a slot owns, then fill them.

    Writing the marker first and substituting after means the bundled graphs
    stay readable as ordinary workflows — you can load one in ComfyUI, see
    real values, and still have CoomKit drive it.
    """
    graph = copy.deepcopy(graph)
    for slot, targets in (spec.get("slots") or {}).items():
        if slot not in values:
            continue
        val = values[slot]
        for nid, field in targets:
            node = graph.get(nid)
            if not node:
                continue
            cur = node["inputs"].get(field)
            if _is_link(cur):
                continue  # driven by another node; leave the wiring alone
            node["inputs"][field] = val
    return graph


def set_at(graph: dict, targets, value) -> None:
    for nid, field in (targets or []):
        if nid in graph and not _is_link(graph[nid]["inputs"].get(field)):
            graph[nid]["inputs"][field] = value


def set_refs(graph: dict, spec: dict, filenames: list) -> dict:
    """Wire N reference images into a ref2v graph and remove the unused slots.

    H3 ships with two `LoadImage` nodes feeding an autogrow input. Handing it
    a slot pointing at a file that was never uploaded fails the whole job, so
    surplus slots are cut rather than left dangling — and the prompt's
    `<Picture N>` labels stay correctly numbered because the refs that remain
    keep their order.
    """
    targets = spec.get("ref_images") or []
    if not targets:
        return graph
    graph = copy.deepcopy(graph)
    slots = (spec.get("slots") or {})
    loaders = [slots.get(k, [[None, None]])[0][0]
               for k in ("image", "image2", "image3") if k in slots]
    for i, (nid, field) in enumerate(targets):
        node = graph.get(nid)
        if not node:
            continue
        if i < len(filenames):
            loader = loaders[i] if i < len(loaders) else None
            if loader and loader in graph:
                graph[loader]["inputs"]["image"] = filenames[i]
        else:
            node["inputs"].pop(field, None)
            loader = loaders[i] if i < len(loaders) else None
            graph.pop(loader, None)
    return graph


def inject_loras(graph: dict, loras: list) -> dict:
    """Insert a portable LoraLoader chain between the loaders and the model.

    Core nodes only, so this works on a bare ComfyUI — unlike the rgthree
    stack the source graphs shipped with, which is why that one gets spliced
    out by default.

    `loras` is [{name, strength, clip_strength?}].

    **Every model source gets its own chain, not just the first.** Wan 2.2 is
    two experts — a high-noise UNETLoader and a low-noise one, sampled in
    sequence — and chaining only the first meant half of the denoising ran
    without the LoRA. That does not fail; it produces a clip that drifts off
    the character halfway through, which is a far more expensive bug than a
    rejection would have been.

    CLIP is the exception and is chained exactly once. The prompt is encoded a
    single time no matter how many model experts consume the result, so
    threading it through a second chain would apply the clip-side strength
    twice.
    """
    loras = [l for l in (loras or []) if str(l.get("name", "")).strip()]
    if not loras:
        return graph
    graph = copy.deepcopy(graph)
    model_srcs = [[nid, 0] for nid, node in graph.items()
                  if node.get("class_type") in ("UNETLoader",
                                                "CheckpointLoaderSimple")]
    clip_src = next(([nid, 0] for nid, node in graph.items()
                     if node.get("class_type") == "CLIPLoader"), None)
    if not model_srcs:
        return graph

    next_id = max((int(k) for k in graph if k.isdigit()), default=0) + 1
    made = set()
    tails = {}          # id(model_src) -> chain tail
    tail_clip = clip_src

    for i, model_src in enumerate(model_srcs):
        tail_model = model_src
        # Only the first chain carries clip; the rest are model-only.
        want_clip = tail_clip is not None and i == 0
        for lora in loras:
            nid = str(next_id)
            next_id += 1
            strength = float(lora.get("strength", 1.0))
            if want_clip:
                graph[nid] = {"class_type": "LoraLoader", "inputs": {
                    "lora_name": lora["name"],
                    "strength_model": strength,
                    "strength_clip": float(lora.get("clip_strength", strength)),
                    "model": tail_model, "clip": tail_clip}}
                tail_model, tail_clip = [nid, 0], [nid, 1]
            else:
                graph[nid] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                    "lora_name": lora["name"],
                    "strength_model": strength,
                    "model": tail_model}}
                tail_model = [nid, 0]
            made.add(nid)
        tails[tuple(model_src)] = tail_model

    # Re-point every original consumer of a raw model/clip at its chain end.
    for nid, node in graph.items():
        if nid in made:
            continue
        for field, val in (node.get("inputs") or {}).items():
            if not _is_link(val):
                continue
            if field == "model" and tuple(val) in tails:
                node["inputs"][field] = tails[tuple(val)]
            elif (field == "clip" and clip_src
                  and list(val) == list(clip_src) and tail_clip != clip_src):
                node["inputs"][field] = tail_clip
    return graph


def build(name: str, values: dict = None, stages: dict = None,
          loras: list = None) -> tuple:
    """Assemble a runnable graph. Returns (graph, meta).

    `stages` is {stage_name: bool}; anything unset falls back to the stage's
    own default. `values` fills the declared slots; a missing seed is rolled.
    """
    spec = BUNDLED[name]
    graph = load(name)
    values = dict(spec.get("defaults") or {}, **(values or {}))
    values.setdefault("seed", random.randint(0, 2 ** 31 - 1))

    # Values go in *before* the splice, not after: a slot often lives on a
    # node that the splice is about to remove. Writing the prompt into the
    # wildcard node first means the bridge carries it into the text encoder,
    # which is exactly the behaviour wanted whether that stage stays or goes.
    graph = set_slots(graph, spec, values)

    wanted = dict(stages or {})
    disabled = []
    for stage_name, stage in STAGES.items():
        on = wanted.get(stage_name, stage.get("default", False))
        if not on:
            graph = splice(graph, stage)
            disabled.append(stage_name)

    for key, at in (("steps", "steps_at"), ("cfg", "cfg_at"),
                    ("fps", "fps_at")):
        if key in values:
            set_at(graph, spec.get(at), values[key])
    if name == "voice-emotion" and values.get("emotions"):
        node = graph.get(spec["emotions_at"])
        if node:
            for k, v in values["emotions"].items():
                if k in spec["emotions"]:
                    node["inputs"][k] = float(v)
    if spec.get("ref_images"):
        graph = set_refs(graph, spec, values.get("refs") or [])
    graph = inject_loras(graph, loras or [])
    graph = prune_orphans(graph)

    meta = {"name": name, "kind": spec["kind"], "label": spec["label"],
            "skill": spec.get("skill", ""), "vram_gb": spec.get("vram_gb", 8),
            "disabled_stages": disabled, "values": values,
            "classes": sorted(classes(graph))}
    return graph, meta


def missing(graph: dict, available: set) -> list:
    """Node classes this graph needs that the server does not have."""
    return sorted(c for c in classes(graph) if c and c not in available)


def catalogue() -> list:
    """Everything shippable, for the UI's workflow picker."""
    out = []
    for name, spec in BUNDLED.items():
        out.append({
            "name": name, "kind": spec["kind"], "label": spec["label"],
            "blurb": spec.get("blurb", ""), "note": spec.get("note", ""),
            "skill": spec.get("skill", ""), "vram_gb": spec.get("vram_gb", 8),
            "needs_image": bool(spec.get("needs_image")),
            "needs_voice": bool(spec.get("needs_voice")),
            "slots": sorted((spec.get("slots") or {}).keys()),
            "defaults": spec.get("defaults", {}),
        })
    return out
