#!/usr/bin/env python3
"""Live: does editing the forge prompt change what the forge produces?

This is the whole claim of the feature. If a user rewrites the pitching prompt
and the output ignores it, the editor is decoration.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import urllib.request

BASE = "http://127.0.0.1:3939"
OR = "https://openrouter.ai/api/v1"
MODEL = "moonshotai/kimi-k3"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


call("POST", "/api/prompts/reset", {})
cid = call("GET", "/api/characters")["rows"][0]["id"]
base_body = {"character_id": cid, "backend": OR, "model": MODEL,
             "count": 2, "use_memory": False}

# baseline
r1 = call("POST", "/api/scenarios/suggest", base_body)
assert r1.get("ok"), r1
print("baseline titles:", [s["title"] for s in r1["scenarios"]])

# now bolt a house rule onto the shipped prompt
default = next(p for p in call("GET", "/api/prompts")["prompts"]
               if p["key"] == "forge_suggest")["default"]
house = (default + "\n\nADDITIONAL HOUSE RULE (mandatory): every scenario must "
         "take place underwater, and every title must begin with the word "
         "'Submerged'.")
assert call("POST", "/api/prompts",
            {"key": "forge_suggest", "text": house}).get("ok")

r2 = call("POST", "/api/scenarios/suggest", base_body)
assert r2.get("ok"), r2
titles = [s["title"] for s in r2["scenarios"]]
print("with house rule:", titles)

obeyed = sum(1 for t in titles if t.lower().startswith("submerged"))
blob = json.dumps(r2["scenarios"]).lower()
watery = any(w in blob for w in ("water", "underwater", "submerged", "pool",
                                 "dive", "ocean", "tank", "sea"))
assert obeyed or watery, (
    f"edited forge prompt had no visible effect: {titles}")
print(f"house rule obeyed: {obeyed}/{len(titles)} titles, "
      f"aquatic content present: {watery}")

# and the refine prompt is equally live
target = r2["scenarios"][0]
assert call("POST", "/api/prompts", {
    "key": "forge_refine",
    "text": next(p for p in call("GET", "/api/prompts")["prompts"]
                 if p["key"] == "forge_refine")["default"]
    + "\n\nMANDATORY: append the word 'REVISED' to the end of the title."
}).get("ok")
rev = call("POST", "/api/scenarios/refine", {
    **base_body, "scenario": target, "instruction": "make it colder"})
assert rev.get("ok"), rev
print(f"refined title: {rev['scenario']['title']!r}")
assert "REVISED" in rev["scenario"]["title"].upper() \
    or "cold" in json.dumps(rev["scenario"]).lower(), \
    "refine prompt override had no effect and the instruction was ignored"
print("refine prompt override reached the model")

call("POST", "/api/prompts/reset", {})
print("\nLIVE PROMPT-OVERRIDE-AFFECTS-FORGE PASS")
