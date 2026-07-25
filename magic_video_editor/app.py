"""macOS app entrypoint: uvicorn in a background thread + a pywebview window.
Run `mve-server` instead if you prefer a plain browser at localhost.

Native shell (spec v6 addendum "native macOS menu bar & app identity" + the
app-first PRINCIPLE section): a native menu bar dispatching into the web app
via window.evaluate_js("MenuBus.dispatch('<action>')") (ui/menubus.js is the
JS side), window size/position persistence, a native "quit anyway?" confirm
while a render is running, and a best-effort dev-mode process rename so the
menu bar doesn't read "python3"."""

import atexit
import os
import socket
import threading
import time

from . import (
    config,
    ffmpeg_utils,
    ollama_manager,
    queue,
    settings,
    single_instance,
    store,
    updater,
)

GITHUB_URL = "https://github.com/carlosedm10/magic-video-editor"


class Api:
    """Exposed to the web UI as window.pywebview.api -- native file dialogs
    plus the app-first "open externally" escape hatch used by ui/menubus.js
    for the Help > GitHub Repository / App > Check for Updates menu actions
    (PRINCIPLE: no external link ever opens inside the app window)."""

    def pick_files(self):
        import webview

        # pywebview >=5 moved OPEN_DIALOG under webview.FileDialog.OPEN and
        # deprecated the module-level constant.
        file_dialog = getattr(webview, "FileDialog", None)
        open_dialog = file_dialog.OPEN if file_dialog is not None else webview.OPEN_DIALOG
        result = webview.windows[0].create_file_dialog(
            open_dialog,
            allow_multiple=True,
            file_types=(
                "Media files (*.mp4;*.mov;*.m4v;*.mkv;*.avi;*.mts;*.m4a;*.wav;*.mp3;*.aac;*.flac)",
            ),
        )
        return list(result or [])

    def pick_folder(self):
        import webview

        # Same pywebview >=5 constant relocation as pick_files above.
        file_dialog = getattr(webview, "FileDialog", None)
        folder_dialog = file_dialog.FOLDER if file_dialog is not None else webview.FOLDER_DIALOG
        result = webview.windows[0].create_file_dialog(folder_dialog)
        return list(result or [])

    def open_external(self, url: str):
        """Open `url` in the OS default browser -- never inside the app
        window (app-first PRINCIPLE). Best-effort: a bad/blocked url must
        never crash the JS caller."""
        import webbrowser

        try:
            if url and (url.startswith("https://") or url.startswith("http://")):
                webbrowser.open(url)
        except Exception:
            pass


def _wait_for_server(timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((config.HOST, config.PORT), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("backend failed to start")


# ----------------------------- window geometry (persist/restore) ---------- #
# Spec v6 addendum "App lifecycle": window size/position persist across
# launches. Saved (debounced) on resize/move into settings.json under the
# "window" key (magic_video_editor/settings.py) and restored on next launch.
# Best-effort throughout -- a settings.json hiccup must never block the app
# from opening or closing.

_DEFAULT_SIZE = (1440, 920)
_MIN_SIZE = (1100, 700)
_GEOM_DEBOUNCE_S = 0.5
_geom_timers: dict[str, threading.Timer] = {}


def _load_geometry() -> dict:
    try:
        return dict(settings.load().get("window") or {})
    except Exception:
        return {}


def _save_window_geometry(window) -> None:
    try:
        data = settings.load()
        data["window"] = {
            "width": window.width,
            "height": window.height,
            "x": window.x,
            "y": window.y,
        }
        settings.save(data)
    except Exception:
        pass  # geometry persistence is best-effort chrome, never fatal


def _debounced_geometry_save(window, key: str):
    """Coalesce a burst of resize/move events (fired continuously while the
    user drags) into a single settings.json write `_GEOM_DEBOUNCE_S` after
    the last one, instead of hammering disk mid-drag."""

    def handler(*_args) -> None:
        prev = _geom_timers.get(key)
        if prev is not None:
            prev.cancel()
        t = threading.Timer(_GEOM_DEBOUNCE_S, _save_window_geometry, args=(window,))
        t.daemon = True
        _geom_timers[key] = t
        t.start()

    return handler


# ----------------------------- quit lifecycle ------------------------------ #
# Spec v6 PRINCIPLE "App lifecycle": quitting with a render in progress warns
# ("A render is running -- quit anyway?"). Wired to BOTH the native red
# close button (via window.events.closing, which pywebview also invokes for
# Cmd+Q / the auto-added app-menu Quit item through applicationShouldTerminate)
# and would need to be re-run for any menu item that calls window.destroy()
# directly (destroy() bypasses events.closing) -- we don't add one, since
# pywebview's own built-in Quit item already routes through events.closing.


def _any_render_running() -> bool:
    """True if ANY project has a queue item currently `running` -- cheap
    best-effort scan (small number of local projects); never raises."""
    try:
        for p in store.list_projects():
            for item in queue.list_queue(p["id"]):
                if item.get("status") == "running":
                    return True
    except Exception:
        pass
    return False


def _on_closing(window):
    """window.events.closing handler: return False to cancel the close,
    True/None to allow it. Cancelable per pywebview's Event.set() semantics
    (a False return from any registered handler cancels)."""
    if _any_render_running():
        try:
            proceed = window.create_confirmation_dialog(
                "Quit Magic Video Editor?",
                "A render is running — quit anyway? It will be stopped.",
            )
        except Exception:
            proceed = True  # dialog itself failing must never trap the user
        if not proceed:
            return False
    _save_window_geometry(window)
    ffmpeg_utils.terminate_all()
    single_instance.release_singleton()
    return True


# ----------------------------- native menu bar ------------------------------ #
# Spec v6 addendum "Native menus via pywebview's menu API". pywebview's
# MenuAction has no cross-platform accelerator support yet (see its `# TODO:
# support platform-agnostic shortcut` in webview/menu.py) -- Cmd+N/Cmd+I/
# Cmd+E are therefore NOT real key equivalents here; ui/menubus.js adds a
# best-effort JS keydown fallback for those three (Undo/Redo/Split/Delete
# already have real bindings via ui/editor/timeline.js and are unaffected).
# Every action funnels through window.evaluate_js(MenuBus.dispatch(...)) so
# the web app stays the single source of truth; MenuBus no-ops gracefully
# when no project is open (see ui/menubus.js).


def _build_menu(window):
    from webview.menu import Menu, MenuAction, MenuSeparator

    def dispatch(action: str):
        def _fire():
            try:
                window.evaluate_js(f"window.MenuBus && MenuBus.dispatch({action!r})")
            except Exception:
                pass  # window may be mid-teardown -- never let a menu click crash the app

        return _fire

    return [
        # '__app__' is pywebview's magic title for injecting custom items into
        # the actual macOS application menu (after its built-in About/Services
        # and before its built-in Quit -- see webview/platforms/cocoa.py
        # _add_app_menu). We do not add our own Quit: the built-in one already
        # routes through window.events.closing (_on_closing above).
        Menu(
            "__app__",
            [
                MenuAction("About Magic Video Editor", dispatch("about")),
                MenuSeparator(),
                MenuAction("Check for Updates…", dispatch("check_updates")),
            ],
        ),
        Menu(
            "File",
            [
                MenuAction("New Project", dispatch("new_project")),
                MenuAction("Import Files…", dispatch("import_files")),
                MenuAction("Import Folder…", dispatch("import_folder")),
                MenuSeparator(),
                MenuAction("Export…", dispatch("export")),
            ],
        ),
        Menu(
            "Edit",
            [
                MenuAction("Undo", dispatch("undo")),
                MenuAction("Redo", dispatch("redo")),
                MenuSeparator(),
                MenuAction("Split", dispatch("split")),
                MenuAction("Delete", dispatch("delete")),
            ],
        ),
        Menu(
            "View",
            [
                MenuAction("Fit Timeline", dispatch("fit_timeline")),
                MenuAction("Zoom In", dispatch("zoom_in")),
                MenuAction("Zoom Out", dispatch("zoom_out")),
                MenuSeparator(),
                MenuAction("Takes", dispatch("takes")),
                MenuAction("Reels", dispatch("reels")),
                MenuSeparator(),
                MenuAction("Projects Home", dispatch("projects_home")),
            ],
        ),
        Menu(
            "Help",
            [
                MenuAction("Keyboard Shortcuts", dispatch("shortcuts")),
                MenuAction("GitHub Repository", dispatch("github")),
            ],
        ),
    ]


# ----------------------------- dev-mode process identity -------------------- #
# Spec v6 addendum "App identity" #1: in the PACKAGED .app, Info.plist fixes
# the menu bar name (see packaging/mve.spec, owned by the BUNDLE-REPORT
# agent). In dev mode (`make app` / `uv run mve`) the process is bare
# python3, so macOS shows "python3" in the menu bar -- this is the
# "nice-to-have, don't overinvest" pyobjc NSBundle trick to rename it.
# Guarded top to bottom: any failure (pyobjc missing, API shape differs,
# non-macOS) is swallowed silently and dev mode just keeps the python3 label.


def _rename_process_dev_mode() -> None:
    try:
        from AppKit import NSBundle

        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Magic Video Editor"
            info["CFBundleDisplayName"] = "Magic Video Editor"
    except Exception:
        pass


def main():
    import uvicorn
    import webview

    from .server import app as fastapi_app

    config.ensure_dirs()

    # Bug fix: the app could be launched a second time while one instance
    # was already running -- both would try to bind config.HOST/PORT and
    # share config.DATA_DIR (port-bind errors, duplicate ollama spawns,
    # project.json races). See single_instance.py for the detection
    # strategy (flock lockfile, health-probe fallback for network volumes).
    # Must run before anything binds the port or touches DATA_DIR.
    if single_instance.detect_existing_instance(config.DATA_DIR, config.HOST, config.PORT):
        print("Magic Video Editor is already running -- not opening a second window.")
        single_instance.focus_existing_instance()
        return
    atexit.register(single_instance.release_singleton)

    # Field bug fix (M2): mlx-whisper shells out to bare `ffmpeg`/`ffprobe`
    # from PATH internally, bypassing our ffmpeg_bin()/ffprobe_bin()
    # resolution entirely -- make sure PATH already points at the right
    # binaries before any pipeline stage can run. Mirrors server.py's
    # main(), which app.py bypasses by driving uvicorn itself (v0.6.1
    # lesson: app.py is the packaged entrypoint and must not depend on
    # server.main running). See ffmpeg_utils.
    ffmpeg_utils.export_binaries_to_path()

    # Field bug fix (auto-update first-relaunch ffprobe failure): after the
    # app updates itself and relaunches (packaging/update_helper.sh), the
    # FIRST launch of the newly swapped-in bundle could have ffprobe fail
    # (a Gatekeeper first-launch assessment / App Translocation race on the
    # just-moved bundle, and/or a cached path or PATH shim left pointing
    # into the PREVIOUS bundle) -- a manual quit + reopen always fixed it,
    # since by then the bundle/cache state had settled. This is the
    # self-heal: a filesystem-only check (never spawns the vendored
    # binary -- exists()+X_OK only) right after export_binaries_to_path(),
    # and if it's unhappy, one automatic re-resolution pass so the FIRST
    # post-update launch recovers on its own instead of needing a manual
    # restart. No-op on a healthy normal launch and in dev (see
    # ffmpeg_utils.ensure_binaries_healthy_at_startup()).
    if not ffmpeg_utils.ensure_binaries_healthy_at_startup():
        print(
            "[app] WARNING: ffmpeg/ffprobe binaries still not healthy after startup "
            "self-heal -- pipeline stages may fail until the app is fully quit and "
            "reopened"
        )

    # v6 packaging Option B (field bug fix): mirrors server.py's main(),
    # which app.py bypasses by driving uvicorn itself -- ensure_ollama() was
    # missing here entirely, so the packaged .app never spawned the bundled
    # `ollama serve` on a machine where Ollama.app was installed but not
    # running; it just silently sat in whatever mode config.OLLAMA_URL
    # happened to be in. Runs on its own background thread (never blocks
    # this window from opening) -- GET /api/health's ollama_mode reflects
    # "starting"/"downloading" progress until it settles.
    ollama_manager.ensure_ollama_async()

    server = uvicorn.Server(
        uvicorn.Config(fastapi_app, host=config.HOST, port=config.PORT, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    _wait_for_server()

    # v6 auto-update: non-blocking GitHub Releases check (mirrors
    # server.py's main(), which app.py bypasses by driving uvicorn itself).
    updater.start_check_async()

    _rename_process_dev_mode()

    geom = _load_geometry()
    width = geom.get("width") or _DEFAULT_SIZE[0]
    height = geom.get("height") or _DEFAULT_SIZE[1]
    x = geom.get("x")
    y = geom.get("y")

    window = webview.create_window(
        "Magic Video Editor",
        f"http://{config.HOST}:{config.PORT}/",
        js_api=Api(),
        width=width,
        height=height,
        x=x,
        y=y,
        min_size=_MIN_SIZE,
    )

    # App lifecycle (spec v6 PRINCIPLE): persist geometry on every
    # resize/move, and gate quitting on a running render.
    window.events.resized += _debounced_geometry_save(window, "resize")
    window.events.moved += _debounced_geometry_save(window, "move")
    window.events.closing += _on_closing

    # Our own View/Edit menus fully replace pywebview's stock ones (Fit
    # Timeline/Zoom In-Out and Undo/Redo/Split/Delete already cover the
    # native equivalents) -- without this, macOS would show a second,
    # redundant "Edit"/"View" menu alongside ours.
    webview.settings["SHOW_DEFAULT_MENUS"] = False

    if os.environ.get("MVE_TEST_AUTOCLOSE") == "1":
        # Verification hook only (native-shell smoke test): auto-destroy the
        # window after 10s so an automated launch can confirm boot + menu
        # construction without hanging a terminal. Never set in normal runs.
        t = threading.Timer(10.0, window.destroy)
        t.daemon = True
        t.start()

    webview.start(menu=_build_menu(window))


if __name__ == "__main__":
    main()
