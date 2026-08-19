#!/usr/bin/env python3
"""Static frontend sanity: every $('id') in app.js must exist in index.html.

Vanilla JS with no build step means a typo'd id is a silent null-deref at
runtime. This catches it in a second instead of during a scene.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import re
import sys
from pathlib import Path

HERE = _bootstrap.ROOT
web = HERE / "web"
html = (web / "index.html").read_text()
js = (web / "app.js").read_text()

html_ids = set(re.findall(r'id="([^"]+)"', html))
js_ids = set(re.findall(r"\$\('([^']+)'\)", js))
js_ids |= set(re.findall(r'getElementById\([\'"]([^\'"]+)[\'"]\)', js))

missing = sorted(js_ids - html_ids)
if missing:
    print("JS references ids that do not exist in the HTML:")
    for m in missing:
        for i, line in enumerate(js.splitlines(), 1):
            if f"'{m}'" in line:
                print(f"  {m:22} app.js:{i}")
                break
    sys.exit(1)
print(f"all {len(js_ids)} referenced ids exist ({len(html_ids)} in html)")

# functions called but never defined (catches the openChatById class of bug)
defined = set(re.findall(r"(?:async\s+)?function\s+(\w+)", js))
defined |= set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", js))
called = set(re.findall(r"\b(\w+)\(", js))
builtins = {
    "if", "for", "while", "switch", "catch", "function", "return", "await",
    "typeof", "new", "fetch", "post", "api", "del", "esc", "fmt", "toast",
    "say", "parseInt", "parseFloat", "Number", "String", "Boolean", "Array",
    "Object", "JSON", "Promise", "Math", "Date", "console", "alert", "setTimeout",
    "clearTimeout", "document", "window", "require", "Set", "Map", "isNaN",
    "encodeURIComponent", "decodeURIComponent", "TextDecoder", "FileReader",
    "URL", "confirm", "prompt", "$",
}
suspect = sorted(c for c in called - defined - builtins
                 if c[0].islower() and "_" not in c and len(c) > 3
                 and f".{c}(" not in js and f"{c}:" not in js)
# only flag things that look like our own helpers
ours = [s for s in suspect if re.search(rf"(?<![.\w]){s}\(", js)
        and not re.search(rf"\b{s}\s*[:=]", js)]
if ours:
    print("possibly undefined helpers:", ", ".join(ours))
else:
    print("no obviously undefined local helpers")

# every fetch path should be one the server routes
paths = sorted(set(re.findall(r"['\"](/api/[a-z_/]+)", js)))
server = (HERE / "server.py").read_text()
unrouted = [p for p in paths if p.rstrip("/") not in server]
if unrouted:
    print("frontend calls paths the server may not route:", unrouted)
else:
    print(f"all {len(paths)} /api paths appear in server.py")
# ── the walkthrough points at things that exist ──────────────────────
# A TOUR step whose target is inside a closed modal is dropped SILENTLY by
# startTour's visibility filter unless it carries a `before` that opens it —
# so a step can rot into nothing without any error. And the boot gate has to
# stay off S.presets.length: server.seed_first_run() installs the shipped
# library into an empty database before the first request is served, so that
# condition is permanently false and the wizard could never fire.
_tour = re.search(r"const TOUR = \[(.*?)\n\];", js, re.S)
assert _tour, "TOUR array not found — did it get renamed?"
_body = _tour.group(1)
_steps = re.findall(r"\{[^{}]*?el:\s*'([^']+)'(.*?)(?=\n  \{|\Z)", _body, re.S)
assert len(_steps) >= 10, f"only found {len(_steps)} tour steps"
for sel, rest in _steps:
    # every id the step points at must exist in the HTML
    for got in re.findall(r"#([A-Za-z][\w-]*)", sel):
        assert got in html_ids, f"TOUR step points at #{got}, which is not in index.html"
    # a target inside a modal is invisible at boot, so it needs a `before`
    if sel.startswith('#forge') or sel.startswith('#cv') or sel.startswith('#lora'):
        assert 'before:' in rest, (
            f"TOUR step '{sel}' lives inside a modal and has no `before` to "
            f"open it — startTour would drop it silently")
print(f"tour: {len(_steps)} steps, every target exists and modal steps open themselves")

_gate = re.search(r"// First run\..*?\n\}\n", js, re.S).group(0)
# strip the comment lines: they legitimately NAME the old broken condition
_gate_code = "\n".join(l for l in _gate.splitlines()
                       if not l.strip().startswith("//"))
assert "S.presets.length" not in _gate_code, \
    "the first-run gate must not depend on S.presets.length — seed_first_run " \
    "installs the shipped library before the first request, so it is " \
    "permanently false and the wizard can never fire"
print("first-run gate: does not depend on a table that seeding fills")

print("FRONTEND WIRING OK")
