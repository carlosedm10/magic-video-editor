"""8-band parametric EQ (spec: "Voice enhancement v2 + 8-band EQ").

project["audio_eq"] is a list of 8 gains in dB (-12..+12) at fixed center
frequencies, applied as chained ffmpeg `equalizer` peaking biquads (Q~1.0)
during render/preview-render, and mirrored live in the browser via WebAudio
peaking BiquadFilterNodes at the same frequencies (see ui/panels/audio.js).

Kept dependency-free (no numpy/ffmpeg imports) — this module only builds an
ffmpeg filter-graph fragment string; it's cheap enough to import eagerly.
"""

# Center frequencies, low -> high. Order matches project["audio_eq"] indices
# and the UI's slider order.
EQ_FREQS_HZ = [60, 150, 400, 1000, 2400, 6000, 12000, 16000]

EQ_BAND_COUNT = len(EQ_FREQS_HZ)
EQ_MIN_DB = -12.0
EQ_MAX_DB = 12.0
EQ_Q = 1.0

FLAT_GAINS = [0.0] * EQ_BAND_COUNT


def clamp_gain(g: float) -> float:
    return max(EQ_MIN_DB, min(EQ_MAX_DB, float(g)))


def normalize_gains(gains8) -> list:
    """Coerce/validate an incoming gains list to exactly EQ_BAND_COUNT
    clamped floats, defaulting missing entries to 0.0 (flat)."""
    gains8 = list(gains8 or [])
    out = []
    for i in range(EQ_BAND_COUNT):
        try:
            out.append(clamp_gain(gains8[i]))
        except (IndexError, TypeError, ValueError):
            out.append(0.0)
    return out


def is_flat(gains8) -> bool:
    return all(abs(g) < 1e-9 for g in normalize_gains(gains8))


def build_audio_filter(gains8) -> str:
    """Build a chained ffmpeg `equalizer=f=...:width_type=q:w=1:g=...`
    filter string from 8 gains (dB, one per EQ_FREQS_HZ band).

    - Bands whose (clamped) gain is ~0 dB are skipped entirely (a 0 dB
      peaking band is a no-op but still costs a biquad; skipping keeps the
      filter graph minimal).
    - Returns "" when every band is flat, so callers can omit `-af`
      entirely for the (default, common) flat-EQ case.
    """
    gains = normalize_gains(gains8)
    bands = []
    for freq, gain in zip(EQ_FREQS_HZ, gains, strict=True):
        if abs(gain) < 1e-9:
            continue
        bands.append(f"equalizer=f={freq}:width_type=q:w={EQ_Q}:g={gain:.3f}")
    return ",".join(bands)
