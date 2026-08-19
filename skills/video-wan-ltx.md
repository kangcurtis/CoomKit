# Wan 2.2 / LTX 2.3 — video prompt dialect

**Wan 2.2 A14B** (T2V + I2V, 4 steps / CFG 1.0) — two-expert MoE, HIGH-noise
expert for early steps, LOW-noise for late.
**LTX 2.3 22B dev fp8** (T2V, I2V, first-last-frame, image+audio-to-video).

Both are **natural-language** video models. They need a prompt that describes
*change over time*, not a still frame.

## Output format

Prose, **one paragraph, 40–110 words**. No tags, no weighting, no shot-list
markup, no line breaks. Unlike MiniMax H3, these models do **not** use labelled
sections or `[Shot N]` markers — plain descriptive prose only.

## Structure

1. **Opening frame** — style, subject, setting, framing, as a still would be
   described.
2. **Motion of the subject** — what moves, in what direction, how fast. Use
   concrete verbs: strides, tilts, unfurls, collapses, drifts.
3. **Camera motion** — one clear move, named plainly: slow push in, pan right,
   tracking shot following the subject, static locked-off shot. **One camera
   move per clip.** Stacking moves produces incoherent motion at 4 steps.
4. **Environmental motion** — rain, smoke, cloth, hair, water, crowd, dust.
   This is what sells a clip as video rather than a photo with drift.
5. **Lighting**, including any change in it over the shot.

## Rules that matter for these models

- **One action, one camera move, one continuous shot.** These are ~5-second
  clips. A prompt describing three beats produces mush.
- **No cuts.** Do not write "then it cuts to" — neither model handles cuts in a
  single generation. That is what H3 is for.
- **Describe motion, not intent.** "she turns her head to the left" works;
  "she realises something is wrong" does not.
- **Loops and continuity**: if the user wants a seamless loop, say the end state
  matches the opening framing.
- For **I2V**, the first frame is supplied by the image — describe what *happens
  next*, not what the image already shows. Establish continuity in one clause
  ("the same woman at the window"), then spend the rest on motion.

## Do not

- No negative prompts (CFG 1.0 on both).
- No booru tags, no weighting syntax.
- No audio description — neither model generates audio. (H3 does.)

## Worked example

*User idea (T2V):* "wolf in snow"

```
A cinematic wide shot of a grey wolf standing in deep snow at the edge of a pine forest, breath steaming in the cold air. The wolf lowers its head, takes three slow deliberate steps forward through the drift, then lifts its gaze toward the camera as its ears rotate. Loose snow lifts and drifts across the ground in a light wind, and flakes fall steadily through the frame. The camera pushes in slowly and steadily toward the wolf's face. Flat overcast winter light, cool blue-grey throughout.
```
