![CoomKit](docs/banner.png)

# CoomKit

A local-first companion harness for adult roleplay. Text, images, video, voice,
music — driven by your own models and your own ComfyUI, on your own machine.

Built as an alternative to SillyTavern for people who want something modern and
less fiddly, without giving up control over the prompt.

```
./run.sh          # http://127.0.0.1:3939
```

No install step. No dependencies. Python 3.10+ standard library and a folder of
static files.

![The chat, with the prompt rail open](docs/shots/chat.png)

---

## Why

SillyTavern works, but two things wear you down.

**Cards go stale.** A card ships one `first_mes` and maybe a few alternate
greetings. After a handful of sessions you've read them all and the character
feels exhausted — even though the character is fine. It's the *setup* that went
stale. CoomKit brainstorms a fresh situation with you instead, using what it
knows about you and what the two of you have already done, then starts the chat
already in motion.

**You can't see what it's sending.** Every harness quietly injects a dozen
instructions on your behalf. When output goes wrong you end up tuning samplers
for an hour because some invisible sentence is steering the model. CoomKit shows
you the exact outgoing prompt, and lets you edit every injected layer.

## What it does

**Chat**
- SillyTavern card import/export (v1, v2, v3) — PNG or JSON, re-embeds on export
  so cards still work in ST. A CoomKit export carries her appearance, pinned
  seed and voice in the v3 `extensions` block, so the multimodal half of a
  card survives the round trip; other harnesses ignore it.
- **Ships with a character.** A fresh install comes up with Mika, already
  given a face, a pinned seed and a voice — so "🤳 Selfie" works in one click
  instead of after an hour of setup.
- Streaming replies, collapsible thought blocks, swipes, regenerate
- Two request modes: instruct/chat and raw completion with full prompt control
- Prompt templates: Gemma 4 (canonical), ChatML, Llama 3, plain
- Thinking styles: off, normal, or **in-character** — her filthy inner monologue
  instead of an assistant analysing a task
- Reasoning prefill and reply prefill, with honest per-backend labelling of what
  actually works where
- **SMS sidechat** — a separate phone-styled thread with the same character
- **Or no character at all.** "Just talk to the model" opens a plain chat with
  your preset, your jailbreak, your samplers and your persona, and nothing
  else — a local LLM front-end when you want one, in the same window.

![A plain chat with no character loaded](docs/shots/plain-chat.png)

- **More than one character in a scene**, with only one of them speaking per
  turn. Who speaks next is decided *before* the turn from six free rules — you
  picked her, she was asked directly, she still has the floor, it is her turn —
  and every reply says which rule it was. No extra model call, no coin flip.
  Everyone else in the room gets a short dossier, so she can describe them
  without borrowing their personality.
- **Lorebooks** — import SillyTavern World Info files or lift the book out of a
  card. Attach to a character, one chat, or everything; several at once, none
  of them merged. The inspector names every entry that fired, what it cost, and
  how many more matched but did not fit.
- **Post it** — turn a log into a PNG worth posting, not a screenshot. Draws the
  real bubbles with no browser chrome, unclips her reasoning and your code
  blocks (both of which scroll on screen and would simply be lost in a
  screenshot), splits a long log into sheets on message boundaries under
  4chan's 10000px limit, signs it with the model and samplers that actually
  wrote each message, and swaps your persona's name for `Anon` **on the server**
  before the text ever reaches the browser. Shows you the 125px thumbnail a
  reply gets, too, which is the part nobody checks until after they post.

**Scenario forge**
- Pitches distinct fresh scenarios from the card, your persona, and optionally
  what she remembers
- Tell it what you're in the mood for; that's treated as a hard requirement
- Revise any pitch conversationally ("rainier", "she initiates", "move it to
  the car") or edit the fields by hand
- Launch it and the chat starts mid-scene, with the scenario in context

**Memory**
- Three scopes: durable facts about **you** (shared across all characters),
  your **relationship** with one character (spans every chat with her), and
  **scene-local** detail that doesn't leak
- Extracted in the background after replies, editable and deletable by hand,
  toggleable per chat

**Studio — images, video, voice, music**

![The studio: ten one-click shots, and the GPU broker](docs/shots/studio.png)

- **Ten one-click shots.** Modelling photo, filthy solo, selfie, handjob,
  blowjob, this-moment, ASMR, song, "say it out loud" — and **"describe it"**,
  where you type what you want in plain English and she translates it into
  whatever your model actually speaks. The explicit ones toggle between a
  still and a clip, and between third person and POV.
- **Workflows included, and already wired up.** Anima, Krea 2, FLUX.2 Klein
  4B/9B, Z-Image, Klein edit, MiniMax H3 (video *with* synchronised audio),
  Wan 2.2, OmniVoice, IndexTTS-2 and MiniMax Music 3 ship as real graphs and
  work on a fresh install with nothing to import. Optional node packs are
  spliced out automatically, so they run on a stock ComfyUI — and the heavy
  quality stages are one checkbox when you want a keeper.
- **You approve every prompt.** Her rough idea is rewritten into the right
  dialect for the target model, shown to you with pre-flight warnings, and
  only runs once you say so.
- **Bring your own** workflow too: export with **Save (API format)**, paste it
  in, mark injection points with `{{prompt}}`, `{{seed}}`, `{{image}}`.
- **One gallery per character**, global across every chat you've had with her.

**Voice**
- Clone her from a 3–15 second clip, or use one of the bundled references, or
  describe the voice in words — OmniVoice and IndexTTS-2 both synthesise one.
- "Say it out loud" speaks the dialogue from her last reply and nothing else,
  so she doesn't read her own stage directions aloud.
- Preview whatever you picked before you commit to it.

**One GPU, two tenants**
- A 12B chat model and a video model do not fit in 32 GB together. CoomKit can
  park your chat model for the render and hand the card straight back —
  reloading at the context length you chose, not the default.
- Off unless you turn it on. Two GPUs or 80 GB of one? Leave it off.

**Control**
- 🔍 **Prompt inspector** — the exact outgoing payload, rendered and as raw
  JSON, with token counts. Built on the same code path the real request uses, so
  it cannot drift. Every block is named, priced, and switchable from here.

![The prompt inspector — every block named, priced and editable](docs/shots/inspector.png)

- **Editable prompt layers** — director framing, texting rules, in-character
  thinking, the forge's own prompts, the memory extractor. All named, described,
  and yours to rewrite. Reset any of them.
- **Director mode** — steer the scene from outside it. Stage direction she obeys
  without acknowledging.
- Personas, sampler presets, jailbreak library, live sampler controls
- **It shows you round itself.** A first-run wizard finds your model, pings your
  ComfyUI and installs a starter library, and an 18-step walkthrough then points
  at the real interface — roster, forge, the studio's ten shots, the GPU broker,
  memory, the inspector, prompt blocks — narrated by the girl in the corner, on
  a throwaway demo conversation so you need no card and no model to take it.
  Replay it any time with **?** in the topbar; **✦** reruns setup.

**Privacy**
- Everything is local: SQLite, a config file, your assets on disk
- **Images you upload never go to a remote provider.** If you've selected a
  hosted model, the picture stays on your machine and the model is told it
  wasn't sent, rather than being allowed to hallucinate what was in it
- API keys stay server-side and never reach the browser

## Backends

Auto-detected: LM Studio (1234), llama.cpp / llama-server (8080), Ollama
(11434), KoboldCpp (5001), TabbyAPI (5000). Any OpenAI-compatible endpoint can
be added by hand, including hosted ones.

Local backends get the most out of CoomKit — they genuinely continue an
assistant turn, which makes prefills real rather than advisory. Hosted providers
mostly strip that; the UI tells you when a control is being emulated instead of
honoured, so you're never guessing.

## Getting started

1. Start your local model server (LM Studio, llama-server, whatever)
2. `./run.sh`, open http://127.0.0.1:3939
3. The first-run wizard finds your model, pings your ComfyUI, and installs a
   starter prompt. Presets, jailbreaks and a persona are seeded automatically
   on an empty database — there is nothing to install by hand.
4. Drag a character card PNG onto the roster
5. **✦ forge** to brainstorm a scene, or just hit **chat**
6. Optional, and offered in the wizard: bring your own ComfyUI graphs, or a
   SillyTavern preset (its regex scripts come across too)

## Layout

```
CoomKit/
  server.py    routes + prompt assembly     web/         the UI
  llm.py       providers, templates          skills/      diffusion dialects
  engine.py    context assembly             data/        yours, gitignored
  cards.py     ST card parsing
  memory.py    scoped memory                tests/       21 test files
  scenarios.py the forge
  prompts.py   editable prompt layers
  comfy.py     ComfyUI bridge               workflows/   16 graphs, 14 wired
  regexrules.py find/replace rules           tags/        tagsets + corpus
  tools.py     tool calls                   voices/      clone references
  studio.py    the generation path
  recipes.py   the one-click shots
  wfpack.py    workflow surgery
  vram.py      GPU brokering
```

## Status

Working and tested against real hardware — an RTX 5090 running ComfyUI with
LM Studio serving Gemma 4 12B. Measured there: a Krea 2 still in 12s, Anima in
9s, 20s of ASMR in 6s, a 40-second song in 36s, and a 5-second MiniMax H3 clip
with native audio in 64s, with the chat model stepping off the card and back
on around it.

A first-run wizard and a walkthrough both ship — a fresh install seeds itself
with presets, jailbreaks, a persona and a character, so there is no empty room
to configure your way out of.

## Contributing

Read **[PHILOSOPHY.md](PHILOSOPHY.md)** first — it is short, and it says what
this is for, what it will not become, and where help is actually wanted.

The short version: local first with cloud APIs as deliberate second-class
citizens; presets are toggleable prompt blocks rather than 24,000-token JSON;
your log stays canonical; no dependencies, ever; measure it and say the number.

**The flagship ask is VRAM parking on every local backend.** LM Studio and
KoboldCpp are done; Ollama, TabbyAPI, vLLM, SGLang and llama-server currently
fall back to a pair of shell commands you write yourself. A driver is one file,
four functions and a stand-in test, and if you run one of those you are the
only person who can test it properly.

## Licence

CoomKit is free software under the **GNU Affero General Public License v3.0 or
later**. The full text is in [LICENSE](LICENSE).

The AGPL rather than the GPL for one reason: this is a network-facing server.
If you run a modified CoomKit and let other people use it over a network, they
are entitled to the source of the version you are running. Running it on your
own machine for yourself carries no obligation whatsoever — which is what
almost everybody is doing.

Bundled third-party material is covered by its own terms, not by this licence:
the Danbooru tag snapshot in `tags/` (see [tags/NOTICE.md](tags/NOTICE.md)) and
the voice references in `voices/` (see [voices/CREDITS.md](voices/CREDITS.md)).
Models, workflows and character cards you supply are yours; nothing here
attaches to what you generate with it.
