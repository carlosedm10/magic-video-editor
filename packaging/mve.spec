# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one-dir "Magic Video Editor.app" bundle from the `mve`
entry point (magic_video_editor.app:main -- uvicorn in a background thread +
a pywebview native window).

Build via `make dist-app` (wraps: uv run pyinstaller packaging/mve.spec
--noconfirm --clean --distpath dist). Output: dist/Magic Video Editor.app

Hidden-import strategy: collect_all() on every package whose native
extensions / dynamic imports PyInstaller's static analysis can't fully see
on its own (torch, mlx, cv2, librosa/numba/llvmlite, uvicorn's dynamic
protocol loaders, pydantic-core, ...). Iterate by running the built binary
and fixing whatever ModuleNotFoundError/ImportError surfaces next -- do not
try to hand-guess the full transitive closure up front.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

block_cipher = None

ROOT = Path(SPECPATH).parent  # repo root (this file lives in packaging/)
sys.path.insert(0, str(ROOT))

from magic_video_editor import __version__  # noqa: E402

APP_NAME = "Magic Video Editor"
BUNDLE_ID = "es.carloseduardo.magicvideoeditor"

# ---------- collect the heavy / dynamic-import packages ----------
datas = []
binaries = []
hiddenimports = []

_COLLECT_ALL_PACKAGES = [
    "static_ffmpeg",
    "mlx",
    "mlx_whisper",
    "resemblyzer",
    "noisereduce",
    "pyloudnorm",
    "torch",
    "cv2",
    "librosa",
    "numba",
    "llvmlite",
    "soundfile",
    "webview",
    "uvicorn",
    "pydantic",
    "pydantic_ai",
    "pydantic_core",
    "httpx",
    "httpcore",
    "imageio_ffmpeg",
    "scipy",
]

try:
    import static_ffmpeg.run as _static_ffmpeg_run

    _static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
except Exception as exc:  # surfaced at build time
    print(f"[mve.spec] static-ffmpeg pre-fetch failed: {exc}")

# ---------- deterministic ffmpeg/ffprobe bundling (field bug fix) ----------
# FIELD BUG (v0.6.1 packaged .app): ffprobe reported missing while ffmpeg
# worked fine. Root cause: the only thing putting ffmpeg/ffprobe in the
# bundle was collect_all("static_ffmpeg") sweeping whatever the pre-fetch
# above happened to leave in that package's install dir as package *data* --
# no explicit `binaries` entry, no fixed in-bundle path, and no guarantee
# PyInstaller's static-analysis-driven data collection actually reaches both
# files (it silently caught ffmpeg but not ffprobe in the field). Fixed by
# resolving the two binaries HERE, at build time (the pre-fetch above has
# already ensured they exist on this machine), and adding them as EXPLICIT
# `binaries` entries at a fixed, first-class path inside the bundle:
# Contents/Frameworks/vendor/ffbin/{ffmpeg,ffprobe} (PyInstaller relocates
# `binaries` datas under Frameworks/ for a onedir macOS build). ffmpeg_utils.
# ffmpeg_bin()/ffprobe_bin() then resolve that exact path as a bundle-mode
# candidate (mirrors how ollama_manager.py locates its own vendored binary
# from sys.executable) instead of trusting collect_all()'s sweep alone.
# collect_all("static_ffmpeg") above is left in place -- harmless, and it's
# still what makes `static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()`
# importable/functional inside the frozen app if anything else calls it.
_VENDOR_FFBIN_DEST = "vendor/ffbin"
try:
    import static_ffmpeg.run as _sf_run

    _ffmpeg_src, _ffprobe_src = _sf_run.get_or_fetch_platform_executables_else_raise()
    binaries += [
        (_ffmpeg_src, _VENDOR_FFBIN_DEST),
        (_ffprobe_src, _VENDOR_FFBIN_DEST),
    ]
    print(
        f"[mve.spec] vendoring ffmpeg={_ffmpeg_src} ffprobe={_ffprobe_src} "
        f"-> Contents/Frameworks/{_VENDOR_FFBIN_DEST}/"
    )
except Exception as exc:  # surfaced at build time -- do not fail the build silently
    print(
        f"[mve.spec] could not resolve static-ffmpeg binaries for explicit vendoring: {exc} "
        "-- packaged app will fall back to collect_all()'s sweep / system binaries only"
    )

for pkg in _COLLECT_ALL_PACKAGES:
    try:
        d, b, h = collect_all(pkg)
    except Exception as exc:  # pragma: no cover -- best-effort during iteration
        print(f"[mve.spec] collect_all({pkg!r}) failed: {exc}")
        continue
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("magic_video_editor")

# Packages that read their own installed version via importlib.metadata at
# import time (pydantic_ai -> genai_prices being the one that actually bit
# us) need their dist-info copied in explicitly; collect_all() does not
# always pull metadata for *transitive* packages that were never passed to
# it directly.
_METADATA_ONLY_PACKAGES = [
    "genai-prices",
    "pydantic-ai-slim",
    "pydantic",
    "pydantic_core",
    "fastapi",
    "starlette",
    "uvicorn",
    "httpx",
    "httpcore",
    "numpy",
    "scipy",
    "opencv-python-headless",
    "torch",
    "mlx",
    "mlx-whisper",
    "resemblyzer",
    "noisereduce",
    "pyloudnorm",
    "soundfile",
    "rapidfuzz",
    "psutil",
    "imageio-ffmpeg",
    "webview",
]
for dist_name in _METADATA_ONLY_PACKAGES:
    try:
        datas += copy_metadata(dist_name)
    except Exception as exc:  # pragma: no cover -- best-effort during iteration
        print(f"[mve.spec] copy_metadata({dist_name!r}) failed: {exc}")

# App static assets: the UI lives at repo-root ui/, served relative to the
# package's parent dir (server.py: UI_DIR = Path(__file__).parent.parent /
# "ui") -- keep that same sibling relationship inside the bundled _internal/.
datas += [(str(ROOT / "ui"), "ui")]

# Auto-update helper script (v6 "Auto-update via GitHub Releases" --
# magic_video_editor/updater.py's _helper_script_path() looks for this at
# the same sibling-of-package-dir path, mirroring ollama_manager.py above).
datas += [(str(ROOT / "packaging" / "update_helper.sh"), "packaging")]

# Bundled Ollama runtime (v6 packaging Option B -- see ollama_manager.py).
# Fetched separately via `bash packaging/fetch_ollama.sh` (not committed --
# see .gitignore); only included when present so a checkout that skipped the
# fetch step still builds (ollama_manager falls back to "unreachable"/system
# mode). Added as `datas` (not `binaries`) so PyInstaller copies the whole
# vendored tree byte-for-byte instead of walking/re-linking these foreign
# Mach-O binaries as if they were our own extension modules; shutil.copy2
# preserves the executable bit. Same sibling-of-package-dir layout as ui/
# above, which is exactly where ollama_manager.bundled_binary_path() looks.
_OLLAMA_VENDOR_DIR = ROOT / "packaging" / "vendor" / "ollama"
if _OLLAMA_VENDOR_DIR.is_dir():
    datas += [(str(_OLLAMA_VENDOR_DIR), "packaging/vendor/ollama")]
else:
    print(
        f"[mve.spec] {_OLLAMA_VENDOR_DIR} not found -- building without a bundled "
        "Ollama (run packaging/fetch_ollama.sh first for Option B packaging)."
    )

a = Analysis(
    [str(Path(SPECPATH) / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(Path(SPECPATH) / "icon.icns"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=str(Path(SPECPATH) / "icon.icns"),
    bundle_identifier=BUNDLE_ID,
    version=__version__,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "carlosedm10",
        "LSMinimumSystemVersion": "12.0",
    },
)
