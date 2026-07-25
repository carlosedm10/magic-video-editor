"""Professional color pipeline (v5.7) — ffmpeg vf-chain builder used by the
render/reels hook (ffmpeg_utils.cut_segment(vf_extra=...)) and the live
preview-frame endpoint.

Schema (project["color"]):
    {
      "preset": "none|bw|sepia|cinematic|vintage",
      "exposure": -3..3,          # EV stops
      "temperature": -1..1,       # + warm .. - cool (~3500K..8500K)
      "tint": -1..1,              # + magenta .. - green (midtones)
      "black_point": 0..0.5,      # lifts/crushes input black
      "white_point": 0..0.5,      # lowers input white ceiling
      "brightness": -1..1,
      "contrast": -1..1,
      "saturation": -1..1,
      "vibrance": -1..1,          # smart/skin-protecting saturation
      "sharpness": 0..1,
      "lut": {"name": <file in the luts library dir> | None, "intensity": 0..1},
    }

Every control maps 1:1 to a real ffmpeg filter (no invented math), applied in
the standard Lightroom/Premiere correction order:

    exposure -> colortemperature -> colorbalance(tint) -> colorlevels
    -> eq(brightness/contrast/saturation) -> vibrance -> unsharp -> LUT (last)

`build_vf()` accepts OLD-shaped configs too ({preset, brightness, contrast,
saturation, temperature}) and migrates them on read via `migrate_color_config`
so callers never need a separate migration step.

Presets are parameter presets over this schema: PRESET_PARAMS supplies the
baseline values a preset dials in; any field the caller has moved away from
DEFAULT_COLOR is treated as an explicit user override and wins over the
preset's baseline for that field.
"""

import functools
import subprocess
from pathlib import Path

from ..ffmpeg_utils import ffmpeg_bin

PRESETS = ("none", "bw", "sepia", "cinematic", "vintage")

# Full default schema (v5.7). Also the "neutral" reference used to detect
# explicit user overrides on top of a preset (see _resolve_preset_base).
DEFAULT_COLOR: dict = {
    "preset": "none",
    "exposure": 0.0,
    "temperature": 0.0,
    "tint": 0.0,
    "black_point": 0.0,
    "white_point": 0.0,
    "brightness": 0.0,
    "contrast": 0.0,
    "saturation": 0.0,
    "vibrance": 0.0,
    "sharpness": 0.0,
    "lut": {"name": None, "intensity": 1.0},
}

_SLIDER_KEYS = (
    "exposure",
    "temperature",
    "tint",
    "black_point",
    "white_point",
    "brightness",
    "contrast",
    "saturation",
    "vibrance",
    "sharpness",
)

# Parameter presets over the new schema. Only non-neutral fields need to be
# listed; everything else stays at DEFAULT_COLOR. These are approximations of
# the old look-based presets -- true split-toning / channel-mixed sepia isn't
# representable by these global controls; built-in LUTs are the planned
# follow-up for exact looks (see spec v5.7).
PRESET_PARAMS: dict[str, dict] = {
    "none": {},
    "bw": {"saturation": -1.0},
    "sepia": {"saturation": -0.5, "temperature": 0.6, "tint": 0.1},
    "cinematic": {
        "temperature": -0.15,
        "tint": 0.05,
        "black_point": 0.03,
        "white_point": 0.03,
        "saturation": -0.1,
        "vibrance": 0.15,
    },
    "vintage": {
        "black_point": 0.07,
        "white_point": 0.07,
        "saturation": -0.25,
        "temperature": 0.35,
    },
}


@functools.cache
def _has_filter(name: str) -> bool:
    try:
        out = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        return name in out
    except Exception:
        return False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def migrate_color_config(raw: dict | None) -> dict:
    """Normalize any stored project["color"] (old 4-slider shape or the new
    v5.7 schema, partial or full) into a complete new-schema dict. Idempotent
    and side-effect free -- safe to call on every read."""
    cfg = dict(DEFAULT_COLOR)
    cfg["lut"] = dict(DEFAULT_COLOR["lut"])
    if not raw:
        return cfg

    preset = raw.get("preset") or "none"
    cfg["preset"] = preset if preset in PRESETS else "none"

    # Old schema had brightness/contrast/saturation/temperature only -- these
    # keys are shared verbatim with the new schema, so a plain per-key read
    # covers the migration; unknown/new keys (exposure, tint, black_point,
    # white_point, vibrance, sharpness) simply stay at their defaults unless
    # present.
    for key in _SLIDER_KEYS:
        if key in raw and raw[key] is not None:
            try:
                cfg[key] = float(raw[key])
            except (TypeError, ValueError):
                pass

    lut = raw.get("lut")
    if isinstance(lut, dict):
        cfg["lut"]["name"] = lut.get("name") or None
        try:
            cfg["lut"]["intensity"] = _clamp(float(lut.get("intensity", 1.0)), 0.0, 1.0)
        except (TypeError, ValueError):
            pass

    return cfg


def _resolve_preset_base(cfg: dict) -> dict:
    """Layer explicit (non-default) slider values from `cfg` on top of the
    preset's baseline parameters."""
    preset = cfg.get("preset") or "none"
    base = dict(DEFAULT_COLOR)
    base.update(PRESET_PARAMS.get(preset, {}))
    resolved = dict(base)
    for key in _SLIDER_KEYS:
        if cfg.get(key, 0.0) != DEFAULT_COLOR[key]:
            resolved[key] = cfg[key]
    resolved["lut"] = cfg.get("lut") or dict(DEFAULT_COLOR["lut"])
    return resolved


def _escape_filter_path(path: str) -> str:
    """Escape a filesystem path for embedding inside a single-quoted ffmpeg
    filter option value."""
    p = str(path)
    return p.replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")


def _lut_vf(lut_path: Path, intensity: float) -> str:
    """Split the stream, apply the LUT (lut3d for .cube/.3dl, haldclut for a
    hald .png) to one branch, blend it back over the untouched original at
    `intensity` -- the standard "LUT strength" trick. Always goes through the
    split+blend form (even at intensity==1.0) so there is a single, simpler
    code path; `blend` at all_opacity=1.0 is equivalent to using the LUT
    branch outright."""
    intensity = _clamp(intensity, 0.0, 1.0)
    if intensity <= 0:
        return ""
    escaped = _escape_filter_path(str(lut_path))
    suffix = lut_path.suffix.lower()
    if suffix == ".png":
        # `movie=` loads the hald CLUT image as its own filter source, so
        # this still fits a single-main-input simple filtergraph (no extra
        # -i needed) once fed into haldclut's second input.
        lut_apply = (
            f"movie='{escaped}'[__mve_clut];[__mve_src][__mve_clut]haldclut[__mve_luted];"
        )
    else:
        lut_apply = f"[__mve_src]lut3d=file='{escaped}'[__mve_luted];"
    # blend's first input is the "top" layer, weighted by all_opacity; the
    # second is "bottom", weighted by (1 - all_opacity). We want
    # orig*(1-intensity) + luted*intensity, so the LUTed branch must be the
    # FIRST (top) input and the untouched original the SECOND (bottom) one.
    return (
        f"split[__mve_orig][__mve_src];"
        f"{lut_apply}"
        f"[__mve_luted][__mve_orig]blend=all_mode='normal':all_opacity={intensity:.3f}"
    )


def build_vf(color_cfg: dict | None, lut_dir: Path | None = None) -> str:
    """Build the ffmpeg -vf fragment for `color_cfg` (any shape --
    migrated internally). `lut_dir` is the LUT library directory used to
    resolve `color_cfg["lut"]["name"]`; pass None to skip LUT resolution
    (e.g. when the caller doesn't have the library available)."""
    migrated = migrate_color_config(color_cfg)
    resolved = _resolve_preset_base(migrated)

    parts: list[str] = []

    exposure = resolved["exposure"]
    if exposure:
        parts.append(f"exposure=exposure={_clamp(exposure, -3, 3):.3f}")

    temperature = resolved["temperature"]
    if temperature:
        t = _clamp(temperature, -1, 1)
        if _has_filter("colortemperature"):
            # +1 (warm) -> 3500K, -1 (cool) -> 8500K.
            kelvin = round(6000 - t * 2500)
            parts.append(f"colortemperature=temperature={kelvin}")
        else:
            shift = round(0.12 * t, 3)
            if shift >= 0:
                parts.append(f"curves=red='0/0 1/{_clamp(1 + shift, 0, 1)}'")
            else:
                parts.append(f"curves=blue='0/0 1/{_clamp(1 - shift, 0, 1)}'")

    tint = resolved["tint"]
    if tint and _has_filter("colorbalance"):
        t = _clamp(tint, -1, 1)
        # Magenta<->green on the midtone axis: +magenta lifts red/blue,
        # trims green.
        rm = round(0.15 * t, 3)
        gm = round(-0.3 * t, 3)
        bm = round(0.15 * t, 3)
        parts.append(f"colorbalance=rm={rm}:gm={gm}:bm={bm}")

    black_point = _clamp(resolved["black_point"], 0, 0.5)
    white_point = _clamp(resolved["white_point"], 0, 0.5)
    if (black_point or white_point) and _has_filter("colorlevels"):
        imin = round(black_point, 3)
        imax = round(1 - white_point, 3)
        parts.append(
            f"colorlevels=rimin={imin}:gimin={imin}:bimin={imin}:"
            f"rimax={imax}:gimax={imax}:bimax={imax}"
        )

    brightness, contrast, saturation = (
        resolved["brightness"],
        resolved["contrast"],
        resolved["saturation"],
    )
    if brightness or contrast or saturation:
        eq_args = []
        if brightness:
            eq_args.append(f"brightness={_clamp(brightness, -1, 1):.3f}")
        if contrast:
            eq_args.append(f"contrast={_clamp(1 + contrast, 0, 3):.3f}")
        if saturation:
            eq_args.append(f"saturation={_clamp(1 + saturation, 0, 3):.3f}")
        parts.append("eq=" + ":".join(eq_args))

    vibrance = resolved["vibrance"]
    if vibrance and _has_filter("vibrance"):
        intensity = _clamp(vibrance * 1.5, -2, 2)
        parts.append(f"vibrance=intensity={intensity:.3f}")

    sharpness = _clamp(resolved["sharpness"], 0, 1)
    if sharpness and _has_filter("unsharp"):
        amount = round(sharpness * 1.5, 3)
        parts.append(f"unsharp=5:5:{amount}")

    chain = ",".join(parts)

    lut_cfg = resolved.get("lut") or {}
    lut_name = lut_cfg.get("name")
    if lut_name and lut_dir is not None:
        lut_path = Path(lut_dir) / lut_name
        if lut_path.is_file():
            lut_frag = _lut_vf(lut_path, float(lut_cfg.get("intensity", 1.0)))
            if lut_frag:
                chain = f"{chain},{lut_frag}" if chain else lut_frag

    return chain
