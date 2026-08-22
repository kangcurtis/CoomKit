#!/usr/bin/env python3
"""Studio pipeline: workflow surgery, recipes, planning, output parsing.

Offline and free — no GPU, no ComfyUI, no model. It pins the things that
were actually wrong at some point during the build, which is the only kind
of assertion worth keeping.
"""

import re

import _bootstrap  # noqa: F401  — repo root on sys.path
from _bootstrap import ROOT
import json

import comfy
import recipes
import studio
import voices
import vram
import wfpack

fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ── 1. every bundled workflow builds ───────────────────────────────
print("bundled workflows")
SAMPLE = {"prompt": "SENTINEL-PROMPT", "audio_text": "SENTINEL-SPEECH",
          "music_prompt": "SENTINEL-CAPTION"}
for name in wfpack.BUNDLED:
    try:
        graph, meta = wfpack.build(name, dict(SAMPLE))
        ok = bool(graph) and all("class_type" in n for n in graph.values())
    except Exception as exc:  # noqa: BLE001
        graph, meta, ok = {}, {}, False
        print(f"       {name}: {type(exc).__name__}: {exc}")
    check(f"{name} builds", ok)

# ── 2. the splice-order bug: values must survive stage removal ─────
print("\nslot values survive the splice")
for name, slot, sentinel in (("anima", "prompt", "SENTINEL-PROMPT"),
                             ("krea2", "prompt", "SENTINEL-PROMPT"),
                             ("klein", "prompt", "SENTINEL-PROMPT"),
                             ("h3", "prompt", "SENTINEL-PROMPT"),
                             ("music", "music_prompt", "SENTINEL-CAPTION"),
                             ("voice-design", "audio_text", "SENTINEL-SPEECH")):
    graph, _ = wfpack.build(name, dict(SAMPLE))
    found = any(isinstance(v, str) and sentinel in v
                for n in graph.values() for v in n["inputs"].values())
    check(f"{name}: {slot} reaches a live node", found)

# The wildcard node that carries the prompt is spliced out by default, so
# this is the regression that shipped a graph rendering someone else's demo
# prompt instead of the user's.
graph, _ = wfpack.build("anima", {"prompt": "SENTINEL-PROMPT"})
check("anima: wildcard node really is gone",
      not any(n["class_type"] == "ImpactWildcardProcessor"
              for n in graph.values()))
check("anima: prompt landed on the text encoder",
      any(n["class_type"] == "CLIPTextEncode"
          and n["inputs"].get("text") == "SENTINEL-PROMPT"
          for n in graph.values()))

# ── 3. stages splice out and back in ───────────────────────────────
print("\nstage surgery")
lean, _ = wfpack.build("anima", dict(SAMPLE))
rich, _ = wfpack.build("anima", dict(SAMPLE),
                       stages={"detail": True, "upscale": True})
lean_cls = [n["class_type"] for n in lean.values()]
rich_cls = [n["class_type"] for n in rich.values()]
check("lean graph drops the detailers", "FaceDetailer" not in lean_cls)
check("lean graph drops the upscaler", "UltimateSDUpscale" not in lean_cls)
check("lean graph is core-only",
      not [c for c in lean_cls if c in wfpack.PACK_OF],
      str([c for c in lean_cls if c in wfpack.PACK_OF]))
check("detail stage restores both detailers", rich_cls.count("FaceDetailer") == 2)
check("detail stage restores its detector loaders",
      rich_cls.count("UltralyticsDetectorProvider") == 2)
check("restored detailers keep a live image chain",
      all(not (isinstance(v, list) and len(v) == 2 and v[0] not in rich)
          for n in rich.values() for v in n["inputs"].values()))
check("rich graph is bigger than lean", len(rich) > len(lean))

# No graph may reference a node that is not in it — that is a 400 from
# ComfyUI and the reason prune_orphans has a repair pass.
print("\ndangling references")
for name in wfpack.BUNDLED:
    graph, _ = wfpack.build(name, dict(SAMPLE))
    dangling = [(nid, f, v) for nid, n in graph.items()
                for f, v in n["inputs"].items()
                if isinstance(v, list) and len(v) == 2
                and isinstance(v[0], str) and v[0] not in graph]
    check(f"{name} has no dangling links", not dangling, str(dangling[:2]))

# ── 4. reference images ────────────────────────────────────────────
print("\nreference images")
one, _ = wfpack.build("h3", {"prompt": "x", "refs": ["her.png"]})
two, _ = wfpack.build("h3", {"prompt": "x", "refs": ["her.png", "him.png"]})
check("one ref leaves one LoadImage",
      [n["class_type"] for n in one.values()].count("LoadImage") == 1)
check("two refs leave two LoadImages",
      [n["class_type"] for n in two.values()].count("LoadImage") == 2)
ref_node = [n for n in one.values()
            if n["class_type"] == "MiniMaxH3ReferenceToVideo"][0]
check("unused ref slot is removed, not left dangling",
      "ref_images.ref_image_1" not in ref_node["inputs"])
check("the supplied ref is wired",
      any(n.get("inputs", {}).get("image") == "her.png" for n in one.values()))

# ── 5. LoRA injection uses portable core nodes ─────────────────────
print("\nlora injection")
lora, _ = wfpack.build("anima", dict(SAMPLE),
                       loras=[{"name": "anima-turbo-lora-v0.2.safetensors",
                               "strength": 0.8}])
lora_nodes = [n for n in lora.values()
              if n["class_type"] in ("LoraLoader", "LoraLoaderModelOnly")]
check("a lora node was added", len(lora_nodes) == 1)
check("the lora is a core node, not rgthree",
      lora_nodes and lora_nodes[0]["class_type"] == "LoraLoader")
check("the sampler now runs through the lora",
      any(n["class_type"] == "KSampler"
          and isinstance(n["inputs"].get("model"), list)
          and lora[n["inputs"]["model"][0]]["class_type"] == "LoraLoader"
          for n in lora.values()))

# ── 6. recipes ─────────────────────────────────────────────────────
print("\nrecipes")
CTX = {"char": "Mika", "user": "anon", "appearance": "short black bob",
       "scene": "back of his car", "voice": "female, young adult"}
for rid in recipes.RECIPES:
    text = recipes.fill(rid, {}, CTX)
    check(f"{rid} leaves no unfilled placeholder",
          "{" not in text or not any(
              "{" + p + "}" in text for p in
              ("char", "user", "appearance", "scene", "wardrobe", "pov",
               "explicit", "lewd", "setting", "mirror", "voice")),
          text[:80])
check("moving toggle switches to video",
      recipes.target_kind("blowjob", {"moving": True}) == "video")
check("still stays an image",
      recipes.target_kind("blowjob", {"moving": False}) == "image")
check("asmr ignores the moving toggle",
      recipes.target_kind("asmr", {"moving": True}) == "asmr")
check("pov wording names the user",
      "anon" in recipes.fill("blowjob", {"pov": True}, CTX))

# ── 7. planning ────────────────────────────────────────────────────
print("\nplanning")
CFG = {"comfyui_url": "http://x", "studio": {"image": "krea2", "video": "h3"}}
CHAR = {"id": 7, "name": "Mika", "avatar": "mika.png",
        "data": {"visual": {"model": "anima", "seed": 99},
                 "voice": {"instruct": "female"}}}
PERS = {"id": 1, "name": "anon",
        "data": {"refs": [{"kind": "cock", "file": "c.png"}]}}
p = studio.plan("selfie", {}, {}, CFG, CHAR, PERS)
check("character's model beats the global default", p["workflow"] == "anima")
check("pinned seed is carried into values", p["values"].get("seed") == 99)
p = studio.plan("selfie", {}, {}, CFG, {"id": 1, "data": {}}, PERS)
check("without an override the global default wins", p["workflow"] == "krea2")

p = studio.plan("blowjob", {"pov": True, "moving": True}, {}, CFG, CHAR, PERS)
check("pov video picks the ref2v workflow", p["workflow"] == "h3")
# The prop leads. H3 weights <Picture 1> hardest and the anatomy is the part
# it invents worst, so that slot is not hers.
check("his picture is ref 1", p["refs"][0]["label"] == "cock")
check("her picture is ref 2", len(p["refs"]) == 2
      and p["refs"][1]["label"] == "her")
p = studio.plan("blowjob", {"pov": False, "moving": True}, {}, CFG, CHAR, PERS)
check("the reference is not gated on pov — the shot that needs it most is "
      "the one looking at the camera",
      [r["label"] for r in p["refs"]] == ["cock", "her"])
p = studio.plan("scene", {"moving": True}, {}, CFG, CHAR, PERS)
check("a recipe that never asked for it does not send his photo",
      all(r["label"] != "cock" for r in p["refs"]))
p = studio.plan("blowjob", {"moving": True}, {}, CFG, CHAR,
                {"id": 9, "name": "anon", "data": {}})
check("with no reference on file she is Picture 1 again",
      [r["label"] for r in p["refs"]] == ["her"])

# The writer never sees the pictures, so the labels have to be spelled out —
# and generated from the real list, or they are wrong exactly when the
# persona has no photo.
job = studio.plan("blowjob", {"moving": True}, CTX, CFG, CHAR, PERS)
clause = studio.refs_clause(job, job["ctx"])
check("the writer is told what Picture 1 is", "<Picture 1> is anon's cock" in clause)
check("the writer is told what Picture 2 is", "<Picture 2> is Mika" in clause)
solo = studio.plan("blowjob", {"moving": True}, CTX, CFG, CHAR,
                   {"id": 9, "name": "anon", "data": {}})
check("with one reference she is Picture 1 in the clause too",
      "<Picture 1> is Mika" in studio.refs_clause(solo, solo["ctx"]))
check("and no second picture is invented",
      "<Picture 2>" not in studio.refs_clause(solo, solo["ctx"]))

novoice = {"id": 2, "name": "X", "data": {}}
check("no voice config still clones — from a shipped reference",
      studio.pick_workflow("tts", CFG, novoice) == "voice-clone")
check("and that reference is the shipped default",
      studio.plan("speak", {}, {}, CFG, novoice, None)["voice_preset"]
      == voices.DEFAULT)
optout = {"id": 2, "name": "X", "data": {"voice": {"preset": "none"}}}
check("opting out is what selects a designed voice",
      studio.pick_workflow("tts", CFG, optout) == "voice-design")
withvoice = {"id": 2, "name": "X", "data": {"voice": {"sample": "s.mp3"}}}
check("a voice sample means cloning",
      studio.pick_workflow("tts", CFG, withvoice) == "voice-clone")
emo = {"id": 2, "name": "X",
       "data": {"voice": {"sample": "s.mp3", "engine": "emotion"}}}
check("engine=emotion selects IndexTTS-2",
      studio.pick_workflow("tts", CFG, emo) == "voice-emotion")

# ── 8. parsing the writer's reply ──────────────────────────────────
print("\nwriter output")
img = studio.parse_writer({"json_output": False, "kind": "image"},
                          "```\n1girl, solo\n```")
check("a fence the model added anyway is stripped",
      img["prompt"] == "1girl, solo", repr(img))
_VOICE_JSON = ('noise\n```json\n{"text": "come here", '
               '"voice_instruct": "female, whisper", "instruct": "female, whisper", '
               '"speed": 1.0, "ambience": "steady continuous rain"}\n```')
voice = studio.parse_writer(
    {"json_output": True, "kind": "asmr", "workflow": "voice-design"}, _VOICE_JSON)
check("voice json parses", voice.get("audio_text") == "come here")
check("voice delivery survives", voice.get("voice_instruct") == "female, whisper")
check("ambience survives", "rain" in voice.get("ambience", ""))

# skills/voice.md carries worked ASMR examples, so a writer reading it will
# happily put `ambience` on a plain clone job — which has no such slot.
# _fit_slots dropped them silently, but only AFTER the approval card had
# offered them as editable fields the user could waste time on.
clone = studio.parse_writer(
    {"json_output": True, "kind": "tts", "workflow": "voice-clone"}, _VOICE_JSON)
check("clone keeps what it has a slot for", clone.get("instruct") == "female, whisper")
check("clone drops ambience it cannot use", "ambience" not in clone, repr(clone))
check("clone drops voice_instruct it cannot use", "voice_instruct" not in clone,
      repr(clone))

# an underspecified job must not have its values swallowed
loose = studio.parse_writer({"json_output": True, "kind": "asmr"}, _VOICE_JSON)
check("unknown workflow stays permissive", "ambience" in loose, repr(loose))

# "say it out loud" never reaches the writer at all
spoken = studio.speak_values({"workflow": "voice-clone"}, "Come here.")
check("speak is just her words", spoken == {"audio_text": "Come here."}, repr(spoken))
# A model that ignores the JSON contract must not lose the take.
prose = studio.parse_writer({"json_output": True, "kind": "asmr"},
                            "just come over here")
check("prose falls back to being the spoken line",
      prose.get("audio_text") == "just come over here")
mus = studio.parse_writer({"json_output": True, "kind": "music"},
                          '{"caption": "Global Metadata\\n…", "lyrics": "[Verse]\\nhi"}')
check("music caption parses", "Global Metadata" in mus.get("music_prompt", ""))
check("music lyrics parse", "[Verse]" in mus.get("lyrics", ""))

# ── 9. review catches the expensive mistakes ───────────────────────
print("\npre-flight review")
notes = studio.review({"kind": "asmr"},
                      {"voice_instruct": "female, sultry, low pitch, whisper",
                       "speed": 0.85, "ambience": "she shifts on the bed"})
joined = " ".join(notes)
check("rejects a word outside the closed vocabulary", "sultry" in joined)
check("flags whisper at reduced speed", "0" in joined and "whisper" in joined)
check("flags low pitch cancelling whisper", "cancels" in joined)
check("flags ambience written as an event", "texture" in joined)
check("a clean job reviews clean",
      not studio.review({"kind": "asmr"},
                        {"voice_instruct": "female, young adult, whisper",
                         "speed": 1.0,
                         "ambience": "steady continuous rain, constant"}))

# ── 10. dialogue extraction for the speak recipe ───────────────────
print("\ndialogue extraction")
check("quotes win over narration",
      studio.dialogue_lines('*she leans in* "You took your time." *a pause*')
      == "You took your time.")
check("multiple quotes are kept in order",
      studio.dialogue_lines('"One." then "Two."') == "One.\nTwo.")
check("smart quotes are handled",
      "Hello" in studio.dialogue_lines('“Hello”'))
check("without quotes, asterisk actions are stripped",
      studio.dialogue_lines("*shifts closer* come here then")
      == "come here then")

# ── 11. comfy failure reporting ────────────────────────────────────
print("\ncomfy diagnostics")
check("mp4 under the images bucket is still a video",
      comfy.kind_of("clip_00001_.mp4", "images") == "video")
check("flac is audio", comfy.kind_of("a.flac", "audio") == "audio")
check("png is an image", comfy.kind_of("a.png", "images") == "image")
err = comfy._failure({"status": {"status_str": "error", "messages": [
    ["execution_error", {"node_type": "OmniVoiceVoiceDesignTTS",
                         "exception_message": "Unsupported instruct items"}]]}})
check("a failed job names the node", "OmniVoiceVoiceDesignTTS" in err)
check("a failed job quotes the reason", "Unsupported instruct" in err)
oom = comfy._failure({"status": {"status_str": "error", "messages": [
    ["execution_error", {"node_type": "KSampler", "exception_type":
                         "torch.OutOfMemoryError", "exception_message": "CUDA"}]]}})
check("OOM is called OOM and suggests the fix", "VRAM management" in oom)
check("a healthy job reports no failure",
      comfy._failure({"status": {"status_str": "success"}}) == "")

# ── 12. vram settings ──────────────────────────────────────────────
print("\nvram policy")
check("off is the shipped default", vram.settings({})["policy"] == "off")
check("user config overrides the default",
      vram.settings({"vram": {"policy": "auto"}})["policy"] == "auto")
check("unset keys keep their defaults",
      vram.settings({"vram": {"policy": "auto"}})["headroom_gb"] == 2.0)
check("policy off short-circuits before touching anything",
      vram.make_room({"vram": {"policy": "off"}}, "", 99)["acted"] is False)

# ── 13. bundled voices ─────────────────────────────────────────────
print("\nbundled voices")
shipped = voices.available()
check("at least one voice ships", bool(shipped))
check("every shipped voice has audio on disk",
      all(voices.path_for(v["name"]) for v in shipped))
check("every shipped voice is credited",
      all(v["credit"] for v in shipped))
# Below ~180 Hz OmniVoice can drop the clone an octave into a male voice.
check("no shipped reference sits in the octave-collapse band",
      all(v["f0_hz"] >= 185 for v in shipped),
      str([(v["name"], v["f0_hz"]) for v in shipped if v["f0_hz"] < 185]))
check("a character's own clip beats a preset",
      voices.resolve({"sample": "mine.wav", "preset": "natural-warm"})
      == ("asset", "mine.wav"))
check("an explicit none means describe it instead",
      voices.resolve({"preset": "none"}) == ("none", ""))
check("an unknown preset falls back rather than failing",
      voices.resolve({"preset": "does-not-exist"})[0] == "preset")

# Reusing an upload name serves ComfyUI's cached copy of the OLD bytes —
# measured as a 187 Hz clone turning into a 78 Hz man.
a = studio._stamped("coomkit_voice_x", b"one", ".wav")
b = studio._stamped("coomkit_voice_x", b"two", ".wav")
check("changed audio gets a different upload name", a != b)
check("unchanged audio keeps its name",
      a == studio._stamped("coomkit_voice_x", b"one", ".wav"))
check("the extension survives", a.endswith(".wav"))

# A preset is read off disk, so it must not be gated behind asset_path — when
# it was, nothing uploaded and the graph kept its own built-in reference
# (sample-6.mp3, a man at 78 Hz). Every archetype voice came out as him.
CFGV = {"comfyui_url": "http://x"}
pj = studio.plan("speak", {}, {}, CFGV,
                 {"id": 1, "data": {"voice": {"preset": "brat"}}}, None)
check("a preset job carries no asset and no refs",
      not pj["voice_sample"] and not pj["refs"])
check("...so run() must still upload something for it",
      bool(pj["voice_preset"]))
check("a preset pins the speed it was rendered at",
      pj["values"].get("speed") == voices.PRESETS["brat"]["speed"])
check("her own speed beats the preset's",
      studio.plan("speak", {}, {}, CFGV,
                  {"id": 1, "data": {"voice": {"preset": "brat", "speed": 1.1}}},
                  None)["values"].get("speed") == 1.1)
# The writer returns "speed": 1.0 because that is the obvious default, which
# threw away the 0.85 a brat voice was rendered at.
pinned = studio.apply_pins({"values": {"speed": 0.85}},
                           {"audio_text": "hi", "speed": 1.0})
check("a pinned speed survives the writer's guess", pinned["speed"] == 0.85)
check("the writer's other fields are kept", pinned["audio_text"] == "hi")
check("nothing is invented when nothing is pinned",
      "speed" not in studio.apply_pins({"values": {}}, {"audio_text": "hi"}))

# ── 14. asmr in her own voice ──────────────────────────────────────
print("\nasmr routing and ambience")
CFGA = {"comfyui_url": "http://x"}
voiced = {"id": 1, "data": {"voice": {"preset": "onee-san"}}}
described = {"id": 1, "data": {"voice": {"preset": "none"}}}
check("a character with a voice gets the cloned ASMR graph",
      studio.pick_workflow("asmr", CFGA, voiced) == "asmr-clone")
check("without one it falls back to a designed voice",
      studio.pick_workflow("asmr", CFGA, described) == "voice-design")
ap = studio.plan("asmr", {"ambience": "mouth", "seconds": 45}, {}, CFGA,
                 voiced, None)
check("the chosen bed reaches the graph",
      "wet mouth sounds" in ap["values"].get("ambience", ""))
check("the clone graph still gets a reference", bool(ap["voice_preset"]))
# EmptyLatentAudio maxes out at 47.6s; a longer request must clamp, not fail.
long = studio.plan("asmr", {"ambience": "nails", "seconds": 90}, {}, CFGA,
                   voiced, None)
check("an over-long bed is clamped to the model ceiling",
      long["values"]["ambience_seconds"] <= 47.0)
# Every bed must be phrased as a texture or Stable Audio emits sporadic bangs.
stationary = ("steady", "continuous", "constant", "unchanging")
for name, text in recipes.AMBIENCE.items():
    if not text:
        continue
    check(f"ambience '{name}' is phrased as a texture",
          any(w in text for w in stationary), text[:60])

print("\nsong length")
sp = studio.plan("song", {"seconds": 210}, {}, CFGA, voiced, None)
check("the requested length reaches the graph",
      sp["values"].get("duration") == 210.0)
check("a pinned duration beats the writer's guess",
      studio.apply_pins({"values": {"duration": 210.0}},
                        {"music_prompt": "x", "duration": 45})["duration"]
      == 210.0)

print("\npath-aware review")
# The whisper/speed finding was measured on Voice Design. On a clone the
# reference carries the delivery, so firing it there is noise.
check("no whisper/speed warning on the clone path",
      not any("destroys a whisper" in n for n in
              studio.review({"workflow": "asmr-clone"},
                            {"instruct": "female, whisper", "speed": 0.82})))
check("but it does warn on the design path",
      any("destroys a whisper" in n for n in
          studio.review({"workflow": "voice-design"},
                        {"voice_instruct": "female, whisper", "speed": 0.85})))
check("the closed vocabulary is enforced on both paths",
      any("reject" in n for n in
          studio.review({"workflow": "asmr-clone"},
                        {"instruct": "female, sultry"})))

# ── non-verbal tags: an invented one is SPOKEN, not ignored ────────
# The tags are not special tokens — verified against the shipped tokenizer,
# where none of them is a token or even a single vocab entry. They are plain
# text the model was trained to perform, so one it does not know comes out of
# her mouth as the word. That is the reported bug, and it is why this warns.
print("\nnon-verbal tags")
def tagnote(text):
    return " ".join(n for n in studio.review(
        {"workflow": "asmr-clone"}, {"audio_text": text}) if "SPOKEN" in n)

check("an invented tag is caught", "[moan]" in tagnote("mm [moan] yes"))
check("several are named, not just the first",
      all(t in tagnote("[moan] a [gasp] b") for t in ("[moan]", "[gasp]")))
check("every real tag passes",
      not tagnote(" ".join(sorted(studio.NON_VERBAL_TAGS))),
      tagnote(" ".join(sorted(studio.NON_VERBAL_TAGS))))
check("case does not matter", not tagnote("[SIGH] hello"))
check("the multi-speaker format is not a tag",
      not tagnote("[Speaker_1]: hi [Speaker_2]: hello"))
check("plain prose is left alone", not tagnote("no brackets here at all"))
check("the warning says what will happen, not just that it is wrong",
      "SPOKEN" in tagnote("[wet]") and "[sigh]" in tagnote("[wet]"))
# The two tags CoomKit's dialect used to omit are real and must not warn.
check("[question-ei] and [question-yi] are real tags",
      not tagnote("[question-ei] hm [question-yi] hm"))

# ── speed is offered only where the graph has the slot ─────────────
# The remake dialog shows its speed picker off the server's `speed` key, and
# the server derives that from the workflow's slots. Offering a control that
# silently does nothing is worse than not offering one.
print("\nspeed slots")
for wf in ("voice-clone", "voice-design", "asmr-clone"):
    check(f"{wf} exposes speed",
          "speed" in (wfpack.BUNDLED[wf].get("slots") or {}))
check("voice-emotion does NOT (IndexTTS-2 has no speed input)",
      "speed" not in (wfpack.BUNDLED["voice-emotion"].get("slots") or {}))

# ── 15. tags, artists and dialect gating ───────────────────────────
print("\ntags and artist blending")
import tags as _tags  # noqa: E402
sets = _tags.tagsets()
check("the curated tag sets ship", len(sets) >= 15)
check("every set has tags", all(s.get("tags") for s in sets))

# Underscores are Danbooru's storage form; parens are weighting syntax in
# booru-lineage samplers, so an artist name containing them must stay escaped
# or `kouji_(campus_life)` silently becomes 1.1x on two unrelated words.
check("underscores become spaces", _tags._pretty("blue_eyes") == "blue eyes")
check("parens are escaped",
      _tags._pretty("kouji_(campus_life)") == r"kouji \(campus life\)")
check("the clause is prefixed with 'by'",
      _tags.artist_clause([{"prompt": "kantoku"}]) == "by kantoku")
check("an empty artist list makes no clause", _tags.artist_clause([]) == "")

# The bundled corpus. It is redistributed third-party tag data, and the export
# it was cut from had several hundred lines of the author's own prompt library
# appended to the end by hand — so "every row is name,int,int" is not tidiness,
# it is the check that stops a private document shipping in a public repo.
if _tags.BUNDLED_DB.exists():
    import csv as _csv
    import gzip as _gzip
    with _gzip.open(_tags.BUNDLED_DB, "rt", encoding="utf-8") as fh:
        corpus = list(_csv.reader(fh))
    bad = []
    for row in corpus:
        if len(row) < 3:
            bad.append(row)
            continue
        try:
            int(row[1]), int(row[2])
        except ValueError:
            bad.append(row)
    check("every bundled corpus row is name,category,count", not bad)
    # `", "` is the real discriminator — a prompt line is comma-and-space
    # separated and a tag never is. Length is only a backstop, and the ceiling
    # has to clear the longest real tag: a 111-character light-novel
    # copyright tag is genuine Danbooru data, not someone's prompt.
    check("no row's tag name looks like a prompt",
          not [r for r in corpus if ", " in r[0] or len(r[0]) > 160])
    check("the corpus is big enough to be worth shipping", len(corpus) > 100000)
    check("it carries the artist tags the blender needs",
          sum(1 for r in corpus if r[1] == "1") > 50000)

# find_db precedence: an explicit path wins, a broken one degrades to the
# bundle rather than disabling the picker, and it says so.
p_path, source, problem = _tags.locate({})
check("a database is always found now", p_path is not None)
_, src2, prob2 = _tags.locate({"tags_db": "/definitely/not/here.csv"})
check("a broken tags_db still finds a corpus", src2 != "none")
check("...and reports the broken path", "not exist" in prob2)

# artist_mode 'random' must actually be random. It was seeded with the
# character's pinned image seed, so it returned the same pair forever while
# the UI promised a reroll every time.
_rolls = {tuple(a["tag"] for a in _tags.resolve_artists(
    {"artist_mode": "random", "artist_count": 2})) for _ in range(6)}
check("random artists actually reroll", len(_rolls) > 1)

CFGT = {"comfyui_url": "http://x"}
pinned = [{"tag": "kantoku", "prompt": "kantoku"},
          {"tag": "kouji_(campus_life)", "prompt": r"kouji \(campus life\)"}]
anime = {"id": 1, "data": {"visual": {"model": "anima",
                                      "artist_mode": "pinned",
                                      "artists": pinned}}}
prose = {"id": 1, "data": {"visual": {"model": "krea2",
                                      "artist_mode": "pinned",
                                      "artists": pinned}}}
check("anima is the only tag-dialect workflow",
      [n for n, spec in wfpack.BUNDLED.items() if spec.get("tag_dialect")]
      == ["anima"])
check("a tag-dialect model resolves artists",
      len(studio.plan("solo-model", {}, {}, CFGT, anime, None)["artists"]) == 2)
check("a prose model gets none, however she is configured",
      studio.plan("solo-model", {}, {}, CFGT, prose, None)["artists"] == [])

job = {"artists": pinned}
# The writer drops them often enough that hoping is not a strategy.
put = studio.ensure_artists(
    job, {"prompt": "masterpiece, best quality, safe, 1girl, solo"})
check("dropped artists are put back", "kantoku" in put["prompt"])
check("...after the quality preamble, where anima.md puts them",
      put["prompt"].index("kantoku") > put["prompt"].index("best quality"))
# And when it keeps them but strips the backslashes.
kept = studio.ensure_artists(
    job, {"prompt": "masterpiece, safe, kantoku, kouji (campus life), 1girl"})
check("stripped paren escapes are restored",
      r"kouji \(campus life\)" in kept["prompt"])
check("nothing is injected for a prose model",
      studio.ensure_artists({"artists": []},
                            {"prompt": "a photo"})["prompt"] == "a photo")

# ── 16. character forge ────────────────────────────────────────────
print("\ncharacter forge")
import chargen as _cg  # noqa: E402
VOICES = ["brat", "onee-san", "mommy"]
MODELS = ["anima", "krea2", "klein"]
PERSONA = {"name": "anon", "data": {"description": "29, works nights",
                                    "into": "brats who don't back down"}}
sheet = _cg.persona_sheet(PERSONA, "somewhere at 3am")
check("the persona reaches the prompt", "anon" in sheet)
check("what they're into is used as design input",
      "brats who don't back down" in sheet and "design input" in sheet)
check("a brief is stated as a hard requirement",
      "hard requirement" in sheet)
check("no persona degrades without assuming anything",
      "do not assume" in _cg.persona_sheet(None))

msgs = _cg.build_pitch_messages(PERSONA, "x", 3, voices=VOICES, models=MODELS)
check("valid ids are listed for the model to choose from",
      "brat" in msgs[1]["content"] and "krea2" in msgs[1]["content"])
jb = _cg.build_pitch_messages(PERSONA, "", 2, voices=VOICES, models=MODELS,
                              jailbreak="JAILBREAK-SENTINEL")
check("a preset's jailbreak reaches the system prompt — remote models need it",
      jb[0]["content"].startswith("JAILBREAK-SENTINEL"))

# A hallucinated voice or model id would only fail at generation time.
bad = _cg.parse_pitches(
    '{"characters":[{"name":"X","description":"d","voice":"sultry",'
    '"model":"dall-e"}]}', VOICES, MODELS)
check("an invented voice id is pinned to a real one", bad[0]["voice"] == "brat")
check("an invented model id is pinned to a real one", bad[0]["model"] == "anima")
check("a pitch with no name is dropped",
      _cg.parse_pitches('{"characters":[{"description":"d"}]}', VOICES, MODELS) == [])
check("prose around the JSON is tolerated",
      len(_cg.parse_pitches('sure!\n```json\n{"characters":[{"name":"A",'
                            '"description":"d"}]}\n```', VOICES, MODELS)) == 1)
check("appearance falls back rather than being empty",
      bool(_cg.parse_pitches('{"characters":[{"name":"A","description":"a tall woman"}]}',
                             VOICES, MODELS)[0]["appearance"]))

card = _cg.to_card({"name": "A", "description": "d", "personality": "p",
                    "first_mes": "hi", "appearance": "tall", "voice": "brat",
                    "model": "anima", "tagline": "t", "for_you": "f",
                    "seed": 42})
check("a pitch becomes a v3 card", card["spec"] == "chara_card_v3")
check("card fields land where the editor expects them",
      card["fields"]["description"] == "d" and card["fields"]["name"] == "A")
check("the seed is pinned at creation", card["visual"]["seed"] == 42)
check("her voice comes with her", card["voice"]["preset"] == "brat")
check("the tagline and the why survive as creator notes",
      "t" in card["fields"]["creator_notes"] and "f" in card["fields"]["creator_notes"])

# ── 17. example dialogue ───────────────────────────────────────────
print("\nexample dialogue")
import engine as _eng  # noqa: E402
EX = ("<START>\n{{user}}: You're late.\n{{char}}: *doesn't look up* Observant."
      "\n<START>\n{{user}}: Refill?\n{{char}}: You can *ask* for a refill.")
turns = _eng.parse_examples(EX, "Mika", "anon")
check("both exchanges parse", len(turns) == 4)
check("roles alternate correctly",
      [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"])
check("the speaker label is stripped",
      turns[1]["content"] == "*doesn't look up* Observant.")
# Labels may be raw macros or already-resolved names depending on when
# substitution ran, so both have to work.
check("resolved names are recognised too",
      len(_eng.parse_examples("anon: hey\nMika: what.", "Mika", "anon")) == 2)
check("<START> is optional",
      len(_eng.parse_examples("anon: hey\nMika: what.", "Mika", "anon")) == 2)
check("an unlabelled block is treated as her speaking",
      _eng.parse_examples("She glares.", "Mika", "anon")[0]["role"] == "assistant")
check("empty input yields nothing", _eng.parse_examples("", "M", "a") == [])

built = _eng.build_examples(EX, "Mika", "anon", "[HDR]")
check("a header leads the block", built[0]["role"] == "system")
check("the header text is used", built[0]["content"] == "[HDR]")
# Ending on the user's line reads as a question she left hanging.
check("never ends on the user's turn", built[-1]["role"] == "assistant")
tail = _eng.build_examples("anon: only me talking", "Mika", "anon", "[HDR]")
check("a user-only example yields nothing rather than a dangling question",
      tail == [])
# The cap is what stops example dialogue eating the history budget.
big = "<START>\n" + "\n".join(f"anon: q{i}\nMika: {'word ' * 80}" for i in range(20))
capped = _eng.build_examples(big, "Mika", "anon", "[HDR]", cap_tokens=200)
check("the cap is respected",
      sum(_eng.rough_tokens(m["content"]) for m in capped[1:]) <= 400,
      str(sum(_eng.rough_tokens(m["content"]) for m in capped[1:])))
check("something still survives the cap", len(capped) > 1)

# ── 18. memory lifecycle ───────────────────────────────────────────
print("\nmemory lifecycle")
import memory as _mem  # noqa: E402
import sqlite3 as _sq  # noqa: E402

# Similarity is what stops the same fact being stored twice in slightly
# different words. Observed on a real log: "The user is called anon." four
# times over, because each turn's extractor raced the last one.
check("identical text is a duplicate",
      _mem.similarity("The user is called anon.", "The user is called anon.") == 1.0)
check("a restatement is caught",
      _mem.similarity("They kissed in the lab once.", "They kissed in the lab.") > 0.6)
check("tense changes are caught",
      _mem.similarity("She mocks him for visiting",
                      "She mocked him for visiting") > 0.6)
check("unrelated facts are not merged",
      _mem.similarity("She wears his shirt", "He owns a cat named Widget") < 0.3)

check("extraction is not every turn", not _mem.should_extract(3, 4))
check("...but does happen on the Nth", _mem.should_extract(4, 4))
check("every_n of 1 means every turn", _mem.should_extract(1, 1))

# Ranking: who you are outlives what she is wearing, and what is being talked
# about beats what is old.
mems = [{"kind": "chat", "content": "she is by the centrifuge", "updated": 1},
        {"kind": "user", "content": "the user works nights", "updated": 1},
        {"kind": "character", "content": "they kissed in the lab", "updated": 2}]
order = [m["kind"] for m in _mem.rank(mems)]
check("user scope ranks first", order[0] == "user")
check("chat scope ranks last", order[-1] == "chat")
top = _mem.rank(mems, "tell me about the centrifuge again")[0]
check("relevance lifts a matching memory within its scope",
      _mem.rank([mems[0], {"kind": "chat", "content": "unrelated thing",
                           "updated": 9}],
                "centrifuge")[0]["content"] == "she is by the centrifuge")

big = [{"kind": "chat", "content": "x" * 200, "updated": i} for i in range(20)]
check("the injection cap is enforced",
      len(_mem.budget(big, max_items=5, token_budget=9999, chat_keep=99)) == 5)
check("the token budget is enforced",
      sum(len(m["content"]) // 4 for m in
          _mem.budget(big, 99, token_budget=100, chat_keep=99)) <= 150)
check("scene furniture is capped hardest",
      len(_mem.budget(big, 99, 9999, chat_keep=3)) == 3)

# Write-time dedup, against the database rather than a stale snapshot.
conn = _sq.connect(":memory:")
conn.row_factory = _sq.Row
conn.execute("""CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, character_id INTEGER, kind TEXT, content TEXT,
    created REAL, updated REAL, persona_id INTEGER)""")
_mem.store_memories(conn, 1, 1, [{"scope": "user", "content": "The user is called anon."}])
_mem.store_memories(conn, 1, 1, [{"scope": "user", "content": "The user is called anon."}])
_mem.store_memories(conn, 1, 1, [{"scope": "user", "content": "the user is called anon"}])
n = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
check("the same fact is stored once, however it is phrased", n == 1, f"{n} rows")
_mem.store_memories(conn, 1, 1, [{"scope": "user", "content": "The user has a cat."}])
check("a genuinely new fact still lands",
      conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 2)
# Same sentence, different scope, is a different claim about durability.
_mem.store_memories(conn, 1, 1, [{"scope": "chat", "content": "The user has a cat."}])
check("scopes are deduplicated separately",
      conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 3)

# And the repair pass for data written before any of this existed.
conn2 = _sq.connect(":memory:")
conn2.row_factory = _sq.Row
conn2.execute("""CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, character_id INTEGER, kind TEXT, content TEXT,
    created REAL, updated REAL, persona_id INTEGER)""")
for i in range(4):
    conn2.execute("INSERT INTO memories (kind, content, created, updated)"
                  " VALUES ('user','The user is called anon.',1,?)", (i,))
conn2.execute("INSERT INTO memories (kind, content, created, updated)"
              " VALUES ('user','The user has a cat.',1,1)")
res = _mem.dedupe_existing(conn2)
check("tidy collapses existing duplicates", res["removed"] == 3, str(res))
check("...and keeps the distinct one",
      conn2.execute("SELECT count(*) FROM memories").fetchone()[0] == 2)
check("the surviving row keeps the newest timestamp",
      conn2.execute("SELECT max(updated) FROM memories WHERE content LIKE 'The user is called%'"
                    ).fetchone()[0] == 3)

# Consolidation must compress, never quietly discard.
check("a 'merge' that grew is rejected",
      _mem.consolidate(lambda m: '{"facts":["a","b","c","d","e","f"]}',
                       [{"content": "a"}, {"content": "b"}, {"content": "c"},
                        {"content": "d"}]) == [])
check("a merge that threw everything away is rejected",
      _mem.consolidate(lambda m: '{"facts":["a"]}',
                       [{"content": str(i)} for i in range(20)]) == [])
check("a real merge is accepted",
      _mem.consolidate(lambda m: '{"facts":["they grew close","she teases him"]}',
                       [{"content": str(i)} for i in range(8)])
      == ["they grew close", "she teases him"])

print()
if fails:
    print(f"STUDIO TESTS FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
# ── no shipped-voice id may be hardcoded in the frontend ──────────────
# The picker carried 'female-bright' for two commits after the shipped voices
# were renamed to archetypes. It matched no <option>, so the select rendered
# blank on every character that had never had a voice set — and server-side
# voices.resolve() fell through to DEFAULT, so a button labelled one voice
# produced a different one. Nothing noticed, because test_frontend only knows
# about element ids and /api paths.
_app = (ROOT / "web" / "app.js").read_text()
_live = set(voices.PRESETS)
for _m in re.finditer(r"""preset:\s*['"]([a-z0-9-]+)['"]""", _app):
    assert _m.group(1) in _live, (
        f"web/app.js hardcodes voice preset {_m.group(1)!r}, which is not a "
        f"shipped voice ({sorted(_live)}). Read it from /api/studio's "
        f"voice_default instead.")
assert "voice_default" in (ROOT / "server.py").read_text(), \
    "the client needs voices.DEFAULT served, not guessed"
print("  ok   no dead voice ids hardcoded in the frontend")

# ── the free-form recipe ──────────────────────────────────────────────
check("ten recipes", len(recipes.RECIPES) == 10, len(recipes.RECIPES))
check("describe declares what it cannot work without",
      recipes.RECIPES["describe"]["requires"] == ["brief_text"])
# an explicit kind is honoured, and anything unknown falls back to media[0] —
# which is deliberately the CHEAP mistake: an image is ~12s, H3 is 453s.
for opts, want in (({"kind": "video"}, "video"), ({"kind": "music"}, "music"),
                   ({"kind": "asmr"}, "asmr"), ({"kind": "nope"}, "image"),
                   ({}, "image")):
    check(f"target_kind {opts} -> {want}",
          recipes.target_kind("describe", opts) == want,
          recipes.target_kind("describe", opts))
check("a free-form video does NOT drag in the persona's anatomy refs",
      not recipes.RECIPES["describe"].get("wants_refs"))
_layers = recipes.prompt_layers()
check("its brief is editable like every other injected one",
      "recipe_describe" in _layers)
check("catalogue exposes requires",
      [c for c in recipes.catalogue() if c["id"] == "describe"][0]["requires"]
      == ["brief_text"])

# ── the VRAM broker hands the model back the way it found it ─────────
# The user's question was whether context survives a park. It does, and always
# did. What did NOT survive: the quant variant, parallelism, the TTL, and the
# offload ratio — which was actively OVERWRITTEN by a hardcoded --gpu max.
_argv = {}
_real_run = vram._run
try:
    vram._run = lambda cmd, timeout=0: (_argv.update(cmd=cmd), (0, ""))[1]
    vram._reload_llm({"driver": "lmstudio",
                      "model": "google/gemma-4-12b-qat@q4_0",
                      "identifier": "google/gemma-4-12b-qat",
                      "context": 15616, "ttl_s": 900, "parallel": 4},
                     {"lms_bin": "lms", "load_timeout_s": 300})
finally:
    vram._run = _real_run
_cmd = " ".join(_argv["cmd"])
check("reload replays the exact context", "-c 15616" in _cmd, _cmd)
check("reload pins the quant variant", "@q4_0" in _cmd, _cmd)
check("reload replays parallelism", "--parallel 4" in _cmd, _cmd)
check("reload replays the ttl in SECONDS", "--ttl 900" in _cmd, _cmd)
check("reload does NOT force an offload ratio", "--gpu" not in _cmd, _cmd)

# ── a workflow override must be for the right KIND ───────────────────────
# studio.plan(workflow=) exists so a portrait can be re-rolled on another image
# model. Without a kind check an image recipe could be sent to a VIDEO graph:
# _fit_slots drops every slot the graph does not declare rather than rejecting
# it, so width and height vanish and h3 renders the demo clip it shipped with.
# The caller then files an .mp4 as somebody's avatar.
_ctx = {"char": "X", "user": "anon", "appearance": "", "scene": "", "setting": ""}
_cfg = {"comfyui_url": "http://127.0.0.1:8188"}
for _bad in ("h3", "wan-t2v", "music", "voice-clone", "nonsense", ""):
    _job = studio.plan("solo-model", {"wardrobe": "clothed"}, _ctx, _cfg,
                       None, None, workflow=_bad)
    assert wfpack.BUNDLED[_job["workflow"]].get("kind") == "image", \
        f"{_bad!r} override produced a {_job['workflow']} graph for an image recipe"
_job = studio.plan("solo-model", {"wardrobe": "clothed"}, _ctx, _cfg,
                   None, None, workflow="anima")
assert _job["workflow"] == "anima", "a same-kind override must be honoured"
print("workflow override: kind enforced, same-kind honoured")


print("STUDIO PIPELINE TESTS PASS")
