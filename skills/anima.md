# Anima — anime / illustration (booru-tag dialect)

**Models:** `anima-base-v1.0`, `anima-aesthetic-v1.1`
**Text encoder:** Qwen3-0.6B base (type `stable_diffusion`) · **VAE:** qwen_image_vae
**Sampling:** 30 steps / CFG 4.0, `euler` + `simple` (or 8 / 1.0 with `anima-turbo-lora-v0.2`)

Anima is trained on **Danbooru tags**. It expects comma-separated tags, not
sentences. Prose degrades it badly — a flowing description gets averaged into
mush, while a well-ordered tag list is sharp and controllable.

## Output format

A single line of comma-separated tags. No sentences, no line breaks, no
markdown. Tags use **spaces, not underscores** (`blue eyes`, not `blue_eyes`) —
the encoder handles both, but spaces are the workflow convention here.

## Tag order (this order matters — earlier tags carry more weight)

1. **Quality preamble** — always begin with: `masterpiece, best quality, score_7, safe`
   Change `safe` to `sensitive`, `questionable`, or `explicit` to match the
   requested content rating. Keep the rest verbatim.
2. **Artist tags** — if the user supplied any, place them here, before the
   subject. Artist tags are the strongest style lever Anima has; a single one
   reshapes the entire image. Never invent artist names — use only what the user
   provided.
3. **Copyright / series** — e.g. `vocaloid`, `original`.
4. **Character name** — e.g. `hatsune miku`. Include when the user names a known
   character; the model has strong per-character priors that carry the correct
   hair, eyes, and outfit for free.
5. **Subject count and framing** — `1girl`, `2boys`, `solo`, `multiple girls`.
   Nearly every good Anima prompt has one of these near the front.
6. **Body and face** — hair colour and length, eye colour, expression, notable
   features. `long hair, twintails, aqua hair, aqua eyes, smile`.
7. **Clothing** — garment by garment, outermost first. `detached sleeves,
   pleated skirt, thighhighs, necktie`.
8. **Pose and action** — `sitting, arms up, looking at viewer, head tilt`.
9. **Composition and camera** — `cowboy shot`, `upper body`, `from above`,
   `dutch angle`, `close-up`, `wide shot`, `from side`, `profile`.
10. **Setting and background** — `rooftop, city lights, night sky, cherry
    blossoms, indoors, simple background, white background`.
11. **Lighting and effects** — `cinematic lighting, backlighting, rim light,
    god rays, lens flare, depth of field, bokeh, glowing`.
12. **Rendering suffix** (optional) — `highly detailed, detailed background,
    absurdres`.

Omit any category the idea does not need. A tight 25-tag prompt beats a padded
60-tag one; aim for **25–45 tags** unless the user asks for more.

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

## Weighting

Use SDXL-style parenthetical weights, sparingly — one to three per prompt:

```
(aqua hair:1.2), (cinematic lighting:1.15), (extra arms:0.8)
```

Range 0.8–1.4. Above 1.5 produces artifacts and colour burn. Weight the thing
the user cares most about, not every tag.

## Booru vocabulary to prefer

Use real Danbooru tags — the model was trained on the tag, not on a synonym:

| Instead of | Use |
|---|---|
| "waist-up shot" | `upper body` |
| "full body shot" | `full body` |
| "mid-thigh framing" | `cowboy shot` |
| "looking at camera" | `looking at viewer` |
| "seen from below" | `from below` |
| "over the shoulder" | `from behind` |
| "glowing edges" | `rim lighting` / `backlighting` |
| "blurry background" | `depth of field`, `blurry background`, `bokeh` |
| "very high resolution" | `absurdres` |

## Negative prompt

When asked for one, use this base and extend it with the specific failure the
user wants to avoid:

```
worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia
```

Common additions: `bad hands, bad anatomy, extra digits, fewer digits, missing
fingers, watermark, signature, text, censored, mutated hands`.

## Dynamic-prompt syntax

The positive box is driven by `ImpactWildcardProcessor`, so these are live:

- `{a|b|c}` — pick one at random each run
- `__filename__` — draw a line from a wildcard file (e.g. `__artists__`)

Only emit this syntax when the user explicitly asks for variation or randomness.

## Worked example

*User idea:* "girl on a rooftop at night, city below, dramatic"

```
masterpiece, best quality, score_7, safe, 1girl, solo, very long hair, twintails, aqua hair, aqua eyes, sailor collar, necktie, pleated skirt, thighhighs, standing, looking at viewer, wind lift, cowboy shot, from below, rooftop, railing, night, cityscape, city lights, skyline, (cinematic lighting:1.2), backlighting, rim light, bokeh, depth of field, glowing, highly detailed, detailed background, absurdres
```
