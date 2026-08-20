# What CoomKit is for

We are AIchads: we ship fast, are agile, strong, fast, and a threat to all snailcats.

---

## Local first. Not local-friendly. Local **first**.

CoomKit is built for the machine under your desk. Your models, your ComfyUI,
your GPU, your files on your disk. Nothing phones home, nothing needs an
account, and there is no hosted anything. Nothing ever goes to corpo or cloud unless our user tells it to.

Cloud APIs are supported and they are **second-class citizens**, deliberately.
They work, they are tested, and they will never be the reason a local feature
is compromised. When a design choice is better for a local backend and worse
for a remote one, the local backend wins and the remote gets a badge in the UI
explaining what it is missing. Reasoning prefill is the standing example: it is
the strongest jailbreak vector we have, local backends genuinely continue a
trailing assistant turn, and OpenRouter strips it server-side for most providers. So local gets
the real thing and remote gets an in-band request that says "emulated" on the
screen. We did not weaken the local path to make the two match.

The same rule decides the vision path: if the selected backend is a configured
remote, your uploaded image is **not sent**. Yes, this software allows you to store your cock pic under your persona for reference, but it will never go to the cloud. 
The model is told so in-band
instead.

## Prompt blocks, not preset slop

A preset is a list of toggleable blocks in a defined order. Each one says what
it is, what it costs in tokens, and where it sits. You can reorder them, turn
them off, and read the exact text of every single one, including the ones
CoomKit injects on your behalf, which live in `prompts.py` and are all
user-overridable.

A jailbreak is one of those blocks. That is all it is.

**Keep your cards. Leave the preset slop behind.** The 24,000-token JSON
monsters people trade around are not prompts, they are sediment: a hundred
contradictory instructions nobody has read end to end, most of them fighting
each other, kept because removing any one of them feels risky. We import them
(`stimport`) and we will tell you what they cost, but the destination is a
block list you can actually reason about, not a blob you paste in and hope will work with your corpo model.

The inspector is the enforcement mechanism. There is **one** prompt-assembly
path `_prepare_request` → `assemble_blocks` used by both the real send and
the preview, so the inspector physically cannot show you something different
from what gets sent. There is nothing to drift. Every line in the prompt names
the block that produced it.

If you add a context layer, add it there. Not in a second place.

## Your log is yours and it stays canonical

Messages are stored with `{{user}}` intact. Macros resolve late, on the way to
the model and on the way to the screen — never baked into storage. Regex rules
transform what the model reads or what you read; they do not rewrite what is
saved. Switch persona and your entire back catalogue re-resolves.

This is why SillyTavern's "alter the stored message" regex mode is deliberately
not implemented, and imports of such rules land view-only with the reason
attached. A harness that edits your history to make a feature work is a harness
that has lost your history.

## Measure it, then say the number

This project's commit messages are full of numbers because guesses get
reverted and measurements do not. "Entrance cards cost +235 tokens on a
realistic card and almost nothing on a short one" is useful. "Entrance cards
improve context" is noise.

Two habits that come with it, both non-negotiable in review:

- **When a claim is checked and turns out false, correct it out loud** rather
  than quietly deleting it. Half the comments in this codebase exist to stop
  the next person rebuilding a defence against a problem that does not exist.
- **A test that has never seen the bug fail is not a regression test.** Revert
  your fix, watch it go red, put it back.

## No dependencies. No build step. No exceptions.

Python 3.10+ standard library on the back end. Vanilla JS, HTML and CSS on the
front, served straight off disk. `./run.sh` and you are running.

If you want a library, write the thirty lines. This is not asceticism — it is
what makes "download and run" true for a user who is not a developer, on a
machine you will never see, without a virtualenv, a lockfile, or a bad evening.
The bar for adding a dependency is not "it would be convenient". There is no
bar. Write the thirty lines.

---

# Help wanted, specifically

## VRAM parking on every local backend

This is the flagship ask, and it is the most useful thing you could contribute.

When a render needs the GPU, CoomKit unloads your chat model, runs the job, and
puts the model back **exactly as it was** — same context length, same settings.
Get that wrong and the model silently reloads at its default context, quietly
truncating every conversation afterwards. That is why "just restart it" is not
good enough.

Today:

| Backend | State |
|---|---|
| **LM Studio** | Full. Captures context, parallel and TTL, restores all three. |
| **KoboldCpp** | Full, in admin mode. Written against its source, **never run against a real KoboldCpp** — bug reports very welcome. |
| **`command`** | Generic escape hatch: two shell strings you write. No capture. |
| Ollama, TabbyAPI, vLLM, SGLang, llama-server | Fall back to `command`. |

**Ollama is the obvious next one** — it has `/api/ps` and a `keep_alive` model,
so a real capture-and-restore driver is very achievable. `llama-server` has no
unload at all and needs a supervisor; that is a harder design question and we
would like to talk about it before someone writes it.

A driver is one file, four functions, and a stand-in test. `vram.py` has two
worked examples. If you run something not on that list, you are the only person
who can do this properly, because you are the only one who can test it.

Two traps found the hard way, so you do not find them again: KoboldCpp's admin
endpoints answer **200 and simply refuse to act** when it was started without
`--admin`, and `lms load` will not accept the model key that `lms ps` hands
you.

## Other good first issues

- **Regex rules have no per-character scoping in the UI.** The column exists
  and the query honours it; nothing writes a non-NULL value. One dropdown.
- **A display rule does not affect the live streamed bubble** — it settles on
  the next load. Needs a distinct replace frame and both stream consumers.
- **At-depth lorebook entries.** A quarter of real world-info books ask to sit
  N messages deep. We import them, we say we cannot place them, and we place
  them with the rest. This is the largest fidelity gap with SillyTavern.
- **Ship a workflow for a backend we do not have.** Every bundled ComfyUI graph
  was exported from one working install on one card.

---

## How we work

Fast, strong, ship often. Small commits that each do one thing and explain
**why** in the message not what, the diff says what. If you found a landmine,
write it down where the next person will hit it.

We are not snailcats. There is no roadmap committee, no RFC process and no
six-week review cycle. Open the PR, say what you measured, and let us look at
it.

Be honest in the commit message when something does not work. "Tested against a
stand-in, never against the real thing" is a perfectly good sentence and it has
saved more time here than any amount of confidence would have.

## Non-goals

- **A hosted version.** No.
- **Accounts, telemetry, analytics, crash reporting.** No.
- **A frontend framework or a build step.** No.
- **Making the remote path first-class.** It is supported. It is not the point.
- **Moderating what you write.** It is your machine and your model. Content
  policy is between you and whatever you are running.

## The one hard line

Everything above is a preference. Nothing openly pornographic especially regarding minors or underage characters in ANY message, issue, PR, discussion. We are good and well behaved citizens of the Open Source community.
