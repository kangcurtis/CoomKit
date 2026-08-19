#!/usr/bin/env python3
"""Comfy bridge unit tests: slot detection + substitution (no server needed)."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import comfy

WF = {
    "3": {"class_type": "KSampler", "inputs": {
        "seed": "{{seed}}", "steps": 20, "cfg": 5.5}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {
        "text": "{{prompt}}", "clip": ["4", 0]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {
        "text": "blurry, {{negative_extra}}"}},
    "10": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
    "11": {"class_type": "EmptyLatentImage", "inputs": {
        "width": "{{width}}", "height": "{{height}}"}},
}

slots = comfy.find_slots(WF)
assert set(slots) == {"seed", "prompt", "negative_extra", "image", "width", "height"}, slots
assert slots["prompt"] == [("6", "text")]

out = comfy.substitute(WF, {
    "seed": 12345, "prompt": "1girl, smiling", "negative_extra": "bad hands",
    "image": "dick.png", "width": 832, "height": 1216})
assert out["3"]["inputs"]["seed"] == 12345  # whole-value numeric replace
assert out["3"]["inputs"]["steps"] == 20    # untouched
assert out["6"]["inputs"]["text"] == "1girl, smiling"
assert out["7"]["inputs"]["text"] == "blurry, bad hands"
assert out["10"]["inputs"]["image"] == "dick.png"
assert out["11"]["inputs"]["width"] == 832
# original workflow unchanged
assert WF["6"]["inputs"]["text"] == "{{prompt}}"

# missing slot values leave placeholder in string interpolation but keep
# whole-value slots as-is (so ComfyUI error is understandable)
out2 = comfy.substitute(WF, {"seed": 1})
assert out2["6"]["inputs"]["text"] == "{{prompt}}"  # whole-value, no replacement
assert out2["7"]["inputs"]["text"] == "blurry, "

print("COMFY SLOT TESTS PASS")
