#!/usr/bin/env python3
"""CFTF — "card for that feel": build a character from a picture.

The feature is one vision call wrapped in the existing forge, so most of what
can break is invisible at runtime rather than loud:

  1. The picture must actually reach the model as an image part. A message
     that quietly degrades to text still returns three lovely characters —
     they just have nothing to do with the photograph, and there is no symptom
     beyond "the pitches are a bit generic".
  2. The local-only rule must hold at the ROUTE, before any request is built.
     Vision is local-only by construction everywhere else in CoomKit and this
     is the one path where the usual in-band degrade is not available: a pitch
     built from an image nobody saw is indistinguishable from the feature
     working.
  3. The two hard rules in the shipped prompt — adults only, and never
     identify a real person — are content policy, not taste. The layer is
     user-editable, so what is pinned here is the DEFAULT carrying them.
  4. The refactor that made one renderer serve both forge modes must not have
     changed the chat path's vision message by a single key.

Offline apart from the route section, which needs the server up but never a
model. Free either way.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import base64
import sys

import testkit  # noqa: F401  — registers the fixture sweep at exit
from testkit import BLANK_PNG, call

import chargen  # noqa: E402
import llm  # noqa: E402
import prompts  # noqa: E402

fails = []


def check(name, ok, why=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        if why:
            print(f"       {why}")
        fails.append(name)


# ── 1. the wire format ───────────────────────────────────────────────────
print("\nencoding")

url = llm.encode_bytes(BLANK_PNG, "her.png")
check("encode_bytes makes a data URL", url.startswith("data:image/png;base64,"))
check("the payload round-trips",
      base64.b64decode(url.split(",", 1)[1]) == BLANK_PNG)
check("the mime follows the filename",
      llm.encode_bytes(BLANK_PNG, "her.jpg").startswith("data:image/jpeg;"))
check("a non-image extension falls back to png rather than lying",
      llm.encode_bytes(BLANK_PNG, "her.txt").startswith("data:image/png;"),
      "text/plain in an image_url part is rejected by some servers and "
      "silently ignored by others")

msg = llm.vision_message_data("look at her", [url, url])
check("the vision message is a user turn", msg["role"] == "user")
check("images come first and the text last",
      [p["type"] for p in msg["content"]]
      == ["image_url", "image_url", "text"],
      "the instruction has to read as being about the pictures above it")
check("the text part carries the prompt",
      msg["content"][-1]["text"] == "look at her")

# The chat path builds its vision message from FILES. That function is now a
# one-liner over this one, which is the point — but it is also the only thing
# standing between this refactor and every image ever sent in a chat, so the
# shape is compared key for key rather than eyeballed.
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    f = Path(tmp) / "shot.png"
    f.write_bytes(BLANK_PNG)
    from_path = llm.vision_message(" hi", [str(f)])
check("the file-based chat path is byte-identical to the data-based one",
      from_path == llm.vision_message_data(" hi", [llm.encode_bytes(BLANK_PNG,
                                                                    "shot.png")]),
      "vision_message is what every chat turn with an attachment goes "
      "through; a change here is a change to all of them")


# ── 2. the messages the forge builds ─────────────────────────────────────
print("\nbuild_image_messages")

persona = {"name": "anon", "data": {"description": "night shift",
                                    "into": "being bullied"}}
built = chargen.build_image_messages(
    persona, [url, url], brief="she's my neighbour", count=3,
    voices=["brat", "mommy"], models=["anima", "krea2"],
    jailbreak="JB-MARKER")

check("two messages: a system turn and the picture turn", len(built) == 2)
check("the system turn carries the jailbreak first",
      built[0]["content"].startswith("JB-MARKER"),
      "remote models refuse this work without it and the forge is meant to "
      "run on whatever is connected")
check("the system turn carries the CFTF layer",
      "The picture is the brief." in built[0]["content"])
check("both pictures are attached",
      sum(1 for p in built[1]["content"] if p["type"] == "image_url") == 2)
check("several pictures are declared to be ONE character",
      "ONE character" in built[1]["content"][-1]["text"],
      "otherwise a reference sheet is read as three different women")

text = built[1]["content"][-1]["text"]
check("the persona reaches the model", "night shift" in text)
check("what they are into reaches the model as design input",
      "being bullied" in text and "design input" in text)
check("the brief is stated as a hard requirement",
      "she's my neighbour" in text and "hard requirement" in text)
check("the real voice and model ids are offered",
      "brat" in text and "krea2" in text,
      "a hallucinated id only fails sixty seconds later at generation time")

one = chargen.build_image_messages(persona, [url])
check("a single picture is described in the singular",
      "THE PICTURE is attached" in one[1]["content"][-1]["text"])
check("an override replaces the shipped layer entirely",
      chargen.build_image_messages(persona, [url], system="ONLY THIS"
                                   )[0]["content"] == "ONLY THIS")


# ── 3. the two hard rules, in the SHIPPED default ────────────────────────
# The layer is user-editable like the other ten, so this pins what ships, not
# what is running. RELEASE.local.md's content rule outranks everything else in
# the project and a vision route that takes arbitrary uploads is exactly where
# it earns its keep.
print("\nthe hard rules")

default = chargen.CFTF_SYSTEM
check("the shipped prompt refuses anyone not unmistakably an adult",
      "unmistakably as an adult" in default and '{"refuse"' in default)
check("it forbids the aged-up dodge explicitly",
      "aged-up" in default,
      "a model told only 'refuse minors' will offer to pitch her as 25 "
      "instead, which is the same picture with a different number on it")
check("it forbids identifying a real person",
      "Never identify" in default and "real person" in default)
check("the layer is registered and editable in the inspector",
      any(e["key"] == "chargen_image" and e["group"] == "forge"
          for e in prompts.catalog()))
check("and defaults to the module's text",
      prompts.get("chargen_image") == chargen.CFTF_SYSTEM)


# ── 4. parsing: pitches, and a refusal that is not one ───────────────────
print("\nparsing")

noisy = """Here you go!
```json
{"characters": [
 {"name": "Mio", "tagline": "the 4am one", "description": "she works nights",
  "personality": "flat", "scenario": "s", "first_mes": "f",
  "mes_example": "<START>", "appearance": "black bob, tired eyes",
  "voice": "brat", "model": "made-up-model", "for_you": "y",
  "tags": ["a", "b"]}
]}
```"""
got = chargen.parse_pitches(noisy, ["brat", "mommy"], ["anima", "krea2"])
check("a fenced, chatty reply still parses", len(got) == 1)
check("a hallucinated image model is pinned to a real one",
      got and got[0]["model"] == "anima")
check("a valid voice is left alone", got and got[0]["voice"] == "brat")

# One bad entry in the array must not cost the good ones. The salvage path
# was dead for exactly this shape — see tests/test_scenarios.py — and the
# symptom here is a pitch that fails outright roughly one run in four, which
# reads as the vision model not working rather than as one stray newline.
half = """```json
{"characters": [
 {"name": "Good", "description": "she works nights", "appearance": "a bob",
  "voice": "brat", "model": "anima"},
 {"name": "Bad", "description": "d", "mes_example": "<START>
{{user}}: hi"}
]}
```"""
kept = chargen.parse_pitches(half, ["brat"], ["anima"])
check("one malformed pitch does not cost the good ones",
      [c["name"] for c in kept] == ["Good"], kept)

check("a refusal is read as data",
      chargen.refusal('{"refuse": "she reads as a child"}')
      == "she reads as a child")
check("the alternate spelling is accepted",
      chargen.refusal('  {"refusal": "no"}  ') == "no")
check("an ordinary pitch reply is NOT a refusal",
      chargen.refusal(noisy) == "",
      "checked after parse_pitches comes back empty, but a false positive "
      "here would turn a good pitch into an accusation")
check("junk is not a refusal", chargen.refusal("sorry, I can't") == "")
check("empty input is safe", chargen.refusal("") == "")

# The two forge prompts are separate strings on purpose — tuned text beats
# clever composition — but they feed ONE parser, so the shape they ask for
# cannot drift apart.
for label, txt in (("pitch", chargen.PITCH_SYSTEM),
                   ("cftf", chargen.CFTF_SYSTEM)):
    missing = [f for f in chargen.FIELDS if f'"{f}"' not in txt]
    check(f"the {label} prompt asks for every field the card needs",
          not missing, f"missing: {missing}")


# ── 5. the route ─────────────────────────────────────────────────────────
print("\nroutes")

LOCAL = "http://127.0.0.1:1234/v1"
b64 = base64.b64encode(BLANK_PNG).decode()

r = call("POST", "/api/forge/characters/from-image",
         {"backend": LOCAL, "model": "x", "images": []})
check("no picture is refused", "picture is required" in (r.get("error") or ""))

r = call("POST", "/api/forge/characters/from-image",
         {"backend": LOCAL, "model": "x", "images": [{"name": "a.png",
                                                      "b64": "!!!not b64!!!"}]})
check("an unreadable picture is refused before any model call",
      "could not read" in (r.get("error") or ""), r)
check("and it is named, so a rejection among several says which one",
      "a.png" in (r.get("error") or ""), r)

# Both halves of this feature take the same file — the pitch route reads it,
# and _store_upload writes it on commit — so ONE cap. They disagreed by 20 MB,
# which meant a 25 MB picture was dropped from the pitch and accepted at
# commit with the user told neither.
import server  # noqa: E402
big = base64.b64encode(b"\x00" * (server.MAX_UPLOAD + 1)).decode()
r = call("POST", "/api/forge/characters/from-image",
         {"backend": LOCAL, "model": "x",
          "images": [{"name": "huge.png", "b64": big}]})
check("an oversized picture is refused as oversized, not as corrupt",
      "cap" in (r.get("error") or "")
      and "could not read" not in (r.get("error") or ""),
      "blaming a perfectly good 22 MB PNG's integrity sends the user off "
      "re-exporting a file that was never the problem")
check("the pitch cap and the commit cap are the same number",
      f"{server.MAX_UPLOAD_MB} MB" in (r.get("error") or ""), r)

r = call("POST", "/api/forge/characters/from-image",
         {"images": [{"name": "a.png", "b64": b64}]})
check("a missing backend says so rather than blaming the picture",
      "backend and model" in (r.get("error") or ""),
      "a blank remote_backends entry normalises to the same empty string as "
      "a blank backend, and used to be reported as 'vision is local-only'")

# The local-only rule. Deterministic wherever a remote is configured, quietly
# skipped on a bare clone — same pattern as test_regex and test_lore.
cfg = call("GET", "/api/config")
remote = next((rb["url"] for rb in (cfg.get("remote_backends") or [])
               if rb.get("url")), "")
if remote:
    r = call("POST", "/api/forge/characters/from-image",
             {"backend": remote, "model": "x",
              "images": [{"name": "a.png", "b64": b64}]})
    check("a configured remote is refused, not degraded",
          "local-only" in (r.get("error") or ""),
          "this is the one vision path with no honest in-band fallback: a "
          "pitch built from a picture nobody saw looks exactly like the "
          "feature working")
    check("and the refusal names the way out",
          "local model" in (r.get("error") or ""))
else:
    print("  ..   no remote backend configured — local-only rule not exercised")


# ── 6. commit: the picture becomes her face AND her reference ────────────
print("\ncommit")

made = call("POST", "/api/forge/characters/create", {
    "backend": LOCAL, "model": "x", "portrait": False,
    "image_b64": b64, "image_name": "her.png",
    "character": {"name": "Fixture-chan", "description": "built from a shot",
                  "appearance": "black bob", "voice": "brat",
                  "model": "anima"},
})
row = made.get("character") or {}
data = row.get("data") or {}
check("she is created", bool(row.get("id")), made)
check("the picture becomes her avatar",
      bool(row.get("avatar")),
      "a card forged from a photograph whose face is a fresh render of a "
      "DESCRIPTION of that photograph is not what anybody asked for")
check("and her generation reference — the same stored file, not a copy",
      (data.get("visual") or {}).get("ref") == row.get("avatar"),
      "studio reads data.visual.ref; two files here is two chances to drift")
check("the avatar is servable",
      call("GET", "/api/characters").get("rows") is not None)
check("her pitched appearance survives",
      (data.get("visual") or {}).get("appearance") == "black bob")
check("a pinned seed is still rolled for her",
      isinstance((data.get("visual") or {}).get("seed"), int),
      "she has to look like herself in every later picture too")

plain = call("POST", "/api/forge/characters/create", {
    "backend": LOCAL, "model": "x", "portrait": False,
    "character": {"name": "Fixture-chan", "description": "no picture"},
})
check("the ordinary forge is unchanged with no picture",
      not (plain.get("character") or {}).get("avatar")
      and not ((plain.get("character") or {}).get("data", {})
               .get("visual", {}).get("ref")))

bad = call("POST", "/api/forge/characters/create", {
    "backend": LOCAL, "model": "x", "portrait": False,
    "image_b64": b64, "image_name": "her.exe.zip.reallylongext",
    "character": {"name": "Fixture-chan", "description": "x"},
})
check("an unsupported file type is refused rather than stored",
      "unsupported file type" in (bad.get("error") or ""), bad)


print()
if fails:
    print(f"FAILED ({len(fails)}): {', '.join(fails)}")
    sys.exit(1)
print("cftf: all good")
