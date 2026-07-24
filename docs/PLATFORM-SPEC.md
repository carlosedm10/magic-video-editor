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

---

# v3 — "Magic Video Editor" (editor-first rebuild)

Owner direction (2026-07-24 evening). Supersedes conflicting v2 points.

## Naming
Product name: **Magic Video Editor** — "by carlosedm10" visible in the UI (sidebar/brand
area) and window title. Repo/package names unchanged.

## The mental model changed: it IS a video editor
Design like CapCut, not like a pipeline dashboard. The AI first pass is the "magic import"
that pre-builds your timeline; after that the user lives in a real editor:
- **Layout**: left = media bin (clips/camera groups); center = player with playhead,
  play/pause, time display, virtual preview of the CURRENT EDL (no render needed — seek
  the source <video> per segment, auto-advance across segments/files); right = inspector
  (Video / Color / Audio / Suggestions panels); bottom = **timeline**: horizontal track,
  clip blocks with width ∝ duration, ruler + zoom, playhead synced with player,
  drag-to-reorder, drag block EDGES to trim, split-at-playhead button, delete,
  per-junction transition chips. Toolbar: select, split, delete, undo (client-side
  history), zoom in/out. Keep the garnet/navy branding and fx aurora.
- Tabs (Takes / Reels / Settings / Activity) remain reachable but secondary (icon rail or
  top-right). The old "Files" flow becomes the media bin + an import dialog.
- The run-all progress panel must live OUTSIDE tab panes and stay visible whatever view is
  active while a pipeline job runs (v2 bug: it collapsed when switching tabs).

## Ingestion model: camera groups, not per-file cameras
Real projects = MANY loose clips of the SAME camera (session shared here: IMG_9232–9237).
- Import accepts files AND folders. A folder = one **camera group** (its clips share
  clip["camera_group"]); loose files default to group "main" unless assigned. One group is
  the main camera. UI: group headers in the media bin, "set main" per group.
- **Sync (cross-correlation) only runs BETWEEN different camera groups** — never between
  clips of the same group (same-camera clips are different takes by definition).
- Ordering/narrative works across the whole set of same-camera clips (this is the main
  case now): understand the full transcript, order clips coherently.

## Suggest, don't delete
Only auto-cut what is unambiguous: explicit restart/blooper patterns ("ay, me he vuelto a
equivocar. Bueno, continúo. Ahora sí que sí", "venga, va, otra vez") and their abandoned
takes — the existing transcript_cleaner behavior. Everything else that is merely suspect
(redundant across clips, repeated idea, off-topic tangent, doesn't fit the narrative)
becomes a **suggestion**, never a silent cut:
- New `reviewer` agent task (runs as its own stage after `order`): reads the full kept
  transcript across all clips and emits project["suggestions"] =
  [{id, kind: redundant|repeated_idea|off_topic|incoherent, refs: [sentence ids],
    message (user language!), proposed_action: cut|merge|reorder}].
- API: list / accept (applies the proposed cut/edit) / dismiss. UI: Suggestions panel in
  the inspector with accept/dismiss per card.
- Reviewer + cleaner default to the biggest configured model; per-task override in Settings.

## Transitions (junction-level)
EDL gains per-junction transitions: {type: none|fade|crossfade, duration<=1.5s} stored on
the segment that FOLLOWS the junction. Render implements: fade = fade-out/fade-in via
per-segment fade filters (cheap); crossfade = ffmpeg xfade between adjacent segment files
(re-encode at junctions only where feasible). Timeline UI: clickable chip between blocks.

## Resource safety (MANDATORY — the app froze a 48GB M4 Pro)
Root causes found: unlimited concurrent jobs per project (no 409 guard → repeated clicks
spawned parallel pipelines), each ffmpeg unbounded threads, no child-process registry (
orphaned ffmpeg survived Ctrl-C ~2min), no RAM guard.
- Global ffmpeg **semaphore** (settings.performance.max_parallel_ffmpeg, default 2).
- `-threads` cap per encode: settings.performance.ffmpeg_threads, default max(2, cores//2).
- All ffmpeg children spawned via a tracked Popen wrapper (registry); SIGINT/SIGTERM/atexit
  and pywebview window-close terminate the whole registry (terminate → kill after 5s).
- Per-project job lock: starting a stage/run-all while one runs → HTTP 409; UI disables the
  Run button while running and shows a **Stop** button (job cancel endpoint: cooperative
  flag + terminate that job's registered children).
- RAM guard via psutil: before spawning a heavy step, if available RAM < settings guard
  (default 4GB) wait/queue with a log line instead of spawning.
- Prefer graceful degradation: slower renders are fine; freezing the Mac is not.

## Settings placement (owner request, 2026-07-24)
Settings access lives at the BOTTOM of the app: a gear icon/button pinned to the bottom of
the left sidebar (media bin column), like typical desktop apps — NOT in the top icon rail.
Clicking it opens the existing Settings view (overlay/drawer). The health line (ffmpeg/
ollama/model) sits next to it at the bottom.

## Known regression to verify (observed mid-build)
Opening the secondary views (Takes/Reels/Settings/Activity overlay/drawer) rendered an
EMPTY panel. Integrator + UI verification must confirm each overlay actually mounts its
module content (TABS.* renderers get a valid container) and Close restores the editor.
