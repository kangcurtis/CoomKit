# CLAUDE.md — CoomKit

Read this before touching anything. It exists so a fresh session doesn't
rediscover the same landmines.

> **This file is published.** It is a tracked file and it ships verbatim in the
> public release, so treat every line as public writing: no absolute paths from
> the dev machine, no account names, no filenames from anybody's private
> collection, no credentials. Measurements and reasoning are the point and they
> belong here; the provenance of the data usually does not — say "a Chinese
> lorebook", not the filename.
>
> Operational notes that are NOT publishable — releasing, remotes, credentials
> — live in `RELEASE.local.md`, which is gitignored. If you are about to touch a
> git remote, read that first; it exists because getting this wrong is not
> recoverable once pushed.

## What this is

A local-first NSFW companion harness — a SillyTavern alternative aimed at
/lmg/. Text roleplay plus media generation through the user's own ComfyUI.
Code lives at the repo root — this repo *is* CoomKit.

## Hard constraints (do not violate)

- **Python 3.10+ standard library only.** No pip, no venv, no deps. `http.server`,
  `sqlite3`, `json`, `urllib`, `base64`, `threading`. If you want a library,
  write the 30 lines instead.
- **Frontend is vanilla JS/HTML/CSS with no build step.** `web/` is served
  directly. No bundler, no framework, no npm.
- **Port 3939.** `./run.sh` starts, `./restart.sh` restarts via pidfile.
- **Vision is local-only by construction.** If the selected backend is a
  configured remote, uploaded images are *not* sent — the model is told so
  in-band instead. Never weaken this without the user explicitly asking.
  Note the *other* half: raw `/completions` cannot carry pictures at all, so
  a local turn with an image borrows the chat endpoint for that turn only
  (`meta["vision_fallback"]`, badged in the inspector and toasted on send).
  Without it the image was silently dropped and she answered "I don't see an
  image."
- **Default user name is `anon`**, not any real name. Placeholders, fixtures,
  examples.
- Tone: bratty/tsundere "Gemma-chan" supervisor voice in UI copy. Playful, a
  little mean, never coy about what the tool is for.
- **The product is archetypes.** The audience is /lmg/ and adjacent — what
  they want is a brat, an onee-san, an ara-ara milf, not a neutral assistant
  with a voice. Defaults should land on a recognisable type rather than a
  tasteful average: the shipped voices are named `brat` / `onee-san` /
  `mommy` for exactly that reason, and the recipes are the shots people
  actually ask for. When a choice is between "safe and generic" and "commits
  to a type", commit.

## Layout

```
CoomKit/          ← repo root
  server.py       HTTP + routes + the single prompt-assembly path
  llm.py          providers: chat & raw completion, streaming, templates, prefill
  engine.py       context assembly, history budget, swipes, chat creation
  cards.py        SillyTavern v1/v2/v3 card parse + PNG re-embed
  macros.py       {{char}}/{{user}}/… card macro substitution
  memory.py       scoped memory: extraction, storage, retrieval
  scenarios.py    the scenario forge: pitch/revise + tolerant JSON parsing
  chargen.py      the character forge: invent a whole card with the model
  tags.py         danbooru tag lookup + weighted artist blending
  prompts.py      EVERY injected text layer, user-overridable
  library.py      shipped presets & jailbreaks
  comfy.py        ComfyUI bridge, {{slot}} substitution
  tools.py        fenced ```tool block parsing, dialect rewrite, pending queue
  regexrules.py   find/replace rules: JS->Python, scoping, HTML allowlist
  studio.py       recipe -> plan -> draft -> run. THE generation path
  recipes.py      the ten one-click shots and their editable briefs
  wfpack.py       bundled workflows + stage splicing + slot filling
  vram.py         GPU broker: park the chat model for a big render, hand it back
  voices.py       shipped voice references for cloning
  tests/          the suite + testkit.py + _bootstrap.py + run.sh
  skills/         prompt dialects (anima, krea2, h3, klein, zimage, wan/ltx,
                  voice, music)
  tags/           tagsets.json (ours) + danbooru.csv.gz (bundled, see NOTICE)
  workflows/      15 API-format ComfyUI graphs, exported from a working install
  voices/         bundled clone references + CREDITS.md
  web/            index.html, app.js, style.css
  data/           gitignored: config.json, prompts.json, coomkit.sqlite, assets/
```

## Architecture decisions that matter

**The prompt is an ordered block list.** `blocks.py` holds the model,
`blocklib.py` the shipped library, `engine.assemble_blocks` renders it.
CoomKit's own layers are blocks in the same list as the user's, so there is
one order and one inspector. A block is `kind="text"` (literal content in a
role) or `kind="marker"` (a slot the engine fills: card, persona, lore,
memory, examples, history, tools). Placement is `order` or `depth` (N messages
from the end; depth 0 is the last thing the model reads).

The division of labour matters: **the server decides whether a layer has
content this turn** (is the director bar open, is this SMS, are tools on) and
passes it in `layers`; **the blocks decide where it goes and in whose voice.**
An empty layer contributes nothing, exactly as if its block were off. Don't
put conditional logic in blocks.py, and don't append to `messages[0]` in
server.py — that was the old design and it is why everything ended up in one
undifferentiated system message.

Built-in blocks are **reorderable but marked** `builtin: true`. Moving the
jailbreak after the history is a legitimate power move and locking it would
reproduce the rigidity people leave ST for.

**Correction, 2026-08-19:** this file used to say `engine.assemble` (legacy,
one big system string) "is still there and still tested". It is not. The
function no longer exists — only `assemble_blocks` does — and the comments
still naming it (blocks.py:78, server.py, tests/test_fixes.py) are describing
history, not code. What *does* survive is `engine.build_system`, which is dead:
nothing calls it and no test covers it. Deleting it is safe; it is left only
because nothing depends on the decision.

**One prompt-assembly path.** `server._prepare_request(body, persist)` builds
the outgoing payload. `/api/chats/send` calls it with `persist=True`,
`/api/chats/preview` with `persist=False`. The prompt inspector therefore
cannot drift from reality — there is nothing to drift from. **If you add a
context layer, add it there, not in a second place.**

**Every injected instruction lives in `prompts.py`.** Nine layers: director
wrapper, sms rules, in-character thinking (chat + completion variants), memory
header, forge pitch, forge revise, memory extractor, tool spec. Each has a
label, description, placeholder list, default. Overrides in
`data/prompts.json`. **Do not hardcode injected prompt text anywhere else** —
that was the whole point of the refactor.

**Memory has three scopes** (`memories.kind`):
- `user` — durable facts about the player. `chat_id` and `character_id` NULL.
  Shared across every character.
- `character` — the relationship. `character_id` set, `chat_id` NULL. Spans all
  chats with her.
- `chat` — scene furniture. Both set. Must not leak into a new chat.

Retrieval for a turn is `memory.for_turn()` = user ∪ character ∪ this chat.
The forge uses `memory.for_scenario()` = user ∪ character only.
**Never go back to filtering memory on `chat_id` alone** — that was the original
bug, and it made returning chats amnesiac.

**The lifecycle is as important as the scoping, and was originally absent.**
Measured on a real 155-message log: 53 memories, **897 tokens injected on
every turn**, 21 near-duplicate pairs and several byte-identical ones. Three
separate faults, all fixed and all easy to reintroduce:

- **Dedup happens at write time, against the database.** Extraction runs in a
  background thread per turn, so two turns raced: both read the existing set,
  both decided a fact was new, both stored it. Checking against a snapshot the
  caller read earlier does not work. `store_memories` bumps `updated` on a
  near-match instead of inserting.
- **Extraction runs every Nth reply** (`memory.should_extract`, default 4).
  Per-turn extraction is an LLM call per message that mostly finds nothing —
  but a helpful model always answers, and that answer is the clog.
- **Injection is ranked and capped** (`rank` + `budget`). Pass `limits` to
  `for_turn` for a turn; pass nothing for the memory panel and the forge,
  which want everything. Ranking is scope, then word overlap with the recent
  conversation, then recency — stdlib only, no embeddings.

`♥ remember this` (`/api/chats/remember`) is the deliberate counterpart: it
reads the whole scene rather than the last exchange, and is allowed to be
slower and greedier because the user asked for it. Consolidation merges a fat
scope, and refuses any result that grew or that dropped most of the detail.
`/api/memories/tidy` repairs data written before any of this existed.

**The character forge invents her, the scenario forge situates her.** Two
tabs on the same modal, same interaction — pitch, argue in plain English,
commit. `chargen.py` mirrors `scenarios.py` deliberately. Committing writes a
real v3 card, pins a random seed at creation (so she looks like herself from
her *first* picture, not from whenever the user notices the setting), picks
her a shipped voice, and renders her portrait through the ordinary
`studio.plan/run` path rather than a private one.

Two things it must keep doing: **the persona's `data.into` is design input**,
stated to the model as something to build the character around; and **a
pitched `voice`/`model` id is validated against the real lists** in
`chargen._clean`, because a hallucinated id only fails sixty seconds later at
generation time. The active preset's jailbreak is prepended to the system
prompt — remote models refuse this work without it, and the forge is
explicitly meant to run on whatever backend is connected.

**Scenario forge** solves stale `first_mes`. A forged scenario is stored in
`chats.data.scenario`, *replaces* the card's static scenario in the system
prompt (they fight otherwise), and its `opening` seeds message one instead of
`first_mes`.

**`mes_example` goes in as fake turns, not as system text.** That is
measurably the stronger form — the model sees the pattern in the position it
is about to imitate. Two costs come with it and both are managed in
`engine.assemble`: the examples eat tokens that would otherwise hold real
history (hence `EXAMPLE_CAP_TOKENS`), and examples written for the card's
original setup drag her back toward it (hence `EXAMPLE_RETIRE_TOKENS` — once
the real scene is established her voice is set by what she has actually said).
Per-chat toggle in `chats.data.examples`, default on. The header is a prompt
layer and is load-bearing: without it the model reads the examples as things
that happened and answers the last one.

**Card macros are resolved late, never baked in.** `macros.expand` runs in
`engine.assemble` (everything going to the model) and in `_chat_detail`
(everything shown on screen). Messages are *stored* with `{{user}}` intact, so
the log stays portable and switching persona re-resolves the whole history.
Don't "fix" this by substituting at write time. Unknown `{{...}}` is left
alone deliberately — `{{prompt}}` is a ComfyUI slot, not ours to eat.

**`mes_example` is parsed but never injected** into the prompt. That is a
known gap, not a decision — see State/roadmap.

**Lorebooks: two adapters, ONE matcher.** `lore.py`. The embedded
`character_book` and an imported SillyTavern world are genuinely different
semantics, so the difference lives in the ADAPTER and there is one matcher —
the same discipline as one `_prepare_request`. `from_card` reproduces today
exactly: keyless-always, `constant` ignored, `disable` ignored, lowercase
SUBSTRING not whole-word, source order not `order`, and an oversized entry
skipped with a `continue` so smaller later ones still land. Anyone "fixing"
that last one to a `break` changes which entries appear in every existing chat.

**The compatibility claim is PROVED, not asserted.** `tests/test_lore.py` keeps
the old `engine._lorebook_entries` verbatim as an oracle and diffs the module
against it over 19 entry shapes and 6 budget boundaries. Writing the oracle
first immediately caught a divergence that would otherwise have shipped: an
entry keyed `[""]` HAS a key list and today's matcher skips it, so filtering
the blanks out first makes it read as KEYLESS — which fires unconditionally,
every turn, forever. Keyless-ness is decided by the RAW list. One deliberate
divergence, one direction only: today's matcher `.get()`s every entry, so a
non-dict entry takes the whole turn down; `lore` skips it.

**The invariant is "byte-identical while zero stored books are ATTACHED"**, not
"while you ignore the feature". Fair-share pins the embedded book first and
counts it as a book, so attaching one standalone book halves the embedded
book's guaranteed share. That is a real change to an existing chat from an
action that reads as purely additive. Verified live: importing a 75-entry world
changes nothing until it is linked.

**The header is gated on a STORED book having fired.** Lore reaches the model
with no header at all today, so emitting one unconditionally would add a
paragraph to every existing chat with an embedded book — and
`cast_baseline.json` would stay green only because its fixture card has no
`character_book`, which is the test not covering it rather than the change
being safe. `tests/test_lore.py` asserts the gate directly.

**Stored books are the UNION over everyone present**, plus chat-scoped, plus
global — deliberately independent of who is speaking, so a speaker swap does
not change the lore half of the prompt. That helps the prefix cache, and it
matters more now that `auto` changes the speaker most turns. The legacy
embedded book stays speaker-only.

**Facts measured on the real 17-book / 281-entry corpus**, not guessed. All
17 files key `entries` as a DICT of stringified uids; an embedded book is a
LIST. `constant` is set on 140, `disable` on 89 (87 of them keyless AND
constant, so a naively adopted book injects 87 entries the author switched
off). Several traps:

- **`originalData` is the wrong door and it is on 16 of 17 files.** It carries
  a DIFFERENT dialect — snake_case, `position` as a string enum or `""` or
  absent, blank strings inside key lists, `token_budget` as a string. Read
  top-level `entries` and never fall back to it. The tolerant instinct is
  exactly wrong here.
- **Only `position == 4` means at-depth.** `depth` is present on 281 of 281
  with a default of 4 and `role` on 147 with a meaningful 0, so testing either
  reports every book as fully at-depth — which is what the first run did.
- **Falsy is meaningful.** `role: 0`, `depth: 0`, `probability: 0`. Use
  `.get(k, default)`, never `x or default`.
- **`probability: 0` is a hard OFF, not a coin flip** — all four entries of
  one real book are that, so they never fired in ST either and are imported
  disabled. A book like that is useless as a demo.

**Whole-word matching is ON for imports and that is a unilateral divergence.**
`caseSensitive` is null on all 281 real entries and `matchWholeWords` is never
true on any of them. It is still right — measured, it stops `Rem` and `age`
firing on "remember the message" while both still fire on "Rem is her age" —
but the import summary SAYS so, or the first bug report is "entries stopped
firing after I imported into CoomKit".

**The CJK carve-out, and how to test it.** `\b` never matches between two CJK
characters, so whole-word would kill every key in the Chinese and Korean books:
50 of 50 keys in a Chinese book match as substrings and 0 of 50 with `\b`.
Whole-word therefore applies only when the key starts AND ends with an ASCII word
character. **Test it on UNPUNCTUATED Han** — `\b` matches fine beside `，。`, so
a careless test passes by accident.

**One stop-word refusal, and the stated reason matters.** An entry keyed only
on `a`/`and`/`the` fires on every English line; exactly one exists
(one 342-token entry = 28% of the ceiling, every turn).
It is imported DISABLED with the reason attached. **Not** because it wins a
sort race — that claim was checked and is false: `order: 100` is ST's default
and the modal value, and all 29 entries in its own book share it, so it sorts
16th of 29. Left uncorrected, the next reader builds a sort-priority defence
against a problem that does not exist.

**The slot is a list of `{content, src}`** — the cast's card-slot pattern
reused, so per-entry provenance reaches the inspector. Two things ride on it:
`blocks.squash` merges adjacent SYSTEM messages and nothing else, so a user who
moved their lore block to `role: user` gets a JOINED STRING instead or they
would get N messages where they used to get one; and a fired entry's `src.id`
is deliberately NOT `"lore"`, because `renderSegments` draws a "turn off" button
for any part whose id matches a block and ten entries would draw ten buttons
that all disable the same thing. Only the header carries one.

**`ON CONFLICT` cannot target the `lore_links` COALESCE index.** Uniqueness is
an expression index over `COALESCE(character_id,0), COALESCE(chat_id,0)` —
needed because a plain unique index treats NULLs as distinct and lets duplicate
global links through — and `ON CONFLICT(book_id, character_id, chat_id)` raises
OperationalError, not IntegrityError, which the route wrapper turns into a 500.
Use `INSERT OR IGNORE`.

**Lifting an embedded book out COPIES it** and stamps `data.from_card_id`. The
card is never touched, so it keeps round-tripping through `cards.CARD_KEYS`, and
the embedded book is then skipped for her — matched on that stored provenance,
**never** on comparing text, because a text comparison is the silent near-miss
that produces every entry twice. The confirm says the awkward part out loud:
full semantics switch on, so MORE entries fire than before.

Measured live on gemma-4-31b-qat: a 29-entry world attached to a character,
147 tokens injected for the turn (55 header + 92 entry), and she answered "what
is the NCWF?" with "the Northern and Western Federation" from an entry reading
"a federation of northern and western states" — genuine use, not confabulation.

**Two request modes.** `chat` → `/chat/completions`. `completion` → raw
`/completions` with client-side `llm.render_prompt`. Templates: `gemma4`
(canonical), `chatml`, `llama3`, `plain`.

**The database heals itself.** `get_db()` stamps `PRAGMA user_version` and
re-applies the schema when it is missing. Deleting `data/` under a running
server used to leave sqlite creating an empty file and every route 500ing
with "no such table" until a restart — which presents to the user as "it
lost all my chats". Don't remove this in the name of saving a pragma read.

**The director bar is two-way.** You type stage direction she obeys silently
(`director` layer); while the bar is open she also answers in a fenced
```director block that `tools.split_director_note` cuts out of the visible
reply and stores in `messages.data.director`. It never touches the prose.
Gated on `director_notes` in the request body, so it is opt-in.

**Session state lives in localStorage** under `coomkit.session.v1` — open
chat, model, preset, samplers, thinking, rail tab, director state. The
messages were always in sqlite; what was missing was the UI knowing which
chat it had been in. `restoreChat()` falls back to the empty state if the
chat is gone rather than rendering a broken half-view.

**Samplers have exactly one editor** — the collapsible block in the scene
rail. It writes through to the active preset on demand. Don't add a second
set of sliders; there were three, none of which persisted.

**One generation path, and it drafts before it runs.** `studio.plan()` picks
the workflow and gathers references without touching an LLM or the GPU;
`studio.run()` brokers VRAM, uploads, queues, fetches. In between the user
sees and edits the prompt. That approval step is not politeness — a local
model's first draft is wrong often enough that skipping it wastes a minute of
video render per mistake. **If you add a media type, add it there, not in a
second place** — same rule as `_prepare_request` for chat.

**Bundled workflows are spliced, not forked.** `workflows/` holds real
API-format graphs exported from a working ComfyUI, complete with Impact Pack /
rgthree / Ultimate SD Upscale nodes. `wfpack.STAGES` removes those by *node
class*, wiring each removed node's producer straight to its consumer, so what
runs on a bare ComfyUI is core-only. The same machinery turns quality stages
on and off — anima is 19 nodes with everything and 9 without, 9 seconds
instead of ~90. **Slots are filled BEFORE the splice**, because the prompt
lives on the wildcard node that the splice removes; fill after and you ship a
graph rendering the demo prompt it came with.

**Recipe briefs are prompt layers.** Every one is registered into
`prompts.DEFAULTS` from `recipes.prompt_layers()`, so they are editable in the
inspector like the other nine. Don't add a recipe brief that bypasses that.

**The gallery is keyed on `character_id`, never `chat_id`.** Same reasoning as
memory scopes: a gallery that empties when you start a new scene is a folder,
not a record of the two of you. `assets.character_id` arrived via
`MIGRATIONS`, not the schema — `CREATE TABLE IF NOT EXISTS` cannot retrofit a
column onto an existing table.

**A chat can have a cast, and a cast of one is not a cast.** `chat_cast` holds
who else is in the scene; `chats.character_id` stays the **lead** because the
gallery, the chat list, `_rp_digest` and the export footer all key off it.
`engine.cast_active()` is ONE predicate, decided once in `_prepare_request`, so
the multi-character branches cannot disagree about whether they apply — and
when it is False every existing chat assembles down exactly the path it always
did. `tests/test_cast.py` pins that byte-for-byte against a recorded baseline,
because every branch here is a chance to silently change the prompt of every
existing chat and nobody would notice except as "replies got worse".

**One speaker per turn, and she gets the only full card.** Everyone else
present gets a ~25-token dossier (`engine.dossier_line`) carrying the first 140
chars of their description plus the user's staging note. That fixes the real
hole in swapping cards — the speaker has never read the other character's card
and so cannot describe her — without buying the merge that SillyTavern's append
mode causes; ST's own docs warn it yields "merged personalities". **The
speaker's card goes LAST**, after the dossiers: llama.cpp and LM Studio cache
the prompt prefix, and her card is the only part that changes when the turn
passes, so everything above the swap point stays cached. Ordering is free.

`card_text(fields, name=)` heads the card with `[Name]` **only** when several
people are present — a nameless "Personality: bratty, hostile" sitting next to
a name-headed dossier binds to the wrong woman. Default `name=""` keeps the
solo prompt byte-identical.

**`{{char}}` follows the SPEAKER, not the lead.** `assemble_blocks` swaps
`char` for the speaker before macros resolve, so her own greeting names her.
So do `post_history_instructions` and `mes_example`, which are speaker-only —
N depth-0 instructions contradicting each other is the worst failure available.

**`cast_absent` is deliberately NOT gated on `cast_active`.** Sending the only
guest off-stage takes the scene back to one speaker, which is exactly when the
warning matters most: her lines are still in the history and the model will
happily keep writing her. This was a real bug in the first cut — the layer
never fired, because it was inside the multi branch.

**The baton: who speaks next is decided before the turn, and says why.**
`engine.pick_speaker` is six rules over data already in memory — a regex and a
scan of the history. No round trip, no randomness. Strict priority: **you**
(an explicit pick in `#sendAs`), **same again** (a re-roll keeps the take's own
speaker), **asked directly** (the text names exactly one present character),
**still answering** (the last speaker keeps the floor), **her turn** (least
recently spoken, ties by `ord`), **lead**.

The reason is stamped on the TAKE beside `speaker` and `gen`, so a swipe
carries the reason that produced *that* swipe, and it is read back with the
same two-step fallback `_chat_detail` uses for `gen` — `add_swipe` seeds
swipes[0] with content/think/director only, so reading the swipe alone gives
every re-rolled bubble a blank chip. **The chip is the feature, not
decoration**: `auto` that cannot explain itself reads as a coin flip and gets
switched off.

**Asking the MODEL to nominate is CUT, on measurement.** A live 31B, twelve
turns, three-hander, layer text stating plainly that passing is normal: 4/12
emitted nothing, 2/12 passed, and all 6 that nominated named the FIRST name in
the list — including on "Mika, tell me about your day". Positional degeneracy,
~80 prompt tokens a turn, to produce a stuck pointer. Do not re-propose it.
Reply chains are cut with it: `_prepare_request` runs *before*
`send_response(200)`, so a second call inside the stream loop cannot return a
status code.

Two rules the original design lacked, both of which are bugs without them:
**`same again`** exists because the regenerate branch truncates history above
the take being replaced, so routing off the last assistant turn would hand a
re-roll to a *different character*; the take's own speaker is read BEFORE the
truncation. And **`pick_speaker` falls back to the last user turn** when
`body["text"]` is empty, or "asked directly" could never fire on a re-roll
even with the naming message sitting right there.

Matching is whole-word (`Rem` otherwise fires on "remember") with the CJK
carve-out — `\b` never matches between two CJK characters, so a whole-word
pattern would make every such name permanently unmatchable. Names are
`re.escape`d because cards really are called `Rin (twin)`. The persona's name
is excluded. **Ambiguity is safe by construction**: two matches falls through
rather than guessing.

Fairness needs BOTH halves — a streak of `CAST_STREAK` AND somebody shut out of
the last `CAST_STARVE_WINDOW × len(present)` — because two people going back
and forth is a conversation, not unfairness. It can only ever force a swap.

**Stop sequences are the only anti-merge layer that WORKS, because they are not
a request.** One `"\nName:"` per other present character, merged into a **copy**
of `samplers` — the same dict is stamped into `data["gen"]["samplers"]` and read
by the 📸 export footer, so mutating it writes an automation's strings into the
user's own record. On a remote the cap is 4 and **the user's own stops go first
and are never dropped**; completion mode appends `default_stops()` afterwards,
so those slots are reserved too or the cap is a lie by two on gemma4. Dropped
stops are named in a notice on that turn. `engine.trim_cast_leak` is the belt:
only a name at the START of a line counts ("Rin nodded" is the dossier working),
the remainder is discarded because it was written with the wrong card in the
prompt, and a leak at position 0 is left alone because trimming it would store
an empty message. Counted in `chats.data["cast_leaks"]`; the third says so.

**Name-prefixed history, and the gate that nearly never opened.** Assistant
turns are headed with who wrote them and the prefill becomes `"<Speaker>: "`.
The design gated this on every retained assistant message being *stamped* — and
the card's greeting is never stamped, so that gate would have stayed shut for
the life of every chat that has one. It is not a special case: `data["speaker"]`
is written only when the scene is multi, so **an unstamped reply is proof the
chat was one-to-one when it was written**, and the only person who could have
written it is the lead. What genuinely cannot be named is a stamp pointing at
somebody removed from the cast outright; one of those shuts the gate for the
whole log, because half-labelled is worse than unlabelled. Prefixes are priced
into the history budget *in the same loop that chooses it*, or a long log
overflows by exactly the prefixes. `engine.strip_speaker_prefix` takes the name
back off at store time — the server prepends the prefill to what came back — and
strips only a leading KNOWN name, which is what makes `12:30` safe with no
special case. The name composes AHEAD of `reply_prefill`, not instead of it.

**Entrance cards**: someone the model has not seen speak in the *retained*
history gets a real card instead of a dossier, once. Measured on a realistic
card: **+235 tokens**, and the model gains detail the 140-char dossier truncates
away entirely. On a *short* card the gap nearly vanishes — say so rather than
claiming a uniform win. Budget is 8% of context, at most 2 a turn, and **nobody
at all below `CAST_ENTRY_MIN_CONTEXT`** (12000): handing a small model half a
character sheet is the incoherence the cast exists to prevent. Oversize cards
are trimmed at a sentence boundary with a visible marker, never dropped. The
budget is **reserved before history is chosen and spent afterwards**, because
entrances are picked from `kept`, which does not exist when the card slot is
built — promote first and you overflow by up to two cards. A promoted character
**loses her dossier**; the slot is rebuilt, not patched, because carrying both
is the append-mode merge in miniature. `{names}` is left unsubstituted by the
server and filled by the engine, since only the engine knows who is new.

Tested adversarially against the merge on gemma-4-31b-qat — a filthy-mouthed
punk speaking with a silent formal librarian's whole card in the prompt: she
described the newcomer accurately across three turns and borrowed nothing.

**`auto` as the default is measured, not assumed.** The speaker's card sits near
the front of the block order, so changing speaker reprocesses everything after
it. Ten sends per mode in a three-hander, run as two **separate uninterrupted
blocks**: median TTFT 1.64s pinned vs 1.77s auto (+8.0%), means identical at
1.82s / 1.80s. Interleaving the two modes makes every pinned send pay for the
preceding auto send's cache miss and reports a bogus +13% with obvious
0.7s/2.3s bimodality — measure in blocks. Under 15%, so the `cast_card` block
fallback was not needed.

**`S.cast` must be assigned BEFORE the message render loop in `loadChat()`.**
`buildMsg` resolves each reply's face, name and reason out of it, so assigning
it afterwards drew every bubble against the *previous* chat's cast. That was
already true of the per-speaker avatar; the reason chip only made it visible.
`#sendAs` likewise needs its `onchange` and a `sendAs` key in
`coomkit.session.v1` — `renderCast()` rebuilds the option list after every send,
so without both the user's pick lasts exactly one turn.

**The image export resolves the speaker too.** It used to build its own
`msg-who` from the single character name and avatar and never consult
`m.speaker`, so an exported two-hander attributed both women to the lead — in
the one place a log gets posted in public. The routing chip is deliberately NOT
carried across: who spoke belongs in the picture, why she was picked does not.
The chip's CSS lives beside `.msg-who` and outside every `@media` block, for the
reason `test_export.py` brace-matches them.

**A per-character layer is re-rendered for the speaker, and re-rendering means
re-substituting from scratch.** `prompts.get` re-reads the default, so EVERY
placeholder the layer takes must be supplied again. `director` takes two —
`{char}` and `{director}` — and passing only `char` left the literal string
`{director}` in the prompt, silently dropping the user's stage direction in
every multi-character scene with the bar open. Invisible by construction: the
note is meant to be invisible to the character, so the only symptom is that she
ignores it.

**`blocks.render` used to let a spliced dict's role win unconditionally**, so a
user who moved their card block to `role: user` was silently overridden. Now
the dict wins only when it *has* a role (history and example turns must keep
theirs) and the block's role applies otherwise. That is what makes a list-valued
`card` slot safe.

**`chat_cast` is NOT in `VALID_TABLES`** — `rows_get`/`rows_upsert` assert on
it, same as `chats`, `messages` and `assets`. Every read is a direct query.

Measured on a real two-hander with gemma-4-31b-qat: +165 tokens for a second
character (94 header + 23 dossier + 37 turn note), 8s and 13s per reply, and
neither character wrote the other's dialogue. The second character correctly
described what the first had done from the dossier alone, having never seen her
card.

**A portrait can be re-rolled, and the cheapest fix is usually her gallery.**
The forge pins a seed at creation so she looks like herself from her *first*
picture — right up until that first picture is ugly, which is what people
actually reported. Three ways out, all on the character editor:
`POST /api/characters/<id>/portrait` re-renders through `_render_portrait`,
which is the SAME function the forge calls (extracted, not copied — a re-roll
down a private path would drift from how her first picture was made);
`new_seed: true` rolls a fresh pin and *keeps* it, so the rest of her gallery
follows the face you settled on; and `POST /api/characters/<id>/avatar` just
promotes a picture she already has, which is faster than any render and is what
most people want. It refuses an asset belonging to someone else.
Two keys that are easy to confuse: her looks live in `data.visual`, not
`data.looks` (studio.py reads `visual`, so a re-roll reading the wrong key
silently ignores her appearance and pinned seed); and an upload with
`kind: "character_ref"` sets `data.visual.ref`, a *generation reference*, which
does not change her face on screen — `kind: "avatar"` is the one that does.
Measured on the 5090: 36.2s for a krea2 portrait, avatar swapped, and the
render lands in her gallery so the previous one stays pickable.

**Two guards sit under that, and they are independent on purpose** — one stops
the wrong graph, the other stops the wrong file, and only the second protects
stored data.

`studio.plan` takes a `workflow=` override, and an override for the WRONG KIND
now falls through to the resolved default instead of being honoured. `_fit_slots`
does not reject values it does not recognise, it *drops* them: verified by
execution, `plan('solo-model', …, workflow='h3')` returned `kind: image,
workflow: h3` and every image slot vanished, so h3 rendered the demo video it
shipped with. Checked in `studio.plan` and not at the route, for the same reason
`cast_active` is one predicate — `_render_portrait`, `_studio_draft` and
`_tool_via_studio` cannot be allowed to disagree about it.

`_render_portrait` then refuses to write a non-image into `characters.avatar`.
`comfy.kind_of` classifies by extension precisely because `SaveVideo` files
arrive under the history's `images` key, so without the check the symptom is
**her avatar becomes an .mp4** and renders as a broken `<img>` forever. The
render still lands in her gallery and she is told what happened; it just is not
her face. Do not remove either as redundant — they catch different halves.

Considered and DECLINED with it: a `duo` recipe rendering two characters in one
picture. The writer half works (6/6 runs kept two appearances distinct) and the
render half cannot — every anima run put `flat chest` and `large breasts` into
one flat tag list, and there is no regional or masked conditioning anywhere in
`workflows/`. The capability survives with zero code, because `describe` already
takes free-form text. If two real faces are ever wanted the honest project is
reviving `07-klein-9b-edit.json` (currently unreachable — no recipe declares
`image-edit`) and chaining a second `ReferenceLatent`. That is a workflow change,
not a recipe change.

**The prompt owns the rail, and the inspector says who wrote each line.**
/lmg/'s sharpest complaint was "entire sidebar dedicated to extensions instead
of actually managing your prompt" — three of five rail tabs were generation
extras while the thing the README calls the point lived behind ⚙. `prompt` is
now the first rail tab and the default one. It is not a second block editor:
`renderBlocks()` paints the rail and the settings tab from one row builder with
a `compact` flag, and `saveBlocks()` is the single save path for the rail, the
settings tab and the inspector's turn-off button.

Two things this shook out, both of which were live:

- **The rail must follow the ACTIVE preset, never `presets[0]`.** Blocks live
  on the preset, so the first version showed preset 33's blocks while the
  topbar said "no preset" and the server assembled from the built-in defaults.
  Turning a block off changed nothing and explained nothing. With no preset
  selected the rail now shows the defaults, says so in red, and disables the
  save — a prompt panel that is not the prompt is worse than no panel.
- **A built-in text block carries no content of its own**; its text lives in
  `prompts.py` under `layer`. Pricing `b.content` alone reported ZERO for every
  shipped block, so a fresh install opened the panel and was told its whole
  prompt cost 0% of context. `blockTokens` resolves the layer through
  `/api/prompts`. The two `__`-prefixed layers (jailbreak, card post-history)
  are genuinely unknown until send time and stay uncounted.

**Provenance rides a SIDE CHANNEL, not the messages.** `blocks.render` tags
each message with the block that produced it and `squash` merges the tags
alongside the text, so one system message assembled from a dozen blocks can
still say which paragraph came from where. But `assemble_blocks` strips
`src`/`parts` off the returned messages and hands them back via an optional
`trace` dict, because `tests/test_gallery.py` asserts structurally that every
assembled turn reduces to role+content — that is the guarantee the per-message
gallery rests on, and attaching provenance to the messages broke it the moment
it was tried. `llm.build_payload` strips both keys again at the wire as a
backstop, so no caller can leak them to a backend.

**The image export is a SECOND consumer of `style.css`, and it fails silently
and totally.** `📸 post` builds a clean offscreen subtree from the app's own
classes, serialises it into an SVG `<foreignObject>` with `/style.css` inlined,
and rasterises that through an `Image` onto a canvas. Five things were measured
through the real pipeline in Firefox 140 ESR and Chromium 151, and every one of
them fails *without an error* if you get it wrong:

- **An SVG loaded as an image is frozen at animation time 0.** `style.css:376`
  gives `.msg` an `animation: rise .2s ease` whose first keyframe is
  `opacity: 0`, so without `.ck-export *{animation:none!important}` the entire
  export renders **blank — 1 distinct colour** against 931 (Chromium) / 1146
  (Firefox) with it. Nothing throws. `ckCanary()` renders a known fixture and
  counts colours before every export session precisely so this is caught by the
  app rather than by the user posting a black rectangle. **Any new CSS effect
  that only makes sense on a live scrollable page — `position: sticky`,
  `backdrop-filter`, a `max-height` scroll clamp — needs a matching
  `.ck-export` override.**
- **An `<img>` pointing at a URL does not paint inside a `foreignObject`** —
  byte-identical PNG with and without the `src`, in both engines. A `data:` URI
  does. So `ckInline()` re-encodes every bitmap at display size as JPEG and
  inlines it. The first version composited the bitmaps onto the canvas
  afterwards using `getBoundingClientRect` coordinates and **that was wrong**:
  the live page and the SVG viewport wrap text fractionally differently, so the
  error accumulates down the sheet — measured 0px at the first bubble and 33px
  by the fifth, which put every photo a visible notch below its own frame.
  Inlining hands layout back to the browser, which is the only thing that knows
  where it actually put the box. Do not go back.
- **That same drift is why tile cuts come from the picture, not the DOM.**
  `ckBgRows()` finds rows that are entirely background across the bubble
  column — true in the 12px gap between messages, false inside one. Measured on
  a 90-message log: the cut landed with six clear rows above it and content
  resuming one row into the next sheet.
- **Firefox refuses a canvas past 32767 per side** (`getImageData` throws,
  `toDataURL` returns `"data:,"`); Chromium goes to 65535. 4chan rejects over
  10000, so `CK_MAX_TILE_H` is 9000 and the engine ceiling is only a backstop.
- **A single C0 control character makes the SVG invalid XML** and the only
  symptom is `img.onerror` with nothing on it. `xmlSafe()` strips them and lone
  surrogates before anything reaches the DOM.

Two more rules with teeth: **never `blob:` for the SVG** (Chrome taints the
canvas), **never `crossOrigin`** (same-origin with no ACAO makes the load fail
rather than taint), **never `image/webp`** (both engines encode it happily and
4chan rejects it), and **never draw a bitmap from ComfyUI's `:8188`** — a
different origin, one image, and `toBlob` dies with a `SecurityError`. That
last one is safe today only because `studio.run` pulls outputs server-side into
`data/assets/`.

**`fmt()` is the export's renderer too.** The bubble is filled with exactly one
line, `bubble.innerHTML = m.html ? fmtHtml(safe) : fmt(stripBlocks(safe))`. A
second renderer for message content would drift from what is on screen and
nothing would detect it. If the export needs to look different, that is a
`.ck-export` rule, not a JS branch.

**The persona name is redacted by BINDING, not by replacing.** `_display_ctx`
expands `{{user}}` into the poster's real handle before any JSON exists, so a
client-side scrub would be a find-and-replace over already-expanded text — it
mangles substrings ("Al" inside "Alice"), needs regex escaping on user input,
and cannot be tested. `?user_as=` re-binds the macro at expansion time and
`name_scrubber` then catches the name the model typed out longhand, which it
always does and which binding alone never covers. Both are server-side and both
are pinned by `test_export.py`. It also scrubs the chat title and `think` —
`think` is drawn into the picture whenever "her thoughts" is on, and it was the
one field that leaked after the first pass.

**Nothing recorded which model wrote which message before this.** `data["gen"]`
is stamped on the take at the assistant store, so a swipe carries the model that
produced *that* swipe. Messages written earlier are marked `N unstamped` in the
footer and never guessed at — on /lmg/ the model line is the point of the post,
and a log where the backend changed halfway deserves `gemma-4-12b-qat ×36 ·
kimi-k3 ×20` rather than a confident single name.

**`@media` may never touch `.stream` / `.msg` / `.bubble` / `.think` /
`.code-block`.** The export measures in the live page, where media queries
evaluate against the window, and rasterises inside the SVG viewport, where they
evaluate against the export width. Divergence is undetectable —
`img.naturalHeight` always equals the height you declared. `test_export.py`
brace-matches every `@media` block and fails on it.

**VRAM brokering is off by default.** `vram.make_room()` unloads the local LLM
when a job needs more than is free, `give_back()` reloads it. The parked list
is persisted to `data/vram-parked.json` because a crash between the two halves
otherwise loses the debt forever, and LM Studio then JIT-reloads at its
*default* context instead of the one the user chose — silently truncating
every later chat. Reload always uses the context length `lms ps` reported.

**`lms load` will not accept the model key `lms ps` hands you, and this broke
brokering completely.** `lms ps` reports `selectedVariant` as
`publisher/name@quant` — `google/gemma-4-12b-qat@q4_0`. `lms load` answers that
with *"No model found that matches model key"*. Only the bare `modelKey` loads;
verified against `lms load --estimate-only`, which refuses both `@`-forms and
accepts the plain key. The capture deliberately stored the variant (the comment
argued that `modelKey` alone can match several quants and `-y` then takes "the
first matching model" — true, and it is right there in `lms load --help`), so
**every reload failed, every time**: the model stayed unloaded and the debt sat
in `data/vram-parked.json` forever. A long-running install accumulated two entries for the same model at
different context lengths. So: park by `modelKey`,
keep `selectedVariant` only to *notice* if a multi-variant model comes back as a
different quant, and say so — that hazard can be reported, not prevented.
`_lms_key()` also strips an `@` suffix so a debt parked by the old build still
loads. `make_room` dedupes on (driver, model) for the same reason.
Measured end to end on the real thing: park 0.6s, hand back 2.3s, identical
variant/context/parallel/TTL.

**Brokering has an LLM-to-LLM half now, and it was the commoner case.**
`make_room` parks the chat model so a ComfyUI job can have the card and gives
it back. Nothing covered: you swap models in LM Studio, CoomKit asks for one
that is not resident, and the load fails outright because a 24 GB model already
owns the GPU. LM Studio answers `Failed to load model "X"`, which reads like
the model is broken rather than like the card is full — measured here with
gemma-4-31b-qat resident and a request naming the 12b.

`vram.ensure_model()` evicts what is in the way and loads what was asked for.
It is **not a park**: you asked for a different model, so the old one does not
come back and nothing is written to `data/vram-parked.json`.

It is reached through a hook rather than a call site. `llm._post` is the single
place every LLM request leaves the process, so it carries `set_load_fixer()`;
server.py registers one at import and llm.py never imports vram (that would
invert the layering). One hook therefore covers chat, both forges, the studio
writer and memory extraction. `_LOAD_FAIL` matches only genuine load failures —
a refusal or a 429 must not trigger an eviction — and `_retry=False` on the
second attempt stops it recursing.

**Load at the context the turn was budgeted against, not the config default.**
The fixer runs deep inside `_post` with no request in scope, so
`_prepare_request` stashes `ctx` in a thread-local first. Without it a swap
loaded at 8192 while the user's preset said 20480, which is the JIT-default
truncation hazard arriving from the other direction. Measured: swap out at
19712, back in at 20480 because the preset asked for it.

Guarded, because this evicts models a user loaded by hand: `policy: off` never
touches anything and says so, `driver` must be `lmstudio`, and the backend URL
must look like LM Studio — an OpenRouter request that fails to load a model
must never run `lms unload`.

**KoboldCpp can park too, but only in admin mode.** `--admin --admindir <dir>`
(optionally `--adminpassword`). Two special targets do all the work:
`POST /api/admin/reload_config {"filename": "unload_model"}` frees the model
and keeps the server up, and `"initial_model"` reloads whatever it launched
with — so the restore is faithful *by construction*, without CoomKit capturing
context or quant at all. That is the thing the generic `command` driver cannot
do. Three traps: the endpoints answer **200 and simply refuse to act** when
`--admin` is off, so check `admin` in `/api/extra/version` rather than trusting
the status; the reload replies *before* a supervisor bounces the inner process,
so **the port disappears** and every poll must read a connection error as "still
working"; and the admin API lives at the root while the URL people have to hand
ends in `/v1`, so `kcpp_base()` strips it (longest suffix first, or `/api/v1`
leaves a stray `/api`). If the user swapped configs mid-session, `initial_model`
would restore the *startup* model, so the driver first looks for a config whose
name normalises into the loaded model's name and falls back only if there is
none. **Written against koboldcpp.py at concedo and tested against a stand-in
that reproduces that protocol (`tests/test_vram_kcpp.py`) — never against a real
KoboldCpp.** Say so if it misbehaves.

## Backend behaviour learned the hard way

- **Local (llama.cpp / LM Studio / TabbyAPI) genuinely continues a trailing
  assistant turn.** Reasoning-prefill and reply-prefill work for real. The
  gemma4 renderer leaves the thought channel *open* seeded with the prefill —
  this is the strongest jailbreak vector measured. Verified on gemma-4-e4b.
- **OpenRouter strips `reasoning_content` prefills server-side** and ignores a
  trailing assistant turn. Proven with kimi-k3. For remote, `build_payload`
  gets `force_prefill=True` which adds an in-band instruction instead; it's a
  soft request the model may decline. The UI badges this as "emulated".
- **Turning thinking off needs `reasoning_effort: "none"`, not just
  `chat_template_kwargs.enable_thinking`.** Measured on LM Studio +
  gemma-4-12b-qat: `enable_thinking: false` alone still produced ~765
  characters of reasoning; adding `reasoning_effort: "none"` took it to zero
  with the reply intact. Both are sent, and only when switching thinking
  *off* — asking a non-reasoning model to switch it on is a 400 for nothing.
  Belt and braces: `llm.stream` also splits inline `<think>…</think>` out of
  chat content, because plenty of local models put reasoning there instead
  of in `reasoning_content` and it lands in the bubble otherwise.
- **Thinking models spend the token budget before the first visible word.**
  This caused three separate "empty reply" bugs. `llm.once` now raises
  `ThinkingBudgetExhausted` when content is empty but reasoning isn't;
  `llm.once_retry` escalates `max_tokens` once. Any non-streaming helper call
  should use `once_retry`. Forge budget is 10000 for a reason.
  The **streamed** turn does the same thing inline in `_chat_send`: reasoning
  but no visible text → escalate to `max(3x, 2048)` and stream again, with a
  notice. A bare multiplier is not enough — 2.5x of a 120-token budget is
  still less than gemma-4-12b's reasoning, hence the floor. Without this a
  turn just landed blank, which is the same bug wearing a fourth hat.
**The shipped starter card is `cards/mika.png`, and it is a real v3 card.**
`seed_first_run` imports it when the `characters` table is empty — same
emptiness rule as the rest of seeding. It exists because an empty roster is
not just a sad first screen: the walkthrough's middle section is entirely
about upgrading a card into something multimodal, and with nothing to point
at those steps have no target and the ring lands in a corner.

Her looks and voice ride in the card's own v3 `extensions.coomkit` block, and
`_card_export`/`_card_import` put them there and lift them back. Before that,
exporting a character and reimporting her lost the appearance, the pinned
seed and the voice — the whole multimodal half — and she came back as text.
Other harnesses ignore extension keys they do not know, so the card still
works in ST. Regenerate her portrait through the ordinary `studio.plan/run`
path if she ever needs redoing; `tests/test_cards.py` asserts the file parses
and still carries looks + voice.

**A fresh install seeds itself, and it has to.** `server.seed_first_run()`
installs the shipped library and an `anon` persona when those tables are
*empty*. Before it existed a first run came up with zero presets and zero
jailbreaks waiting for the user to find ⚙ → library → install — and worse, it
silently broke the wizard: the blocks step does `S.presets[0]`, so with no
presets the headline step of setup rendered its summary and wrote nothing.
Setup appeared to succeed and configured nothing. Emptiness is the trigger, not
a marker file: a non-empty table is left completely alone.

**There are two notions of "workflow" and only one of them is the floor.**
`wfpack.BUNDLED` is the fourteen shipped graphs — files on disk, spliced per
run. The sqlite `workflows` table is the bring-your-own escape hatch, driven by
`comfy.run_workflow`'s `{{slot}}` markers. **They are not interchangeable and
the table must never be seeded from the bundle**: the bundled graphs contain
zero `{{slot}}` markers (their slots are a node/field map in wfpack) and the
node holding the prompt is a non-core class the splice removes, so a bundled
graph stored verbatim renders the demo prompt it shipped with — for h3, a
comic-book superboy on a rooftop. Seeding would also be a second generation
path, which this file forbids. So the *consumers* were taught about wfpack
instead: `_tool_via_studio` runs an approved ```tool``` call through
`studio.run` when no user row matches, and the settings tab lists the shipped
graphs read-only alongside the user's own. Before that, every free-form tool
call on a fresh install 400'd with "upload one in the workflows tab first"
while fourteen working graphs sat in the repo.

**Some workflows require a LoRA, and it belongs on the workflow.**
`wfpack.BUNDLED[…]["loras"]` declares it; `studio._merge_loras` merges it ahead
of the character's own stack and dedups on filename. Krea 2 needs
`MysticXXX_KREA2_v3` and all three Klein entries need `KLEIN-Unchained-V2` —
not a style preference, the models are otherwise aligned out of doing this work
at all. Two rules around it: a character's LoRAs are gated on
`spec["kind"] in LORA_KINDS`, because the audio graphs have an attachable
loader too and a face LoRA was being chained onto the stable-audio ambience
model on every ASMR render; and `inject_loras` builds **one chain per model
source**, because Wan 2.2 is two experts and chaining only the first meant half
the denoising ran without the LoRA — which does not fail, it just drifts.

**H3's references are the prop first, her second.** `<Picture 1>` is weighted
hardest and anatomy is what the model invents worst, so `studio._gather_refs`
gathers the recipe's `wants_refs` from the persona before appending her. Two
things had to change with it. The reference is no longer gated on `opts.pov` —
"she is looking at the camera the entire video" is not the first-person framing
that flag injects, so the shot that most needs the reference was the one that
never got it. And **the writer is told what each picture is**
(`studio.refs_clause`), generated from `job["refs"]` and never hardcoded: it
never sees the images, so without that the `<Picture N>` labels it writes are a
coin flip, and with no persona photo she is `<Picture 1>` and any fixed mapping
is wrong.

**The persona has to be resolved independently of the character.**
`_studio_context` used to look the chat up only `if not char`, and the studio
pane always sends `character_id` and never `persona_id` — so the persona was
never resolved on the path the UI actually uses, and the reference photo was
dead. Read `chats` with a direct query there: it is not a named-row table and
`rows_get` asserts on it with a bare AssertionError.

**The writer may choose the canvas, in a closed vocabulary.** `SHAPE:` offers
`tall` / `wide` / `square` on the first line; `studio._peel_framing` takes it
off and `wfpack.framing_values` resolves it per workflow — pixels for the
latent graphs, a ResolutionSelector combo string for h3. Words rather than
numbers because ComfyUI does not reject an off-grid size, it silently floors
it. A first line that is not one of the three is left in the prompt untouched,
so a writer that ignores the directive produces byte-identical output to
before. It is `tall`/`wide` and **not** `portrait`/`landscape` because anima's
curated `framing` tag set already uses `portrait` to mean a shot distance, and
both lists land in the same brief.

**Regex rules exist now** (`regexrules.py`). `on_prompt` and `on_display` are
independent booleans, mirroring ST's `promptOnly`/`markdownOnly`. ST's fourth
state — neither set, meaning "rewrite the stored message" — is deliberately not
implemented: messages are stored with `{{user}}` intact so the log stays
portable, and a regex that edits stored text is the same mistake as baking
macros in at write time. Such a rule imports as view-only and says so. Order is
macros first, then regex, result never written back. Patterns are converted
from JS literals (`/foo/gi`, `$1`) and a rule that will not convert is stored
**disabled with the reason attached** rather than silently never firing.
Display-scope output goes through `regexrules.sanitize`, a narrow allowlist of
layout tags with a filtered `style` attribute — that is the one path where
markup reaches a bubble, and it is markup a rule the *user* installed produced,
never anything the model wrote. `fmt()` still escapes everything else.

**Booru tags belong to Anima and nothing else.** `wfpack.BUNDLED[…]
["tag_dialect"]` is set on anima alone. Krea 2, Klein and Z-Image are
natural-language models whose own skills say tag salad and weighting syntax
actively hurt them, so artist tags and the curated tag sets are gated on that
flag rather than on the character. A character configured with artists simply
has no effect when a prose model renders her — that is correct, not a bug.

**The Danbooru corpus IS bundled now — a deliberate reversal.** An earlier
version declined to redistribute it (3.7 MB of third-party tag data, and everyone
running this already has a copy because tag autocomplete is near-universal in
a ComfyUI install). That reasoning holds for the common case and is why a copy
found in ComfyUI still *wins*. What it did not cover is the machine where
ComfyUI is remote, fresh, or absent: there the artist blender had 59,201
fewer names to roll from and said "no tag database found", which reads as
broken rather than as a missing optional extra.

`tags/danbooru.csv.gz` is 1.5 MB gzipped / 140,782 rows and loads through
stdlib `gzip`. Precedence in `tags.locate()` is **explicit `tags_db` → the
user's ComfyUI → the bundle**, and a `tags_db` that does not exist degrades to
the bundle while reporting itself in `status()["problem"]` rather than
disabling the feature over a typo.

**The bundled file must stay clean, and this is not hypothetical.** The export
it was cut from had ~9,300 hand-appended lines at the end — 391 full NSFW
prompt lines and 395 emoji section headers from the author's own private
prompt document. `tags.load()` already ignored them (they do not parse as
`name,int,int`), so nothing looked wrong; committing the file verbatim would
have published the lot. The bundle is filtered **by row shape**, not by line
number, and `test_studio.py` asserts it — every row parses, no tag name
contains `", "`. Regenerate it the same way or not at all.

`tags/tagsets.json` — fifteen curated sets, original work — also ships, and
now actually reaches the model: `studio.vocab_clause` puts the two *exclusive*
sets (rating, framing) into the anima brief. Only those two, because all
fifteen is ~1,500 tokens competing with the brief on every draft and
`skills/anima.md` already carries the rest inline.

Artist sampling is weighted by post count because uniform sampling over 59,201
artist tags mostly returns names the model has never seen. **Never seed the
artist roll with the character's pinned image seed** — that seed exists to keep
her face consistent across a gallery, and feeding it to the roll made
`artist_mode: "random"` return the identical pair forever while the UI promised
a reroll every time.

## Media behaviour learned the hard way

- **OmniVoice's `instruct` / `voice_instruct` is a CLOSED vocabulary.** Gender,
  age, accent, pitch, `whisper` — nothing else. The model *rejects* unlisted
  values and the job dies; "close mic, intimate" killed the first live ASMR
  run. Everything expressive has to live in the text and the non-verbal tags.
  Full list in `skills/voice.md`; `studio.review()` catches it before the
  render.
- **Reused upload filenames serve stale audio.** ComfyUI keeps the existing
  file and OmniVoice caches the reference embedding against the name, so
  replacing a voice sample changes nothing. Measured: the same clip cloned to
  187 Hz under a fresh name and 78 Hz — audibly male — under a reused one.
  `studio._stamped()` content-addresses every upload. **Never upload a
  reference under a stable name.**
- **A clone reference below ~180 Hz can drop an octave.** 186 Hz and up held
  across five references; a 167 Hz alto came out at 78 Hz. That band is also
  where pitch stops indicating gender, so never pick a reference by "sounds
  low". Shipped voices sit above 185 Hz and a test enforces it.
- **ComfyUI says exactly what is wrong and we used to throw it away.** A
  rejected graph 400s with a body naming the node, field and value;
  `comfy._explain_rejection` quotes it. A job that *errors* never produces
  outputs, so the old `wait_outputs` sat out the full timeout and then blamed
  a timeout — it now reads `status.status_str` and surfaces the node's message.
- **Artist names must keep their paren escapes.** `kouji_(campus_life)` has
  to reach the sampler as `kouji \\(campus life\\)`, or the parens read as
  weighting syntax and the artist silently becomes 1.1x emphasis on two
  unrelated words. The prompt-writer strips the backslashes often enough that
  `studio._escape_artists` puts them back deterministically.
- **`SaveVideo` files arrive under the history's `images` key.** Classify
  outputs by extension (`comfy.kind_of`), or an mp4 gets rendered in an
  `<img>`.
- **Combo values must match ComfyUI's option list exactly.** `"9:16
  (Portrait)"` is not `"9:16 (Portrait Widescreen)"` and the whole graph is
  rejected for it.

- Boundary-inversion jailbreaks (`[END OF INPUT]`) are patched on current
  frontier models. Deliberately not shipped in `library.py`.

## Testing

Server must be running. `./restart.sh` first — a stale process serving old code
has burned multiple sessions.

```
./restart.sh && sleep 2 && ./tests/run.sh          # 22 offline
./tests/run.sh --live                              # + the 5 that cost tokens
```

**Tests live in `tests/` and are still plain scripts** — `python3
tests/test_studio.py` runs one. There is no pytest and nothing to install, so
each file imports `_bootstrap` first: that puts the repo root on `sys.path`
(otherwise `import server` fails from one directory down) and exposes
`_bootstrap.ROOT` for anything on disk. **Use `ROOT`, never
`Path(__file__).parent`** — that now means `tests/`, not the repo, and never a
hardcoded absolute path: this tree has been relocated twice and every absolute
path in it broke both times.

All 22 offline tests pass as of the last commit. Live tests (`test_*_live.py`,
`test_live_chat`, `test_ui_smoke`, `test_phase4`) hit a real model and cost
tokens; the rest are offline or local-only.

- `tests/test_frontend.py` is static and free: verifies every `$('id')` in app.js
  exists in index.html and every `/api` path is routed. It caught
  `pickModel()` being referenced but undefined after a UI rewrite. **Run it
  after any frontend change.**
- `tests/testkit.py` generates fixture cards in memory. There are **no fixture files
  on disk** — the old `jmpjro.png` sample is gone. Don't reintroduce a
  hardcoded path to it.
- Tests resolve their own paths from `Path(__file__).resolve().parent`. **Don't
  hardcode absolute paths in tests** — the tree has been relocated once
  already and every absolute path in it broke.
- `tests/test_fixes.py` pins the bug-fix round: thinking payload, `<think>`
  splitting, director parsing, card write-through, message edit/delete and
  the schema self-heal. Offline/local, costs nothing — run it freely.
  **It deletes `data/coomkit.sqlite`** to prove the self-heal, so the suite
  wipes characters, chats and the gallery index. Generated files survive in
  `data/assets/` but become orphans. Don't run the suite over data you want.
- `tests/test_chats.py` pins the chat lifecycle: list, rename (and that a
  rename does NOT reorder), start-another leaving earlier chats openable, and
  the scoping of delete — chat-scope memories go, user/character memories and
  the gallery survive. Offline and free.
- `tests/test_gallery.py` pins the inline per-message gallery, and mostly
  exists to prove the **context exclusion**: a marker in an asset's path and
  prompt must appear nowhere in the assembled messages, and every history
  turn must reduce to role+content. That is the guarantee the feature rests
  on — media lives in `assets` keyed on message_id and never in a message.
- `tests/test_regex.py` pins the regex rules: JS-literal conversion (`$1`, `$&`,
  named groups, the backslash-doubling order that is easy to get backwards),
  scope separation, depth gating, the HTML allowlist against real injection
  attempts, and ST import including the destructive-state refusal. It also
  walks any preset in `st-presets/` and asserts every script either compiles
  or explains itself, and skips that pass quietly on a bare clone. Offline
  and free.
- `tests/test_vram_kcpp.py` pins the KoboldCpp VRAM driver against a stand-in
  that speaks its protocol — including the admin gate (which answers 200 and
  refuses to act) and the restart gap (the port vanishes mid-reload). Also
  pins `_lms_key`, the LM Studio key bug. Offline and free. It does not prove
  a real KoboldCpp behaves as documented; nothing here does.
- `tests/test_lore.py` pins lorebooks, and its FIRST section is the gate on the
  whole feature: today's `_lorebook_entries` is kept verbatim as an oracle and
  diffed against `lore.from_card` + `lore.select` over 19 entry shapes and 6
  budget boundaries. If that is red the compatibility claim is false. It also
  covers the two importers, the CJK carve-out on unpunctuated Han, whole-word,
  fair share, the header gate, the overflow report, and the routes including
  scoped cleanup and the COALESCE unique index. It walks all 17 real books in
  `~/bin/SillyTavern/data/default-user/worlds` when the machine has them and
  skips quietly on a bare clone, the same pattern test_regex uses.
- `tests/test_baton.py` pins speaker routing: the whole `pick_speaker` truth
  table (a forced pick wins; one name in the text wins; two names or zero fall
  through; the persona's name is not a match; `Rin (twin)` matches and does not
  raise; the CJK carve-out; the fairness guard needs BOTH halves; a re-roll
  keeps its own speaker), `strip_speaker_prefix`, `trim_cast_leak`, the
  name-prefix gate, and the entrance budget and card-slot order. All pure
  except the last section, which goes through `/api/chats/preview` because the
  stop cap depends on whether the backend is a configured remote. Free.
- `tests/test_export.py` pins the image export. It cannot assert anything about
  the pixels — the renderer is a canvas pipeline and there is no headless
  browser here — so it covers the two halves that ARE reachable from Python:
  the server contract (redaction by binding, whole-word scrubbing, word
  boundaries so `Ezekielson` survives, longest-name-first so `Alice` does not
  become `Anonice`, `created`/`gen` on the wire, the PNG signature check on the
  gallery-save route) and a static scan of `web/app.js` and `web/style.css` for
  the mistakes that are silent at runtime: a second renderer, a hex literal, a
  `blob:` SVG, `crossOrigin`, `image/webp`, a positioned serialise host, a
  missing C0 sanitiser, the animation override, and any `@media` rule touching
  the export's own elements. Offline and free.
- `tests/test_studio.py` also guards the **bundled tag corpus**: every row must
  parse as `name,category,count` and no tag name may contain `", "`. That is
  not tidiness — it is what stops the author's private prompt library
  shipping in a public repo. See the tags section above.
- `tests/test_studio.py` pins the studio: splice order, dangling links in every
  bundled graph, stage restore, ref wiring, lora injection, workflow choice,
  writer parsing, the review rules, dialogue extraction, comfy diagnostics
  and the voice rules. Offline and free.

## Licence

**AGPLv3-or-later.** `LICENSE` is the verbatim FSF text; do not edit it. The
AGPL and not the GPL because this is a network-facing server: someone running a
modified CoomKit and serving it to others owes them the source. Running it
locally for yourself carries no obligation, which is what almost every user is
doing.

Bundled third-party material keeps its own terms and is called out separately:
`tags/NOTICE.md` for the Danbooru snapshot, `voices/CREDITS.md` for the clone
references. Do not fold either into the project licence.

## Credentials

`or-key` in the repo root holds `COOM_OR_KEY=sk-...` and is gitignored. Read it
to configure OpenRouter; never commit it, never print it. The server attaches
keys for configured remote backends server-side so they never reach the
browser — `/api/config` and `/api/chats/preview` both omit key material, and
`test_library.py` asserts no `sk-` appears in a preview response.

## Pitfalls

- **Restart after editing Python.** The running server holds old code. Symptoms
  look like impossible bugs (a fix "not working", an endpoint 404ing).
  `restart.sh` now waits for the old process to actually die, reclaims the
  port from whoever holds it, and refuses to print success until
  `/api/health` answers — the old version slept 0.4s and lost that race, so
  the replacement died with EADDRINUSE while the *old* server kept answering.
  **If restart.sh is silent about success, believe it and read coomkit.log.**
- **`context_tokens` has to be passed to `engine.assemble_blocks`.** It has a
  default of 8192 and for a long time nothing passed it, so every chat was
  trimmed to 8k no matter what the model held — a 20k local model was losing
  12k of history for nothing. It now comes from `preset.data.context`, then
  `config.defaults.context_tokens`.
- **Never trust an imported context figure.** ST authors tick
  `max_context_unlocked` and drag the slider; Nemo's preset claims 1,000,000.
  Budgeting against that means history is never trimmed and the first long
  chat overflows the real model. `stimport` accepts 1024–200000 and otherwise
  falls back with a note.
- **`api_call` returns the open response, not bytes.** `.read().decode()` it.
- **`rows_get`/`rows_upsert` only accept named-row tables.** `VALID_TABLES` is
  presets/jailbreaks/workflows/characters/personas — `chats`, `messages` and
  `assets` are NOT in it and `rows_get` asserts on them. Read those with a
  direct query. This has bitten twice now (the speak recipe, the examples
  toggle), both times as a bare AssertionError with no message.
- **`rm -rf data` plus killing the port often trips the approval prompt.** Use
  `./restart.sh`; it handles the pidfile. Wiping `data/` also destroys imported
  cards and the OpenRouter config, which then presents as 401s.
- **`sqlite3.Row` `data` columns are JSON strings.** `rows_get()` parses them;
  raw `conn.execute` does not. Indexing a string with `["workflow"]` silently
  became a 502 once.
- Name-unique tables (presets, jailbreaks, workflows, personas) **upsert by
  name**. Re-saving updates in place rather than raising IntegrityError.
- Model output is never trusted as markup. Forge pitches and chat bubbles go in
  via `textContent`, not `innerHTML`.
- **Scope modal wiring to `#settingsBack`.** The prompt inspector reuses the
  `.modal-tab` / `.modal-pane` classes, so an unscoped
  `querySelectorAll('.modal-tab')` binds the settings handler over the
  inspector's own tabs and `openSettings()` blanks the inspector's panes on
  the way past — the inspector then reopens empty and reads as a dead button.
- **`[hidden] { display: none !important }` in style.css is load-bearing.** An
  author `display:` rule outranks the UA sheet's `[hidden]`, so every element
  styled `display:flex/grid` ignored its own `hidden` attribute — the empty
  state, the stream and the director bar were all on screen at once, which
  is what "the Nothing Open dialogue covers my chat" was. Any new
  `.thing { display: … }` on a toggled element depends on that rule.
- `loadChat()` renders into a detached fragment and swaps it in only when every
  message rendered. A throw mid-loop used to blank the entire conversation
  (the "vanishing replies" bug, fixed twice — second time properly).

## State / roadmap

**Working and verified against real hardware** (RTX 5090 + ComfyUI 0.33 +
LM Studio serving gemma-4-12b): scaffold, providers, presets, jailbreaks,
card import/export, chat engine, chat UI, thinking modes, ComfyUI bridge,
tool calls with approve/edit, scoped memory, director mode, personas, prompt
inspector, starter library, scenario forge, editable prompt layers, card
viewer/editor, message editing, session persistence, self-healing schema,
**studio recipes**, **bundled workflows**, **VRAM brokering**, **per-character
gallery**, **shipped voices**, **persona reference photos**, **character
forge**, **example dialogue**, **prompt blocks + ST import**, **the phone**,
**first-run wizard**, **walkthrough**, **unprompted texts**, **regex rules**,
**bundled tag corpus**, **workflow-required LoRAs**, **first-run seeding**,
**lorebooks**,
**many chats per character**, **roster search/favourites**, **per-message
swipes + re-roll**, **the Forge as the character workshop**, **inline
per-message galleries with same/new-seed remakes**, **the free-form "describe
it" recipe**, **phone message controls + blank openings**, **fenced code in
bubbles**, **the fit-to-window viewer**, **the chatlog image export**, **the
baton** (speaker routing, reason chips, cast stop sequences, name-prefixed
history, entrance cards).

Measured on that machine: anima 9.0s, krea2 12.0s (13.5s with the required
MysticXXX LoRA), klein 9.1s, ASMR 20s in 6.4s, MiniMax Music 3 40s in 36s,
H3 5s video with native audio in 63.8s at the old 480x864 defaults.

**H3 at the current defaults costs 453.3s and peaks at 30.9 GB of 31.36** —
10s at 896x1184, measured on the 5090 with the chat model parked. That is 98.5%
of the card and 7.1x the old figure, and it is why `vram_gb` went 26 → 30: at
26 the broker would have left a chat model resident and OOM'd the render. It
fits inside the default 900s `comfy_timeout` with half the budget spare on that
card and will not fit on a smaller one; the fallback that keeps the framing is
0.7 MP at 3:4 (736x992).

**15s at 1.0 MP does not OOM, but it is the ceiling**: 876.5s and 31.2 GB of
31.36 — 99.5% of the card. The hazard turned out not to be memory at all. It
was the clock: 876.5s against the old 900s `comfy_timeout` default is a 2.6%
margin, so a marginally slower card threw away a quarter of an hour of real GPU
work to a timeout. The default is 1800 now. That costs nothing, because a job
that genuinely *fails* is surfaced the moment ComfyUI records it
(`comfy._failure`) rather than by waiting the timeout out — the ceiling is only
a backstop for a hung server. `studio.review` warns before a ≥15s 1.0 MP clip
rather than after.

Measure this with the chat model *off* the card. A first attempt at the 10s job
read 488.5s / 30.4 GB with gemma-4-12b resident for part of the run — the
contention cost ~35s and, less obviously, *understated* the peak, because the
render had less room to expand into. A contaminated VRAM figure is worse than
none: it reads as headroom that is not there.

### Things that were true and are no longer

A pass over nineteen reported issues in one session moved several of these,
and two of the reports turned out to be correct premises with a different bug
hiding behind them. Recorded because the shape of the mistake repeats:

- **The swipe arrows were wired the whole time.** They were dead because
  `add_swipe` never seeded `swipes[0]` with the original take, so one regen
  produced a list of length 1 that both arrows clamped inside — and the two
  sides counted in different index spaces on top of it. Looking at the
  handler would have found nothing.
- **The thought channel had a writer and a reader that disagreed.**
  `render_prompt` leaves it open on purpose; `stream()` assumed it started
  closed. Neither half is wrong alone.
- **A gate can be invalidated by a feature landing hours later.** The
  first-run check required an empty presets table; seeding filled it before
  the first request. Both commits were correct when written.
- **A hardcoded id outlives the rename that killed it.** `female-bright`
  survived in the frontend for two commits, rendering the voice picker blank
  and resolving server-side to a different archetype. test_frontend could not
  see it — it only knows element ids and route strings.
- **"It shows my local list" was right, and still had two bugs behind it.**
  The LoRA list really is read live from the user's ComfyUI; a failed probe
  was cached forever and an unknown name was silently deleted on save.

### Designed but not built

Nothing. All three tracks from the 2026-08-19 design passes have shipped and
`docs/NEXT.md` has been deleted rather than left to rot — the rule was that
anything built moves into this file and comes out of that one, and the file
reached zero.

- **Track A — studio kind guards + per-shot model.** SHIPPED 2026-08-19.
  Documented above, under the portrait re-roll.
- **Track B — the baton.** SHIPPED 2026-08-19. Documented above, under the
  cast. Model *nomination* and reply chains were CUT on measurement and the
  numbers are recorded so they stay cut.
- **Track C — lorebooks.** SHIPPED 2026-08-19. Documented above,
  under the prompt architecture. `docs/NEXT.md` is now empty and gone.

### Not done, roughly in order of value

- **Regex rules have no per-character scoping in the UI.** The column exists
  (`regex_rules.character_id`, NULL = global) and the query honours it, but
  nothing writes a non-NULL value yet — every imported rule is global. A
  character-scoped import is one dropdown away.
- **Nothing applies regex to user input.** ST has a third placement for it.
  `on_prompt`/`on_display` cover what people actually import; an input scope
  would need a third boolean and a pass in `_chat_send` before the message is
  stored.
- **A display rule does not affect the live streamed bubble.** The client
  renders from accumulated chunks and discards the `done` frame, so a
  hide-the-thinking rule settles only on reload or on the next `loadChat()`.
  Fixing it means emitting a distinct replace frame and having both stream
  consumers swap the bubble.
- **Python `re` has no timeout.** An imported pattern runs in the request
  thread with no way to abort it, so a nested-quantifier pattern is a
  self-inflicted hang. None of the eleven real Celia patterns misbehaved, but
  that is luck, not a guarantee.
- **The two LTX graphs ship without a `BUNDLED` entry**, so `wfpack.load()`
  raises KeyError for them and no test parses them. Either wire them up or
  delete the files — shipping unreachable graphs also makes the README claim
  about them false.

- **VRAM brokering covers LM Studio and KoboldCpp; the rest fall back to a
  command.** `vram.DEFAULTS["driver"]` is `none | lmstudio | koboldcpp |
  command`. Those two capture and restore faithfully — lmstudio by recording
  context/parallel/TTL, koboldcpp by letting the server reload its own launch
  config. llama.cpp, Ollama, TabbyAPI, vLLM and SGLang still fall to the
  generic `command` driver: two shell strings, no capture, so a reload is
  exactly as faithful as whatever the user wrote. Ollama would be the next
  worthwhile one — it has `/api/ps` and a keep_alive model. llama-server has
  no unload at all and would need a supervisor.
  **The KoboldCpp path has never been run against a real KoboldCpp** — only
  against a stand-in built from its source. Treat a bug report there as
  plausible.
  Also unverified: whether raw-**completion** mode (`/v1/completions`, and
  therefore the reasoning-prefill jailbreak vector) is served by Ollama's
  OpenAI shim. Chat mode is fine everywhere; that one needs testing on a box
  that actually has it.

- **She only texts while the tab is open.** The scheduler is in the browser.
  A server-side one needs somewhere to keep a backend + model + key, which is
  why it was not done — decide that deliberately before building it.
- **Unread only shows on the minimised pill**, not in the page title. A
  `document.title` badge is ten lines.
- ~~The tests litter the roster.~~ **Fixed.** `testkit.sweep_fixtures()` runs
  at exit and deletes every character named in `FIXTURE_NAMES` plus its chats,
  messages, assets and memories. Swept BY NAME, not by tracking ids, so a
  fixture left by a file that never called `ensure_character` still goes; any
  test file that creates one imports `testkit` purely to register the sweep.
  It had got bad: hundreds of fixture characters against one real one, and
  hundreds of orphaned chats.
  Two related bugs came with it, and both wrote on the user's own data:
  `testkit.ensure_character` reused *any* character with a `first_mes`, which
  on a real install means it adopted the shipped starter and ran its chats
  over her; and `test_library` took `chars[0]` outright, which then failed its
  own "Fixture-chan is in the prompt" assertion once the roster was clean.
  Both now insist on a fixture. Asset FILES are deliberately left on disk —
  a render is expensive, a stray file is harmless, a roster full of
  Fixture-chan is not.
- **Consolidation has never fired for real.** It triggers at 40 facts in one
  scope and has only been exercised synthetically. Watch it the first time.
- **`mes_example` retirement is a guess.** 3000 tokens of history was chosen
  by reasoning, not measurement.
- **Real-ComfyUI verification for LTX and wan-t2v.** Bundled and building
  cleanly, never actually run.
- **First-run on a machine with nothing installed.** The wizard has only been
  walked with LM Studio and ComfyUI already up. The no-backend branch is
  written but untested.
- **j-space / Subtext integration** (late game). The original plan folder is
  gone — everything in it shipped except this, so it is written down here
  instead of living in an untracked file nobody reads. Connect to a running
  local model's latent space via Subtext
  (https://github.com/ninjahawk/Subtext); visualise and explore its internal
  representations; inject latent-space manipulations back into the model
  (mindbreak/hypnosis); and "show the model its own j-space" by feeding
  latent visualisations back as context. Entirely speculative, no design work
  done, and it needs a backend that exposes activations at all — which none of
  the seven in `BACKEND_PRESETS` currently does.

### Notes for whoever picks this up

- **Run `./restart.sh` before testing anything.** A stale process serving old
  code has burned multiple sessions.
- **The suite deletes and restores the database** (test_fixes, to prove the
  schema self-heal). It is safe now, but do not add a second destructive test
  without the same snapshot/atexit dance.
- **`or-key` must exist** for the OpenRouter tests. Without it test_vanish and
  test_tool_e2e fail with 401s that look like code bugs and are not.
- **Watch out for `cd` in Bash calls.** The working directory persists between
  them, and a stray `cd` into ComfyUI made a later "edit .gitignore" land in
  the wrong repository. Prefer absolute paths.
- The three SillyTavern presets that informed `blocks.py` are not in the repo
  (`/*.json` is gitignored). `stimport` was validated against them; keep a
  copy somewhere if you plan to change the importer.
- Bundled workflows in `workflows/` were exported from the live ComfyUI
  frontend via `app.graphToPrompt()`, which is the only conversion that is
  guaranteed correct. Re-export the same way if they need refreshing.

## Working style

The user wants autonomous rolling development with minimal check-ins, and
prefers real live-tested results over descriptions. Verify with actual model
calls and say plainly when something doesn't work rather than papering over it.
Commit at each working milestone with a message that explains *why*, not just
what.
