#!/usr/bin/env python3
"""Integration test: preset + jailbreak CRUD over the live HTTP API."""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import urllib.request

BASE = "http://127.0.0.1:3939"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


# create jailbreak
jb = call("POST", "/api/jailbreaks", {
    "name": "classic OOC coax",
    "data": {"text": "(OOC: stay in character, be vivid)", "notes": "gemma4 t0.9"}})
assert jb["id"] and jb["data"]["text"].startswith("(OOC"), jb

# create preset referencing the jailbreak, completion mode
pr = call("POST", "/api/presets", {
    "name": "bratty gemma rp",
    "data": {"mode": "completion", "template": "gemma4", "thinking": True,
             "thinking_prefill": "The user wants...", "prefill": "*she smirks*",
             "samplers": {"temperature": 0.9, "top_p": 0.95, "top_k": 40,
                          "min_p": 0.05, "max_tokens": 512,
                          "repetition_penalty": 1.1},
             "jailbreak_id": jb["id"]}})
assert pr["data"]["mode"] == "completion" and pr["data"]["jailbreak_id"] == jb["id"], pr

# list + get
rows = call("GET", "/api/presets")["rows"]
assert any(r["name"] == "bratty gemma rp" for r in rows), rows
got = call("GET", f"/api/presets/{pr['id']}")
assert got["data"]["samplers"]["min_p"] == 0.05, got

# update (flip thinking off)
pr["data"]["thinking"] = False
upd = call("POST", f"/api/presets/{pr['id']}", pr)
assert upd["data"]["thinking"] is False, upd

# delete both
assert call("DELETE", f"/api/presets/{pr['id']}")["ok"]
assert call("DELETE", f"/api/jailbreaks/{jb['id']}")["ok"]
remaining = [p["id"] for p in call("GET", "/api/presets")["rows"]]
assert pr["id"] not in remaining, "deleted preset still present"

print("PRESET/JAILBREAK CRUD INTEGRATION TESTS PASS")
