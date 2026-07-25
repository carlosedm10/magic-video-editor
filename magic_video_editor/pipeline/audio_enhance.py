"""Voice-enhancement hook — render/reels call this on the final file's
extracted audio, then remux it back (see ffmpeg_utils.mux_audio).

Pipeline v2 (spec: "Voice enhancement v2 + 8-band EQ"):
  1) neural speech enhancement via DeepFilterNet (DNS-grade, CPU-fast — see
     the A/B benchmark in the v7.9 report: ~1s of CPU time per minute of
     audio on Apple Silicon, and measurably lower do-no-harm spectral drift
     on already-clean speech than the old noisereduce chain).
  2) loudness normalize to -16 LUFS (pyloudnorm) with a simple true-peak-ish
     limiter (normalize then clip-protect via extra gain reduction, ceiling
     -1 dBTP)

DeepFilterNet replaces the old gate/highpass/presence-shelf stack entirely —
the model already handles denoising and tonal balance, and the old stack was
DEGRADING already-good audio (tinny artifacts; the original owner complaint).
noisereduce + the legacy highpass/presence chain is kept ONLY as a fallback
for when torch/deepfilternet aren't installed or fail to load, so enhance()
never hard-fails just because the optional heavy dependency is missing.

Model weights (~railshipped via the `deepfilternet` PyPI package's own repo)
are downloaded on first use into our own app data dir (config.DATA_DIR /
"models" / "deepfilternet"), not the package's default ~/Library/Caches
location, so they travel with the rest of our on-disk state. A logged
line brackets the download since it can take a few seconds on first run.

All heavy imports (torch, df.*, noisereduce) are function-local/lazy so
importing this module (as `make smoke` does) stays fast and never requires
torch to be importable.

Mono/stereo safe, preserves the input sample rate and sample count.
"""

import logging
import threading

import numpy as np
import pyloudnorm as pyln
import soundfile as sf

from .. import config

logger = logging.getLogger(__name__)

TARGET_LUFS = -16.0
CEILING_DBTP = -1.0

DFN_MODEL_NAME = "DeepFilterNet3"

# --- legacy fallback chain (noisereduce + highpass + presence lift) ---
NOISE_PROP_DECREASE = 0.85
HIGHPASS_HZ = 80.0
PRESENCE_FREQ = 4000.0  # center of the 3-5kHz presence lift
PRESENCE_GAIN_DB = 2.5
PRESENCE_Q = 1.0

_dfn_lock = threading.Lock()
# None = not yet attempted, False = unavailable/failed, (model, df_state) = ready
_dfn_state = None


def _dfn_base_dir():
    """Where DeepFilterNet model weights live, inside our own data dir
    rather than the package's default ~/Library/Caches/DeepFilterNet."""
    return config.DATA_DIR / "models" / "deepfilternet"


def _ensure_dfn_weights(base_dir) -> str:
    """Download DeepFilterNet3 weights into base_dir/DeepFilterNet3 on
    first use (idempotent — skipped once checkpoints exist). Returns the
    model directory to hand to init_df(model_base_dir=...)."""
    model_dir = base_dir / DFN_MODEL_NAME
    already_present = (model_dir / "config.ini").exists() or (model_dir / "checkpoints").is_dir()
    if not already_present:
        from df.utils import download_file

        base_dir.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "Downloading %s model weights into %s (first use only)...",
            DFN_MODEL_NAME,
            model_dir,
        )
        url = f"https://github.com/Rikorose/DeepFilterNet/raw/main/models/{DFN_MODEL_NAME}.zip"
        download_file(url, str(base_dir), extract=True)
        logger.warning("%s model weights ready at %s", DFN_MODEL_NAME, model_dir)
    return str(model_dir)


def _load_dfn():
    """Lazily initialize (and cache) the DeepFilterNet model + state.
    Returns (model, df_state) or False if unavailable — never raises, so
    callers can unconditionally fall back to the legacy chain."""
    global _dfn_state
    if _dfn_state is not None:
        return _dfn_state
    with _dfn_lock:
        if _dfn_state is not None:
            return _dfn_state
        try:
            from df.enhance import init_df

            model_dir = _ensure_dfn_weights(_dfn_base_dir())
            model, df_state, _ = init_df(model_base_dir=model_dir, log_file=None)
            _dfn_state = (model, df_state)
        except Exception:
            logger.exception(
                "DeepFilterNet unavailable (missing torch/deepfilternet or model "
                "download failed) — falling back to the noisereduce chain."
            )
            _dfn_state = False
    return _dfn_state


def _process_dfn(data: np.ndarray, sr: int, model, df_state) -> np.ndarray:
    """Run DeepFilterNet over every channel at once. data is (samples,
    channels) float32. Resamples to/from the model's native rate as needed
    and pads/truncates back to the exact input sample count."""
    import torch
    from df.enhance import enhance as df_enhance
    from df.io import resample

    target_sr = df_state.sr()
    n_samples = data.shape[0]

    audio = torch.from_numpy(np.ascontiguousarray(data.T.astype(np.float32)))
    if sr != target_sr:
        audio = resample(audio, sr, target_sr)

    enhanced = df_enhance(model, df_state, audio)

    if sr != target_sr:
        enhanced = resample(enhanced, target_sr, sr)

    enhanced = enhanced.detach().cpu().numpy().T  # -> (samples, channels)

    if enhanced.shape[0] < n_samples:
        enhanced = np.pad(enhanced, ((0, n_samples - enhanced.shape[0]), (0, 0)))
    elif enhanced.shape[0] > n_samples:
        enhanced = enhanced[:n_samples]

    return enhanced.astype(np.float32)


def _highpass(x: np.ndarray, sr: int, cutoff: float = HIGHPASS_HZ) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt

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


def _process_fallback(data: np.ndarray, sr: int) -> np.ndarray:
    """Legacy chain (noisereduce gate + highpass + presence lift), used only
    when DeepFilterNet is unavailable."""
    n_channels = data.shape[1]
    processed = np.zeros_like(data)
    for ch in range(n_channels):
        chan = data[:, ch]
        chan = _denoise_channel(chan, sr)
        chan = _highpass(chan, sr)
        chan = _presence_lift(chan, sr)
        processed[:, ch] = chan
    return processed


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

    dfn = _load_dfn()
    processed = None
    if dfn:
        model, df_state = dfn
        try:
            processed = _process_dfn(data, sr, model, df_state)
        except Exception:
            logger.exception("DeepFilterNet processing failed — falling back for this file.")
            processed = None
    if processed is None:
        processed = _process_fallback(data, sr)

    processed = _loudness_normalize(processed, sr)
    processed = np.clip(processed, -1.0, 1.0).astype(np.float32)

    sf.write(out_wav_path, processed, sr, subtype="PCM_16")
    return out_wav_path
