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
APP = (ROOT / "web" / "app.js").read_text()

# Derived from the stylesheet, never hand-listed. Every section below iterates
# BLOCKS, so a palette missing from it is not partially tested — it is entirely
# untested, including for contrast, and the suite stays green while shipping it.
# One selector per palette block and no grouping, or the pattern misses it.
BLOCKS = {"rose (:root)": r":root\s*\{.*?\n\}"}
for _id in re.findall(r'\[data-theme="([a-z0-9-]+)"\]\s*\{', CSS):
    BLOCKS[_id] = r'\[data-theme="%s"\]\s*\{.*?\n\}' % _id

# A palette that SELLS itself as high-contrast is held to a higher bar than the
# rest — read off the label the UI actually shows, so the product claim and the
# test cannot drift apart. Rename it in THEMES and the bar follows.
THEME_LABELS = dict(re.findall(r"\['([a-z0-9-]+)',\s*'([^']+)'\]", APP))
HIGH_CONTRAST = {k for k, v in THEME_LABELS.items() if "high contrast" in v.lower()}


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


def scalars(b):
    """Palette tokens that are numbers rather than colours (--shadow-k).

    hexes() cannot see them, so without this they are the one kind of token a
    theme could forget in silence — and forgetting --shadow-k means a light
    palette draws every dialog with the dark palette's .7 black smudge.
    """
    return {m.group(1): m.group(2)
            for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([0-9]*\.?[0-9]+)\s*;", b)}


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
    s_base, s_t = scalars(block(BLOCKS["rose (:root)"])), scalars(block(pat))
    check(f"{name} defines every scalar token :root does",
          not (set(s_base) - set(s_t)), str(sorted(set(s_base) - set(s_t))))

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
# Comments are prose, not paint. This file explains its own colour decisions
# and quotes measured hex values while doing it; scanning those as if they were
# rules makes the check punish documentation.
body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
lit = re.findall(r"rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+", body)
# Pure black is deliberate: shadows and scrims read as shadow in any theme.
non_black = [x for x in lit if not re.match(r"rgba?\(\s*0\s*,\s*0\s*,\s*0", x)]
check("no numeric rgb() outside the palette except pure black",
      not non_black, str(non_black[:4]))
hexlit = [h for h in re.findall(r"#[0-9A-Fa-f]{3,8}\b", body)
          if h.lower() not in ("#fff", "#000", "#ffffff", "#000000")]
check("no hex literal outside the palette except #fff/#000",
      not hexlit, str(hexlit[:6]))

# ── 4. contrast, every palette ───────────────────────────────────────────
# The original seventeen were written by reading a DARK palette, which left
# holes that only bite on a light ground: --bg was assumed to be the darkest
# ground (so --accent was checked against --surface alone), --surface-0 and
# --ink were never treated as grounds at all, and --gold / --bad / --bad-lit
# were never measured despite being the destructive-control colours. Both
# shipped palettes already clear every addition below, so widening the list
# restyles nothing — it only stops the next palette shipping the hole.
print("\nWCAG contrast")
PAIRS = [
    # body text on every ground in the ramp
    ("--text", "--bg", 4.5), ("--text", "--surface", 4.5),
    ("--text", "--surface-2", 4.5), ("--text", "--surface-3", 4.5),
    ("--text", "--surface-0", 4.5), ("--text", "--ink", 4.5),
    ("--text-dim", "--surface", 4.5), ("--text-dim", "--bg", 4.5),
    ("--text-dim", "--surface-2", 4.5), ("--text-dim", "--surface-3", 4.5),
    ("--text-mute", "--surface", 4.5), ("--text-mute", "--surface-2", 4.5),
    ("--text-mute", "--surface-3", 4.5), ("--text-mute", "--bg", 4.5),
    ("--text-mute", "--surface-0", 4.5), ("--text-mute", "--ink", 4.5),
    # coloured text
    ("--accent", "--surface", 4.5), ("--accent", "--bg", 4.5),
    ("--accent-lit", "--surface", 4.5), ("--accent-lit", "--surface-2", 4.5),
    ("--second-lit", "--surface", 4.5), ("--second-lit", "--surface-2", 4.5),
    ("--gold-lit", "--surface", 4.5), ("--gold-lit", "--surface-2", 4.5),
    ("--gold", "--surface", 4.5), ("--ok", "--surface", 4.5),
    ("--ok", "--surface-2", 4.5),
    # the destructive pair: hover text and border on the two delete controls
    ("--bad", "--surface", 4.5), ("--bad-lit", "--surface", 4.5),
    ("--bad-line", "--surface", 3.0),
    # non-text: borders, the scrollbar thumb, the ▸ marker
    ("--second", "--surface", 3.0), ("--line-lit", "--surface", 3.0),
    ("--line-lit", "--surface-3", 3.0), ("--line-lit", "--bg", 3.0),
    # Ink on an accent fill. Which END of a .primary-btn / .phone-tab gradient
    # is hardest flips with the palette — dark ink struggles on --accent-deep,
    # light ink struggles on --accent-lit — so all three stops are measured.
    ("--on-accent", "--accent-deep", 4.5), ("--on-accent", "--second-deep", 4.5),
    ("--on-accent", "--accent", 4.5), ("--on-accent", "--accent-lit", 4.5),
]

# --line draws the 1px .layout gaps that are the ONLY separation between the
# three columns, plus every .ghost-btn, .mini-btn, input and .bubble edge. Both
# shipped palettes run it as a near-invisible hairline on purpose (rose 1.66:1)
# and lifting it would restyle them, so it is required of the high-contrast
# palette only — where a hairline nobody can see is the whole thing being fixed.
STRUCTURE = [("--line", "--surface", 3.0), ("--line", "--bg", 3.0),
             ("--line", "--surface-2", 3.0)]


def bar(req, strict):
    """AA for an ordinary palette; AAA body text and AA non-text for one that
    advertises itself as high-contrast. Clearing exactly the same 4.5 as the
    rose theme would make that label a lie."""
    if not strict:
        return req
    return 7.0 if req == 4.5 else 4.5


for name, pat in BLOCKS.items():
    t = dict(base)
    t.update(hexes(block(pat)))
    strict = name in HIGH_CONTRAST
    pairs = PAIRS + STRUCTURE if strict else PAIRS
    bad = [f"{a} on {b} {ratio(t[a], t[b]):.2f} < {bar(req, strict)}"
           for a, b, req in pairs if ratio(t[a], t[b]) < bar(req, strict)]
    check(f"{name}: all {len(pairs)} pairs pass"
          + (" at the high-contrast bar" if strict else ""),
          not bad, "; ".join(bad))

# ── 4b. the semantic colours stay tellable apart ─────────────────────────
# CLAUDE.md records this constraint for the green theme in prose ("--ok has 28
# degrees of hue clearance and no more") and prose does not fail a build. A
# red-accented palette is where it bites hardest: --bad is red, so error state
# can end up indistinguishable from ordinary chrome — every button and border
# reading as an alarm. Hue angle alone is the wrong measure (it ignores how far
# apart two colours are in lightness), so this is CIE76 dE over the whole pair.
#
# The floor is not invented: 25 is what the TIGHTEST shipped pair already
# clears — rose's --accent-lit against --bad-lit, at 25.2. It says no new
# palette may make error harder to spot than rose already does.
print("\nsemantic colours stay distinguishable")
DE_FLOOR = 25.0
DE_PAIRS = [("--accent", "--bad"), ("--accent-lit", "--bad-lit"),
            ("--ok", "--bad"), ("--accent", "--ok"), ("--gold", "--bad")]


def _lab(h):
    def f(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb(h))
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def k(v):
        return v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116
    fx, fy, fz = k(x), k(y), k(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a, b):
    return sum((p - q) ** 2 for p, q in zip(_lab(a), _lab(b))) ** 0.5


for name, pat in BLOCKS.items():
    t = dict(base)
    t.update(hexes(block(pat)))
    bad = [f"{a} vs {b} dE {delta_e(t[a], t[b]):.1f} < {DE_FLOOR}"
           for a, b in DE_PAIRS if delta_e(t[a], t[b]) < DE_FLOOR]
    check(f"{name}: error, ok and chrome are {DE_FLOOR}+ dE apart",
          not bad, "; ".join(bad))

# ── 4d. chips: coloured text on a wash of a colour ───────────────────────
# A token pair measures a colour against a FLAT ground, and roughly twenty
# rules in this file do not have one: they paint `rgba(var(--x-rgb), .16)` and
# then write `var(--x-lit)` on top of it. The wash moves the ground toward the
# text — by about a fifth of the ratio, measured — so a pair that clears the
# bar on --surface can miss it on the chip. Live-measured in a browser at the
# time of writing: `.blk-tag.ex` came out at 6.33 on the daylight palette while
# its token pair read 7.94.
#
# The pairs are SCANNED OUT OF THE STYLESHEET rather than listed, so a new chip
# is covered the day it is written and a deleted one stops being checked. The
# ground is approximated as --surface and --surface-2, which is where these
# chips actually sit; a chip on some third ground would be measured slightly
# optimistically, which is the honest limit of a static check.
print("\nchips: text on a wash of its own colour")
CHIP = re.compile(r"background:\s*rgba\(var\((--[a-z0-9-]+)-rgb\),\s*(\.\d+)\)[^;]*;"
                  r"\s*color:\s*var\((--[a-z0-9-]+)\)")
CHIPS = sorted({(m.group(1), float(m.group(2)), m.group(3)) for m in CHIP.finditer(CSS)})
check("the chip scan still finds them", len(CHIPS) >= 6, f"found {len(CHIPS)}")


def composite(fg, bg, a):
    f, b = rgb(fg), rgb(bg)
    return "#%02X%02X%02X" % tuple(round(f[i] * a + b[i] * (1 - a)) for i in range(3))


for name, pat in BLOCKS.items():
    t = dict(base)
    t.update(hexes(block(pat)))
    strict = name in HIGH_CONTRAST
    need = bar(4.5, strict)
    bad = []
    for tint, alpha, fg in CHIPS:
        for ground in ("--surface", "--surface-2"):
            g = composite(t[tint], t[ground], alpha)
            r = ratio(t[fg], g)
            if r < need:
                bad.append(f"{fg} on {tint}@{alpha} over {ground} {r:.2f} < {need}")
    check(f"{name}: all {len(CHIPS) * 2} chip composites pass", not bad, "; ".join(bad))

# ── 4c. every palette in app.js exists in the CSS, and vice versa ────────
print("\nthe cycle and the stylesheet agree")
th = re.search(r"const THEMES = \[(.*?)\];", APP, re.S)
check("app.js declares THEMES", bool(th))
if th:
    js_ids = set(re.findall(r"\['([a-z0-9-]+)'", th.group(1)))
    # 'rose' is the default and lives in :root with no attribute of its own.
    css_ids = set(re.findall(r'\[data-theme="([a-z0-9-]+)"\]\s*\{', CSS)) | {"rose"}
    check("every theme in the cycle has a palette", not (js_ids - css_ids),
          str(sorted(js_ids - css_ids)))
    check("every palette is reachable from the cycle", not (css_ids - js_ids),
          str(sorted(css_ids - js_ids)))
    check("the head boot script special-cases the default only",
          "!== 'rose'" in (ROOT / "web" / "index.html").read_text())

# ── 5. the export must be able to carry every token ──────────────────────
print("\nthe export carries the theme")
m = re.search(r"const CK_TOKENS = \[(.*?)\];", APP, re.S)
check("app.js declares CK_TOKENS", bool(m))
if m:
    carried = set(re.findall(r"'([a-z0-9-]+)'", m.group(1)))
    want = {k.lstrip("-") for k in base if k not in NON_COLOUR}
    want |= {k.lstrip("-") + "-rgb" for k in triplets(block(BLOCKS["rose (:root)"]))}
    want |= {k.lstrip("-") for k in scalars(block(BLOCKS["rose (:root)"]))}
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
