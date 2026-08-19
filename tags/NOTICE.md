# Tag data in this folder

## `tagsets.json` — ours

Fifteen curated tag sets (framing, lighting, hair, pose, and so on) with notes
about which ones fight each other. Original work, covered by this repository's
licence.

## `danbooru.csv.gz` — third-party tag names and post counts

A snapshot of the Danbooru tag autocomplete database, in the CSV export format
that ships with
[ComfyUI-Custom-Scripts](https://github.com/pythongosssss/ComfyUI-Custom-Scripts):

```
name,category,post_count,"comma,separated,aliases"
```

150,098 rows / 140,782 with usable counts. Categories are Danbooru's own:
0 general, 1 artist, 3 copyright, 4 character, 5 meta.

**What this is:** tag *names* and how many posts carry them. No images, no
post content, no user data — a vocabulary list with frequencies. It is here
because the booru-lineage image models were trained on these exact strings, so
without it the artist blender has 59,201 fewer names to roll from and the tag
search has nothing to search.

**Why it is bundled at all.** An earlier version of this tooling declined to
redistribute it on the grounds that everyone running it already has a copy,
since tag autocomplete is near-universal in a ComfyUI install. That is still
true, and CoomKit still prefers your copy when it can find one — see
`tags.find_db()`. The bundled snapshot exists so that a first run on a machine
with no ComfyUI, or with a ComfyUI that never installed autocomplete, is not a
degraded one. Shipping it was a deliberate reversal of that call, not an
oversight.

**Freshness.** It is a snapshot and will drift. Point `tags_db` in
`data/config.json` at a newer export, or just install tag autocomplete in
ComfyUI, and yours wins automatically.

Danbooru tag data is compiled by the Danbooru community. It is included here as
factual reference data, not as a creative work, and no ownership over it is
claimed by this project.
