#!/usr/bin/env python3
"""Regex rules: JS->Python conversion, scoping, the HTML allowlist, ST import.

Offline and free. Uses the real SillyTavern preset when one is present at
st-presets/ (gitignored, someone else's work) and skips that section quietly
when it is not — the rest of the file must pass on a bare clone.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import re
from pathlib import Path

import regexrules as R

ROOT = _bootstrap.ROOT
ok = fail = 0


def check(label, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {label}")
    else:
        fail += 1
        print(f"  FAIL {label}")


print("--- JS literal -> Python ---")
c = R.compile_js("/<think>[\\s\\S]*?</think>/g", "")
check("strips the delimiters and honours /g", c["count"] == 0)
check("pattern compiles", c["re"].search("a<think>x</think>b") is not None)
check("global replace removes every match",
      c["re"].sub(c["replace"], "<think>a</think>y<think>b</think>", count=0) == "y")

c = R.compile_js("/cat/i", "dog")
check("no /g means replace once", c["count"] == 1)
check("/i maps to re.I", bool(c["re"].flags & re.I))

c = R.compile_js("/(\\w+)@(\\w+)/g", "$2 at $1")
check("$1/$2 become Python group refs",
      c["re"].sub(c["replace"], "anon@example") == "example at anon")

c = R.compile_js("/x/g", "cost: $$5")
check("$$ is an escaped literal dollar",
      c["re"].sub(c["replace"], "x") == "cost: $5")

c = R.compile_js("/(a)/g", "[$&]")
check("$& is the whole match", c["re"].sub(c["replace"], "a") == "[a]")

c = R.compile_js("/(?<who>\\w+) speaks/g", "$<who>!")
check("named groups convert both ways",
      c["re"].sub(c["replace"], "mika speaks") == "mika!")

c = R.compile_js("/a/g", "line\\nbreak")
check("a literal backslash-n in the replacement is not a group ref",
      c["re"].sub(c["replace"], "a") == "line\\nbreak")

c = R.compile_js("bare pattern with no slashes", "")
check("an undelimited pattern is treated as the body", c["count"] == 0)

for bad, why in (("", "empty"), ("/(unclosed/g", "syntax"),
                 ("/(?<=.*)x/g", "lookbehind")):
    try:
        R.compile_js(bad, "")
        check(f"rejects {why}", False)
    except R.RuleError:
        check(f"rejects {why} with a RuleError", True)

print("\n--- scoping ---")
rules = R.prepare([
    {"name": "p", "pattern": "/SECRET/g", "replace": "", "on_prompt": 1,
     "on_display": 0, "enabled": 1},
    {"name": "d", "pattern": "/ugly/g", "replace": "pretty", "on_prompt": 0,
     "on_display": 1, "enabled": 1},
    {"name": "off", "pattern": "/keep/g", "replace": "gone", "on_prompt": 1,
     "on_display": 1, "enabled": 0},
])
check("a disabled rule never compiles in", len(rules) == 2)
check("prompt scope applies only prompt rules",
      R.apply("SECRET ugly keep", rules, "prompt") == " ugly keep")
check("display scope applies only display rules",
      R.apply("SECRET ugly keep", rules, "display") == "SECRET pretty keep")

deep = R.prepare([{"name": "d", "pattern": "/x/g", "replace": "y",
                   "on_display": 1, "enabled": 1, "min_depth": 2,
                   "max_depth": None}])
check("min_depth leaves the recent turns alone",
      R.apply("x", deep, "display", 0) == "x")
check("min_depth fires further back",
      R.apply("x", deep, "display", 5) == "y")

trim = R.prepare([{"name": "t", "pattern": "/a/g", "replace": "b",
                   "on_display": 1, "enabled": 1, "trim": ["ZZ"]}])
check("trim strings are removed after the substitution",
      R.apply("aZZ", trim, "display") == "b")

print("\n--- the HTML allowlist ---")
check("script is escaped, not executed",
      "<script" not in R.sanitize("<script>alert(1)</script>").lower())
check("details/summary survive",
      R.sanitize("<details><summary>t</summary>x</details>")
      == "<details><summary>t</summary>x</details>")
check("event handlers are stripped",
      "onclick" not in R.sanitize('<div onclick="x()">a</div>'))
check("style survives when it is inert",
      'style="color:red"' in R.sanitize('<div style="color:red">a</div>'))
for css in ("url(http://x)", "expression(alert(1))", "position:fixed"):
    check(f"style is dropped when it contains {css}",
          "style" not in R.sanitize(f'<div style="{css}">a</div>'))
check("an unbalanced close tag cannot swallow the page",
      R.sanitize("</div></div>hello") == "hello")
check("tags are closed at the end",
      R.sanitize("<div>open") == "<div>open</div>")
check("has_markup is false for plain text", not R.has_markup("just words"))

print("\n--- SillyTavern import ---")
scripts = [
    {"scriptName": "hide think", "findRegex": "/<think>[\\s\\S]*?</think>/g",
     "replaceString": "", "markdownOnly": True, "promptOnly": False,
     "disabled": False, "minDepth": None, "maxDepth": None,
     "trimStrings": [], "substituteRegex": 0},
    {"scriptName": "both", "findRegex": "/a/g", "replaceString": "b",
     "markdownOnly": False, "promptOnly": False, "disabled": False,
     "minDepth": None, "maxDepth": None, "trimStrings": [],
     "substituteRegex": 0},
    {"scriptName": "macro user", "findRegex": "/x/g", "replaceString": "y",
     "markdownOnly": True, "promptOnly": False, "disabled": False,
     "minDepth": None, "maxDepth": None, "trimStrings": [],
     "substituteRegex": 1},
]
out = [R.from_st(sc) for sc in scripts]
check("markdownOnly becomes display-only",
      out[0]["rule"]["on_display"] == 1 and out[0]["rule"]["on_prompt"] == 0)
# ST's neither-flag state edits the saved message. CoomKit never does that —
# the log is canonical, exactly as with macros — so it lands as view-only.
check("the destructive ST state is imported as view-only",
      out[1]["rule"]["on_prompt"] == 1 and out[1]["rule"]["on_display"] == 1)
check("...and the change is reported, not hidden", bool(out[1]["note"]))
check("a macro-substituting pattern is imported disabled",
      out[2]["rule"]["enabled"] == 0 and bool(out[2]["problem"]))

sm = R.summarise_import(out)
check("the summary counts what will actually be on", sm["enabled"] == 2)

check("scripts_in finds a bare list", len(R.scripts_in(scripts)) == 3)
check("scripts_in finds a single script", len(R.scripts_in(scripts[0])) == 1)
check("scripts_in finds them on a preset",
      len(R.scripts_in({"extensions": {"regex_scripts": scripts}})) == 3)
check("scripts_in finds them on a v3 card",
      len(R.scripts_in({"data": {"extensions": {"regex_scripts": scripts}}})) == 3)
check("scripts_in is empty for an unrelated file", R.scripts_in({"a": 1}) == [])

real = sorted(ROOT.glob("st-presets/*.json"))
if real:
    print("\n--- against the real presets on this machine ---")
    found = 0
    for f in real:
        try:
            raw = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        got = R.scripts_in(raw)
        found += len(got)
        for sc in got:
            res = R.from_st(sc)
            # Every rule must either compile or say why it does not. Silence
            # is the failure mode this whole module exists to avoid.
            check(f"{f.name[:18]}/{res['rule']['name'][:22]}: compiles or explains",
                  bool(res["problem"]) or bool(
                      R.prepare([res["rule"]]) or not res["rule"]["enabled"]))
    check("at least one real preset carried regex scripts", found > 0)
else:
    print("\n(no st-presets/ on this machine — skipping the real-file pass)")

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
