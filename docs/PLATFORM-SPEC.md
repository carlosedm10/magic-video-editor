# CutRoom Platform Spec (v2 — "magic editor")

Owner-approved direction (2026-07-24). This doc is the shared contract for all
implementation agents. Read it fully before touching code.

## Product

Local-first AI video editor. Two halves:
1. **AI first pass** (exists): transcribe → sync → best takes → order → render → reels.
2. **Studio** (new): a basic Premiere-lite manual editor on top of the AI result —
   trim/reorder/delete segments, color filters, voice enhancement — plus Settings.

Everything runs locally: whisper (mlx), Ollama via pydantic_ai, ffmpeg, scipy/OpenCV.
LLMs only emit typed decisions; ffmpeg does all pixel/sample work.

## Brand / visual identity (from carloseduardo.es)

- Background: near-black with a **very dark navy** radial glow (`#05070d` → `#0a1020` range).
- Typography: white, bold, tight tracking for headings (system -apple-system stack is fine);
  soft gray (`#9aa3b2`) body.
- Components: dark "glass" pills and cards (subtle 1px borders `#1c2333`, blur, rounded-full nav).
- **Signature effect**: slow-moving, low-opacity (~0.10–0.18) flowing blobs/aurora of
  **garnet/maroon red** (`#7a1220`, `#a01828`, accents up to `#c22030`) drifting behind the UI
  on a canvas layer; the red must fade **through black** into the navy (no purple mixing).
  It should feel "magical/intelligent", never noisy. Respect `prefers-reduced-motion`.
- Accent for interactive elements: garnet red; success `#35c28f`; keep current dark scheme vars
  but re-tune to this palette.

## Per-task model strategy (Settings-driven)

`~/CutRoom/settings.json` (managed by `cutroom/settings.py`):

```json
{
  "default_model": "qwen2.5:14b",
  "task_models": {
    "take_judge": null,
    "transcript_cleaner": null,
    "clip_order": null,
    "reel_scorer": null
  },
  "whisper_model": "mlx-community/whisper-large-v3-turbo"
}
```

`null` = use `default_model`. `cutroom/agents/agents.py` exposes
`get_agent(task: str) -> Agent` which resolves the model per task at call time
(cache per (task, model) pair; settings changes apply without restart).
Rationale: judging single takes works on 7–8B; transcript cleanup, ordering and
reel scoring want 14B+. The UI Settings tab lets the user pick any pulled
Ollama model per task (`GET /api/ollama/models` proxies `/api/tags`).

## Smarter take analysis (transcript_cleaner agent)

Real recordings contain explicit restarts: "vale, vuelvo a empezar", "a ver, otra vez",
"wait, let me start over", plus trailing abandoned sentences before them. The current
fuzzy dedup misses these. New LLM pass (task `transcript_cleaner`) runs inside the
`takes` stage BEFORE fuzzy dedup:

- Input: numbered sentences (id, text) of one clip, in order, chunked ≤40 sentences
  with 5-sentence overlap between chunks.
- Output (flat schema, small-model friendly): `{cut_ids: list[int], reason: str}` where
  cut_ids are sentence numbers that are restart markers, abandoned takes superseded by
  a later retake, or pure filler asides. The agent must understand context/meaning
  (the retake may be worded differently) — this is semantic, not string matching.
- Marked sentences → `kept=False, reason="restart/abandoned take (AI)"`, still
  toggleable in the Takes tab. Fail-open: on agent error, skip the pass and log.

## Studio tab (manual editor, Premiere-lite)

- Timeline = ordered list of EDL segments (existing `build_edl` output persisted as
  `project["edl"]` once takes/order run; Studio edits THIS, render consumes it).
- Per segment: drag to reorder; trim start/end (±0.1s nudge buttons + direct numeric
  input); delete/restore; split at a time; preview (HTML5 video of the source clip
  seeked to segment start — use existing media endpoints).
- "Reset to AI cut" button regenerates from build_edl.
- Backend: `cutroom/api/edl.py` — GET/PUT `/api/projects/{pid}/edl` (validated),
  POST `/api/projects/{pid}/edl/reset`. `render.run` uses `project["edl"]` when present.

## Color filters

- Per-project `project["color"]` config: `{preset, brightness, contrast, saturation, temperature}`.
  Presets: none, bw, sepia, cinematic (teal-orange), vintage. Sliders -1..1 (0 default).
- Implementation `cutroom/pipeline/filters.py`: `build_vf(color_cfg) -> str` returning an
  ffmpeg filter chain (eq/hue/colortemperature/colorchannelmixer/curves). Render and reel
  renders prepend it to their vf chain via the hook in ffmpeg_utils.cut_segment(vf_extra=...).
- Live preview: `GET /api/projects/{pid}/preview-frame?clip_id&t&<color params>` returns a
  filtered JPEG (extract frame → ffmpeg with the same vf). UI shows before/after.

## Voice enhancement ("Enhance voice")

- Per-project toggle `project["audio_enhance"]` (bool) + applied on final render and reels.
- `cutroom/pipeline/audio_enhance.py`: `enhance(in_wav) -> out_wav` doing:
  1) noisereduce spectral gating (non-stationary), 2) high-pass 80 Hz,
  3) gentle presence lift ~3–5 kHz (+2–3 dB biquad, scipy), 4) de-esser optional skip,
  5) loudness normalize to −16 LUFS (pyloudnorm), peak-limit −1 dBTP.
- Integration: render pipeline extracts each segment's audio, enhances, muxes back
  (the hook: cut_segment already supports external audio_src — enhance produces a temp wav
  per segment or one pass over the concatenated audio; choose the simpler correct one).
- Deps: `noisereduce`, `pyloudnorm`, `soundfile` (add via uv).
- Endpoint to A/B: `POST /api/projects/{pid}/audio-preview` returns enhanced sample of 10s.

## Pipeline orchestration UX

- `POST /api/projects/{pid}/run-all` → ONE job running all stages sequentially with
  per-stage progress: job dict gains `stages: {name: {status: pending|running|done|error,
  progress: 0..1}}` (JobLog.stage() helper). Stage list/labels:
  ingest "Reading files", sync "Syncing cameras", transcribe "Transcribing",
  takes "Analyzing takes", order "Ordering the story", render "Editing the video",
  reels "Making shorts".
- UI: big "✦ Run pipeline" primary button (Files tab + header). While running, a
  progress panel shows one labeled bar per stage. Individual stage pills remain for re-runs.
- Reels stage failure must not kill the pipeline (it's last; mark error, continue reporting).

## Conventions (non-negotiable)

- Python 3.12, uv, ruff (line 100, select E,F,I,UP,B) — `make lint` and `make smoke` must pass.
- Vanilla JS, no build step; UI modules as separate files under `ui/` loaded via script tags.
- pydantic_ai agents: prompts in agents/prompts.py, flat single-purpose schemas in
  agents/schemas.py (NO nested/batch schemas — small models fail them), agents/agents.py.
- ffmpeg only through cutroom/ffmpeg_utils.py (`ffmpeg_bin()` handles the libass fallback).
- Never `git commit` — the orchestrator handles git.
- File ownership per task is strict (listed in each task brief) to allow parallel work.
