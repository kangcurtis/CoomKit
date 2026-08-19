#!/usr/bin/env python3
"""Danbooru tag data and artist blending, for the tag-dialect image models.

Two very different things live here.

**The curated tag sets** (`tags/tagsets.json`) are original work — fifteen
hand-picked lists covering framing, lighting, hair, pose and so on, with notes
about which ones fight each other. They ship.

**The 150k-tag Danbooru database ships, gzipped, as a fallback.** An earlier
version of this tooling declined to redistribute it — everyone running this
already has a copy, because tag autocomplete is near-universal in a ComfyUI
install — and that reasoning is
still sound for the common case, so a copy found in ComfyUI still wins. What it
did not cover is the machine where ComfyUI is remote, or fresh, or absent: there
the artist blender had nothing to roll from and said so, which reads as broken
rather than as a missing optional extra. `tags/danbooru.csv.gz` is 1.5 MB and
loads through stdlib `gzip`. Provenance is in `tags/NOTICE.md`.

Artist blending is the interesting half. Anima and other booru-lineage models
respond enormously to artist tags — it is the single strongest style lever
they have — but a randomly chosen artist out of 59,201 is usually one the
model has never seen. Sampling is therefore weighted by post count, which is a
decent proxy for "did this survive into the training set".
"""
import csv
import gzip
import io
import json
import random
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TAGSETS_PATH = ROOT / "tags" / "tagsets.json"
# The shipped snapshot. Last resort, never first choice — see find_db.
BUNDLED_DB = ROOT / "tags" / "danbooru.csv.gz"

# Where tag autocomplete databases live. First hit wins; a configured path
# beats all of them. Format is Danbooru's CSV export:
#     name,category,post_count,"comma,separated,aliases"
KNOWN_DB_PATHS = [
    "custom_nodes/comfyui-custom-scripts/user/autocomplete.txt",
    "custom_nodes/ComfyUI-Custom-Scripts/user/autocomplete.txt",
    "custom_nodes/comfyui-custom-scripts/user/autocomplete.csv",
    "user/default/ComfyUI-Custom-Scripts/autocomplete.txt",
]

CATEGORIES = {0: "general", 1: "artist", 3: "copyright", 4: "character",
              5: "meta"}

_lock = threading.Lock()
_cache = {"path": None, "rows": None, "by_cat": None, "source": "none",
          "problem": ""}


# --------------------------------------------------------------------------
# Curated sets (shipped)
# --------------------------------------------------------------------------

def tagsets() -> list:
    try:
        return json.loads(TAGSETS_PATH.read_text()).get("sets", [])
    except (OSError, json.JSONDecodeError):
        return []


# --------------------------------------------------------------------------
# The Danbooru database (found, not shipped)
# --------------------------------------------------------------------------

def locate(cfg: dict = None) -> tuple:
    """Find the tag database. Returns (path, source, problem).

    Precedence, and the reason for it:

    1. `tags_db` in config — an explicit path is the user saying "this one",
       and it is the only way to reach a database on a remote ComfyUI box.
       A path that is set but missing falls through rather than disabling the
       feature: a stale config key should cost freshness, not the artist
       picker. `status()` reports which source answered and names the broken
       path, so it degrades in the open rather than silently.
    2. The copy in their own ComfyUI. Theirs is the one that gets updated when
       they update autocomplete, and it is the vocabulary their own prompting
       already assumes.
    3. `tags/danbooru.csv.gz`, shipped. A snapshot, so it drifts — which is
       precisely why it sits below a live copy rather than above one.
    """
    cfg = cfg or {}
    broken = ""
    explicit = (cfg.get("tags_db") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return p, "config", ""
        broken = f"tags_db points at {p}, which does not exist"

    roots = []
    if cfg.get("comfyui_path"):
        roots.append(Path(cfg["comfyui_path"]).expanduser())
    roots += [Path.home() / "bin" / "ComfyUI", Path.home() / "ComfyUI",
              ROOT.parent / "ComfyUI"]
    for root in roots:
        for rel in KNOWN_DB_PATHS:
            p = root / rel
            if p.exists():
                return p, "comfyui", broken
    if BUNDLED_DB.exists():
        return BUNDLED_DB, "bundled", broken
    return None, "none", broken


def find_db(cfg: dict = None) -> Path:
    """Just the path. `locate` is the one that also says where it came from."""
    return locate(cfg)[0]


def _problem(cfg: dict = None) -> str:
    return locate(cfg)[2]


def _open(path: Path):
    """Text handle for a tag database, gzipped or not.

    Sniffing the magic number rather than trusting the suffix: people rename
    these, and a .txt that is actually gzip fails with a UnicodeDecodeError
    two hundred lines away from the cause.
    """
    head = path.open("rb").read(2)
    if head == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open(encoding="utf-8", errors="replace")


def load(cfg: dict = None, force: bool = False) -> list:
    """Parse the database once and keep it. Returns [(name, cat, count)]."""
    path, source, problem = locate(cfg)
    with _lock:
        if not force and _cache["rows"] is not None and _cache["path"] == path:
            return _cache["rows"]
        rows = []
        if path:
            try:
                with _open(path) as f:
                    for r in csv.reader(f):
                        if len(r) < 3:
                            continue
                        try:
                            rows.append((r[0].strip(), int(r[1]), int(r[2])))
                        except ValueError:
                            continue
            except (OSError, EOFError, gzip.BadGzipFile):
                rows = []
        by_cat = {}
        for name, cat, count in rows:
            by_cat.setdefault(cat, []).append((name, count))
        for cat in by_cat:
            by_cat[cat].sort(key=lambda x: -x[1])
        _cache.update(path=path, rows=rows, by_cat=by_cat, source=source,
                      problem=problem)
        return rows


def status(cfg: dict = None) -> dict:
    rows = load(cfg)
    by_cat = _cache["by_cat"] or {}
    return {
        "found": bool(rows),
        "path": str(_cache["path"]) if _cache["path"] else "",
        # Which of the three sources answered. The UI says "yours" vs "the one
        # we shipped" differently, and a stale bundled snapshot is worth
        # admitting to rather than presenting as authoritative.
        "source": _cache.get("source", "none"),
        # A broken `tags_db` no longer disables the picker, so it has to be
        # reported instead of just absorbed — otherwise a typo is invisible
        # forever and the user wonders why their newer corpus never applies.
        "problem": _cache.get("problem", ""),
        "bundled": bool(BUNDLED_DB.exists()),
        "total": len(rows),
        "categories": {CATEGORIES.get(c, str(c)): len(v)
                       for c, v in sorted(by_cat.items())},
        "tagsets": [{"id": s["id"], "label": s.get("label", s["id"]),
                     "hint": s.get("hint", ""), "tags": s.get("tags", [])}
                    for s in tagsets()],
    }


def _pretty(tag: str) -> str:
    """Danbooru stores underscores; prompts want spaces.

    Parentheses stay escaped — `hammer_(sunset_beach)` becomes
    `hammer \\(sunset beach\\)`, because a bare paren is weighting syntax in
    booru-lineage samplers and would silently change the emphasis instead of
    naming the artist.
    """
    out = tag.replace("_", " ")
    return out.replace("(", r"\(").replace(")", r"\)")


def search(q: str, category=None, limit: int = 25, cfg: dict = None) -> list:
    """Tags matching `q`, most-used first. Prefix matches rank above the rest."""
    load(cfg)
    q = (q or "").strip().lower().replace(" ", "_")
    by_cat = _cache["by_cat"] or {}
    pool = []
    cats = [category] if category is not None else list(by_cat)
    for c in cats:
        pool += [(n, c, k) for n, k in by_cat.get(c, [])]
    if not q:
        pool.sort(key=lambda x: -x[2])
        hits = pool[:limit]
    else:
        starts = [t for t in pool if t[0].startswith(q)]
        contains = [t for t in pool if q in t[0] and not t[0].startswith(q)]
        starts.sort(key=lambda x: -x[2])
        contains.sort(key=lambda x: -x[2])
        hits = (starts + contains)[:limit]
    return [{"tag": n, "prompt": _pretty(n), "category": CATEGORIES.get(c, str(c)),
             "count": k} for n, c, k in hits]


def random_artists(n: int = 2, min_posts: int = 500, seed=None,
                   cfg: dict = None) -> list:
    """Pick `n` artists, weighted by post count.

    Uniform sampling over 59,201 artist tags returns names the model has never
    seen — most of that tail has a handful of posts. Weighting by count keeps
    the roll inside the part of the distribution the model actually learned,
    while still being a roll.
    """
    load(cfg)
    pool = [(name, count) for name, count in (_cache["by_cat"] or {}).get(1, [])
            if count >= min_posts]
    if not pool:
        return []
    rng = random.Random(seed)
    weights = [c for _, c in pool]
    picked, seen = [], set()
    for _ in range(min(n, len(pool)) * 8):
        if len(picked) >= n:
            break
        name, count = rng.choices(pool, weights=weights, k=1)[0]
        if name in seen:
            continue
        seen.add(name)
        picked.append({"tag": name, "prompt": _pretty(name), "count": count})
    return picked


def artist_clause(artists: list, weight: float = 1.0) -> str:
    """Render chosen artists as a prompt fragment.

    `(by artist a:1.15)` — booru-lineage models take weighting syntax, and the
    "by" prefix is how the style tags were captioned.
    """
    names = [a["prompt"] if isinstance(a, dict) else _pretty(str(a))
             for a in artists or []]
    names = [n for n in names if n]
    if not names:
        return ""
    body = "by " + ", ".join(names)
    if abs(weight - 1.0) < 0.01:
        return body
    return f"({body}:{weight:.2f})"


def resolve_artists(visual: dict, seed=None, cfg: dict = None) -> list:
    """Work out this generation's artists from a character's visual config.

    visual.artist_mode: "off" | "pinned" | "random"
    visual.artists:     [{tag, prompt, count}] when pinned
    visual.artist_count / artist_min_posts for the random roll
    """
    visual = visual or {}
    mode = visual.get("artist_mode", "off")
    if mode == "pinned":
        return list(visual.get("artists") or [])
    if mode == "random":
        return random_artists(int(visual.get("artist_count", 2)),
                              int(visual.get("artist_min_posts", 500)),
                              seed=seed, cfg=cfg)
    return []
