#!/usr/bin/env python3
"""Prompt blocks: ordering, roles, markers, depth, gating, ST import.

Offline and free. Every assertion here corresponds to something observed in
three real shared SillyTavern presets — the failure modes are theirs, not
hypothetical.
"""

import _bootstrap  # noqa: F401  — repo root on sys.path
import json
import pathlib

import blocklib
import blocks
import engine
import stimport

fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# ── 1. the default order is a complete prompt ──────────────────────
print("default order")
d = blocks.default_blocks()
ids = [b["id"] for b in d]
check("every block has a unique id", len(ids) == len(set(ids)))
check("history is a marker", any(b["marker"] == "history" for b in d))
check("the card is a marker", any(b["marker"] == "card" for b in d))
check("every marker names a slot the engine knows",
      all(b["marker"] in blocks.MARKERS for b in d if b["kind"] == "marker"))
check("every block declares a group",
      all(b["group"] for b in d))
check("history comes before post-history",
      ids.index("history") < ids.index("post_history"))

# ── 2. rendering ───────────────────────────────────────────────────
print("\nrendering")
SLOTS = {"card": "CARD-TEXT", "persona": "PERSONA-TEXT",
         "history": [{"role": "user", "content": "hi"},
                     {"role": "assistant", "content": "hey"}]}
msgs, depth = blocks.render(d, SLOTS)
joined = " ".join(m["content"] for m in msgs)
check("marker content is substituted", "CARD-TEXT" in joined)
check("history is spliced in as real turns",
      any(m["role"] == "assistant" and m["content"] == "hey" for m in msgs))
check("unfilled markers vanish rather than leaving a hole",
      "lore" not in joined.lower() or "LORE" not in joined)
check("no empty messages are emitted",
      all(m["content"].strip() for m in msgs))

# Order is the whole point: a block after the history marker must land after
# the conversation, which is where a reminder has the most force.
custom = [
    blocks.block("a", "before", "opening", content="BEFORE"),
    blocks.block("h", "hist", "conversation", kind="marker", marker="history"),
    blocks.block("z", "after", "conversation", content="AFTER"),
]
m2, _ = blocks.render(custom, SLOTS)
texts = [m["content"] for m in m2]
check("a block before history lands before it",
      texts.index("BEFORE") < texts.index("hi"))
check("a block after history lands after it",
      texts.index("AFTER") > texts.index("hey"))

# ── 3. roles — the handshake technique needs all three ─────────────
print("\nroles")
hs = [
    blocks.block("u", "challenge", "opening", role="user", content="Q"),
    blocks.block("a", "admission", "opening", role="assistant", content="A"),
]
m3, _ = blocks.render(hs, {})
check("user and assistant blocks keep their roles",
      [m["role"] for m in m3] == ["user", "assistant"])
check("an unknown role falls back to system",
      blocks.normalise([{"id": "x", "role": "wizard", "content": "c"}])[0]["role"]
      == "system")

# ── 4. depth placement ─────────────────────────────────────────────
print("\ndepth placement")
convo = [{"role": "user", "content": "m1"},
         {"role": "assistant", "content": "m2"},
         {"role": "user", "content": "m3"}]
out = blocks.apply_depth(convo, [{"depth": 0, "role": "system",
                                  "content": "LAST", "id": "z"}])
check("depth 0 is the last thing the model reads",
      out[-1]["content"] == "LAST")
out2 = blocks.apply_depth(convo, [{"depth": 2, "role": "system",
                                   "content": "MID", "id": "z"}])
check("depth 2 lands two messages from the end",
      [m["content"] for m in out2] == ["m1", "MID", "m2", "m3"])
multi = blocks.apply_depth(convo, [
    {"depth": 0, "role": "system", "content": "D0", "id": "a"},
    {"depth": 2, "role": "system", "content": "D2", "id": "b"}])
check("several depths stay in the right order",
      [m["content"] for m in multi] == ["m1", "D2", "m2", "m3", "D0"])
depth_block = [blocks.block("d", "reminder", "conversation", place="depth",
                            depth=0, content="REMIND")]
m4, dd = blocks.render(depth_block, {})
check("a depth block is held back from the main list", m4 == [])
check("...and reported for the caller to place", dd[0]["content"] == "REMIND")

# ── 5. exclusive groups ────────────────────────────────────────────
# ST fakes these with checkboxes and a "(Choose One)" label, so enabling two
# contradictory blocks is a normal accident.
print("\nexclusive groups")
povs = [blocks.block(f"p{i}", n, "style", exclusive="pov", content=n)
        for i, n in enumerate(["third", "second", "first"])]
res = blocks.resolve_exclusive(povs)
check("only the first of an exclusive group survives",
      [b["enabled"] for b in res] == [True, False, False])
check("the losers are marked as shadowed, not deleted",
      res[1].get("_shadowed") is True and len(res) == 3)
m5, _ = blocks.render(povs, {})
check("only one reaches the prompt", [m["content"] for m in m5] == ["third"])
check("different exclusive groups do not interfere",
      sum(b["enabled"] for b in blocks.resolve_exclusive([
          blocks.block("a", "a", "style", exclusive="pov", content="a"),
          blocks.block("b", "b", "style", exclusive="length", content="b")]))
      == 2)

# ── 6. model gating ────────────────────────────────────────────────
# The reason shared presets are monolithic: the format cannot say "this patch
# is for Claude", so authors ship every patch and ask you to toggle.
print("\nmodel gating")
check("gemma is local", blocks.family("google/gemma-4-12b") == "gemma")
check("claude is recognised", blocks.family("claude-opus-4", True) == "claude")
check("an unknown hosted model is 'remote'",
      blocks.family("some-new-thing", True) == "remote")
check("an unknown local model is 'local'",
      blocks.family("some-new-thing", False) == "local")

lib_on = [{**b, "enabled": True} for b in blocklib.library()]
for model, remote, want, unwanted in (
        ("google/gemma-4-12b", False, "lib.uncensored.local", "lib.claudeisms"),
        ("claude-opus-4", True, "lib.claudeisms", "lib.deepseekisms"),
        ("deepseek-chat", True, "lib.deepseekisms", "lib.claudeisms")):
    got = {b["id"] for b in blocks.for_model(lib_on, model, remote) if b["enabled"]}
    check(f"{model}: keeps {want.split('.')[-1]}", want in got)
    check(f"{model}: drops {unwanted.split('.')[-1]}", unwanted not in got)
check("untagged blocks survive every model",
      "lib.antirepeat" in {b["id"] for b in
                           blocks.for_model(lib_on, "claude-opus-4", True)
                           if b["enabled"]})

# ── 7. the library ─────────────────────────────────────────────────
print("\nlibrary")
lib = blocklib.library()
check("library ids are unique",
      len({b["id"] for b in lib}) == len(lib))
check("every library block explains itself", all(b["why"] for b in lib))
check("every library block has content or is a marker",
      all(b["content"].strip() or b["kind"] == "marker" for b in lib))
check("every library group is a known group",
      all(b["group"] in dict((g, l) for g, l, _ in blocks.GROUPS) for b in lib),
      str({b["group"] for b in lib} - {g for g, _, _ in blocks.GROUPS}))
# A local starter that costs what a hosted one costs would defeat the point.
local, remote = blocklib.starter("local"), blocklib.starter("remote")
lt = sum(engine.rough_tokens(b["content"]) for b in local)
rt = sum(engine.rough_tokens(b["content"]) for b in remote)
check("both starters are enabled", all(b["enabled"] for b in local + remote))
check("the local starter is small", lt < 700, f"{lt} tokens")
check("the local starter is smaller than the hosted one", lt < rt,
      f"local {lt} vs remote {rt}")
check("both starters are a fraction of a shared ST preset", rt < 3000,
      f"{rt} tokens")

# ── 8. merge keeps new built-ins ───────────────────────────────────
# Otherwise adding a feature silently disables it for everyone with a saved
# preset.
print("\nmerge")
old = [{"id": "history", "kind": "marker", "marker": "history"}]
merged = blocks.merge(old)
check("a stored preset keeps its own order", merged[0]["id"] == "history")
check("built-ins it has never seen are appended",
      "director" in {b["id"] for b in merged})
check("nothing is duplicated",
      len({b["id"] for b in merged}) == len(merged))

# ── 9. squash ──────────────────────────────────────────────────────
print("\nsquash")
sq = blocks.squash([{"role": "system", "content": "a"},
                    {"role": "system", "content": "b"},
                    {"role": "user", "content": "c"}])
check("adjacent system messages merge", len(sq) == 2)
check("their content is joined", "a" in sq[0]["content"] and "b" in sq[0]["content"])
check("other roles are untouched", sq[1]["content"] == "c")

# ── 10. token cost ─────────────────────────────────────────────────
print("\ncost")
c = blocks.cost(d, SLOTS, engine.rough_tokens)
check("cost is reported per block", len(c["blocks"]) == len(d))
check("the total is the sum of what is on",
      c["total"] == sum(b["tokens"] for b in c["blocks"]))
check("disabled blocks cost nothing",
      all(b["tokens"] == 0 for b in c["blocks"] if b["off"]))

# ── 11. importing a real SillyTavern preset ────────────────────────
print("\nsillytavern import")
FIXTURE = {
    "prompts": [
        {"identifier": "sep1", "name": "━━━( TONE )━━━", "content": "",
         "role": "system", "marker": False},
        {"identifier": "t1", "name": "Everyday tone", "content": "be dry",
         "role": "system", "marker": False},
        {"identifier": "sep2", "name": "=== POV (Choose One) ===",
         "content": "", "role": "system", "marker": False},
        {"identifier": "p1", "name": "Third", "content": "third person",
         "role": "system", "marker": False},
        {"identifier": "p2", "name": "Second", "content": "second person",
         "role": "system", "marker": False},
        {"identifier": "chatHistory", "name": "Chat History", "marker": True},
        {"identifier": "d0", "name": "turn rules", "content": "stay put",
         "role": "user", "marker": False, "injection_position": 1,
         "injection_depth": 0},
        {"identifier": "off", "name": "Disabled thing", "content": "nope",
         "role": "system", "marker": False},
        {"identifier": "hs", "name": "Handshake", "content": "yes",
         "role": "assistant", "marker": False},
    ],
    "prompt_order": [{"character_id": 100001, "order": [
        {"identifier": "sep1", "enabled": True},
        {"identifier": "t1", "enabled": True},
        {"identifier": "sep2", "enabled": True},
        {"identifier": "p1", "enabled": True},
        {"identifier": "p2", "enabled": True},
        {"identifier": "chatHistory", "enabled": True},
        {"identifier": "d0", "enabled": True},
        {"identifier": "off", "enabled": False},
        {"identifier": "hs", "enabled": True},
    ]}],
    "temperature": 1.1, "top_p": 0.95, "openai_max_tokens": 900,
}
r = stimport.convert(FIXTURE)
got = {b["id"]: b for b in r["blocks"]}
check("separators are dropped", r["dropped"]["separators"] == 2)
check("disabled blocks are dropped", r["dropped"]["disabled"] == 1)
check("real blocks survive", "st.t1" in got)
check("markers are mapped to our slots",
      got["st.chatHistory"]["marker"] == "history")
check("depth injection is preserved",
      got["st.d0"]["place"] == "depth" and got["st.d0"]["depth"] == 0)
check("roles are preserved", got["st.hs"]["role"] == "assistant")
check("'(Choose One)' becomes a real exclusive group",
      got["st.p1"]["exclusive"] and
      got["st.p1"]["exclusive"] == got["st.p2"]["exclusive"])
check("a tone block is grouped as style", got["st.t1"]["group"] == "style")
check("samplers come across",
      r["samplers"]["temperature"] == 1.1 and r["samplers"]["max_tokens"] == 900)
check("ornament is stripped from names", got["st.t1"]["name"] == "Everyday tone")
check("an imported preset renders",
      len(blocks.render(r["blocks"], SLOTS)[0]) > 0)
check("a non-preset is rejected clearly",
      not stimport.looks_like_st({"nope": 1}))

# The real files, if they happen to be around — never required.
# Optional local fixtures: drop real SillyTavern presets in ./st-presets/ to
# exercise the importer against them. Never required, never shipped.
REAL = _bootstrap.ROOT / "st-presets"
if REAL.is_dir():
    for f in sorted(REAL.glob("*.json")):
        try:
            data = json.loads(f.read_bytes().decode("utf-8", "replace"))
            res = stimport.convert(data)
            blocks.render(res["blocks"], SLOTS)
            check(f"real preset imports: {f.name[:30]}", bool(res["blocks"]))
        except Exception as exc:  # noqa: BLE001
            check(f"real preset imports: {f.name[:30]}", False, str(exc)[:90])

# ── 12. context handling ───────────────────────────────────────────
# The history budget is computed against this number, so an implausible one
# is not a cosmetic problem: a 1,000,000 context means history is never
# trimmed and the first long chat overflows whatever model is connected.
print("\ncontext")
def _st(ctx, unlocked=False):
    return {"prompts": [{"identifier": "a", "name": "A", "content": "x",
                         "role": "system", "marker": False}],
            "prompt_order": [{"character_id": 1,
                              "order": [{"identifier": "a", "enabled": True}]}],
            "openai_max_context": ctx, "max_context_unlocked": unlocked}
check("a plausible context is carried",
      stimport.convert(_st(32000))["context"] == 32000)
check("ST's unlocked slider is rejected",
      stimport.convert(_st(1000000))["context"] == 0)
check("...and says why",
      any("unlocked" in n for n in stimport.convert(_st(1000000))["notes"]))
check("a nonsense small value is rejected",
      stimport.convert(_st(12))["context"] == 0)
check("128k is still plausible",
      stimport.convert(_st(128000))["context"] == 128000)

# ── 13. the phone's roleplay awareness ─────────────────────────────
# A texting thread is a separate chat row, so she has no idea what happened
# face to face unless the slot is filled. Opt-in: some people want the phone
# to be a clean slate.
print("\nsms awareness")
check("rp is a known marker", "rp" in blocks.MARKERS)
rp_block = [b for b in blocks.default_blocks() if b["marker"] == "rp"]
check("the default order carries an rp block", len(rp_block) == 1)
check("it sits before the history",
      [b["id"] for b in blocks.default_blocks()].index("rp")
      < [b["id"] for b in blocks.default_blocks()].index("history"))
msgs, _ = blocks.render(blocks.default_blocks(), {**SLOTS, "rp": ""})
check("an empty rp slot injects nothing",
      not any("in person" in m["content"] for m in msgs))
msgs, _ = blocks.render(blocks.default_blocks(),
                        {**SLOTS, "rp": "IN-PERSON-DIGEST"})
check("a filled rp slot reaches the prompt",
      any("IN-PERSON-DIGEST" in m["content"] for m in msgs))

print()
if fails:
    print(f"BLOCK TESTS FAILED ({len(fails)}): " + ", ".join(fails))
    raise SystemExit(1)
print("PROMPT BLOCK TESTS PASS")
