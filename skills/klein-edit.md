# FLUX.2 Klein — image editing (instruction dialect)

**Model:** `flux-2-klein-9b-fp8` in the edit workflow (`07-klein-9b-edit`)

Editing is a different dialect from generation. The model already has the source
image; your prompt is an **instruction describing the change**, not a
description of the finished picture.

## Output format

One to three plain sentences, imperative voice, no line breaks, no tags, no
markdown, no negative prompt.

## The rule that matters most

**Describe only what changes.** Everything you mention gets reconsidered by the
model, so naming things you want left alone is the main cause of unwanted drift.
Do not re-describe the subject, the background, or the lighting unless you are
changing them.

Weak: `a woman in a red jacket standing in a forest, now at night`
Strong: `Change the time of day to night, lit only by moonlight.`

## Structure

1. **The verb** — Change, Replace, Remove, Add, Make, Turn.
2. **The precise target**, identified by position or appearance when the image
   could be ambiguous: "the mug on the left", "the man's jacket".
3. **The desired end state**, concretely.
4. **A preservation clause**, only when the edit risks collateral change —
   "keeping her pose and expression unchanged", "without altering the
   background".

## Patterns that work

- **Object swap** — `Replace the coffee mug with a glass teapot.`
- **Attribute change** — `Change the jacket colour to deep burgundy.`
- **Removal** — `Remove the parked car on the right and fill in the road behind it.`
- **Addition** — `Add a pair of round wire-frame glasses to the man's face.`
- **Relight** — `Relight the scene with warm low sunlight coming from the left, casting long shadows to the right.`
- **Restyle** — `Restyle the image as a pen and ink drawing with cross-hatched shading, keeping the composition identical.`
- **Text** — `Change the sign to read "CLOSED".` Keep replacement text short and
  in double quotes.
- **Background** — `Replace the background with an overcast beach, keeping the subject and her lighting unchanged.`

## Do not

- No negative prompt (CFG 1.0, distilled).
- No `(weight:1.2)`, no booru tags.
- Do not say "high quality, detailed, 8k" — it does nothing here and can nudge
  the whole image to re-render.
- Do not stack several unrelated edits into one prompt. If the user asks for
  many changes, order them into one coherent instruction, or state the single
  most important one first and keep the rest tightly scoped.

## Worked example

*User idea:* "make it winter and give her a scarf"

```
Change the season to winter, covering the ground and rooftops in fresh snow under flat overcast light. Add a chunky knitted red scarf around her neck, keeping her face, pose, and hair exactly as they are.
```
