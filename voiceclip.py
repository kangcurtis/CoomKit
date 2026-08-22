#!/usr/bin/env python3
"""Capture a voice reference for cloning, and check it before you trust it.

Voice cloning needs 3-15 seconds of somebody talking, and the sample decides
almost everything about the result — a clone inherits the reference's range,
its pace and whatever performance is in it. `voices.py` ships five references
for exactly that reason. This is the tool for the sixth: a character whose
voice you already know, from a clip you already have.

    ./voiceclip.py "https://…" 1:30 1:42 -o rin.wav
    ./voiceclip.py some-episode.mkv 4:10 4:22 -o rin.wav
    ./voiceclip.py --inspect rin.wav
    ./voiceclip.py "https://…" 1:30 1:42 -o rin.wav --install 42

**Still stdlib-only.** yt-dlp and ffmpeg are external *binaries*, not Python
packages — the same arrangement as ComfyUI, LM Studio and the `lms` subprocess
the VRAM broker shells out to. Nothing here imports anything that is not in
the standard library, and everything degrades with a sentence naming the tool
you are missing rather than a traceback. `--inspect` needs neither: a WAV is
read with `wave` and the arithmetic is ours.

**The point is the CHECK, not the download.** Grabbing audio is four lines of
yt-dlp. What actually goes wrong is silent: a reference below ~180 Hz can drop
an octave when it is cloned — measured across five references here, everything
at 186 Hz and up held its range while a 167 Hz alto came back at 78 Hz and
sounded male. Nothing in the pipeline warns you. You find out after a render,
and you blame the model. So this measures the pitch before the file is worth
keeping, along with the things that are just as silently fatal: a clip too
short to clone from, one long enough to be mostly silence, one that clipped on
the way in, or one that is mostly music.

The measurement is autocorrelation over the voiced windows, in pure Python.
Validated against the shipped references, whose pitches were recorded by hand
from a different tool: `--check-shipped` re-derives them, which is also the
only thing that has ever verified that `voices.PRESETS`'s `f0_hz` numbers
describe the audio actually on disk rather than what somebody typed.

**On what you point it at.** A clone of a real, identifiable person saying
things they never said is the one use of this that hurts somebody, and it is
worth being deliberate rather than incidental about not doing it. The bundled
references are public domain, CC BY, or synthesised — see `voices/CREDITS.md`,
and add a line to it if you ship anything you captured with this.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── the rules a reference has to pass ────────────────────────────────────
# Both ends are measured, not chosen. Below ~180 Hz cloning collapsed an
# octave (167 Hz in, 78 Hz out); 186 Hz and up held across five references,
# so the shipped floor is 185 and this warns under it rather than at it.
SAFE_F0_HZ = 185.0
# 3 seconds is the shortest OmniVoice reliably clones from; past 15 the extra
# audio buys nothing and any silence in it starts to cost.
MIN_SECONDS = 3.0
MAX_SECONDS = 15.0
# What the cloner wants: mono, 16-bit, 24 kHz. Matches the shipped .wav
# references so a captured clip and a bundled one are the same kind of thing.
OUT_RATE = 24000

# Search range for a speaking voice. Deliberately wide at the bottom: the
# whole point is to NOTICE a reference that is too low, so the estimator has
# to be able to report one rather than clamping into the safe band.
F0_MIN_HZ = 70.0
F0_MAX_HZ = 520.0


class MissingTool(RuntimeError):
    """An external binary is not installed. Carries the sentence to print."""


# ── time parsing ─────────────────────────────────────────────────────────
_COMPOSITE = re.compile(
    r"^(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?"
    r"\s*(?:(\d+(?:\.\d+)?)\s*s)?$")


def parse_timestamp(text: str) -> float:
    """Seconds from `90`, `1:30`, `01:15:30.5`, `1h2m3s`, `start`, `end`.

    Returns a float; `end` is inf. Raises ValueError on anything else rather
    than guessing, because a misparsed timestamp is a clip of the wrong part
    of the video and you will not notice until you listen to it.
    """
    raw = (text or "").strip().lower()
    if not raw or raw in ("start", "begin", "beginning"):
        return 0.0
    if raw in ("end", "inf", "finish", "max", "full"):
        return math.inf
    m = _COMPOSITE.match(raw)
    if m and any(m.groups()):
        h, mi, s = (float(g or 0) for g in m.groups())
        return h * 3600 + mi * 60 + s
    parts = re.sub(r"(secs?|seconds?|s)$", "", raw).strip().split(":")
    try:
        vals = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"could not read the time {text!r} — try 90, 1:30 "
                         f"or 01:15:30") from None
    if len(vals) == 1:
        total = vals[0]
    elif len(vals) == 2:
        total = vals[0] * 60 + vals[1]
    elif len(vals) == 3:
        total = vals[0] * 3600 + vals[1] * 60 + vals[2]
    else:
        raise ValueError(f"could not read the time {text!r}")
    if total < 0:
        raise ValueError(f"a timestamp cannot be negative: {text!r}")
    return total


def hhmmss(seconds: float) -> str:
    """Back to HH:MM:SS(.mmm), which is what yt-dlp and ffmpeg both take."""
    if seconds == math.inf:
        return "inf"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if float(s).is_integer():
        return f"{h:02d}:{m:02d}:{int(s):02d}"
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ── reading audio without a single dependency ────────────────────────────
def read_wav(path) -> tuple[array.array, int, int]:
    """(samples, rate, full_scale) as mono signed ints, from any PCM WAV.

    full_scale is what "as loud as this format goes" means for these samples,
    so the level checks are a real percentage rather than a guess from the
    values present — a quiet 32-bit clip and a loud 16-bit one are otherwise
    indistinguishable by inspection.

    `audioop` would do the width conversion and the stereo fold in C, and it
    is NOT used: it was removed in Python 3.13, and this project supports
    3.10+ — importing it would mean the tool works on the maintainer's box
    and raises ModuleNotFoundError on a current one.
    """
    with wave.open(str(path)) as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 2:
        data = array.array("h")
        data.frombytes(raw)
        full = 32767
    elif width == 1:
        # 8-bit WAV is UNSIGNED, unlike every other width.
        data = array.array("h", [(b - 128) << 8 for b in raw])
        full = 32767
    elif width == 4:
        data = array.array("i")
        data.frombytes(raw)
        full = 2 ** 31 - 1
    else:
        raise ValueError(f"{width * 8}-bit WAV is not something this reads")
    if sys.byteorder == "big":
        data.byteswap()          # WAV is little-endian on every platform
    if channels > 1:
        folded = array.array("i", [0]) * (len(data) // channels)
        for i in range(len(folded)):
            base = i * channels
            folded[i] = sum(data[base:base + channels]) // channels
        data = folded
    return data, rate, full


def _decimate(samples, rate: int, target: int = 11025):
    """Cheap anti-aliased downsample, so the autocorrelation is affordable.

    Pitch needs periodicity, not bandwidth. At 11 kHz a 500 Hz voice is still
    22 samples per period — enough to place the peak within a couple of
    percent — and the correlation loop gets four times cheaper, which is what
    makes this a fraction of a second in pure Python instead of several.
    """
    factor = max(1, int(rate // target))
    if factor == 1:
        return samples, rate
    # A box filter over the decimation window. Not elegant; it is the
    # difference between measuring pitch and measuring aliases.
    out = array.array("i", [0]) * (len(samples) // factor)
    for i in range(len(out)):
        chunk = samples[i * factor:(i + 1) * factor]
        out[i] = sum(chunk) // factor
    return out, rate // factor


def f0_picks(samples, rate: int, max_windows: int = 24) -> list[float]:
    """One fundamental per voiced window, unsorted. The measurement itself.

    Autocorrelation, normalised so the threshold means the same thing at any
    volume. Both octave errors are real and they pull in opposite directions,
    so the peak-picking is worth stating exactly:

    * Twice a period correlates almost as well as the period, so the global
      maximum sometimes lands on 2P and reports half the true pitch. Guarded
      by testing the submultiples of the winning lag and preferring one only
      when it correlates nearly as well (>= 0.9).
    * A strong second harmonic makes P/2 a real peak, so a loose "take the
      earliest good-enough lag" rule reports DOUBLE. Measured: that rule read
      all five shipped references high, by 5% on the natural readings and 22%
      on a synthesised one. Hence the strict threshold above, and taking the
      maximum rather than the earliest.
    """
    samples, rate = _decimate(samples, rate)
    lag_min = max(2, int(rate / F0_MAX_HZ))
    lag_max = int(rate / F0_MIN_HZ)
    win = lag_max * 2
    if len(samples) < win:
        return []

    # Spread the windows across the whole clip rather than reading the first
    # second: the start of a captured line is often the tail of the previous
    # one, or a breath.
    starts = list(range(0, len(samples) - win, win))
    if len(starts) > max_windows:
        step = len(starts) / max_windows
        starts = [starts[int(i * step)] for i in range(max_windows)]

    # A floor for "this window has speech in it", relative to the clip as a
    # whole, so a quiet recording is not read as entirely unvoiced.
    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return []
    floor = peak * 0.08

    picks = []
    for start in starts:
        w = samples[start:start + win]
        mean = sum(w) / len(w)
        w = [float(s) - mean for s in w]
        half = len(w) // 2
        energy0 = sum(v * v for v in w[:half])
        if energy0 <= 0 or math.sqrt(energy0 / half) < floor:
            continue
        best_r, corrs = 0.0, {}
        for lag in range(lag_min, lag_max + 1):
            dot = 0.0
            energy = 0.0
            for i in range(half):
                b = w[i + lag]
                dot += w[i] * b
                energy += b * b
            if energy <= 0:
                continue
            r = dot / math.sqrt(energy0 * energy)
            corrs[lag] = r
            if r > best_r:
                best_r = r
        if best_r < 0.35:            # unvoiced, or music, or noise
            continue
        best_lag = max(corrs, key=corrs.get)
        # Octave-down guard: if the winning lag is a multiple of the real
        # period, the period itself is still a strong peak. Only step down
        # when it nearly matches, or a second harmonic drags the estimate up.
        for div in (4, 3, 2):
            cand = best_lag // div
            if cand >= lag_min and corrs.get(cand, 0.0) >= best_r * 0.9:
                best_lag = cand
                break
        picks.append(rate / best_lag)
    return picks


def _pct(sorted_vals, fraction):
    return sorted_vals[min(len(sorted_vals) - 1,
                           int(len(sorted_vals) * fraction))]


def f0_stats(samples, rate: int) -> dict:
    """Median pitch AND the spread, because one number can lie.

    Measured on the shipped `onee-san` reference: successive two-second
    slices read 255, 286, 293, 226, 364, 235 and 387 Hz. That is not an
    estimator wobbling — the voice really does range that far, and any single
    figure describes none of it. A reference whose median clears the floor
    while a third of it sits underneath is exactly the clip that clones
    unpredictably, so the spread is reported and the verdict looks at it.
    """
    picks = f0_picks(samples, rate)
    if not picks:
        return {"median": None, "low": None, "high": None,
                "windows": 0, "below_floor_pct": 0.0}
    picks.sort()
    # The median survives a few bad windows; PERCENTILES do not, and an
    # octave error is exactly the outlier that ruins them — it lands at half
    # or a quarter, far outside any real speaking range. Measured on the
    # shipped `brat` reference, a 399 Hz voice: the raw spread read 109-462
    # and claimed a quarter of it sat under the floor, which is arithmetic
    # about mistakes rather than about the voice. Anything more than two
    # thirds of an octave from the median is one of those, so it is dropped
    # before the spread is computed — and the median is taken again from
    # what is left.
    rough = picks[len(picks) // 2]
    picks = [p for p in picks if rough / 1.6 <= p <= rough * 1.6] or picks
    below = sum(1 for p in picks if p < SAFE_F0_HZ)
    return {
        "median": picks[len(picks) // 2],
        "low": _pct(picks, 0.1),
        "high": _pct(picks, 0.9),
        "windows": len(picks),
        "below_floor_pct": 100.0 * below / len(picks),
    }


def estimate_f0(samples, rate: int):
    """The single headline number: median over voiced windows, or None."""
    return f0_stats(samples, rate)["median"]


def inspect_wav(path) -> dict:
    """Everything that decides whether this clip is worth cloning from."""
    samples, rate, full = read_wav(path)
    seconds = len(samples) / rate if rate else 0.0
    peak = max((abs(s) for s in samples), default=0)
    rms = (math.sqrt(sum(float(s) * s for s in samples) / len(samples))
           if samples else 0.0)
    st = f0_stats(samples, rate)
    return {
        "file": str(path),
        "seconds": round(seconds, 2),
        "rate": rate,
        "f0_hz": round(st["median"], 1) if st["median"] else None,
        "f0_low": round(st["low"], 1) if st["low"] else None,
        "f0_high": round(st["high"], 1) if st["high"] else None,
        "below_floor_pct": round(st["below_floor_pct"], 1),
        "peak_pct": round(100.0 * peak / full, 1),
        "rms_pct": round(100.0 * rms / full, 1),
    }


def verdict(report: dict) -> tuple[bool, list[str]]:
    """(usable, notes). Every note names what to do about it."""
    notes, ok = [], True
    secs, f0 = report["seconds"], report["f0_hz"]
    if secs < MIN_SECONDS:
        ok = False
        notes.append(f"{secs:.1f}s is too short — cloning wants at least "
                     f"{MIN_SECONDS:.0f}s of speech. Take a longer line.")
    elif secs > MAX_SECONDS:
        notes.append(f"{secs:.1f}s is longer than the {MAX_SECONDS:.0f}s that "
                     f"buys anything; the extra is mostly a chance to include "
                     f"silence. Trim it.")
    if f0 is None:
        ok = False
        notes.append("no speaking pitch found at all — this is probably "
                     "music, noise, or silence rather than a voice.")
    elif f0 < SAFE_F0_HZ:
        ok = False
        notes.append(f"{f0:.0f} Hz is below the {SAFE_F0_HZ:.0f} Hz floor. "
                     f"Measured here, a reference this low can come back an "
                     f"octave down and sound male. Pick a higher line — and "
                     f"do not pick by ear for 'warm', that is the same "
                     f"mistake.")
    else:
        # The median can clear the floor while a good part of the clip sits
        # under it, which is the reference that clones differently every time.
        under = report.get("below_floor_pct") or 0.0
        if under >= 25.0:
            notes.append(f"{under:.0f}% of the voiced audio is under "
                         f"{SAFE_F0_HZ:.0f} Hz even though the median is not. "
                         f"A line with less range in it clones more "
                         f"predictably.")
    if report["peak_pct"] >= 99.5:
        notes.append("it clips — the loudest part is at the ceiling, so some "
                     "of the waveform is flat. Recapture a little quieter.")
    if report["rms_pct"] < 1.0:
        notes.append("it is very quiet, which usually means the clip is "
                     "mostly silence with a word in it.")
    if ok and not notes:
        notes.append("good reference: long enough, in range, cleanly levelled.")
    return ok, notes


# ── capture ──────────────────────────────────────────────────────────────
def _need(tool: str) -> str:
    found = shutil.which(tool)
    if not found:
        raise MissingTool(
            f"{tool} is not installed, and this needs it. "
            + {"yt-dlp": "Install it from your package manager or "
                         "https://github.com/yt-dlp/yt-dlp — or download the "
                         "clip yourself and pass the file instead of a URL.",
               "ffmpeg": "Install it from your package manager; every "
                         "distribution has it."}.get(tool, ""))
    return found


def _run(cmd: list[str], quiet: bool) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.PIPE if quiet else None)
    if proc.returncode != 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        tail = tail.splitlines()[-1] if tail else f"exit {proc.returncode}"
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {tail}")


def fetch_section(url: str, start: float, end: float, dest: Path,
                  quiet: bool = False) -> Path:
    """Pull just the requested seconds out of a URL, as an audio file.

    `--download-sections` means only that span is fetched, so a twelve-second
    reference does not cost a two-hour download.
    """
    yt = _need("yt-dlp")
    section = f"*{hhmmss(start)}-{hhmmss(end)}"
    template = str(dest.with_suffix("")) + ".%(ext)s"
    cmd = [yt, "--download-sections", section, "--force-keyframes-at-cuts",
           "--extract-audio", "--audio-format", "wav", "--audio-quality", "0",
           "--no-playlist", "-o", template]
    if quiet:
        cmd.append("--quiet")
    cmd.append(url)
    _run(cmd, quiet)
    got = sorted(dest.parent.glob(dest.stem + ".*"))
    if not got:
        raise RuntimeError("yt-dlp reported success but wrote no file")
    return got[0]


def normalise(src: Path, dest: Path, start: float = 0.0,
              end: float = math.inf, quiet: bool = False) -> Path:
    """Trim, fold to mono, resample: the shape the cloner wants.

    `loudnorm` is deliberately NOT used. It is the obvious next thing to
    reach for and it works against you here: it lifts the quiet parts, which
    on a speech clip means room tone and breath come up with the voice, and
    the clone inherits that as texture.
    """
    ff = _need("ffmpeg")
    cmd = [ff, "-y", "-loglevel", "error"]
    # -ss BEFORE -i is the fast seek: ffmpeg jumps rather than decoding its
    # way there, which on a long source is the difference between a second
    # and a minute.
    if start:
        cmd += ["-ss", hhmmss(start)]
    cmd += ["-i", str(src)]
    if end != math.inf:
        cmd += ["-t", hhmmss(max(0.0, end - start))]
    cmd += ["-ac", "1", "-ar", str(OUT_RATE), "-sample_fmt", "s16", str(dest)]
    _run(cmd, quiet)
    return dest


def capture(source: str, start: float, end: float, dest: Path,
            quiet: bool = False) -> Path:
    """URL or local file in, a checked-shape WAV out."""
    dest = Path(dest).with_suffix(".wav")
    dest.parent.mkdir(parents=True, exist_ok=True)
    is_url = "://" in source
    with tempfile.TemporaryDirectory() as tmp:
        if is_url:
            raw = fetch_section(url=source, start=start, end=end,
                                dest=Path(tmp) / "grab", quiet=quiet)
            # yt-dlp already cut the span, so ffmpeg only reshapes it.
            return normalise(raw, dest, quiet=quiet)
        src = Path(source).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"no such file: {src}")
        return normalise(src, dest, start=start, end=end, quiet=quiet)


def install(wav: Path, character_id: int, server: str,
            ref_text: str = "") -> dict:
    """Set the clip as a character's own voice sample on a running CoomKit.

    Goes through the ordinary upload route, so the file lands in data/assets
    and `data.voice.sample` is set the same way the character editor sets it.
    A character's own sample always beats a shipped preset.
    """
    import base64
    import urllib.request

    body = {"filename": wav.name, "kind": "voice", "owner_id": int(character_id),
            "b64": base64.b64encode(wav.read_bytes()).decode()}
    if ref_text:
        body["ref_text"] = ref_text
    req = urllib.request.Request(
        server.rstrip("/") + "/api/assets/upload",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


# ── cli ──────────────────────────────────────────────────────────────────
def _print_report(report: dict, label: str = "") -> bool:
    ok, notes = verdict(report)
    f0 = report["f0_hz"]
    head = label or Path(report["file"]).name
    pitch = f"{f0:.0f} Hz" if f0 else "not found"
    lo, hi = report.get("f0_low"), report.get("f0_high")
    if f0 and lo and hi:
        pitch += f" (mostly {lo:.0f}-{hi:.0f})"
    print(f"\n  {head}")
    print(f"    {report['seconds']:.1f}s · {report['rate']} Hz · pitch {pitch}")
    print(f"    peak {report['peak_pct']:.0f}% of full scale, "
          f"average {report['rms_pct']:.0f}%")
    for note in notes:
        print(f"    {'·' if ok else '!'} {note}")
    return ok


def _check_shipped() -> int:
    """Re-derive the shipped references' pitches from the audio itself.

    `voices.PRESETS` carries an `f0_hz` per voice and a test asserts they are
    all above the floor — but that only ever checked the NUMBER, which was
    typed in by hand from another tool. This is the part that checks the
    files.
    """
    sys.path.insert(0, str(ROOT))
    import voices                                    # noqa: E402

    worst = 0
    for name, spec in voices.PRESETS.items():
        path = voices.VOICE_DIR / spec["file"]
        if not path.exists():
            print(f"  {name}: missing on disk")
            continue
        if path.suffix != ".wav":
            # FLAC needs a decoder, and `wave` is not one.
            if not shutil.which("ffmpeg"):
                print(f"  {name}: {path.suffix} needs ffmpeg to read; skipped")
                continue
            with tempfile.TemporaryDirectory() as tmp:
                conv = normalise(path, Path(tmp) / "x.wav", quiet=True)
                report = inspect_wav(conv)
        else:
            report = inspect_wav(path)
        measured, declared = report["f0_hz"], spec["f0_hz"]
        drift = abs(measured - declared) / declared * 100 if measured else 100
        flag = "" if drift < 20 else "   <-- does not match the declared value"
        span = ""
        if report.get("f0_low") and report.get("f0_high"):
            span = f" · mostly {report['f0_low']:.0f}-{report['f0_high']:.0f}"
            under = report.get("below_floor_pct") or 0.0
            if under >= 25:
                span += f" · {under:.0f}% under the floor"
        print(f"  {name:16s} declared {declared:4d} Hz · "
              f"measured {measured or 0:6.1f} Hz{span}{flag}")
        worst = max(worst, 1 if flag else 0)
    return worst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Capture and check a voice reference for cloning.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  ./voiceclip.py "https://…/watch?v=…" 1:30 1:42 -o voices/rin.wav
  ./voiceclip.py episode.mkv 4:10 4:22 -o rin.wav
  ./voiceclip.py --inspect voices/rin.wav
  ./voiceclip.py --check-shipped
  ./voiceclip.py "https://…" 1:30 1:42 -o rin.wav --install 42
""")
    ap.add_argument("source", nargs="?", help="a URL or a local media file")
    ap.add_argument("start", nargs="?", default="0", help="e.g. 1:30")
    ap.add_argument("end", nargs="?", default="end", help="e.g. 1:42")
    ap.add_argument("-o", "--out", help="where to write the .wav")
    ap.add_argument("--inspect", metavar="WAV",
                    help="measure a WAV that already exists and stop")
    ap.add_argument("--check-shipped", action="store_true",
                    help="re-derive the bundled references' pitches")
    ap.add_argument("--install", type=int, metavar="CHARACTER_ID",
                    help="set the clip as that character's voice sample")
    ap.add_argument("--server", default="http://127.0.0.1:3939",
                    help="the running CoomKit, for --install")
    ap.add_argument("--ref-text", default="",
                    help="what is said in the clip, if you want it recorded")
    ap.add_argument("--keep", action="store_true",
                    help="keep the file even if it fails the checks")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.check_shipped:
        return _check_shipped()

    if args.inspect:
        path = Path(args.inspect).expanduser()
        if not path.exists():
            print(f"no such file: {path}", file=sys.stderr)
            return 1
        return 0 if _print_report(inspect_wav(path)) else 1

    if not args.source:
        ap.print_help()
        return 2
    if not args.out:
        print("give me -o where to write it", file=sys.stderr)
        return 2

    try:
        start = parse_timestamp(args.start)
        end = parse_timestamp(args.end)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if end <= start:
        print(f"the end ({hhmmss(end)}) is not after the start "
              f"({hhmmss(start)})", file=sys.stderr)
        return 2

    out = Path(args.out).expanduser()
    try:
        wav = capture(args.source, start, end, out, quiet=args.quiet)
    except MissingTool as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (RuntimeError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    ok = _print_report(inspect_wav(wav))
    if not ok and not args.keep:
        wav.unlink(missing_ok=True)
        print("\n  not kept — pass --keep to write it anyway.")
        return 1

    if args.install is not None:
        try:
            res = install(wav, args.install, args.server, args.ref_text)
        except Exception as exc:                      # noqa: BLE001
            print(f"\n  saved, but could not install it: {exc}",
                  file=sys.stderr)
            return 1
        if res.get("error"):
            print(f"\n  saved, but the server refused it: {res['error']}",
                  file=sys.stderr)
            return 1
        print(f"\n  installed as character #{args.install}'s voice "
              f"({res.get('file')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
