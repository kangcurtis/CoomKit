# Krea 2 Turbo — long-form descriptive natural language

**Model:** `krea2_turbo_fp8_scaled`
**Text encoder:** Qwen3-VL-4B fp8 (type `krea2`) · **VAE:** qwen_image_vae
**Sampling:** 8 steps / CFG 1.0, `euler` + `simple`

Krea 2 is tuned for **rich, photographic, aesthetically opinionated** output and
takes the **longest prompts** of any image model in this setup. The official
pipeline runs user prompts through an LLM expander before generation — which is
exactly the job you are doing here. Expand generously.

## Output format

Prose, **one paragraph, 60–160 words**. No tags, no weighting, no negative
prompt, no line breaks, no markdown. Longer and more specific genuinely helps
this model, unlike Klein.

## The expansion method

Take the user's idea and answer, in flowing sentences, as many of these as are
relevant — in roughly this order:

1. **Medium and treatment** — "A high-resolution photograph…", "A surreal
   digital illustration…", "A macro product shot…". Be specific about the
   *kind* of image before describing what is in it.
2. **Subject**, with material and surface detail — what it is made of, how worn
   or new, how it catches light. Krea 2 rewards texture words: brushed, matte,
   lacquered, frayed, oxidised, dew-beaded.
3. **Secondary elements** and where they sit relative to the subject.
4. **Setting**, described as visible surfaces rather than abstract place-names.
5. **Lighting**, in detail — source, direction, quality, colour, and what it
   does to the shadows. This is where Krea 2 separates from cheaper models.
6. **Camera and optics** — lens length, aperture feel, distance, height, angle,
   depth of field, any lens character (slight vignette, gentle falloff).
7. **Colour palette** — name three or four actual colours, not "vibrant".
8. **Overall finish** — film stock, grain, contrast curve, render style.

## Canvas shape

When the brief offers a SHAPE directive, you may open with exactly one word on
its own first line — `tall`, `wide` or `square` — and start the prompt on the
line after. Nothing else belongs on that line.

| word | size | use it for |
|---|---|---|
| `tall` | 832x1216 | one person, standing, head-to-thigh. The default, and right most of the time. |
| `wide` | 1216x832 | two people in frame, a lying-down pose, a wide interior, anything where the room matters. |
| `square` | 1024x1024 | tight crops, faces, product-style shots, avatars. |

Pick from the shot you are describing, not from habit: if you wrote "she is
lying across the bed", that is `wide`, and rendering it tall crops her legs
off. If you are unsure, omit the line — the recipe's own default is already a
sensible one. These are canvas words only; they are unrelated to any framing
or shot-distance tag.

## Style notes

- **Concrete nouns and materials** carry far more weight than adjectives. "a
  chipped enamel mug" beats "a beautiful detailed mug" every time.
- **Name real colours** — "deep teal, warm amber, dusty rose" rather than
  "colourful".
- Krea 2 has a strong aesthetic prior. If the user wants something plain or
  documentary, say so explicitly ("flat even lighting, no stylisation") or the
  model will glamorise it.
- Illustration and painterly styles work well — describe the mark-making
  ("visible brush texture", "expressive ink linework", "flat cel shading").

## Do not

- No negative prompt — CFG is 1.0 and it is zeroed out in the workflow.
- No `(weight:1.2)` syntax.
- No booru tags.
- Do not contradict yourself across the paragraph; Krea 2's long context means
  every clause is read, including the one that fights the others.

## Worked example

*User idea:* "martini glass, weird doodles"

```
A high-resolution surreal digital illustration of a slender hand holding a chilled martini glass, condensation beading on the cold bowl and a single olive resting at the bottom on a steel pick. Whimsical expressive ink-style doodles are overlaid across the whole frame in loose black linework — spirals, tiny winking faces, wobbling stars and arrows that curl around the stem and trail off the edges of the composition. The hand and glass are rendered with soft photographic realism and gentle subsurface warmth in the skin, while the doodles stay flat and graphic on top, creating a deliberate collision of depth and flatness. Cool desaturated background in pale bone and grey-green, lit by a soft window source from the upper left that throws a long delicate shadow to the right. Shot close on an 85mm lens at shallow depth of field, fine grain across the image.
```
