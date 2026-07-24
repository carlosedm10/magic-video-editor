"""Color-filter ffmpeg vf-chain builder — render/reels hook.

build_vf(cfg) turns a project["color"] config into an ffmpeg `-vf` fragment
that render.run / reels.render_reel prepend to their own vf chain via
ffmpeg_utils.cut_segment(vf_extra=...). Presets contribute a look (via
colorchannelmixer/hue/colorbalance/curves); the four sliders (brightness,
contrast, saturation, temperature, each -1..1 with 0 = neutral) layer on top
via `eq` and `colortemperature`. Empty/None/all-neutral cfg returns "".

`colortemperature` ships in the ffmpeg build this project bundles (checked
via `ffmpeg_bin() -filters`); if a future bundled ffmpeg lacks it, fall back
to a `curves`-based warm/cool tint instead.
"""

import functools
import subprocess

from ..ffmpeg_utils import ffmpeg_bin

PRESETS = ("none", "bw", "sepia", "cinematic", "vintage")

# Classic sepia matrix (colorchannelmixer rr:rg:rb:ra:gr:gg:gb:ga:br:bg:bb:ba).
_SEPIA = "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131:0"


@functools.cache
def _has_colortemperature() -> bool:
    try:
        out = subprocess.run(
            [ffmpeg_bin(), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
        return "colortemperature" in out
    except Exception:
        return False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _preset_vf(preset: str) -> str:
    if preset == "bw":
        # Desaturate, then a slight contrast lift so it doesn't look flat.
        return "hue=s=0,eq=contrast=1.08"
    if preset == "sepia":
        return _SEPIA
    if preset == "cinematic":
        # Subtle teal shadows / orange highlights (colorbalance range -1..1).
        return "colorbalance=rs=-0.06:gs=0.02:bs=0.07:rh=0.08:gh=0.01:bh=-0.07"
    if preset == "vintage":
        # Faded (lifted) blacks + slightly crushed highlights, desaturated,
        # slight warm cast.
        return (
            "curves=master='0/0.07 0.5/0.5 1/0.93',"
            "eq=saturation=0.75"
            + (",colortemperature=temperature=5800" if _has_colortemperature() else "")
        )
    return ""


def _slider_vf(brightness: float, contrast: float, saturation: float, temperature: float) -> str:
    parts = []
    if brightness or contrast or saturation:
        eq_args = []
        if brightness:
            eq_args.append(f"brightness={_clamp(brightness, -1, 1):.3f}")
        if contrast:
            eq_args.append(f"contrast={_clamp(1 + contrast, 0, 3):.3f}")
        if saturation:
            eq_args.append(f"saturation={_clamp(1 + saturation, 0, 3):.3f}")
        parts.append("eq=" + ":".join(eq_args))
    if temperature:
        t = _clamp(temperature, -1, 1)
        if _has_colortemperature():
            # +1 (warm) -> 3500K, -1 (cool) -> 9500K, 0 -> 6500K neutral.
            kelvin = round(6500 - t * 3000)
            parts.append(f"colortemperature=temperature={kelvin}")
        else:
            # Fallback: fake warm/cool via a per-channel curves tint.
            shift = round(0.12 * t, 3)
            if shift >= 0:
                parts.append(f"curves=red='0/0 1/{_clamp(1 + shift, 0, 1)}'")
            else:
                parts.append(f"curves=blue='0/0 1/{_clamp(1 - shift, 0, 1)}'")
    return ",".join(parts)


def build_vf(color_cfg: dict | None) -> str:
    if not color_cfg:
        return ""
    preset = color_cfg.get("preset") or "none"
    brightness = float(color_cfg.get("brightness") or 0)
    contrast = float(color_cfg.get("contrast") or 0)
    saturation = float(color_cfg.get("saturation") or 0)
    temperature = float(color_cfg.get("temperature") or 0)

    slider_vf = _slider_vf(brightness, contrast, saturation, temperature)
    chain = [c for c in (_preset_vf(preset), slider_vf) if c]
    return ",".join(chain)
