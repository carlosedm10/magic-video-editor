"""Generates packaging/icon.icns from scratch: a 1024px dark-navy rounded
square with a garnet ✦ sparkle, matching the brand identity in
docs/PLATFORM-SPEC.md (near-black -> dark navy background, garnet/maroon
accent, "magical" feel).

Pure PIL (no CoreGraphics/pyobjc needed) + the macOS `iconutil` CLI to pack
the iconset into a .icns. Run standalone:

    uv run python packaging/make_icon.py

Idempotent -- safe to re-run; overwrites packaging/icon.icns and cleans up
the intermediate .iconset directory it builds in packaging/.
"""

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

PACKAGING_DIR = Path(__file__).parent
ICONSET_DIR = PACKAGING_DIR / "icon.iconset"
ICNS_PATH = PACKAGING_DIR / "icon.icns"
SIZE = 1024

# Brand palette (docs/PLATFORM-SPEC.md "Brand / visual identity")
NAVY_DARK = (5, 7, 13)  # #05070d
NAVY = (10, 16, 32)  # #0a1020
GARNET_DARK = (122, 18, 32)  # #7a1220
GARNET = (160, 24, 40)  # #a01828
GARNET_LIGHT = (194, 32, 48)  # #c22030


def _rounded_square_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _radial_navy_background(size: int) -> Image.Image:
    """Near-black -> dark navy radial glow, per the brand spec."""
    bg = Image.new("RGB", (size, size), NAVY_DARK)
    glow = Image.new("L", (size, size), 0)
    gdraw = ImageDraw.Draw(glow)
    cx, cy = size * 0.5, size * 0.42
    max_r = size * 0.75
    steps = 160
    for i in range(steps, 0, -1):
        r = max_r * i / steps
        alpha = int(255 * (1 - i / steps) ** 1.6)
        gdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=alpha)
    navy_layer = Image.new("RGB", (size, size), NAVY)
    bg = Image.composite(navy_layer, bg, glow)
    return bg


def _sparkle_path(cx: float, cy: float, r_long: float, r_short: float) -> list[tuple[float, float]]:
    """A classic 4-point "✦" sparkle: alternating long/short points around
    the center, giving the concave-diamond star silhouette."""
    pts = []
    n_points = 4
    for i in range(n_points * 2):
        angle = math.pi / 2 * (i / 2) - math.pi / 2  # start pointing up
        radius = r_long if i % 2 == 0 else r_short
        # offset the short points 45deg between the long points
        a = angle if i % 2 == 0 else angle + (math.pi / 4)
        pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return pts


def _draw_sparkle(canvas: Image.Image) -> None:
    size = canvas.size[0]
    cx, cy = size * 0.5, size * 0.5

    # Soft garnet glow behind the sparkle (fades through black -> feels
    # "magical/intelligent" per spec, never a hard-edged logo).
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    glow_r = size * 0.30
    for i in range(60, 0, -1):
        r = glow_r * i / 60
        alpha = int(120 * (1 - i / 60) ** 1.4)
        gdraw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*GARNET, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.02))
    canvas.alpha_composite(glow)

    # Main sparkle body: vertical gradient garnet-dark -> garnet-light,
    # drawn at high supersample-equivalent resolution (1024px canvas).
    star_mask = Image.new("L", (size, size), 0)
    sdraw = ImageDraw.Draw(star_mask)
    main_pts = _sparkle_path(cx, cy, size * 0.30, size * 0.085)
    sdraw.polygon(main_pts, fill=255)

    grad_rgb = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grdraw = ImageDraw.Draw(grad_rgb)
    for y in range(size):
        t = y / size
        col = tuple(int(GARNET_DARK[c] + (GARNET_LIGHT[c] - GARNET_DARK[c]) * t) for c in range(3))
        grdraw.line([(0, y), (size, y)], fill=(*col, 255))

    tinted = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tinted.paste(grad_rgb, (0, 0), star_mask)
    canvas.alpha_composite(tinted)

    # A small companion sparkle, lighter + smaller, upper-right -- adds the
    # "twinkle" read without cluttering the mark.
    mini = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mdraw = ImageDraw.Draw(mini)
    mini_pts = _sparkle_path(cx + size * 0.22, cy - size * 0.22, size * 0.075, size * 0.022)
    mdraw.polygon(mini_pts, fill=(*GARNET_LIGHT, 235))
    mini = mini.filter(ImageFilter.GaussianBlur(size * 0.001))
    canvas.alpha_composite(mini)


def build_icon_png() -> Image.Image:
    bg = _radial_navy_background(SIZE).convert("RGBA")
    radius = int(SIZE * 0.22)  # macOS-ish continuity curve approximation
    mask = _rounded_square_mask(SIZE, radius)

    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(bg, (0, 0))
    _draw_sparkle(canvas)

    # subtle 1px border, per brand ("glass cards, subtle 1px borders")
    border = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(border)
    bdraw.rounded_rectangle(
        [1, 1, SIZE - 2, SIZE - 2], radius=radius, outline=(28, 35, 51, 200), width=3
    )
    canvas.alpha_composite(border)

    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(canvas, (0, 0), mask)
    return out


ICONSET_SIZES = [16, 32, 128, 256, 512]


def build_iconset(icon: Image.Image) -> None:
    if ICONSET_DIR.exists():
        shutil.rmtree(ICONSET_DIR)
    ICONSET_DIR.mkdir(parents=True)
    for s in ICONSET_SIZES:
        icon.resize((s, s), Image.LANCZOS).save(ICONSET_DIR / f"icon_{s}x{s}.png")
        icon.resize((s * 2, s * 2), Image.LANCZOS).save(ICONSET_DIR / f"icon_{s}x{s}@2x.png")


def main() -> None:
    icon = build_icon_png()
    PACKAGING_DIR.mkdir(exist_ok=True)
    icon.save(PACKAGING_DIR / "icon_preview.png")  # handy for eyeballing
    build_iconset(icon)

    if shutil.which("iconutil") is None:
        print("iconutil not found (non-macOS?) -- .iconset built, .icns skipped.", file=sys.stderr)
        return

    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET_DIR), "-o", str(ICNS_PATH)],
        check=True,
    )
    shutil.rmtree(ICONSET_DIR)
    print(f"Wrote {ICNS_PATH}")


if __name__ == "__main__":
    main()
