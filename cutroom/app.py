"""macOS app entrypoint: uvicorn in a background thread + a pywebview window.
Run `cutroom-server` instead if you prefer a plain browser at localhost."""

import socket
import threading
import time

from . import config


class Api:
    """Exposed to the web UI as window.pywebview.api — native file dialogs."""

    def pick_files(self):
        import webview

        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=(
                "Media files (*.mp4;*.mov;*.m4v;*.mkv;*.avi;*.mts;*.m4a;*.wav;*.mp3;*.aac;*.flac)",
            ),
        )
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

    webview.create_window(
        "CutRoom",
        f"http://{config.HOST}:{config.PORT}/",
        js_api=Api(),
        width=1440,
        height=920,
        min_size=(1100, 700),
    )
    webview.start()


if __name__ == "__main__":
    main()
