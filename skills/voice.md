# Voice — OmniVoice & IndexTTS-2

**Nodes:** `OmniVoiceVoiceCloneTTS` · `OmniVoiceVoiceDesignTTS` ·
`IndexTTSEngineNode` + `IndexTTSEmotionOptionsNode`
**Ambience bed:** Stable Audio Open 1.0, 50 steps / CFG 5.0

Three paths, and which one you are writing for is decided before you start:

| Path | When | You control |
|---|---|---|
| **clone** | the character has a voice sample | text, `instruct`, speed |
| **design** | no sample — the voice is described in words | text, `voice_instruct`, speed |
| **emotion** | the performance matters more than the turnaround | text, 8-slider emotion vector |

## Output format

Emit **one fenced `json` block and nothing else**:

```json
{
  "text": "the words she actually says, with inline tags",
  "voice_instruct": "female, young adult, moderate pitch",
  "instruct": "",
  "speed": 1.0,
  "ambience": "",
  "emotions": {}
}
```

Include only the keys the path needs. `text` is always required.

## The vocabulary is closed

`voice_instruct` and `instruct` take values from a **fixed list**. An
unsupported value is not ignored and not interpreted — it is passed to the
model as text it has no meaning for, so it steers nothing while reading as if
it should. CoomKit warns you before the render rather than after it. This is
the single most common way to waste a voice job.

| Category | The only permitted values |
|---|---|
| Gender | `male`, `female` |
| Age | `child`, `teenager`, `young adult`, `middle-aged`, `elderly` |
| Accent | `american accent`, `british accent`, `australian accent`, `canadian accent`, `chinese accent`, `indian accent`, `japanese accent`, `korean accent`, `portuguese accent`, `russian accent` |
| Pitch | `very low pitch`, `low pitch`, `moderate pitch`, `high pitch`, `very high pitch` |
| Style | `whisper` |

Comma-separated, one value per category, e.g.
`female, young adult, low pitch, british accent`.

**Never** write `sultry`, `breathy`, `close mic`, `intimate`, `raspy`,
`seductive`, `moaning`, or any other adjective, however well it describes what
you want. There is nowhere for it to go. Everything expressive that is *not*
in that table has to be carried by the **text** instead — by word choice,
punctuation, sentence length, and the non-verbal tags below.

## Non-verbal tags — where the performance actually lives

Insert these inline, in the text:

`[laughter]` · `[sigh]` · `[sniff]` · `[question-en]` · `[question-ah]` ·
`[question-oh]` · `[question-ei]` · `[question-yi]` · `[surprise-ah]` ·
`[surprise-oh]` · `[surprise-wa]` · `[surprise-yo]` ·
`[dissatisfaction-hnn]` · `[confirmation-en]`

**That list is exact, and inventing a tag is worse than using none.** A tag
is not a special token — checked against the shipped tokenizer, not one of
these is a token or even a single vocabulary entry. They are ordinary text
the model was trained to perform, which has one consequence you have to
write around: a tag it does not know is not ignored, it is **read out as a
word**. `[moan]`, `[gasp]`, `[kiss]`, `[wet]` and `[breathy]` do not exist,
and each one puts that word in her mouth, out loud, in the middle of the
line.

### What each one actually sounds like

Rendered here, one tag per take, and transcribed back to see what came out:

| Tag | What you get |
|---|---|
| `[sigh]`, `[laughter]` | breath — no words. **The safest two.** |
| `[surprise-ah]` | a wordless vocalisation |
| `[confirmation-en]` | "mmm" |
| `[question-en]` | "mmm?" |
| `[question-ah]` / `[question-oh]` | "ah?" / "oh?" |
| `[question-ei]` / `[question-yi]` | "hey?" / "yi?" |
| `[surprise-oh]` / `[surprise-wa]` / `[surprise-yo]` | "oh!" / "wah!" / "yo!" |
| `[dissatisfaction-hnn]` | "hmph!" |
| `[sniff]` | **the spoken word "sniff" — broken, do not use** |

`[sniff]` is documented by OmniVoice and does not work: five renders across
five seeds all pronounced it, while `[sigh]` performed correctly in all
five. It is deterministic, so rerolling will not save it. Use `[sigh]`.

The rest are learned text rather than switches, so treat the table as what
they did here, not a guarantee. If a take reads a tag aloud, cut the tag.

These are the whole expressive toolkit. A line reads as intimate because it is
short, because it breaks, because there is a `[sigh]` before it — not because
you asked for "intimate".

Punctuation does real work: ellipses slow the read, full stops harden it, a
line break is a beat. Write in short sentences. Long clauses flatten out.

## Two settings that will wreck the audio

Both measured on this exact stack, neither documented upstream.

**Keep `speed` at 1.00 for anything whispered.** A whisper *is* high-frequency
turbulent noise, and the speed control throws exactly that away — at 0.85 the
read is 99% sub-1 kHz, boomy and audibly distorted. Pace with `duration` or
with shorter sentences instead. On the *cloning* path 0.9–1.1 is safe.

**Never combine `low pitch` with `whisper`.** They conflict and the model
picks pitch: `moderate pitch, whisper` gives 38.8% of energy above 2 kHz,
`low pitch, whisper` only 17.7%. Counter-intuitive, consistent. For a whisper,
use `moderate pitch` — or omit pitch entirely.

Whisper quality is also a **seed lottery** — across six seeds on one script,
HF energy ranged 0.15% to 14.6%. If a take sounds dull, reroll it.

## Choosing a reference clip to clone

Measured on this stack, and the single most surprising result here: **a
reference below roughly 180 Hz median F0 can collapse an octave.** A female
alto reference at 166.7 Hz cloned to **77.6 Hz** — an unmistakably male
voice — while every reference at 186 Hz and above held:

| Reference median F0 | Clone median F0 |
|---|---|
| 166.7 Hz | **77.6 Hz** — collapsed |
| 186.6 Hz | 177.7 Hz |
| 193.7 Hz | 179.7 Hz |
| 198.3 Hz | 210.1 Hz |
| 213.7 Hz | 217.5 Hz |

So do not pick a reference by "sounds low" if you want a low female voice —
the ambiguous 165–180 Hz band is exactly where the model guesses wrong. Aim
for 185–200 Hz for a warm/alto result and let the *text* carry the intimacy.

The same trap catches gender: 166 Hz sits on the male/female boundary, so
pitch alone cannot tell you what a clip is. Listen to it, or clone it once and
listen to that.

Other reference rules: 3–15 seconds, one speaker, no music or background,
plain delivery. A clone inherits whatever performance is in the clip, so an
emotive reference fights every later instruction.

## Writing for the ear, not the page

This is a performance whispered a few inches from someone's ear, and the model
has exactly two levers: the words and the tags. Everything that reads as
arousal on the page — adjectives, elaborate description — reads as *narration*
in audio, which is the opposite of intimate.

What actually works: short fragments. Ellipses. Broken breathing. Half-
finished thoughts. Repetition. Second person, present tense, saying what she
is doing right now. `[sigh]` before a line changes it more than any adjective
could.

Non-verbal sounds — kissing, licking, sucking, wet mouth noises — are **not**
written into her lines as text, and OmniVoice has no tags for them. They come
from the ambience bed underneath. Write her words as if that sound is already
happening around her: if the bed is her nails, she talks about her nails; if
it is her mouth, the lines get shorter, wetter and sloppier.

## Ambience — ask for a texture, never an event

The bed comes from Stable Audio Open, and it has one failure mode: name an
event and you get sporadic bangs instead of a bed. Measured level spread
across 5s windows — `rain, distant muffled thunder` swung **±10 dB**;
`steady continuous rain, constant unchanging texture` swung **±0.3 dB**.

So: always include the words `steady`, `continuous`, `constant`,
`unchanging`. Describe a surface and a microphone distance, never a moment.

Good: `steady continuous fabric rustling against a microphone, constant
unchanging texture, close mic` · `steady continuous rain on a window,
constant level` · `soft continuous room tone, unchanging`

Bad: `a door slams` · `she shifts on the bed` · `thunder rolls`

Ceiling is **47.6 seconds**.

## IndexTTS-2 emotion vector

Eight sliders, each **0–1.2**: `Happy` `Angry` `Sad` `Surprised` `Afraid`
`Disgusted` `Calm` `Melancholic`. `emotion_alpha` scales the whole vector.

Voice identity holds independently of emotion — a reference at 77.8 Hz median
F0 renders at 80.8 Hz with the vector at zero and stays recognisably the same
person at `Angry 0.9`, which lands at 164.5 Hz. So set emotion freely; it will
not turn her into someone else.

Set **one or two** sliders. Stacking four averages into nothing. Arousal maps
monotonically onto pace and pitch: `Afraid 0.85` → 3.2 syl/s, `Angry 0.9` →
4.1 syl/s.

High-arousal emotions run hot and clip during generation, where no downstream
gain can repair it. Prefer ≤0.9 on `Angry` and `Happy`.

## Writing the text

- Say only what she says. No narration, no stage directions outside the tags,
  no quotation marks around the whole line.
- Keep a single take under ~40 seconds of speech. Beyond that, chunk it.
- Numbers, symbols and abbreviations get spoken literally — write `twenty
  past three`, not `3:20`.
- Keep her actual words when they were given to you. You are choosing the
  delivery, not rewriting her dialogue.
