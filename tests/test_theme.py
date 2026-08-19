#!/usr/bin/env python3
"""Themes: every palette defines every token, keeps its -rgb triplets in sync,
and clears WCAG contrast.

Offline and free — pure arithmetic over style.css. Four failure modes, and
every one of them is silent in a browser:

  · A token the default defines and a theme forgets. The theme simply inherits
    the default's value, so a green app shows one pink border and nothing says
    why.
  · A hex that moves without its `-rgb` companion. Composed `rgba(var(--x-rgb))`
    colours keep the OLD hue, so you get a green app with pink glows and badge
    fills — the single most likely way this feature rots.
  · A palette colour written as a literal outside `:root`. It cannot be themed
    and it degrades one rule at a time.
  · Contrast. The shipped rose palette had SIX failures nobody had measured,
    including `--line-lit` at 1.66:1, which makes every button and input border
    effectively invisible to a low-vision user.
"""

import re

import _bootstrap  # noqa: F401  — repo root on sys.path
from _bootstrap import ROOT

fails = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label
          + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


CSS = (ROOT / "web" / "style.css").read_text()
BLOCKS = {"rose (:root)": r":root\s*\{.*?\n\}",
          "hunter": r'\[data-theme="hunter"\]\s*\{.*?\n\}'}


def block(pat):
    m = re.search(pat, CSS, re.S)
    assert m, pat
    return m.group(0)


def hexes(b):
    return {m.group(1): m.group(2)
            for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*(#[0-9A-Fa-f]{3,8})\s*;", b)}


def triplets(b):
    return {m.group(1): m.group(2).replace(" ", "")
            for m in re.finditer(r"(--[a-z0-9-]+)-rgb\s*:\s*([\d, ]+);", b)}


def rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lum(h):
    def f(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ── 1. every theme covers every token ────────────────────────────────────
print("token coverage")
base = hexes(block(BLOCKS["rose (:root)"]))
NON_COLOUR = {"--r", "--r-s", "--mono", "--sans", "--pop-w", "--pop-h"}
for name, pat in BLOCKS.items():
    if name.startswith("rose"):
        continue
    t = hexes(block(pat))
    missing = sorted(set(base) - set(t) - NON_COLOUR)
    check(f"{name} defines every token :root does", not missing, str(missing))
    extra = sorted(set(t) - set(base))
    check(f"{name} invents no token :root lacks", not extra, str(extra))

# ── 2. hex and -rgb never drift ──────────────────────────────────────────
print("\nhex / -rgb agreement")
for name, pat in BLOCKS.items():
    b = block(pat)
    hx, tr = hexes(b), triplets(b)
    bad = []
    for tok, trip in tr.items():
        if tok not in hx:
            bad.append(f"{tok}-rgb has no {tok}")
            continue
        want = ",".join(str(c) for c in rgb(hx[tok]))
        if want != trip:
            bad.append(f"{tok}: {hx[tok]} -> {want} but -rgb says {trip}")
    check(f"{name}: every -rgb matches its hex", not bad, "; ".join(bad))
    # and a triplet must exist wherever one is composed
    used = {m.group(1) for m in re.finditer(r"var\((--[a-z0-9-]+)-rgb\)", CSS)}
    if name.startswith("rose"):
        check("every composed -rgb is defined",
              not (used - set(tr)), str(sorted(used - set(tr))))

# ── 3. no palette colour escapes :root ───────────────────────────────────
print("\nno literals outside the palette blocks")
body = CSS
for pat in BLOCKS.values():
    body = body.replace(block(pat), "")
lit = re.findall(r"rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+", body)
# Pure black is deliberate: shadows and scrims read as shadow in any theme.
non_black = [x for x in lit if not re.match(r"rgba?\(\s*0\s*,\s*0\s*,\s*0", x)]
check("no numeric rgb() outside the palette except pure black",
      not non_black, str(non_black[:4]))
hexlit = [h for h in re.findall(r"#[0-9A-Fa-f]{3,8}\b", body)
          if h.lower() not in ("#fff", "#000", "#ffffff", "#000000")]
check("no hex literal outside the palette except #fff/#000",
      not hexlit, str(hexlit[:6]))

# ── 4. contrast, both palettes ───────────────────────────────────────────
print("\nWCAG contrast")
PAIRS = [("--text", "--bg", 4.5), ("--text", "--surface", 4.5),
         ("--text", "--surface-2", 4.5), ("--text-dim", "--surface", 4.5),
         ("--text-mute", "--surface", 4.5), ("--text-mute", "--surface-2", 4.5),
         ("--text-mute", "--surface-3", 4.5), ("--accent", "--surface", 4.5),
         ("--accent-lit", "--surface", 4.5), ("--second-lit", "--surface", 4.5),
         ("--gold-lit", "--surface", 4.5), ("--ok", "--surface", 4.5),
         ("--second", "--surface", 3.0), ("--line-lit", "--surface", 3.0),
         ("--line-lit", "--surface-3", 3.0),
         # dark ink on the dark stop of a .primary-btn / .phone-tab gradient
         ("--on-accent", "--accent-deep", 4.5),
         ("--on-accent", "--second-deep", 4.5)]
for name, pat in BLOCKS.items():
    t = dict(base)
    t.update(hexes(block(pat)))
    bad = [f"{a} on {b} {ratio(t[a], t[b]):.2f} < {req}"
           for a, b, req in PAIRS if ratio(t[a], t[b]) < req]
    check(f"{name}: all {len(PAIRS)} pairs pass", not bad, "; ".join(bad))

# ── 5. the export must be able to carry every token ──────────────────────
print("\nthe export carries the theme")
APP = (ROOT / "web" / "app.js").read_text()
m = re.search(r"const CK_TOKENS = \[(.*?)\];", APP, re.S)
check("app.js declares CK_TOKENS", bool(m))
if m:
    carried = set(re.findall(r"'([a-z0-9-]+)'", m.group(1)))
    want = {k.lstrip("-") for k in base if k not in NON_COLOUR}
    want |= {k.lstrip("-") + "-rgb" for k in triplets(block(BLOCKS["rose (:root)"]))}
    missing = sorted(want - carried)
    # ckStage snapshots these onto the serialised host. Anything absent falls
    # back to the :root default inside the SVG, so an exported log comes out
    # with that one colour from the WRONG theme and nothing reports it.
    check("...and it carries every palette token", not missing, str(missing))
    check("...and invents none", not (carried - want), str(sorted(carried - want)))
    check("ckStage applies them to the host",
          "host.style.setProperty('--' + t, v)" in APP)

print()
if fails:
    print(f"THEME TESTS FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("theme ok")
