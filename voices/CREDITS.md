# Bundled voice references

These are the reference clips CoomKit clones from when a character has no
voice sample of her own. A character's own upload always wins over any of
these, and "describe it instead" skips cloning entirely — OmniVoice and
IndexTTS-2 can both synthesise a voice from words.

## The archetypes — synthesised here, no licence at all

| File | Voice | Rendered at | Source |
|---|---|---|---|
| `brat.flac` | high, sharp, unimpressed | speed 0.85 | **Synthesised** with OmniVoice Voice Design |
| `onee-san.flac` | warm, unhurried, a little older | speed 0.82 | **Synthesised** with OmniVoice Voice Design |
| `mommy.flac` | low, slow, indulgent | speed 0.82 | **Synthesised** with OmniVoice Voice Design |

No audiobook narrator has ever read in these registers, so these were not
found — they were made, on this machine, with the same OmniVoice model that
ships in the workflows. **No third-party audio is involved and no licence
applies.** The scripts were written to carry the register, because a clone
inherits the performance in its reference, not just the timbre.

## The natural readings — real people, permissive licences

| File | Voice | Median F0 | Source | Licence |
|---|---|---|---|---|
| `female-bright.wav` | clear, higher | 210 Hz | **LJ Speech** — Linda Johnson, recorded for [LibriVox](https://librivox.org/). Two consecutive clips joined. | **Public domain** |
| `female-warm.wav` | lower, warmer | 194 Hz | **LibriTTS-R** (speaker 7976, `dev.clean`), derived from LibriVox recordings. | **CC BY 4.0** |

`female-warm.wav` is CC BY 4.0 and requires attribution when redistributed:

> LibriTTS-R: A Restored Multi-Speaker Text-to-Speech Corpus.
> Y. Koizumi et al., 2023. Derived from LibriTTS (H. Zen et al., 2019),
> itself derived from public-domain LibriVox audiobook recordings.
> Licensed CC BY 4.0.

`female-bright.wav` is public domain and carries no restriction.

## Why nothing here is lower-pitched

A warmer voice is tempting to chase down the pitch scale, and it breaks.
Cloning was measured across five references on this stack: everything at
186 Hz and above held its range, while a 167 Hz alto reference **collapsed an
octave to 78 Hz** and came out unmistakably male. The 165–180 Hz band is also
exactly where pitch stops telling you a speaker's gender.

So depth comes from the *words*, from the speaking-speed dropdown, or from
IndexTTS-2's emotion vector — never from a lower reference.

## Replacing them

Drop any 3–15 second clip of somebody talking into a character's voice slot
and it takes priority over all of these. Longer than 15 seconds gains nothing;
under 3 the clone gets unstable. Listen to it first — the preview player is
right there, and a clone sounds exactly like whatever you feed it.
