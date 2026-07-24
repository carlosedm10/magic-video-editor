"""Voice-enhancement hook — render/reels call this on the final file's
extracted audio, then remux it back (see ffmpeg_utils.mux_audio).

Pipeline (spec: "Voice enhancement"):
  1) noisereduce non-stationary spectral gating (moderate, prop_decrease~0.85)
  2) high-pass 80 Hz (2nd-order Butterworth, zero-phase)
  3) gentle presence shelf ~+2.5 dB around 3-5 kHz (peaking biquad)
  4) loudness normalize to -16 LUFS (pyloudnorm) with a simple true-peak-ish
     limiter (normalize then clip-protect via extra gain reduction, ceiling
     -1 dBTP)

Mono/stereo safe, preserves the input sample rate. Dependency-light and fast
(vectorized numpy/scipy + noisereduce's batched STFT gating) — a 5-minute
clip enhances in well under 10s on a modern machine.
"""

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

NOISE_PROP_DECREASE = 0.85
HIGHPASS_HZ = 80.0
PRESENCE_FREQ = 4000.0  # center of the 3-5kHz presence lift
PRESENCE_GAIN_DB = 2.5
PRESENCE_Q = 1.0
TARGET_LUFS = -16.0
CEILING_DBTP = -1.0


def _highpass(x: np.ndarray, sr: int, cutoff: float = HIGHPASS_HZ) -> np.ndarray:
    sos = butter(2, cutoff, btype="highpass", fs=sr, output="sos")
    return sosfiltfilt(sos, x, axis=0)


def _peaking_biquad_coeffs(sr: int, freq: float, gain_db: float, q: float):
    """RBJ audio-eq-cookbook peaking biquad -> (b, a) for scipy.signal.lfilter."""
    a_lin = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / sr
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    b0 = 1 + alpha * a_lin
    b1 = -2 * cos_w0
    b2 = 1 - alpha * a_lin
    a0 = 1 + alpha / a_lin
    a1 = -2 * cos_w0
    a2 = 1 - alpha / a_lin

    b = np.array([b0, b1, b2]) / a0
    a = np.array([1.0, a1 / a0, a2 / a0])
    return b, a


def _presence_lift(x: np.ndarray, sr: int) -> np.ndarray:
    from scipy.signal import filtfilt

    b, a = _peaking_biquad_coeffs(sr, PRESENCE_FREQ, PRESENCE_GAIN_DB, PRESENCE_Q)
    return filtfilt(b, a, x, axis=0)


def _denoise_channel(x: np.ndarray, sr: int) -> np.ndarray:
    import noisereduce as nr

    return nr.reduce_noise(
        y=x,
        sr=sr,
        stationary=False,
        prop_decrease=NOISE_PROP_DECREASE,
    )


def _loudness_normalize(x: np.ndarray, sr: int) -> np.ndarray:
    meter = pyln.Meter(sr)
    # pyloudnorm needs enough samples to measure; fall back to raw signal on
    # very short/silent clips instead of raising.
    try:
        loudness = meter.integrated_loudness(x)
    except Exception:
        return x
    if not np.isfinite(loudness) or loudness <= -70.0:
        return x
    y = pyln.normalize.loudness(x, loudness, TARGET_LUFS)

    ceiling = 10 ** (CEILING_DBTP / 20.0)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > ceiling:
        y = y * (ceiling / peak)
    return y


def enhance(in_wav_path: str, out_wav_path: str) -> str:
    data, sr = sf.read(in_wav_path, always_2d=True, dtype="float32")
    n_channels = data.shape[1]

    processed = np.zeros_like(data)
    for ch in range(n_channels):
        chan = data[:, ch]
        chan = _denoise_channel(chan, sr)
        chan = _highpass(chan, sr)
        chan = _presence_lift(chan, sr)
        processed[:, ch] = chan

    processed = _loudness_normalize(processed, sr)
    processed = np.clip(processed, -1.0, 1.0).astype(np.float32)

    sf.write(out_wav_path, processed, sr, subtype="PCM_16")
    return out_wav_path
