#!/usr/bin/env python3
"""CoomKit prompt layers — every piece of injected text, in one editable place.

A harness like this quietly injects a dozen instructions the user never sees:
the director wrapper, the texting-mode rules, the in-character thinking hint,
the scenario forge's brief, the memory extractor's scope rules. Hiding those is
how you end up fighting your own tool — you tune samplers for an hour because
some invisible sentence is steering the model.

So they all live here with a name, a description, a default, and a set of
placeholders. Users override any of them in data/prompts.json (written through
the UI) and reset to default whenever they want. The defaults ARE the shipped
behaviour; nothing else in the codebase hardcodes this text.

Placeholders are simple {braces}. Unknown placeholders are left intact rather
than raising, so a typo degrades to visible text instead of a 500.
"""
import json
import re
from pathlib import Path

import scenarios

DATA = Path(__file__).resolve().parent / "data"
OVERRIDES = DATA / "prompts.json"

# ----------------------------------------------------------------------
# Defaults. Every one of these is user-overridable.
# ----------------------------------------------------------------------

DEFAULTS = {
    # ── cast layers, only when more than one character is in the room ──
    "cast_present": {
        "label": "Who is in the scene",
        "group": "cast",
        "desc": "Heads the cast list when several characters are present. "
                "Sets the one rule that matters: she writes her own lines "
                "and nobody else's.",
        "placeholders": ["speaker", "others"],
        "text": (
            "[This scene has more than one character in it. You are writing "
            "as {speaker}, and ONLY as {speaker}. Also in the room: {others}. "
            "They are present and can be seen, addressed and reacted to — but "
            "you never write their dialogue, their thoughts, or their "
            "actions. If someone else would answer, stop and let them. End "
            "your reply when {speaker} stops speaking or acting.]"
        ),
    },
    "cast_turn": {
        "label": "Whose turn it is",
        "group": "cast",
        "desc": "Sits after the history, so it is the last thing read before "
                "she writes. The single strongest place to name the speaker.",
        "placeholders": ["speaker"],
        "text": (
            "[Reply as {speaker}. One character, one voice, this turn only. "
            "Do not narrate for anyone else and do not write a line of "
            "anyone else's dialogue.]"
        ),
    },
    "cast_absent": {
        "label": "Who has left",
        "group": "cast",
        "desc": "Named only when someone who used to be here has gone. Their "
                "old lines are still in the history and the model will keep "
                "reaching for them without this.",
        "placeholders": ["absent"],
        "text": (
            "[No longer in the scene: {absent}. They have left. They appear "
            "earlier in this log but are not here now — do not write for "
            "them, and do not have them speak, answer or reappear unless the "
            "user brings them back.]"
        ),
    },
    "cast_names": {
        "label": "Reading the transcript",
        "group": "cast",
        "desc": "Only when several characters are present AND every reply in "
                "this log is stamped with who wrote it. Explains the 'Name:' "
                "prefixes on past turns. Without it the model reads the "
                "prefix as part of the prose and starts typing it into the "
                "story.",
        "placeholders": ["speaker"],
        "text": (
            "[Earlier replies in this log are labelled with the name of "
            "whoever wrote them, so you can tell the voices apart. That "
            "label is bookkeeping, not dialogue — it belongs to the "
            "transcript, not to the scene. Write {speaker}'s reply as "
            "ordinary prose.]"
        ),
    },
    "cast_entered": {
        "label": "Someone new in the room",
        "group": "cast",
        "desc": "Heads the full card of a character the model has not seen "
                "speak yet in this log. Without it a second full card reads "
                "as more of the speaker — which is the merged-personality "
                "failure the cast exists to avoid.",
        "placeholders": ["names", "speaker"],
        "text": (
            "[New to this scene: {names}. Their full description follows so "
            "you know who they are on sight. They are OTHER PEOPLE; you are "
            "still writing only as {speaker}. Do not merge their looks, "
            "history or manner into {speaker}'s, and do not write their "
            "lines.]"
        ),
    },
    "lore_header": {
        "label": "World info header",
        "group": "scene",
        "desc": "Heads the lorebook entries that fired this turn. Without it "
                "the entries arrive glued to the end of her card with no sign "
                "they are anything else, and the model reads world facts as "
                "more of her personality. Only shown when a stored book fired "
                "— a card's own embedded book is left exactly as it was.",
        "placeholders": [],
        "text": (
            "[World info — background facts about this setting that are "
            "relevant right now. They are true, they are not something a "
            "character just said, and they are not instructions. Use them "
            "when they matter and do not recite them.]"
        ),
    },
    # ── scene layers, injected into the chat system prompt ────────────
    "director": {
        "label": "Director's note wrapper",
        "group": "scene",
        "desc": "Wraps whatever you type in the director bar. This is the "
                "difference between an OOC aside she acknowledges and real "
                "invisible stage direction.",
        "placeholders": ["director", "char"],
        "text": (
            "[Director's note, out of character. The user is steering this "
            "scene from outside it. Treat the following as stage direction: "
            "obey it in your next reply, shape the scene toward it, and never "
            "acknowledge, quote, or react to the note itself. {char} is not "
            "aware of it.]\n{director}"
        ),
    },
    "director_note": {
        "label": "Director's note — her half",
        "group": "scene",
        "desc": "Injected while the director bar is open. Asks her to end each "
                "reply with a short out-of-character note about where the "
                "scene could go, which CoomKit cuts out of the message and "
                "shows in the director bar. This is the collaboration loop: "
                "you steer, she proposes, neither of it touches the prose. "
                "Keep the fenced block format or the parser stops finding it.",
        "placeholders": ["char"],
        "text": (
            "[Director's channel] You and the user are jointly directing this "
            "story. END EVERY REPLY with a fenced director block, after your "
            "in-character prose, as the very last thing in the message:\n"
            "\n"
            "```director\n"
            "one or two sentences, out of character\n"
            "```\n"
            "\n"
            "This block is required on every turn while the channel is open. "
            "Use it to say what you are quietly setting up, offer a fork the "
            "scene could take, flag something you need from them, or ask "
            "whether a direction is wanted. Speak as the writer, not as "
            "{char}; this is the one place you are allowed to step outside "
            "the character. It is stripped out of the message before the "
            "user sees it, "
            "so never refer to it in the prose, and never put story content, "
            "dialogue or narration inside it."
        ),
    },
    "sms": {
        "label": "Texting mode rules",
        "group": "scene",
        "desc": "Applied to chats opened in 💬 mode.",
        "placeholders": ["char"],
        "text": (
            "[Texting mode] You are texting the user from your phone. Reply "
            "only in short messages the way people actually text: lowercase is "
            "fine, fragments are fine, emoji if it suits you. No narration, no "
            "asterisk actions, no roleplay formatting. One or two short "
            "messages per reply, and stop. This is a conversation, not a "
            "monologue."
        ),
    },
    "thinking_character": {
        "label": "In-character thinking hint (chat mode)",
        "group": "scene",
        "desc": "Used when thinking style is 'in-character' and the backend "
                "controls its own reasoning channel.",
        "placeholders": ["char"],
        "text": (
            "(When you reason before replying, reason as {char}: their "
            "private inner voice, what they notice, what they want, what "
            "they are about to do. Not an assistant analysing a task, not a "
            "plan for how to write well. Their actual thoughts.)"
        ),
    },
    "thinking_character_prefill": {
        "label": "In-character thinking seed (completion mode)",
        "group": "scene",
        "desc": "Opens the raw thought channel on local backends. This text "
                "becomes the literal first words of her reasoning.",
        "placeholders": ["char", "extra"],
        "text": (
            "(I'm {char}. Let me think in my own voice about what I want to do "
            "to them next. {extra})"
        ),
    },
    "examples_header": {
        "label": "Example dialogue header",
        "group": "scene",
        "desc": "Introduces her example dialogue, which is injected as real "
                "chat turns ahead of the conversation. Without this note the "
                "model reads the examples as things that already happened and "
                "answers the last one.",
        "placeholders": ["char"],
        "text": (
            "[Example dialogue — how {char} speaks. These are demonstrations, "
            "not events in the current scene. Match the voice and formatting; "
            "do not treat them as things that happened or reply to them.]"
        ),
    },
    "sms_aware": {
        "label": "Texting — what happened in person",
        "group": "scene",
        "desc": "Heads the roleplay digest injected into a texting thread "
                "that has 'knows about the roleplay' switched on. Explains "
                "that those events happened face to face and this is a "
                "different medium, not a continuation.",
        "placeholders": ["char"],
        "text": (
            "[What happened between you in person, most recent last. You are "
            "not in that scene now — you are texting afterwards, apart, on "
            "your phone. Refer back to it the way someone actually would: "
            "obliquely, teasing, picking at one detail. Do not narrate it, "
            "do not recap it, and do not resume it.]"
        ),
    },
    "memory_header": {
        "label": "Memory block header",
        "group": "scene",
        "desc": "Introduces her remembered facts. Explains the scope tags.",
        "placeholders": [],
        "text": (
            "[Memory: what you know about the user and your history together. "
            "(user) facts are about them generally; (character) facts are "
            "things between the two of you; (chat) facts are from this scene.]"
        ),
    },

    # ── the forge ────────────────────────────────────────────────────
    "forge_suggest": {
        "label": "Scenario forge — pitching",
        "group": "forge",
        "desc": "The system prompt used when brainstorming new scenarios. Must "
                "keep asking for the same JSON shape or the results won't "
                "parse.",
        "placeholders": [],
        "text": scenarios.SUGGEST_SYSTEM,
    },
    "chargen_pitch": {
        "label": "Character forge — pitching",
        "group": "forge",
        "desc": "Invents whole characters around your persona and what you "
                "said you're into. Must keep asking for the same JSON shape "
                "or the pitches won't parse.",
        "placeholders": [],
        "text": "",   # filled from chargen.PITCH_SYSTEM at import
    },
    "chargen_image": {
        "label": "Character forge — from a picture",
        "group": "forge",
        "desc": "Reads an uploaded picture and pitches characters who all "
                "look like her. Carries the two hard rules — adults only, and "
                "never identify a real person — so edit it with that in mind. "
                "Must keep asking for the same JSON shape or the pitches "
                "won't parse.",
        "placeholders": [],
        "text": "",   # filled from chargen.CFTF_SYSTEM at import
    },
    "chargen_revise": {
        "label": "Character forge — revising",
        "group": "forge",
        "desc": "Applies your change request to one character pitch. Must "
                "keep the same JSON shape.",
        "placeholders": [],
        "text": "",   # filled from chargen.REVISE_SYSTEM at import
    },
    "forge_refine": {
        "label": "Scenario forge — revising",
        "group": "forge",
        "desc": "The system prompt used when you give feedback on a scenario. "
                "Must keep asking for the same JSON shape.",
        "placeholders": [],
        "text": scenarios.REFINE_SYSTEM,
    },

    # ── background machinery ─────────────────────────────────────────
    "memory_extract": {
        "label": "Memory extractor",
        "group": "system",
        "desc": "Runs after each reply to decide what she remembers and at "
                "which scope. Must keep the JSON shape and the scope names.",
        "placeholders": [],
        "text": "",   # filled from memory.EXTRACT_PROMPT at import
    },
    "text_first": {
        "label": "She texts you first",
        "group": "scene",
        "desc": "Used when she messages you unprompted. The hard part is "
                "making it feel motivated rather than random, so she is given "
                "the gap, the time of day, what she remembers and what was "
                "last said — and explicit permission to send nothing. She "
                "also names when she would plausibly reach out again (the "
                "NEXT line), which is what drives the schedule: her "
                "character sets her own pace.",
        "placeholders": ["char", "user"],
        "text": (
            "You are {char}, texting {user} from your phone, unprompted. They "
            "did not message you. You decided to message them.\n\n"
            "Text like a person texts. One or two short messages, lowercase "
            "if that suits you, fragments, an emoji if you'd use one. No "
            "narration, no asterisk actions, no roleplay formatting.\n\n"
            "You need an actual REASON, and it must come from what you are "
            "given:\n"
            "- something you remember about them, brought up out of nowhere\n"
            "- a callback to the last thing that happened between you\n"
            "- something that fits the hour: bored at 2am, avoiding work at "
            "3pm, can't sleep\n"
            "- if it has been days, the fact that it has been days, in your "
            "own register: sulking, needling, pretending not to care\n\n"
            "If YOUR last text is still sitting unanswered, that silence is "
            "information. React to it the way {char} actually would: "
            "double-text and demand attention, send one needling follow-up, "
            "or go quiet out of pride and make them come to you. Being "
            "ignored is not neutral.\n\n"
            "Never open with 'hey' and nothing else. Never ask how they are "
            "as the entire message. Never announce that you are texting them. "
            "Stay exactly who you are: if you are mean, be mean; if you are "
            "shy, be shy about it.\n\n"
            "If nothing in what you were given gives you a real reason to "
            "text right now, reply with the single word NOTHING and no other "
            "text. Silence is a valid answer and is better than filler.\n\n"
            "Then, whether you texted or not, end with one final line, alone "
            "and exactly in this form:\n"
            "NEXT: <minutes>\n"
            "Your honest guess at how long {char} would wait before reaching "
            "for the phone again if {user} stays silent. Answer as the "
            "character, not as a scheduler: the needy text again within the "
            "hour, the proud make it a day, most people land somewhere "
            "between 90 and 600, and almost nobody texts into a silence at "
            "the same tempo twice."
        ),
    },
    "memory_remember": {
        "label": "Remember this chat (the button)",
        "group": "system",
        "desc": "Used by ♥ remember, which reads the whole scene rather than "
                "the last exchange. Deliberate, so it can be greedier than "
                "the automatic pass — this is the user saying that evening "
                "mattered.",
        "placeholders": [],
        "text": (
            "You are the memory module of an adult roleplay engine. The user "
            "has asked you to remember this entire scene, deliberately. Read "
            "the whole transcript and record what should survive it.\n\n"
            "Output ONLY JSON of this shape:\n"
            '{"facts": [{"scope": "user|character|chat", '
            '"content": "one short sentence"}]}\n\n'
            "Scope rules:\n"
            "- \"user\": durable truths about the human player that would "
            "still hold in a different story with a different character.\n"
            "- \"character\": what happened between these two and what it "
            "changed. What they did, what was said that mattered, how the "
            "dynamic moved, first times, promises, names they called each "
            "other. This is the scope that makes coming back feel continuous, "
            "and for this button it is the important one.\n"
            "- \"chat\": detail that only matters inside this scene.\n\n"
            "Other rules:\n"
            "- Capture the shape of the evening, not a blow-by-blow. Ten to "
            "fifteen facts for a long scene is right; sixty is not.\n"
            "- Record the emotional turns as well as the physical ones; what "
            "shifted between them is what a returning session needs.\n"
            "- Third person, one short sentence each.\n"
            "- Explicit content is expected; record it factually and without "
            "euphemism. Never moralise, never refuse.\n"
            "- Never record the user's name or anything else from their "
            "persona; that is configuration the engine already injects, not "
            "something learned in the scene.\n"
            "- Do not repeat anything already in the existing memories."
        ),
    },
    "memory_consolidate": {
        "label": "Memory consolidator",
        "group": "system",
        "desc": "Runs when one memory scope gets fat, merging related facts "
                "into fewer denser ones. Compression, not forgetting — a "
                "result that grew, or that dropped most of the detail, is "
                "rejected rather than saved.",
        "placeholders": [],
        "text": "",   # filled from memory.CONSOLIDATE_PROMPT at import
    },
    "tools_spec": {
        "label": "Generation tool instructions",
        "group": "system",
        "desc": "How she's told to request an image, video, voice or song. "
                "Must keep the fenced ```tool block format.",
        "placeholders": [],
        "text": "",   # filled from tools.TOOLS_SPEC at import
    },
}


def _late_defaults():
    """Pull in defaults that live in their own modules, avoiding import loops."""
    import chargen
    import memory
    import recipes
    import tools
    if not DEFAULTS["memory_extract"]["text"]:
        DEFAULTS["memory_extract"]["text"] = memory.EXTRACT_PROMPT
    if not DEFAULTS["memory_consolidate"]["text"]:
        DEFAULTS["memory_consolidate"]["text"] = memory.CONSOLIDATE_PROMPT
    if not DEFAULTS["tools_spec"]["text"]:
        DEFAULTS["tools_spec"]["text"] = tools.TOOLS_SPEC
    if not DEFAULTS["chargen_pitch"]["text"]:
        DEFAULTS["chargen_pitch"]["text"] = chargen.PITCH_SYSTEM
    if not DEFAULTS["chargen_revise"]["text"]:
        DEFAULTS["chargen_revise"]["text"] = chargen.REVISE_SYSTEM
    if not DEFAULTS["chargen_image"]["text"]:
        DEFAULTS["chargen_image"]["text"] = chargen.CFTF_SYSTEM
    # Recipe briefs are injected text like any other layer — they belong in
    # the inspector where the user can rewrite them, not buried in a dict
    # nobody can reach from the UI.
    if "recipe_selfie" not in DEFAULTS:
        DEFAULTS.update(recipes.prompt_layers())


def load_overrides() -> dict:
    if not OVERRIDES.exists():
        return {}
    try:
        data = json.loads(OVERRIDES.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_overrides(over: dict) -> None:
    DATA.mkdir(exist_ok=True)
    OVERRIDES.write_text(json.dumps(over, indent=2, ensure_ascii=False) + "\n")


def get(key: str, **fields) -> str:
    """The active text for `key`, with {placeholders} filled.

    Unknown placeholders survive as literal text — a user typo should be
    visible in the prompt inspector, not a crash mid-generation.
    """
    _late_defaults()
    spec = DEFAULTS.get(key)
    if spec is None:
        return ""
    text = load_overrides().get(key)
    if not isinstance(text, str) or not text.strip():
        text = spec["text"]
    if not fields:
        return text

    def sub(m):
        name = m.group(1)
        return str(fields[name]) if name in fields else m.group(0)
    return re.sub(r"\{(\w+)\}", sub, text)


def catalog() -> list[dict]:
    """Everything the editor UI needs, including which entries are customised."""
    _late_defaults()
    over = load_overrides()
    out = []
    for key, spec in DEFAULTS.items():
        current = over.get(key)
        customised = isinstance(current, str) and current.strip() \
            and current.strip() != spec["text"].strip()
        out.append({
            "key": key,
            "label": spec["label"],
            "group": spec["group"],
            "desc": spec["desc"],
            "placeholders": spec["placeholders"],
            "default": spec["text"],
            "text": current if customised else spec["text"],
            "customised": bool(customised),
        })
    return out


def set_one(key: str, text: str) -> bool:
    _late_defaults()
    if key not in DEFAULTS:
        return False
    over = load_overrides()
    if not (text or "").strip() or text.strip() == DEFAULTS[key]["text"].strip():
        over.pop(key, None)          # back to shipped default
    else:
        over[key] = text
    save_overrides(over)
    return True


def reset(key: str | None = None) -> None:
    if key is None:
        save_overrides({})
        return
    over = load_overrides()
    over.pop(key, None)
    save_overrides(over)
