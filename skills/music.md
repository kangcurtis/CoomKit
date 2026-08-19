# MiniMax Music 3 — structured caption + tagged lyrics

**Model:** `minimax_music3_dit_fp16`
**Text encoder:** `minimax_music3_text_encoder` (type `minimax`) · **VAE:** `minimax_music3_dav`
**Sampling:** 30 steps / CFG 1.7, `euler` + `simple` · **Length:** up to 360s

This model does not take a one-line prompt. It takes a **structured caption**
describing the record as a producer would, plus **separately tagged lyrics**.
Writing "sad piano song" gets you the average of everything sad.

## Output format

Emit **one fenced `json` block and nothing else**:

```json
{
  "caption": "Global Metadata\n…\nVocal Details\n…\nArrangement\n…",
  "lyrics": "[Verse]\n…\n[Chorus]\n…",
  "duration": 120
}
```

`duration` is seconds — the ceiling is 360, and the render is **cut hard**
there. Ask generously: the model stops when the song is finished, so a
request of 210s that only needs 170s ends properly, while lyrics that overrun
a 45s request end mid-word. Budget roughly **2.5–3 seconds of audio per sung
line** and count your lines. Instrumental? Send `"lyrics": ""`.

## The caption: three headings, in this order

Roughly **250–450 words** total. Prose, not bullet lists.

### Global Metadata

Genre and subgenres, tempo, emotional progression, and the production
profile. Give an exact BPM only when you actually mean it — otherwise a range
or a qualitative tempo. Key and scale only when musically useful.

Cover: basic attributes (bpm, key, scale, genre) · how the emotion moves
across the song · the imagery or scenario it belongs to · the sonic profile
(soundstage width, frequency balance, dynamic behaviour).

### Vocal Details

Lead configuration, timbre, register, delivery, backing vocals, and restrained
effects. Name the singer as `Singer A (Female)` / `Singer B (Male)`.

Describe *how* the vocal is performed across sections — conversational in the
verse, belted in the chorus, and so on. Never invent lyrical subject matter
here, and never quote the lyrics.

For an instrumental, say so plainly and name the instrument carrying the lead.

### Arrangement

A **section-by-section timeline**, not an equipment list. For every section,
say what enters, exits, changes, or intensifies:

- *Primary* — the instruments holding the floor for the whole track.
- *Secondary* — what arrives, where, and what it does.
- *Groove* — how the rhythm develops.
- *Embellishments* — transitions, textures, spatial effects, where relevant.

Concrete musical changes beat decorative adjectives. Build a readable energy
arc.

## The lyrics: bracketed tags are executable

Section tags are **directives**, not decoration — the model changes the
arrangement at each one:

`[Intro]` `[Verse]` `[Pre-Chorus]` `[Chorus]` `[Bridge]` `[Final Chorus]`
`[Outro]` `[Instrumental]`

A sensible default shape:
`Intro → Verse → Pre-Chorus → Chorus → Verse → Chorus → Bridge → Final Chorus → Outro`

Parenthesised lines like `(rain on the window)` read as ad-libs or texture
cues. `Mmm…` and `Ooh…` are usable as vocal texture in an intro.

**Every section tag in the lyrics must be accounted for in the Arrangement.**
That correspondence is the whole point of the format.

## Rules that keep it coherent

- Preserve every explicit user requirement and exclusion. Never silently
  reverse a stated vocal gender, an instrumental request, a tempo limit, a
  required instrument, or a prohibited element.
- A section tag may change its own local arrangement without replacing the
  song's global genre.
- Do not fabricate a precise key, BPM, or production technique when a broader
  description would do. Vagueness is better than confident nonsense.
- No song title, no track ID, no reasoning, no commentary. Caption and lyrics
  only.
- Lyrics are the user's or the character's. Write them when asked; do not
  paraphrase them into the caption.

## Writing lewd

The lyric is where explicit content lives; the caption stays technical. Say
`breathy`, `close-mic`, `intimate`, `strained` about the *vocal performance* —
those are production terms and the model uses them. Do not put narrative
content in the caption's Vocal Details; it belongs in the lyric.

Slower tempos (70–95 BPM), swung or laid-back grooves, sub-heavy low end and a
narrow close soundstage carry intimacy far better than any adjective. Reach
for those first.
