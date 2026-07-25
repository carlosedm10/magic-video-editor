"""macOS app entrypoint: uvicorn in a background thread + a pywebview window.
Run `mve-server` instead if you prefer a plain browser at localhost."""

import socket
import threading
import time

from . import config, ffmpeg_utils


class Api:
    """Exposed to the web UI as window.pywebview.api — native file dialogs."""

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


def _wait_for_server(timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((config.HOST, config.PORT), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("backend failed to start")


def main():
    import uvicorn
    import webview

    from .server import app as fastapi_app

    config.ensure_dirs()
    server = uvicorn.Server(
        uvicorn.Config(fastapi_app, host=config.HOST, port=config.PORT, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    _wait_for_server()

    window = webview.create_window(
        "Magic Video Editor",
        f"http://{config.HOST}:{config.PORT}/",
        js_api=Api(),
        width=1440,
        height=920,
        min_size=(1100, 700),
    )
    # Resource safety: no ffmpeg child must survive the window closing (spec:
    # "Resource safety" -- pywebview window-close must terminate the registry).
    window.events.closing += ffmpeg_utils.terminate_all
    webview.start()


if __name__ == "__main__":
    main()
