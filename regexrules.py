#!/usr/bin/env python3
"""Find/replace rules — SillyTavern's regex scripts, honestly ported.

This is the one ST feature CoomKit deliberately skipped, and importing real
presets is what forced the issue: one of the three presets used to validate
`stimport` carries eleven regex scripts under `extensions.regex_scripts`, and
without them the preset's whole visual language — folded ledgers, hidden
thinking, collapsible logs — simply does not happen. The import looked like it
worked and produced a wall of raw markup.

## What a rule is

`{pattern, replace, on_prompt, on_display, ...}` — a compiled pattern, a
replacement, and two independent booleans saying where it applies:

  on_prompt   rewrites what the MODEL reads, on the way into the request
  on_display  rewrites what YOU read, on the way to the screen

They are orthogonal, exactly as in ST (`promptOnly` / `markdownOnly`), and
both can be on. What is *not* supported is ST's fourth state — neither flag
set — which there means "permanently rewrite the stored message". CoomKit
stores messages with `{{user}}` intact precisely so the log stays portable and
re-resolves when you switch persona; a regex that edits the stored text is the
same class of mistake as baking macros in at write time. Such a rule is
imported as both-on instead, and the import says so.

## Why the conversion is not a copy

ST stores `findRegex` as a JavaScript regex literal — `/foo/gi` — and
`replaceString` with `$1` group references. Python's `re` wants a bare
pattern, its own flags, and `\\1`. Most patterns survive a naive strip; the
replacements do not, and neither do named groups. `compile_js` handles the
six differences that actually occur, and a pattern it cannot convert is
stored **disabled with the error attached** rather than silently never firing.
A rule that does nothing and says nothing is worse than one you can see is
broken.

## The HTML question

Six of those eleven scripts exist only to wrap text in `<details>` so it
folds. CoomKit renders model output with `textContent` — "model output is
never trusted as markup" — so importing them naively would print the tags.
Widening that rule for model output is not acceptable, so the compromise is
narrow: the *output of a display rule* goes through `sanitize`, an allowlist
of layout tags with a filtered `style` attribute. Anything outside the list is
escaped back to visible text. The model can therefore never introduce markup;
only a rule the user installed can, and only from this list.
"""
import html
import re
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# JS regex -> Python
# --------------------------------------------------------------------------

_LITERAL = re.compile(r"^/(.*)/([gimsuyd]*)$", re.S)

# Constructs Python's `re` has no equivalent for. Reported by name at import
# time so the user knows which rule is dead and why.
_UNSUPPORTED = [
    (re.compile(r"\\p\{", re.I), r"\p{...} unicode property escapes"),
    (re.compile(r"\(\?<[=!]"), "lookbehind (Python needs it fixed-width)"),
]


class RuleError(ValueError):
    pass


def _convert_pattern(body: str) -> str:
    """JS pattern body -> Python pattern body."""
    # Named groups: (?<name>...) -> (?P<name>...), \k<name> -> (?P=name).
    # The negative/positive lookbehind forms (?<= (?<! must NOT be touched.
    body = re.sub(r"\(\?<(?![=!])([A-Za-z_]\w*)>", r"(?P<\1>", body)
    body = re.sub(r"\\k<([A-Za-z_]\w*)>", r"(?P=\1)", body)
    return body


def _convert_replacement(repl: str) -> str:
    """JS replacement template -> Python replacement template.

    Order matters and is the part that is easy to get wrong: every backslash
    has to be doubled FIRST, because a literal `\\n` in the user's replacement
    is a backslash-n they want printed, not a group reference. Only then are
    the `$` forms rewritten.
    """
    out = repl.replace("\\", "\\\\")
    out = out.replace("$$", "\0DOLLAR\0")
    out = re.sub(r"\$&", r"\\g<0>", out)
    out = re.sub(r"\$<([A-Za-z_]\w*)>", r"\\g<\1>", out)
    out = re.sub(r"\$(\d{1,2})", r"\\\1", out)
    return out.replace("\0DOLLAR\0", "$")


def compile_js(find: str, replace: str = "") -> dict:
    """Compile a JS-style rule. Returns {re, replace, count}.

    Raises RuleError with a sentence a human can act on.
    """
    find = (find or "").strip()
    if not find:
        raise RuleError("empty pattern")
    m = _LITERAL.match(find)
    if m:
        body, flags = m.group(1), m.group(2)
    else:
        # Not delimited — treat the whole string as the pattern. ST's own UI
        # accepts both, and hand-written rules almost never use delimiters.
        body, flags = find, "g"

    for rx, why in _UNSUPPORTED:
        if rx.search(body):
            raise RuleError(f"uses {why}, which Python's re cannot express")

    py = 0
    if "i" in flags:
        py |= re.I
    if "m" in flags:
        py |= re.M
    if "s" in flags:
        py |= re.S
    try:
        compiled = re.compile(_convert_pattern(body), py)
    except re.error as exc:
        raise RuleError(f"not a valid pattern: {exc}") from exc
    return {"re": compiled, "replace": _convert_replacement(replace or ""),
            # No /g means replace once. ST defaults to global in practice, and
            # a rule written without it almost always still means "all".
            "count": 0 if "g" in flags else 1}


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def prepare(rows: list) -> list:
    """Compile stored rows once. Bad ones are dropped, not raised."""
    out = []
    for row in rows or []:
        if not row.get("enabled", 1):
            continue
        try:
            c = compile_js(row.get("pattern", ""), row.get("replace", ""))
        except RuleError:
            continue
        out.append({**row, "_c": c})
    return out


def apply(text: str, rules: list, scope: str, depth: int = None) -> str:
    """Run every rule whose scope matches. `rules` comes from prepare().

    `depth` is how far back this message sits (0 = most recent), for the
    min/max depth gating ST rules use to leave the recent turns alone.
    """
    if not text or not rules:
        return text or ""
    key = "on_prompt" if scope == "prompt" else "on_display"
    for rule in rules:
        if not rule.get(key):
            continue
        if depth is not None:
            lo, hi = rule.get("min_depth"), rule.get("max_depth")
            if lo is not None and depth < lo:
                continue
            if hi is not None and depth > hi:
                continue
        c = rule["_c"]
        try:
            text = c["re"].sub(c["replace"], text, count=c["count"])
        except re.error:
            continue
        for s in rule.get("trim") or []:
            text = text.replace(s, "")
    return text


# --------------------------------------------------------------------------
# The narrow HTML allowlist
# --------------------------------------------------------------------------
# Only what the imported presets actually use to fold and group text. No
# links, no images, no media, no forms, nothing that loads or navigates.

ALLOWED = {
    "details", "summary", "div", "span", "p", "br", "hr",
    "b", "strong", "i", "em", "u", "s", "code", "pre", "small",
    "ul", "ol", "li", "blockquote", "h3", "h4",
}
VOID = {"br", "hr"}
# `style` only, and only declarations that cannot fetch or execute.
# `position: fixed|sticky` is on the list for the same reason as url(): it is
# the one declaration that lets a bubble escape its bubble and cover the page.
_STYLE_BAD = re.compile(
    r"(url\s*\(|expression\s*\(|javascript:|@import|;\s*behavior"
    r"|position\s*:\s*(fixed|sticky))", re.I)


class _Sanitiser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.out.append(html.escape(self.get_starttag_text() or ""))
            return
        style = ""
        for k, v in attrs:
            if k.lower() == "style" and v and not _STYLE_BAD.search(v):
                style = f' style="{html.escape(v, quote=True)}"'
        if tag in VOID:
            self.out.append(f"<{tag}{style}>")
        else:
            self.out.append(f"<{tag}{style}>")
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in ALLOWED:
            self.out.append(f"<{tag}>")
        else:
            self.out.append(html.escape(self.get_starttag_text() or ""))

    def handle_endtag(self, tag):
        if tag in ALLOWED and tag not in VOID and tag in self.open:
            # Close everything opened after it too, so a stray </div> cannot
            # unbalance the document and swallow the rest of the page.
            while self.open:
                last = self.open.pop()
                self.out.append(f"</{last}>")
                if last == tag:
                    break
        elif tag not in ALLOWED:
            self.out.append(html.escape(f"</{tag}>"))

    def handle_data(self, data):
        self.out.append(html.escape(data))

    def result(self) -> str:
        while self.open:
            self.out.append(f"</{self.open.pop()}>")
        return "".join(self.out)


def sanitize(text: str) -> str:
    """Escape everything, then re-permit the layout tags on the allowlist."""
    p = _Sanitiser()
    p.feed(text or "")
    p.close()
    return p.result()


def has_markup(text: str) -> bool:
    """Cheap test for whether sanitising is worth doing at all."""
    return "<" in (text or "")


# --------------------------------------------------------------------------
# SillyTavern import
# --------------------------------------------------------------------------

def from_st(script: dict) -> dict:
    """One ST regex script -> one CoomKit rule (+ a note when we changed it)."""
    name = (script.get("scriptName") or "unnamed").strip()[:80]
    md_only = bool(script.get("markdownOnly"))
    prompt_only = bool(script.get("promptOnly"))
    note = ""
    if md_only and prompt_only:
        on_prompt, on_display = True, True
    elif md_only:
        on_prompt, on_display = False, True
    elif prompt_only:
        on_prompt, on_display = True, False
    else:
        # ST's destructive state. Imported as "both views", never as a
        # rewrite of the stored message — see the module docstring.
        on_prompt, on_display = True, True
        note = ("edits the saved message in SillyTavern; imported as a "
                "view-only rule so your log stays intact")

    rule = {
        "name": name,
        "pattern": script.get("findRegex") or "",
        "replace": script.get("replaceString") or "",
        "on_prompt": int(on_prompt),
        "on_display": int(on_display),
        "min_depth": script.get("minDepth"),
        "max_depth": script.get("maxDepth"),
        "enabled": int(not script.get("disabled")),
        "trim": [t for t in (script.get("trimStrings") or []) if t],
    }
    problem = ""
    try:
        compile_js(rule["pattern"], rule["replace"])
    except RuleError as exc:
        problem = str(exc)
        rule["enabled"] = 0
    if script.get("substituteRegex"):
        problem = problem or ("uses macro substitution inside the pattern, "
                              "which CoomKit does not run")
        rule["enabled"] = 0
    return {"rule": rule, "note": note, "problem": problem}


def scripts_in(raw) -> list:
    """Pull ST regex scripts out of whatever the user uploaded.

    Three shapes reach this: a chat-completion preset with them tucked under
    `extensions.regex_scripts`, a v3 character card with the same key, and a
    single exported script (or a bare list of them) on its own.
    """
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict) and "findRegex" in r]
    if not isinstance(raw, dict):
        return []
    if "findRegex" in raw:
        return [raw]
    for holder in (raw, raw.get("data") or {}):
        ext = holder.get("extensions") if isinstance(holder, dict) else None
        if isinstance(ext, dict) and isinstance(ext.get("regex_scripts"), list):
            return [r for r in ext["regex_scripts"] if isinstance(r, dict)]
    return []


def summarise_import(results: list) -> dict:
    """What to tell the user before they accept a batch of imported rules."""
    return {
        "total": len(results),
        "enabled": sum(1 for r in results if r["rule"]["enabled"]),
        "display": sum(1 for r in results if r["rule"]["on_display"]),
        "prompt": sum(1 for r in results if r["rule"]["on_prompt"]),
        "problems": [{"name": r["rule"]["name"], "why": r["problem"]}
                     for r in results if r["problem"]],
        "notes": [{"name": r["rule"]["name"], "why": r["note"]}
                  for r in results if r["note"]],
    }
