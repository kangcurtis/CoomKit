#!/usr/bin/env python3
"""CoomKit tool-call layer — LLM-invoked ComfyUI generation.

Design: local models emit janky JSON, so instead of native tool-calling APIs
we ask the model for a fenced ```tool block in its reply — dramatically more
reliable on small local models, and easy to scan. The block contains JSON:

  {"action": "generate_image", "prompt": "...", "workflow": "anima",
   "width": 832, "height": 1216}

The server splits the reply into (visible_text, tool_call), rewrites the
draft prompt through the dialect skills, and hands the rewritten
prompt to the user for approve/edit before ComfyUI runs it.

Supported actions: generate_image, generate_video, generate_tts,
generate_music. Workflow names map to the workflows table rows by `kind`.
"""
import json
import re
import time
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

TOOLS_SPEC = """\
You can send the user a picture, a video, a voice note or a song. Emit a
fenced tool block anywhere in your reply — it is invisible to them, so keep
the rest of the message natural and in character.

The easy way is to name a shot and let the studio compose it. It already
knows what you look like and what is happening in this scene:

```tool
{"recipe": "selfie", "opts": {"wardrobe": "lingerie", "mirror": true}}
```

Shots you can ask for, and the options each takes:
  solo-model  a posed modelling photo   — wardrobe: clothed|lingerie|topless|nude
  solo-lewd   an explicit solo photo    — explicit: suggestive|explicit|very-explicit
  selfie      a phone selfie            — wardrobe, mirror: true|false
  handjob     you and them              — pov, moving, explicit
  blowjob     you and them              — pov, moving, explicit
  scene       whatever is happening now — pov, moving
  asmr        whispered in their ear    — lewd, seconds
  song        a song you wrote for them — lewd, seconds
Set `moving: true` for a video instead of a still. Set `pov: true` to shoot it
from their eyeline.

If you want something none of those cover, write the prompt yourself instead:

```tool
{"action": "generate_image", "prompt": "1girl, ...", "workflow": "anima"}
```

Actions: generate_image | generate_video | generate_tts | generate_music.
Either way the user sees the prompt and approves or edits it before anything
is generated, then the result appears inline in the chat. Ask for one thing at
a time, and only when it genuinely fits the moment."""

TOOL_RE = re.compile(r"```tool\s*\n(.*?)\n?```", re.DOTALL)
ACTION_TO_KIND = {
    "generate_image": "image",
    "generate_video": "video",
    "generate_tts": "tts",
    "generate_music": "music",
}


def split_tool_call(text: str) -> tuple[str, dict | None]:
    """Extract the first ```tool block. Returns (visible_text, call|None)."""
    m = TOOL_RE.search(text or "")
    if not m:
        return text, None
    visible = TOOL_RE.sub("", text).strip()
    try:
        call = json.loads(m.group(1))
    except json.JSONDecodeError:
        return text, None  # malformed block -> show it rather than lose it
    return visible, call


DIRECTOR_RE = re.compile(r"```director\s*\n(.*?)\n?```", re.DOTALL)


def split_director_note(text: str) -> tuple[str, str]:
    """Extract a ```director block. Returns (visible_text, note).

    Same trick as the tool block, for the same reason: a fenced block is the
    one structured output small local models get right consistently. The note
    is the model's out-of-character half of the conversation about where the
    scene goes next — it belongs in the director bar, never in her mouth, so
    it is cut out of the visible reply.
    """
    m = DIRECTOR_RE.search(text or "")
    if not m:
        return text, ""
    return DIRECTOR_RE.sub("", text).strip(), m.group(1).strip()


def read_skill(name: str) -> str:
    path = SKILLS_DIR / name
    return path.read_text() if path.exists() else ""


DEFAULT_SKILL = {k: k + ".md" for k in (
    "anima", "krea2", "klein", "h3", "zimage", "video-wan-ltx", "klein-edit")}


def rewrite_prompt(draft: str, skill_name: str, core: str = "_core.md") -> list[dict]:
    """Assemble the dialect-rewrite message list for the LLM.

    Returns chat messages; caller runs them through its provider."""
    skill_text = read_skill(skill_name)
    core_text = read_skill(core)
    system = (core_text + "\n\n" + skill_text).strip()
    return [
        {"role": "system", "content": system},
        {"role": "user", "content":
            f"Rewrite this draft into the model's native dialect. Output ONLY "
            f"the rewritten prompt.\n\nDRAFT:\n{draft}"},
    ]


# ----------------------------------------------------------------------
# Pending tool calls (server-side store, shared across the approve/edit flow)
# ----------------------------------------------------------------------

_pending: dict[int, dict] = {}
_pending_seq = [0]


def register(call: dict, rewritten_prompt: str) -> int:
    _pending_seq[0] += 1
    pid = _pending_seq[0]
    _pending[pid] = {"call": call, "prompt": rewritten_prompt,
                     "created": time.time()}
    return pid


def pending_all() -> list[dict]:
    return [{"id": pid, **p} for pid, p in _pending.items()]


def pending_pop(pid: int, final_prompt: str | None = None) -> dict | None:
    p = _pending.pop(pid, None)
    if p and final_prompt is not None:
        p["prompt"] = final_prompt
    return p
