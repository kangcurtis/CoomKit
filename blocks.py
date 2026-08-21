#!/usr/bin/env python3
"""CoomKit prompt blocks — one ordered list, and that is the whole prompt.

SillyTavern got the core idea right: a prompt is an ordered list of blocks,
each with a role, each toggleable, with placeholders marking where the engine
drops its own content. Everything downstream of that idea went wrong.

Three files of shared presets were read closely before this was written. What
they showed:

  * A 123-block preset spending **24,300 tokens** before the character is
    described, with no indication anywhere in the UI that it does.
  * 21 of one preset's 64 active blocks being **empty separator bars** —
    `━━━( RP TYPE )━━━` with no content — because the flat list has no
    grouping. Pure UI debt, serialised and shared worldwide.
  * "Choose one" radio groups faked with checkboxes and documented in the
    label, because the format cannot express exclusivity.
  * 302 blocks shipped so that 64 could be used. That is a library being
    posted as a preset.
  * Most active blocks being *model-defect patches* — Anti-Claudisms,
    Anti-Geminisms, Anti-Deepseekisms, Anti-Slop, Anti-Repetition — bundled
    together because the format cannot say "this one is for Claude".

So blocks here carry the four things that were missing: a **group** (real
nesting, no separator hacks), **exclusive** (real radio groups), **models**
(who actually needs this), and **why** (what it is for, in a sentence). And
CoomKit's own injected layers are blocks in the same list rather than a
parallel system, so there is one order, one inspector, one mental model.

A block is either:
  kind="text"    literal content, sent as system/user/assistant
  kind="marker"  a slot the engine fills — the card, the persona, the
                 lorebook, memory, example dialogue, the chat history, the
                 tool spec. Position matters enormously: a jailbreak before
                 the history is a different prompt from one after it.

Placement is either "order" (its position in this list) or "depth" (N
messages from the end of the conversation, counting backwards — depth 0 is
after the last message, the last thing the model reads).
"""
import copy

# Markers the engine knows how to fill. Anything else is ignored rather than
# raising: a preset imported from elsewhere may name slots we do not have,
# and losing one section beats losing the whole preset.
MARKERS = {
    "card": "Her card — description, personality, and scenario",
    "persona": "Who you are in the scene",
    "lore": "Lorebook entries triggered by the conversation",
    "memory": "What she remembers about you",
    "examples": "Her example dialogue, as demonstration turns",
    "history": "The conversation so far",
    "tools": "How she asks for a picture, a voice note or a song",
    "rp": "What happened in person — the roleplay thread, for texting mode",
}

ROLES = ("system", "user", "assistant")


def block(bid, name, group, why="", content="", role="system", kind="text",
          marker="", place="order", depth=0, enabled=True, exclusive="",
          models=None, builtin=False, layer=""):
    """One block. `layer` ties a built-in block to its prompts.py key."""
    return {
        "id": bid, "name": name, "group": group, "why": why,
        "content": content, "role": role, "kind": kind, "marker": marker,
        "place": place, "depth": int(depth), "enabled": bool(enabled),
        "exclusive": exclusive, "models": list(models or []),
        "builtin": bool(builtin), "layer": layer,
    }


# --------------------------------------------------------------------------
# The default order — CoomKit's own prompt as a block list
# --------------------------------------------------------------------------
# This IS the shipped behaviour. Reordering these is a supported thing to do,
# which is the point of putting them here instead of burying the order in
# engine.assemble.

def default_blocks() -> list:
    return [
        block("jailbreak", "Jailbreak", "opening",
              why="Whatever the selected jailbreak carries. Local models "
                  "usually need one line; hosted ones need the essay.",
              layer="__jailbreak__", builtin=True),
        block("card", "Character card", "character", kind="marker",
              marker="card", builtin=True,
              why="Her description, personality and scenario."),
        block("persona", "Your persona", "character", kind="marker",
              marker="persona", builtin=True,
              why="Who you are, so she is not talking to a blank."),
        block("lore", "Lorebook", "character", kind="marker", marker="lore",
              builtin=True, why="Entries triggered by what has been said."),
        block("memory", "Memory", "character", kind="marker", marker="memory",
              builtin=True,
              why="Durable facts about you, this relationship, this scene."),
        block("sms", "Texting mode", "style", layer="sms", builtin=True,
              enabled=True,
              why="Only applied in 💬 chats. Short messages, no narration."),
        block("thinking", "In-character thinking", "style",
              layer="thinking_character", builtin=True,
              why="Her inner voice instead of an assistant analysing a task. "
                  "Only used when thinking style is 'in-character'."),
        # ── point of view: a real radio group, off by default ─────
        # Narration POV drifts over a long chat — the model slides between
        # "She leans in", "You feel her lean in" and "I lean in" — and a
        # card rarely pins it. These used to live in blocklib, where nobody
        # found them; they are part of the default stack now so the select
        # is always on screen. The ids keep the old `lib.` prefix on
        # purpose: merge() dedups by id, so a preset that added them from
        # the library keeps its stored copy instead of gaining twins.
        # All three ship DISABLED — "off" means the card decides, and every
        # existing prompt stays byte-identical until someone picks one.
        block("lib.pov.third", "Third person", "style", exclusive="pov",
              enabled=False, builtin=True,
              why="'She leans in.' The default for most cards.",
              content=(
                  "Narrate in third person limited, from the character's "
                  "perspective: 'She leans in.' Every turn, the whole scene. "
                  "Never slip into narrating as 'I', and never address the "
                  "user as 'you' outside the character's spoken dialogue.")),
        block("lib.pov.second", "Second person", "style", exclusive="pov",
              enabled=False, builtin=True,
              why="'You feel her lean in.' More immediate, easier to break.",
              content=(
                  "Narrate in second person, addressed to the user: 'You "
                  "feel her lean in.' Every turn, the whole scene: the "
                  "narration calls the user 'you' and describes the "
                  "character in third person. The character's spoken lines "
                  "stay in their own voice inside quotes. Never drift into "
                  "narrating the character as 'I' or into plain third-person "
                  "narration of the user.")),
        block("lib.pov.first", "First person", "style", exclusive="pov",
              enabled=False, builtin=True,
              why="'I lean in.' She narrates herself.",
              content=(
                  "Narrate in first person as the character: 'I lean in.' "
                  "Every turn, the whole scene: the character tells it as "
                  "'I', and the user is 'you'. Never drift into describing "
                  "the character's own actions in third person.")),
        block("director", "Director's note", "steering", layer="director",
              builtin=True,
              why="Your stage direction, which she obeys without mentioning."),
        block("director_note", "Director's channel", "steering",
              layer="director_note", builtin=True,
              why="Asks her to write back out of character. Only while the "
                  "director bar is open."),
        block("tools", "Generation tools", "tools", kind="marker",
              marker="tools", builtin=True,
              why="How she asks for a picture, a clip, a voice note or a "
                  "song mid-scene."),
        block("rp", "What happened in person", "character", kind="marker",
              marker="rp", builtin=True,
              why="In a texting thread, the recent roleplay with the same "
                  "character — so she can text you about what just happened "
                  "instead of starting from nothing. Off unless the thread "
                  "opts in."),
        block("examples", "Example dialogue", "character", kind="marker",
              marker="examples", builtin=True,
              why="Demonstration turns showing how she talks. Retired once "
                  "the real scene is established."),
        block("history", "Chat history", "conversation", kind="marker",
              marker="history", builtin=True,
              why="The conversation. Everything after this block lands "
                  "*after* the whole scene — which is where a reminder has "
                  "the most force."),
        block("post_history", "Post-history instructions", "conversation",
              layer="__post_history__", builtin=True,
              why="The card's own final instruction, if it has one."),
        # After the history on purpose: the last thing the model reads before
        # it writes is whose turn it is. Empty, and therefore invisible, in
        # every single-character chat — the server only fills the layer when
        # more than one person is present.
        # Fires whenever someone who used to be in the scene has gone, even
        # when only one person is left — that is exactly when it matters,
        # because her lines are still in the history and the model will keep
        # reaching for her.
        block("cast_absent", "Who has left", "conversation",
              layer="cast_absent", builtin=True,
              why="Only when a character has been sent out of the scene. "
                  "Stops the model writing for someone who walked out."),
        block("cast_turn", "Whose turn it is", "conversation",
              layer="cast_turn", builtin=True,
              why="Only when several characters are in the room. Names the "
                  "one who is speaking, right before she speaks."),
    ]


GROUPS = [
    ("opening", "Opening", "Sets the stance before anything else is said."),
    ("character", "Who she is", "The card, you, and what she remembers."),
    ("style", "How she writes", "Voice, formatting, length, point of view."),
    ("quality", "Model patches", "Fixes for the specific ways models go wrong."),
    ("content", "Content", "How explicit, how dark, how far."),
    ("steering", "Steering", "Director mode and out-of-character control."),
    ("tools", "Tools", "Letting her make things mid-scene."),
    ("conversation", "Conversation", "The scene itself, and anything after it."),
]


# --------------------------------------------------------------------------
# Ordering and assembly
# --------------------------------------------------------------------------

def normalise(raw) -> list:
    """Coerce stored data into a clean block list, dropping nothing silently.

    Presets get hand-edited and imported; a block missing half its keys should
    take its defaults rather than break the whole prompt.
    """
    out = []
    for i, b in enumerate(raw or []):
        if not isinstance(b, dict):
            continue
        bid = str(b.get("id") or b.get("identifier") or f"block{i}")
        out.append(block(
            bid, b.get("name") or bid, b.get("group") or "style",
            why=b.get("why", ""), content=b.get("content", ""),
            role=b.get("role") if b.get("role") in ROLES else "system",
            kind="marker" if (b.get("kind") == "marker" or b.get("marker")) else "text",
            marker=b.get("marker", ""), place=b.get("place", "order"),
            depth=b.get("depth", 0), enabled=b.get("enabled", True),
            exclusive=b.get("exclusive", ""), models=b.get("models"),
            builtin=b.get("builtin", False), layer=b.get("layer", "")))
    return out


def merge(stored, defaults=None) -> list:
    """A stored order, reconciled with the built-ins.

    Built-in blocks that a stored preset has never heard of are appended
    rather than dropped — otherwise adding a feature to CoomKit would
    silently disable it for everyone with a saved preset.
    """
    defaults = defaults if defaults is not None else default_blocks()
    stored = normalise(stored)
    seen = {b["id"] for b in stored}
    out = list(stored)
    for d in defaults:
        if d["id"] not in seen:
            out.append(d)
    return out


def resolve_exclusive(blocks: list) -> list:
    """Within an exclusive group, only the first enabled block survives.

    ST fakes radio groups with checkboxes and a "(Choose One)" label, so
    enabling two contradictory blocks is a normal accident. Here it is simply
    not representable in the output.
    """
    out, claimed = [], set()
    for b in blocks:
        if b["enabled"] and b.get("exclusive"):
            if b["exclusive"] in claimed:
                b = {**b, "enabled": False, "_shadowed": True}
            else:
                claimed.add(b["exclusive"])
        out.append(b)
    return out


def for_model(blocks: list, model: str = "", remote: bool = False) -> list:
    """Disable blocks tagged for a model family that is not in play.

    This is the whole reason those presets are monolithic: the format cannot
    say "this patch is for Claude", so authors ship every patch and ask you to
    toggle. Tagging makes that automatic.
    """
    fam = family(model, remote)
    out = []
    for b in blocks:
        tags = b.get("models") or []
        if b["enabled"] and tags and fam not in tags:
            b = {**b, "enabled": False, "_off_model": True}
        out.append(b)
    return out


def family(model: str, remote: bool = False) -> str:
    m = (model or "").lower()
    for needle, fam in (("claude", "claude"), ("gpt", "openai"),
                        ("o1", "openai"), ("o3", "openai"),
                        ("gemini", "gemini"), ("gemma", "gemma"),
                        ("deepseek", "deepseek"), ("kimi", "kimi"),
                        ("qwen", "qwen"), ("llama", "llama"),
                        ("mistral", "mistral"), ("glm", "glm")):
        if needle in m:
            return fam
    return "remote" if remote else "local"


def render(blocks: list, slots: dict, model: str = "", remote: bool = False):
    """Turn the block list into (messages, depth_injections).

    `slots` maps marker name -> either a string (becomes one message in the
    block's role) or a list of message dicts (spliced in as-is, which is how
    history and example turns arrive).

    Depth-placed blocks are returned separately because they are inserted
    relative to the *end* of the conversation, which the caller knows and this
    function deliberately does not.
    """
    blocks = for_model(resolve_exclusive(blocks), model, remote)
    messages, depth_items = [], []

    def tag(b):
        """Where a piece of the prompt came from, so the inspector can say so.

        Rides on the message through squash() and apply_depth() and is
        stripped at the wire in llm.build_payload — nothing here reaches the
        model. `layer` is the prompts.py key when there is one, which is what
        lets the UI offer to edit the actual text rather than just name it.
        """
        return {"id": b["id"], "name": b.get("name") or b["id"],
                "builtin": bool(b.get("builtin")),
                "layer": b.get("layer", ""), "marker": b.get("marker", "")}

    for b in blocks:
        if not b["enabled"]:
            continue
        if b["place"] == "depth" and b["kind"] == "text":
            if b["content"].strip():
                depth_items.append(
                    {"depth": b["depth"], "role": b["role"],
                     "content": b["content"], "id": b["id"], "src": tag(b)})
            continue
        if b["kind"] == "marker":
            filled = slots.get(b["marker"])
            if not filled:
                continue
            if isinstance(filled, str):
                messages.append({"role": b["role"], "content": filled,
                                 "src": tag(b)})
            else:
                # The dict's own role wins when it HAS one — history and
                # example turns carry real user/assistant roles and must keep
                # them. When it does not, the BLOCK's role applies, so a user
                # who moved their card block to role:user is honoured instead
                # of silently overridden. A slot-provided `src` also wins, so
                # the inspector can name each cast member separately rather
                # than pricing them as one lump.
                messages.extend({"role": b["role"], **m,
                                 "src": m.get("src") or tag(b)}
                                for m in filled)
            continue
        if b["content"].strip():
            messages.append({"role": b["role"], "content": b["content"],
                             "src": tag(b)})
    return messages, depth_items


def squash(messages: list) -> list:
    """Merge adjacent system messages into one.

    Some backends only honour a single leading system message, and a dozen
    consecutive ones is a reliable way to have most of them ignored.
    """
    def parts_of(m):
        return m.get("parts") or ([{**m["src"], "content": m["content"]}]
                                  if m.get("src") else [])

    out = []
    for m in messages:
        if out and m["role"] == "system" and out[-1]["role"] == "system":
            # Merging the text must not merge away WHERE it came from: the
            # inspector shows one system message assembled from a dozen
            # blocks, and "which block produced this paragraph" is the whole
            # question a user opens it to answer.
            out[-1] = {**out[-1],
                       "content": out[-1]["content"].rstrip() + "\n\n"
                       + m["content"].lstrip(),
                       "parts": parts_of(out[-1]) + parts_of(m)}
            out[-1].pop("src", None)
        else:
            new_m = dict(m)
            if new_m.get("src"):
                new_m["parts"] = parts_of(new_m)
            out.append(new_m)
    return out


def apply_depth(messages: list, depth_items: list) -> list:
    """Insert depth-placed blocks N messages from the end.

    depth 0 = after everything, the last thing the model reads. depth 2 = two
    messages back. Ties are broken by declaration order so the result is
    stable rather than dependent on dict ordering.
    """
    if not depth_items:
        return messages
    out = list(messages)
    for item in sorted(depth_items, key=lambda d: -d["depth"]):
        at = len(out) - item["depth"]
        out.insert(max(0, at), {"role": item["role"],
                                "content": item["content"]})
    return out


def cost(blocks: list, slots: dict, rough_tokens) -> dict:
    """Per-block token cost and a total.

    Nemo's preset spends 24,300 tokens before the character is described and
    nothing in SillyTavern says so. Showing this next to the toggle is most of
    the argument for switching.
    """
    per, total = [], 0
    for b in blocks:
        if not b["enabled"]:
            per.append({"id": b["id"], "tokens": 0, "off": True})
            continue
        if b["kind"] == "marker":
            filled = slots.get(b["marker"])
            if isinstance(filled, str):
                n = rough_tokens(filled)
            elif filled:
                n = sum(rough_tokens(m.get("content", "")) for m in filled)
            else:
                n = 0
        else:
            n = rough_tokens(b["content"])
        per.append({"id": b["id"], "tokens": n, "off": False})
        total += n
    return {"blocks": per, "total": total}


def clone(blocks: list) -> list:
    return copy.deepcopy(blocks)


def migrate_preset(pdata: dict) -> tuple:
    """Give a preset a block list if it has none. Returns (data, changed).

    Non-destructive by design: the legacy fields (jailbreak_id, mode,
    template, samplers, prefill) are left exactly as they were, because they
    still drive everything except ordering and a release that cannot be rolled
    back is not a release anyone should ship.
    """
    if not isinstance(pdata, dict):
        return pdata, False
    if isinstance(pdata.get("blocks"), list) and pdata["blocks"]:
        return pdata, False
    out = dict(pdata)
    out["blocks"] = default_blocks()
    out["blocks_migrated"] = True
    return out, True
