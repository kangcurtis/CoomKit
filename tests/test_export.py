#!/usr/bin/env python3
"""The chatlog image export: redaction, provenance, and the static guards.

Offline and free — no model is called and no picture is drawn here. The
renderer is a canvas pipeline in the browser and there is no headless browser
in this repo, so this file covers the two halves that ARE reachable from
Python: the server contract the exporter reads, and a static scan of the
frontend for the mistakes that are silent at runtime.

The security-critical half is deliberately server-side. `_display_ctx` expands
{{user}} into the poster's real handle before any JSON exists, so a client-side
find-and-replace would be operating on already-expanded text: it mangles
substrings ("Al" inside "always"), needs regex escaping on user input, and
cannot be tested from here. Binding the name at macro expansion and scrubbing
what the model typed out longhand are both exact, and both are checked below.

What the static scan is for: every item in it is something that produces a
blank, rejected or silently wrong picture rather than an error.
"""

import re

import _bootstrap
from _bootstrap import ROOT

import engine
import testkit
from server import get_db, rows_upsert
from testkit import call

fails = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


# ── fixtures ─────────────────────────────────────────────────────────────
char_id = testkit.ensure_character()
p = rows_upsert("personas", {"name": "Ezekiel",
                             "data": {"description": "an export fixture"}})
persona_id = p["id"] if isinstance(p, dict) else p

with get_db() as conn:
    chat_id = engine.create_chat(conn, char_id, persona_id=persona_id,
                                 title="Ezekiel and the export")
    engine.add_message(conn, chat_id, "assistant",
                       "Hey {{user}}. Ezekiel, you absolute Ezekielson. "
                       "Ezekiel's log is showing.",
                       data={"think": "thinking about Ezekiel"})
    engine.add_message(conn, chat_id, "user", "Zeke here, and so is Alice.")
    engine.add_message(conn, chat_id, "assistant", "Noted.",
                       data={"gen": {"model": "gemma-4-12b-qat",
                                     "backend": "LM Studio", "preset": "brat",
                                     "mode": "chat",
                                     "samplers": {"temperature": 0.9}}})
print(f"fixture chat {chat_id}, persona {persona_id}")


def detail(qs=""):
    return call("GET", f"/api/chats/{chat_id}{qs}")


# ── the wire ─────────────────────────────────────────────────────────────
print("\nserver contract")
plain = detail()
msgs = plain["messages"]

check("every message carries `created`",
      all(isinstance(m.get("created"), (int, float)) for m in msgs))
check("`gen` surfaces when stamped, null when not",
      msgs[-1]["gen"] and msgs[-1]["gen"]["model"] == "gemma-4-12b-qat"
      and msgs[0]["gen"] is None)
# create_chat seeds the card's greeting, so the fixture messages start at 1.
mine = next(m for m in msgs if "Ezekielson" in m["content"])
check("no redaction by default",
      plain["redacted"] == 0 and "Ezekiel," in mine["content"],
      "the ordinary chat load must be untouched")

red = detail("?user_as=Anon")
rm = red["messages"]
blob = "\n".join(m["content"] for m in rm)

check("{{user}} binds to the pseudonym", "Hey Anon." in blob)
check("the name the model typed out is scrubbed too",
      "Ezekiel," in mine["content"] and "Anon," in blob)
check("possessives follow the name", "Anon's log" in blob)
check("a longer word containing the name is left alone",
      "Ezekielson" in blob, "word-boundary, not substring")
check("`redacted` counts what it did", red["redacted"] >= 3)
check("the chat title is redacted",
      "Ezekiel" not in red["title"] and "Ezekiel" not in (red["chat"]["title"] or ""),
      "chat_label falls back to the first user message, which is exactly "
      "where a name gets introduced")
check("her thinking is redacted",
      all("Ezekiel" not in (m.get("think") or "") for m in rm),
      "think is drawn into the picture when 'her thoughts' is on")
check("nothing named Ezekiel survives anywhere in the payload",
      "Ezekiel" not in str(red).replace("Ezekielson", ""))

alias = detail("?user_as=Anon&aliases=Zeke,Alice")
ablob = "\n".join(m["content"] for m in alias["messages"])
check("aliases are scrubbed", "Zeke" not in ablob and "Alice" not in ablob)

# longest-first ordering: a short name must not eat a longer one it sits inside
p2 = rows_upsert("personas", {"name": "Al", "data": {"description": "fixture"}})
al_id = p2["id"] if isinstance(p2, dict) else p2
with get_db() as conn:
    al_chat = engine.create_chat(conn, char_id, persona_id=al_id, title="Al")
    engine.add_message(conn, al_chat, "user", "Alice and Al walked in.")
al = call("GET", f"/api/chats/{al_chat}?user_as=Anon&aliases=Alice")
altext = "\n".join(m["content"] for m in al["messages"])
check("longest name wins, so Alice does not become Anonice",
      "Anonice" not in altext and altext.count("Anon") >= 2, altext)

check("asset urls are all same-origin under /api/avatars/",
      all(a["url"].startswith("/api/avatars/")
          for m in msgs for a in m.get("assets", [])),
      "a bitmap from ComfyUI's port is a different origin and taints the canvas")

bad = call("POST", f"/api/chats/{chat_id}/export/save", {"b64": "aGVsbG8="})
check("the gallery-save route refuses non-PNG bytes", "error" in bad)

# ── static scan: web/app.js ──────────────────────────────────────────────
print("\nweb/app.js")
app = (ROOT / "web" / "app.js").read_text()
start = app.index("// ── the image export")
exp = app[start:]


def decomment(js):
    """Strip // and /* */ so a rule is not tripped by the comment forbidding it.

    Crude — it does not know about strings or regex literals — but the only
    thing it feeds is substring checks for identifiers that must not be
    CALLED, and the comments explaining why are exactly what has to go.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in js.splitlines())


code = decomment(app)
exp_code = decomment(exp)

check("the export uses fmt() and fmtHtml() rather than its own renderer",
      "fmt(stripBlocks(safe))" in exp and "fmtHtml(safe)" in exp,
      "a second renderer for message content drifts from the bubbles on "
      "screen and nothing detects it")
check("no hex colour literal in the export",
      not re.search(r"#[0-9A-Fa-f]{6}\b", exp),
      "colours come from getComputedStyle so the picture cannot drift from "
      "the app's palette")
check("image/webp is never encoded",
      "image/webp" not in code,
      "both engines encode it happily and 4chan rejects it outright")
check("crossOrigin is never set",
      "crossOrigin" not in code,
      "same-origin with no ACAO header makes the load FAIL, not taint")
check("the SVG is never built from a blob: URL",
      "createObjectURL" not in exp_code.split("function ckSave")[0],
      "Chrome taints the canvas when a foreignObject SVG comes from blob:")
check("the serialised host is styled with width and nothing else",
      "host.style.width = W + 'px';   // width and nothing else" in exp,
      "any position/left on the serialised element carries the offset into "
      "the SVG viewport and the content lands outside it")
check("the C0/C1 sanitiser exists and is applied",
      r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]" in exp and "xmlSafe(m.content)" in exp,
      "one raw control char makes the SVG invalid XML, and the only symptom "
      "is img.onerror with nothing on it")
check("the download path appends the anchor and revokes the url",
      "document.body.appendChild(a)" in exp and "revokeObjectURL" in exp)
check("clipboard support is not gated on navigator.permissions",
      "navigator.permissions" not in exp,
      "it reports denied on working Chromes and Firefox does not implement it")

nums = dict(re.findall(r"const (CK_[A-Z_]+)\s*=\s*(\d+)", exp))
check("MAX_TILE_H stays under 4chan's 10000px side cap",
      int(nums.get("CK_MAX_TILE_H", 99999)) <= 10000, nums.get("CK_MAX_TILE_H"))
check("ENGINE_MAX stays inside Firefox's 32767 ceiling",
      int(nums.get("CK_ENGINE_MAX", 99999)) <= 32767, nums.get("CK_ENGINE_MAX"))
check("CAP_BYTES stays under /g/'s 4096 KB",
      int(nums.get("CK_CAP_BYTES", 9 << 30)) <= 4194304, nums.get("CK_CAP_BYTES"))

# ── static scan: web/style.css ───────────────────────────────────────────
print("\nweb/style.css")
css = (ROOT / "web" / "style.css").read_text()

check("the animation override exists",
      re.search(r"\.ck-export \*.*?animation: none !important", css, re.S) is not None,
      "an SVG loaded as an image is frozen at animation time 0 and .msg "
      "carries `animation: rise` whose first frame is opacity:0 — without "
      "this the ENTIRE export renders blank, measured at 1 distinct colour")
check("the think-body scroll clamp is lifted",
      ".ck-export .think-body" in css and "max-height: none !important" in css,
      "on screen you scroll it; in a PNG you just lose it")
check("code blocks wrap instead of scrolling",
      "white-space: pre-wrap !important" in css)

# The export measures in the live page, where @media evaluates against the
# window, and rasterises inside the SVG viewport, where it evaluates against
# the export width. A responsive rule on a bubble would make the two disagree
# with no way to notice — img.naturalHeight always equals what you declared.
HAZARD = (".stream", ".msg", ".bubble", ".think", ".code-block", ".ck-")
offenders = []
for m in re.finditer(r"@media[^{]*\{", css):
    i, depth = m.end(), 1
    while depth and i < len(css):
        depth += (css[i] == "{") - (css[i] == "}")
        i += 1
    body = css[m.end():i]
    hit = [t for t in HAZARD if t in body]
    if hit:
        offenders.append(m.group(0).strip() + " -> " + ",".join(hit))
check("no @media rule touches the export's own elements", not offenders,
      "; ".join(offenders) + " — the export measures in the page and "
      "rasterises in an SVG viewport, so they will silently disagree. "
      "Scope it away from .ck-export.")

# ── cleanup ──────────────────────────────────────────────────────────────
for cid in (chat_id, al_chat):
    call("DELETE", f"/api/chats/{cid}")

print()
if fails:
    print(f"FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("export ok")
