#!/usr/bin/env python3
"""First-run setup: the API calls the wizard actually makes, in order.

The wizard is the first thing a new user touches and the one flow where a
silent failure looks like the whole project is broken. These are the calls it
fires, checked against a live server.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:3939"
fails = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


print("backend detection")
backends = call("GET", "/api/backends").get("backends", [])
check("the probe answers", isinstance(backends, list))
usable = [b for b in backends if b.get("models")]
check("at least one backend has a model", bool(usable),
      "start LM Studio or llama.cpp for this one")

print("\nblock catalogue")
cat = call("GET", "/api/blocks")
for key in ("default", "library", "groups", "starters"):
    check(f"catalogue carries {key}", bool(cat.get(key)))
check("both starter tiers exist",
      "local" in cat["starters"] and "remote" in cat["starters"])
check("the local tier is smaller than the hosted one",
      len(cat["starters"]["local"]) < len(cat["starters"]["remote"]))

print("\nstarter install")
preset = call("POST", "/api/presets",
              {"name": "wizard-test", "data": {"mode": "chat"}})
pid = preset["id"]
r = call("POST", f"/api/presets/{pid}/blocks/starter", {"kind": "local"})
check("blocks are added", r["added"] > 0)
ids = [b["id"] for b in r["blocks"]]
check("they are library blocks", any(i.startswith("lib.") for i in ids))
check("the built-in order survives alongside them", "history" in ids)
at_hist = ids.index("history")
check("library blocks land before the history marker",
      all(ids.index(i) < at_hist for i in ids if i.startswith("lib.")))

print("\ncontext")
probe = call("POST", "/api/context/probe",
             {"backend": usable[0]["url"], "model": usable[0]["models"][0]}
             if usable else {"backend": "http://127.0.0.1:1", "model": "x"})
check("the probe answers either way", "ok" in probe)
if probe.get("ok"):
    # `context` is what the wizard WRITES into the preset, so it is only ever
    # a number worth budgeting history against. 0 means the probe declined —
    # see below. The old ceiling here was 1,000,000 and it went red the day
    # LM Studio was down, because the fallback backend's first model was a
    # genuine 1,048,576-token remote; 84 OpenRouter models now clear that.
    check("it reports a budgetable context, or none at all",
          probe["context"] == 0 or 512 <= probe["context"] <= 200000,
          str(probe.get("context")))
    check("it never hands back an unbudgetable figure",
          probe["context"] <= 200000, str(probe.get("context")))
    check("declining to answer always says why",
          bool(probe["context"]) or bool(probe.get("note")))

# An architectural maximum is a capability, not a setting. Adopting one is
# how a preset ends up budgeted at 262,144 against a model LM Studio will
# JIT-load at its default — history is then never trimmed AND the figure
# reaches `lms load --context-length` through vram.ensure_model. Probe every
# model the backend offers: any that answers with no measured load must
# either refuse (0 + a note) or come back capped.
for b in usable:
    for mid in (b.get("models") or [])[:8]:
        p = call("POST", "/api/context/probe", {"backend": b["url"],
                                                "model": mid})
        if not p.get("ok"):
            continue
        if p.get("loaded_now"):
            check(f"a loaded model reports its load — {mid}",
                  p["context"] == p.get("loaded"), str(p))
        else:
            check(f"an unmeasured model is never adopted raw — {mid}",
                  p["context"] == 0 or p["context"] <= 200000, str(p))
            check(f"...and it says why — {mid}",
                  bool(p.get("note")) or p["context"] == p.get("max"), str(p))

# The wizard saves the context right after installing blocks. An omitted
# `blocks` key used to mean "wipe them", so setup finished having thrown away
# the starter set it had just described to the user.
before = len(r["blocks"])
r2 = call("POST", f"/api/presets/{pid}/blocks", {"context": 20736})
check("a context-only save keeps the block list",
      len(r2["blocks"]) == before, f"{len(r2['blocks'])} vs {before}")
check("...and stores the context", r2.get("context") == 20736)
r3 = call("POST", f"/api/presets/{pid}/blocks",
          {"blocks": r2["blocks"][:3]})
check("an explicit blocks save still replaces them", len(r3["blocks"]) == 3)

print("\nmascot")
for mood in ("happy", "smug", "laugh", "proud", "flat", "fluster"):
    try:
        with urllib.request.urlopen(f"{BASE}/img/gemma/{mood}.png",
                                    timeout=10) as resp:
            ok = resp.status == 200 and len(resp.read()) > 2000
    except Exception:  # noqa: BLE001
        ok = False
    check(f"{mood} is served", ok)

# ── seeding an empty database ─────────────────────────────────────
# The wizard's blocks step does `S.presets[0]`, so a first run with no presets
# made the headline step of setup render its summary and then write nothing —
# setup looked like it worked and configured nothing at all. Seeding is what
# stops that, so it is checked directly rather than through the UI.
print("\nseeding a genuinely empty database")
import sqlite3            # noqa: E402
import tempfile           # noqa: E402
from pathlib import Path as _Path  # noqa: E402


import server as _srv     # noqa: E402

_tmp = _Path(tempfile.mkdtemp(prefix="coomkit-firstrun-"))
_keep = (_srv.DATA, _srv.DB_PATH)
try:
    _srv.DATA, _srv.DB_PATH = _tmp, _tmp / "coomkit.sqlite"
    _srv.init_db()
    seeded = _srv.seed_first_run()
    check("presets are seeded on an empty database", seeded.get("presets", 0) > 0)
    check("jailbreaks come with them", seeded.get("jailbreaks", 0) > 0)
    check("a persona exists so the persona layer can carry something",
          seeded.get("personas", 0) == 1)
    with sqlite3.connect(_srv.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        names = [r[0] for r in c.execute("SELECT name FROM personas")]
        n_presets = c.execute("SELECT COUNT(*) FROM presets").fetchone()[0]
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    check("the default persona is anon, not a real name", names == ["anon"])
    check("regex_rules is part of the schema", "regex_rules" in tables)

    # Second run must be a no-op: emptiness is the trigger, so someone who
    # renamed or edited the shipped presets keeps their work.
    again = _srv.seed_first_run()
    check("seeding does not run twice", not again)
    with sqlite3.connect(_srv.DB_PATH) as c:
        check("...and nothing was duplicated",
              c.execute("SELECT COUNT(*) FROM presets").fetchone()[0] == n_presets)
finally:
    _srv.DATA, _srv.DB_PATH = _keep
    for f in _tmp.glob("*"):
        f.unlink()
    _tmp.rmdir()

print()
if fails:
    print(f"WIZARD TESTS FAILED ({len(fails)}): " + ", ".join(fails))
    sys.exit(1)
print("FIRST-RUN WIZARD TESTS PASS")
