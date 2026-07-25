# ----------------------------- Setup ----------------------------- #
.PHONY: install models doctor setup

install:
	uv sync

# Usage:
#   make models                        # pull the default local LLM
#   make models MODEL=qwen2.5:14b      # pull a specific model
models:
	ollama pull $(or $(MODEL),qwen2.5:14b)

doctor:
	@command -v ffmpeg >/dev/null && echo "ffmpeg      ✓" || echo "ffmpeg      ✗  -> brew install ffmpeg"
	@command -v uv >/dev/null && echo "uv          ✓" || echo "uv          ✗  -> brew install uv"
	@command -v ollama >/dev/null && echo "ollama      ✓" || echo "ollama      ✗  -> https://ollama.com"
	@curl -s --max-time 2 http://localhost:11434/api/version >/dev/null \
		&& echo "ollama api  ✓" || echo "ollama api  ✗  -> run: ollama serve"

setup:
	make install
	make models
	make doctor

# ----------------------------- App ----------------------------- #
.PHONY: app server

app:
	uv run mve

server:
	uv run mve-server

# ----------------------------- Package Management ----------------------------- #
.PHONY: uv-lock uv-add uv-update uv-remove uv-lock-regenerate

# Usage:
#   make uv-add PKG="package[extras]==version"
#   make uv-update            # update all
#   make uv-update PKG=foo    # update specific package
#   make uv-remove PKG=foo
#   make uv-lock-regenerate   # refresh lock from scratch
uv-lock:
	uv lock

uv-add:
	uv add $(PKG)

uv-update:
ifeq ($(PKG),)
	uv lock --upgrade
else
	uv lock --upgrade-package $(PKG)
endif

uv-remove:
	uv remove $(PKG)

uv-lock-regenerate:
	uv lock --refresh

# ----------------------------- Distribution ----------------------------- #
.PHONY: dist dist-app dist-dmg dist-icon

# make dist: builds "Magic Video Editor.app" (PyInstaller, one-dir) then
# wraps it in a .dmg with a sha256 sidecar. Output: dist/
dist:
	make dist-app
	make dist-dmg

dist-icon:
	uv run python packaging/make_icon.py

dist-app:
	uv run pyinstaller packaging/mve.spec --noconfirm --clean --distpath dist --workpath build

dist-dmg:
	bash packaging/make_dmg.sh

# ----------------------------- Code Formatting ----------------------------- #
.PHONY: lint-backend format-backend lint format

lint-backend:
	uvx ruff check magic_video_editor/

format-backend:
	uvx ruff format magic_video_editor/
	uvx ruff check --fix magic_video_editor/

lint:
	make lint-backend

format:
	make format-backend

# ----------------------------- Debugging ----------------------------- #
.PHONY: health smoke

health:
	@curl -s http://127.0.0.1:8765/api/health | python3 -m json.tool \
		|| echo "server not running — start it with: make server"

smoke:
	uv run python -c "import magic_video_editor.server, magic_video_editor.app, magic_video_editor.settings, magic_video_editor.updater; \
		from magic_video_editor.api import projects, pipeline, settings, audio, filters, edl, suggestions, reels as reels_api, subtitles as subtitles_api, thumbs, ollama, overlays, updater as updater_api; \
		from magic_video_editor.pipeline import ingest, sync, transcribe, takes, ordering, render, reels, faces, filters as pfilters, audio_enhance, review, copywriter, subtitles; \
		from magic_video_editor.agents import agents; \
		print('all modules import OK')"

# ----------------------------- ⛔️ DANGER ZONE ⛔️ ----------------------------- #
.PHONY: clean reset-projects

# Removes the virtualenv (recreate with `make install`).
clean:
	rm -rf .venv

# Deletes ALL projects, transcripts, and renders under the app data dir
# (~/Library/Application Support/Magic Video Editor). Your original footage
# is never touched (clips are referenced in place).
reset-projects:
	rm -rf "$(HOME)/Library/Application Support/Magic Video Editor/projects"
