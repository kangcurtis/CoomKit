#!/usr/bin/env python3
"""The voice-reference capture tool: time parsing, pitch, and the verdict.

Offline and free, and it needs neither yt-dlp nor ffmpeg: every waveform here
is synthesised in memory with `wave` and the standard library, so the parts
that are OURS are tested without the parts that are somebody else's.

The pitch estimator is the reason this file exists. `voices.PRESETS` carries
an `f0_hz` for every shipped reference and `test_studio` asserts they all sit
above the 185 Hz floor — but that only ever checked the NUMBER, which was
typed in by hand from another tool. Nothing has ever checked that the numbers
describe the audio on disk. Section 3 does, for the two references `wave` can
read without a decoder.

Ground truth first, though: a synthesised harmonic stack has an F0 that is
known exactly, which is the only way to tell an estimator that works from one
that agrees with the last estimator.
"""

import array
import math
import tempfile
import wave
from pathlib import Path

import _bootstrap  # noqa: F401  — repo root on sys.path

import voiceclip

FAILED = []


def check(label, ok, extra=""):
    print(("  ok   " if ok else "  FAIL ") + label
          + (f"  [{extra}]" if extra and not ok else ""))
    if not ok:
        FAILED.append(label)


def tone(path, f0, seconds=4.0, rate=24000, harmonics=(1.0, 0.5, 0.33, 0.25),
         amplitude=0.35):
    """A voice-shaped harmonic stack at a known fundamental.

    Not a sine: a pure tone is the easy case and real speech never is. A
    decaying harmonic series is what a vocal fold actually produces, and it is
    what makes the octave errors possible in the first place.
    """
    frames = array.array("h")
    for n in range(int(seconds * rate)):
        t = n / rate
        v = sum(a * math.sin(2 * math.pi * f0 * (k + 1) * t)
                for k, a in enumerate(harmonics))
        v /= sum(harmonics)
        frames.append(int(max(-1.0, min(1.0, v * amplitude)) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames.tobytes())
    return path


tmp = Path(tempfile.mkdtemp())

# ── 1. time parsing ──────────────────────────────────────────────────────
print("reading timestamps")
cases = [("0", 0.0), ("90", 90.0), ("90s", 90.0), ("1:30", 90.0),
         ("01:30", 90.0), ("1:15:30", 4530.0), ("1h2m3s", 3723.0),
         ("2m 10s", 130.0), ("start", 0.0), ("00:00:01.500", 1.5)]
for text, want in cases:
    got = voiceclip.parse_timestamp(text)
    check(f"{text!r} -> {want}", abs(got - want) < 0.001, got)
check("'end' is open-ended", voiceclip.parse_timestamp("end") == math.inf)
for bad in ("banana", "1:2:3:4", "-5"):
    try:
        voiceclip.parse_timestamp(bad)
        check(f"{bad!r} is refused", False, "it parsed")
    except ValueError:
        check(f"{bad!r} is refused rather than guessed at", True)
check("seconds go back to a clock", voiceclip.hhmmss(4530) == "01:15:30")
check("fractions survive the round trip",
      voiceclip.hhmmss(90.5) == "00:01:30.500")

# ── 2. pitch, against a known fundamental ────────────────────────────────
print("\nmeasuring pitch (ground truth: synthesised)")
for f0 in (110, 150, 185, 200, 250, 330, 440):
    path = tone(tmp / f"t{f0}.wav", f0)
    got = voiceclip.estimate_f0(*voiceclip.read_wav(path)[:2])
    off = abs(got - f0) / f0 * 100 if got else 100
    check(f"{f0} Hz reads back as {got and round(got, 1)} ({off:.1f}% off)",
          got is not None and off < 5, got)

# The two ways this goes wrong, and they pull in opposite directions.
path = tone(tmp / "harm.wav", 200, harmonics=(0.3, 1.0, 0.5, 0.25))
got = voiceclip.estimate_f0(*voiceclip.read_wav(path)[:2])
check("a dominant SECOND harmonic does not double the reading",
      got is not None and abs(got - 200) / 200 < 0.06, got)

path = tone(tmp / "missing.wav", 160, harmonics=(0.0, 1.0, 0.6, 0.4))
got = voiceclip.estimate_f0(*voiceclip.read_wav(path)[:2])
check("a missing fundamental is still found from its harmonics",
      got is not None and abs(got - 160) / 160 < 0.06, got)

# ── 2b. the spread, and why the percentiles need defending ───────────────
# A single number lies about an expressive voice: the shipped `onee-san`
# reference reads 226-387 Hz across successive two-second slices. So the
# spread is reported — but percentiles are ruined by exactly the outlier the
# median shrugs off, and an octave error IS that outlier.
print("\nthe spread")
samples, rate, _ = voiceclip.read_wav(tone(tmp / "steady.wav", 220))
st = voiceclip.f0_stats(samples, rate)
check("a steady tone has a tight spread",
      st["high"] - st["low"] < 20, st)
check("...centred on the truth", abs(st["median"] - 220) < 10, st)
check("the voiced windows are counted", st["windows"] > 4, st)
check("nothing is under the floor in a 220 Hz tone",
      st["below_floor_pct"] == 0.0, st)

# Half of an octave-error's picks would drag a percentile to nonsense while
# leaving the median alone. Proven by construction: splice a tone with its
# own half-frequency, which is what a mis-tracked window looks like.
half = tone(tmp / "halfsplice.wav", 400, seconds=3.0)
lo_samples, lo_rate, _ = voiceclip.read_wav(tone(tmp / "lo.wav", 200, seconds=1.0))
hi_samples, _, _ = voiceclip.read_wav(half)
spliced = hi_samples[:]
spliced.extend(lo_samples)
st = voiceclip.f0_stats(spliced, lo_rate)
check("a minority of octave-off windows does not move the reported range",
      st["low"] > 300, st)

# Silence has no pitch, and saying "70 Hz" about it would be worse than
# saying nothing — the verdict turns that into "this is not a voice".
with wave.open(str(tmp / "quiet.wav"), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes(array.array("h", [0] * 24000 * 4).tobytes())
check("silence reports no pitch at all",
      voiceclip.estimate_f0(*voiceclip.read_wav(tmp / "quiet.wav")[:2]) is None)

# ── 3. the shipped references really are above the floor ─────────────────
# The gap this closes: until now the >=185 Hz claim was a claim about a
# hand-typed constant. FLAC needs a decoder, so only the .wav readings are
# checkable without ffmpeg — which is exactly the two natural ones.
print("\nthe shipped references, measured rather than declared")
import voices  # noqa: E402

checked = 0
for name, spec in voices.PRESETS.items():
    path = voices.VOICE_DIR / spec["file"]
    if not path.exists() or path.suffix != ".wav":
        continue
    checked += 1
    report = voiceclip.inspect_wav(path)
    measured = report["f0_hz"]
    check(f"{name}: measured {measured} Hz is really above the "
          f"{voiceclip.SAFE_F0_HZ:.0f} Hz floor",
          measured is not None and measured >= voiceclip.SAFE_F0_HZ, report)
    drift = abs(measured - spec["f0_hz"]) / spec["f0_hz"] * 100
    check(f"{name}: the declared {spec['f0_hz']} Hz describes the audio "
          f"({drift:.0f}% off)", drift < 10, measured)
check("at least one shipped reference was actually read", checked >= 1)

# ── 4. the verdict, which is the whole point of the tool ─────────────────
print("\nthe verdict")
ok, notes = voiceclip.verdict(
    {"seconds": 8.0, "f0_hz": 220.0, "peak_pct": 70.0, "rms_pct": 12.0})
check("a good reference passes", ok, notes)

ok, notes = voiceclip.verdict(
    {"seconds": 1.2, "f0_hz": 220.0, "peak_pct": 70.0, "rms_pct": 12.0})
check("too short is refused", not ok and "too short" in " ".join(notes), notes)

ok, notes = voiceclip.verdict(
    {"seconds": 8.0, "f0_hz": 167.0, "peak_pct": 70.0, "rms_pct": 12.0})
check("the 167 Hz alto that came back male is refused",
      not ok and "octave" in " ".join(notes), notes)

ok, notes = voiceclip.verdict(
    {"seconds": 8.0, "f0_hz": None, "peak_pct": 70.0, "rms_pct": 12.0})
check("music or noise is refused", not ok, notes)

ok, notes = voiceclip.verdict(
    {"seconds": 8.0, "f0_hz": 220.0, "peak_pct": 100.0, "rms_pct": 12.0})
check("clipping is reported but does not condemn the clip",
      ok and "clips" in " ".join(notes), notes)

ok, notes = voiceclip.verdict(
    {"seconds": 22.0, "f0_hz": 220.0, "peak_pct": 70.0, "rms_pct": 12.0})
check("over-long is a note, not a refusal — it is trimmable",
      ok and "longer" in " ".join(notes), notes)

# ── 5. reading WAVs nobody promised would be tidy ─────────────────────────
print("\nreading awkward WAVs")
with wave.open(str(tmp / "stereo.wav"), "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(24000)
    # left silent, right a tone: folding must average, not take channel 0
    src = array.array("h")
    for n in range(24000):
        src.append(0)
        src.append(int(0.5 * 32767 * math.sin(2 * math.pi * 200 * n / 24000)))
    w.writeframes(src.tobytes())
samples, rate, full = voiceclip.read_wav(tmp / "stereo.wav")
check("stereo folds to mono", len(samples) == 24000 and rate == 24000)
check("...by averaging, so a one-sided clip is not silence",
      max(abs(s) for s in samples) > 1000, max(abs(s) for s in samples))
check("full scale is reported for the format, not guessed from the data",
      full == 32767, full)

with wave.open(str(tmp / "eight.wav"), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(1)
    w.setframerate(24000)
    w.writeframes(bytes([128 + int(100 * math.sin(2 * math.pi * 200 * n / 24000))
                         for n in range(24000)]))
samples, rate, full = voiceclip.read_wav(tmp / "eight.wav")
check("8-bit WAV is read as UNSIGNED, so it is not a wall of noise",
      max(abs(s) for s in samples) > 1000
      and abs(sum(samples) / len(samples)) < 2000, max(abs(s) for s in samples))

print()
if FAILED:
    raise SystemExit(f"FAILED ({len(FAILED)}): " + "; ".join(FAILED))
print("test_voiceclip: all sections passed")
