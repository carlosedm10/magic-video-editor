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

`~/CutRoom/settings.json` (managed by `magic_video_editor/settings.py`):

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

`null` = use `default_model`. `magic_video_editor/agents/agents.py` exposes
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
- Backend: `magic_video_editor/api/edl.py` — GET/PUT `/api/projects/{pid}/edl` (validated),
  POST `/api/projects/{pid}/edl/reset`. `render.run` uses `project["edl"]` when present.

## Color filters

- Per-project `project["color"]` config: `{preset, brightness, contrast, saturation, temperature}`.
  Presets: none, bw, sepia, cinematic (teal-orange), vintage. Sliders -1..1 (0 default).
- Implementation `magic_video_editor/pipeline/filters.py`: `build_vf(color_cfg) -> str` returning an
  ffmpeg filter chain (eq/hue/colortemperature/colorchannelmixer/curves). Render and reel
  renders prepend it to their vf chain via the hook in ffmpeg_utils.cut_segment(vf_extra=...).
- Live preview: `GET /api/projects/{pid}/preview-frame?clip_id&t&<color params>` returns a
  filtered JPEG (extract frame → ffmpeg with the same vf). UI shows before/after.

## Voice enhancement ("Enhance voice")

- Per-project toggle `project["audio_enhance"]` (bool) + applied on final render and reels.
- `magic_video_editor/pipeline/audio_enhance.py`: `enhance(in_wav) -> out_wav` doing:
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
- ffmpeg only through magic_video_editor/ffmpeg_utils.py (`ffmpeg_bin()` handles the libass fallback).
- Never `git commit` — the orchestrator handles git.
- File ownership per task is strict (listed in each task brief) to allow parallel work.
- **HARD RULE — versioning: NEVER tag `v1.0.0` (or any `v1.x`/`V1`). Stay sub-1.0 forever.**
  Bump freely within 0.x (0.8.1, 0.9.0, … even 0.12.21 — the minor/patch numbers are
  unbounded), but the `1.0` milestone is released ONLY when the owner (carlosedm10) explicitly
  says so. No agent or orchestrator may cut a v1 tag on its own initiative.

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

---

# v4 — Pro editor parity + AI coherence (owner direction 2026-07-24 late)

Research base: FCP window anatomy (sidebar/browser left, viewer center, INSPECTOR with
multiple tabs right, timeline with filmstrips+waveforms bottom), Premiere render bar +
preview files + proxy workflow, standard timeline interactions (JKL, I/O, ripple delete,
snapping, markers), CapCut auto-captions with style templates. Apply these patterns.

## 1. AI coherence v2 (top priority — the cut still ships duplicates and bloopers)
- **Cross-clip semantic dedup with AUTO-cut**: not all clips are good; several clips say
  the same thing in different words. New pass in `takes` AFTER the per-clip cleaner:
  candidate pairs = sentence pairs across DIFFERENT clips with fuzzy similarity 55–100 or
  sharing rare keywords; agent task `dedup_judge` sees both sentences WITH 1 sentence of
  surrounding context each and answers flat: {same_content: bool, keep: "a"|"b",
  confidence: 1-5, reason}. confidence>=4 and same_content -> AUTO-cut the loser
  (reason "duplicate content across clips (AI)"); confidence 2-3 -> create a suggestion
  instead. Duplicates are allowed only when clearly rhetorical emphasis (the agent is
  told this exception).
- **Blooper hunt v2**: the cleaner must ALSO catch mistake-reaction comments and
  out-of-context asides: "ay, me he equivocado", "otra vez", "esto no", "¿cómo se dice?",
  "se me ha ido", "espera", laughing at oneself, greetings restarted mid-video, camera
  checks. Give the cleaner the VIDEO TOPIC first (one-line summary produced by a cheap
  agent call over the full transcript) so "out of context" is judgeable per sentence.
  Two passes per clip: (1) restart/blooper pass (existing, expanded), (2) context pass
  ("does this sentence belong in a video about <topic>?"). Stay conservative on content;
  be AGGRESSIVE on meta-comments about the recording itself.
- Verify against the REAL project c7642fc7755e transcripts (copy the project first,
  work on the copy) and report precision/recall by hand-checking the decisions.

## 2. Job queue (replace reject-with-409 for user actions)
- New `magic_video_editor/queue.py`: per-project FIFO persisted in project.json ("queue": [...]),
  one global worker thread that executes queue items sequentially per project (parallel
  across projects only within resource limits). Item kinds: stage:<name>, run-all,
  preview_render, final_render, reel_render:<id>, proxies, thumbs.
- API: POST enqueue (replaces direct run for UI), GET queue, DELETE queue item, POST
  reorder. The old direct endpoints stay for compat but enqueue too.
- **Auto-enqueue rules**: after run-all completes -> thumbs + top-5 reel renders; after
  any EDL/color/subtitles/audio change -> debounced (5s) preview_render; ingest of new
  clips -> proxies + thumbs. Everything respects the ffmpeg semaphore + RAM guard.
- Activity view becomes **Queue**: pending/running/done items with cancel per item.

## 3. Preview-render workflow (the pro pattern the owner described)
- Timeline shows a Premiere-style **render bar** above the blocks: garnet/red = the
  current EDL+effects differ from the last preview render; green = preview up to date.
- `preview_render` job: 540p, crf 32, preset ultrafast, all effects INCLUDED (color,
  transitions, subtitles, audio enhance) -> <project>/preview/preview.mp4 + a manifest
  hash of (edl+color+subtitles+audio) it corresponds to.
- Player gets a mode toggle: **Draft (virtual)** | **Preview (rendered)**. Auto-switch to
  Preview when the manifest hash matches current state; fall back to Draft otherwise.
  Draft mode approximates transitions live (CSS opacity crossfade between the two
  stacked <video> elements at junctions; fade via to-black overlay) and shows subtitles
  as a DOM overlay from transcript timings — no render needed.
- final_render = existing render stage; writes to the export dir (see 5).

## 4. Editor UX to pro parity
- **Inspector (right) becomes TABBED like FCP**: tabs Video | Color | Audio | Subtitles |
  FX | Ideas(suggestions). Icons + labels, garnet active underline. Video tab = segment
  in/out/duration + per-junction transition. FX tab = transition defaults + (future)
  effects list. Ideas = suggestions cards.
- **Before/after comparison ON the viewer**: when the Color tab is active, a vertical
  divider handle appears mid-viewer; left half shows original, right half shows graded
  (CSS filter approximation live: brightness/contrast/saturate/sepia/hue-rotate mapped
  from the color config; exact result comes from the preview render). Draggable divider.
- **Timeline pro**: filmstrip thumbnails INSIDE blocks (backend sprite: 1 frame per ~2s
  at 90px height per clip, generated by the thumbs job, drawn via background-position);
  a thin audio waveform strip at block bottom (peaks json from thumbs job); snapping
  (toggle, S) to playhead/edges; ripple delete (delete closes the gap — our EDL is
  gapless so delete already ripples; label it); J/K/L shuttle (reverse not required:
  K pause, L play/2x on repeat, J rewind-jump 2s), I/O set in/out trim of selected
  segment at playhead; M adds a marker (stored in project, shown on ruler); render bar
  strip (see 3).
- **Media bin**: clips draggable INTO the timeline (drag = append segment of full clip
  at drop position). Projects stay in the top switcher.
- Keep keyboard map visible via a small "?" shortcuts popover.

## 5. Settings = full-screen page (NOT a drawer)
- Full-screen overlay page with left section nav: General | Models | Performance |
  Transcription | About. Large, calm, plenty of whitespace.
- **General**: export destination folder — settings.export_dir (default
  ~/Movies/Magic Video Editor), native pick_folder button + editable path; final renders
  and reels are WRITTEN there under <project name>/; "Open folder" button (opens Finder
  via a small API endpoint using `open`).
- **Models**: existing default+per-task dropdowns PLUS an **Ollama model manager**:
  search field querying our backend proxy of the ollama.com library (scrape/parse
  https://ollama.com/search?q=...; cache 1h; fallback to a curated static catalog of
  qwen2.5/llama3.x/gemma/mistral entries when offline), each result shows name, sizes,
  and a compatibility badge computed from psutil total RAM (size_gb <= ram*0.5 "Runs
  great", <= ram*0.75 "Tight", else "Too big"); an Install button POSTs /api/ollama/pull
  (streams ollama /api/pull progress into a queue job with progress bar); installed
  models listed with size + delete button (ollama delete). Short guide text.
- **About/System**: correct product name "Magic Video Editor by carlosedm10" (the
  current System card wrongly reads as CutRoom), version, data dir, health checks.
- Settings gear stays bottom-left but opens this full-screen page.

## 6. Subtitles tool (CapCut-style)
- project["subtitles"] = {enabled, style: clean|bold|karaoke, font (from a curated list
  of macOS system fonts: Helvetica Neue, Arial Black, Futura, Impact, Avenir Next,
  SF Pro if available), size (S/M/L), color, outline_color, position: bottom|center,
  words_per_cue (default 4)}.
- Live preview: DOM overlay on the player driven by transcript word timings mapped
  through the EDL (both Draft and the subtitle styling in the Subtitles inspector tab
  update it instantly).
- Burn-in: the .ass generator (exists for reels) becomes shared
  `magic_video_editor/pipeline/subtitles.py`, parameterized by the style config; final render and
  preview render burn it per segment (cue times re-based per segment); reels reuse it.
- Inspector Subtitles tab: enable toggle, style preset chips with mini-previews, font
  dropdown, size, colors, position, words-per-cue.

## Constraints reminder
Same conventions as v2/v3 (ruff, vanilla JS, PromptedOutput flat schemas, ffmpeg only via
ffmpeg_utils, never git commit, strict file ownership). UI must keep the garnet/navy
brand. All heavy work goes through the queue + semaphore. make lint && make smoke green.

---

# v5 — Reel Editor (owner request 2026-07-24 late-night)

When the user opens a reel for editing, the REEL EDITOR replaces the main editor view
(full takeover, "← Back to project" to return). Scope: fix framing, extend/trim the cut,
hand-edit subtitles, restyle, re-render THAT reel.

## Data model (per reel, all optional overrides)
reel gains: {in_override, out_override (clip-local seconds; may EXTEND beyond the AI cut,
clamped to clip bounds), crop_x (0..1 horizontal center of the 9:16 window; falls back to
the face-detected center), cue_overrides: {cue_index: text}, subtitle_style: partial
override of project["subtitles"], status: suggested|edited|rendered}.
API: PATCH /api/projects/{pid}/reels/{rid} accepting these; POST .../reels/{rid}/render
(existing) honors all overrides; render enqueues via the queue.

## UI (reuse editor chrome, scoped)
- Center: 9:16 preview (virtual playback of the source clip between in/out, using the
  preview proxy). FRAMING: overlay showing the 9:16 crop window over the full 16:9 frame
  (dimmed sides); drag horizontally to set crop_x; the preview crops live via CSS
  (object-fit/translate math).
- Bottom: single-segment mini-timeline with in/out edge handles that CAN extend beyond
  the original AI window (show the AI cut as a subtle highlight inside the full clip
  strip; filmstrip thumbnails from the existing sprite).
- Right inspector tabs: Reel (in/out numbers, duration, title editable, score readonly),
  Subs (cue LIST with per-cue editable text inputs — typo fixes — plus the style
  controls, overriding project defaults for this reel), Export (Render button -> queue
  reel_render, shows last rendered file + play).
- Entry points: Reels drawer card "Edit" button; after render, "Edit" stays available.
- Keyboard: same transport keys; Esc = back to project.

Backend notes: render_reel must use in/out overrides for cutting, crop_x for the crop
filter (bypass face detection when set), cue_overrides + merged style for the .ass.
Subtitle cues for the reel are word-timed from the transcript within [in,out] as today,
then text-overridden by index.

## v5 addendum — Timeline UX fixes (owner feedback on v3/v4 timeline)
1. **Left-edge trim visual anchor**: dragging a block's LEFT edge must visually extend/
   shrink the block's START (left edge moves, right edge stays put on screen). Today the
   math is right but the block visually grows at the END — the block must stay anchored
   by its right edge during a left-edge drag (adjust the layout offset live during the
   drag, not only on commit).
2. **Undo history panel**: a visible edit-history log (Premiere-style): every EDL-affecting
   action pushes a labeled entry ("Trim IMG_9234 start", "Reorder", "Split", "Delete",
   "Transition crossfade"...) into a capped 30-entry history; a small clock icon in the
   timeline toolbar opens the list; clicking an entry restores that state (undo/redo
   keyboard continues to work and stays in sync with the panel).
3. **Zoom-out must fit everything**: minimum zoom = fit ALL clips in the visible strip
   with ~20% spare room (compute from total EDL duration / viewport width), plus a
   "Fit" button that jumps to that level. Zoom slider range adapts per project.

## v5 addendum — pydantic_ai native OllamaModel migration (owner request)
Replace the OpenAIChatModel+OllamaProvider construction in magic_video_editor/agents/agents.py with
the NATIVE `pydantic_ai.models.ollama.OllamaModel` (exists in our installed 2.17; it
subclasses OpenAIChatModel so behavior is compatible). Additionally switch structured
output from PromptedOutput to `NativeOutput(schema)` — self-hosted Ollama >=0.5 enforces
response_format json_schema via llama.cpp grammar-constrained decoding (see the
OllamaModel docstring), which guarantees schema-valid output at generation time and is
strictly better than the PromptedOutput workaround we adopted when tool-calling failed.
MUST be verified live against every registered agent task (take_judge, transcript_cleaner,
video_topic, context_check, dedup_judge, clip_order, reel_scorer, reviewer) with the
settings default model before landing; if any task misbehaves under NativeOutput, keep
PromptedOutput for that task only, with a comment.

## v5 addendum — export filenames (owner request)
Exported files must be named by their TITLE, not timestamps/ids:
- Final render -> "<project name>.mp4" (the project name acts as the video title;
  sanitize for filesystem: strip /:\\ etc., collapse spaces; if the file exists,
  append " (2)", " (3)"...).
- Reel render -> "<reel title>.mp4" (same sanitization/dedup; fall back to
  "Reel <rank>" when the title is empty). Applies to files written to the export dir;
  internal work files keep their ids.

## v5 addendum — SEO copywriter + brand profile (owner request)
Current reel titles are inadequate. Introduce a dedicated copywriting layer:
- **Brand profile**: settings.brand_profile — a free-form plain-text field (Settings gets
  a "Brand" section with a large textarea + short helper copy) where the user describes
  their brand, YouTube channel, audience, tone, links, recurring hashtags, CTAs. Passed
  verbatim to the copywriter agent.
- **copywriter agent task** (flat schema {title, description, hashtags}): input = the
  reel's (or full video's) transcript text + video topic + brand profile + target
  platform hint (reel -> TikTok/Reels/Shorts, video -> YouTube). Output IN THE CONTENT'S
  LANGUAGE: a scroll-stopping SEO/viral title (<=70 chars, no clickbait lies), a
  description written for search+watch-through (keywords early, line breaks, CTA aligned
  with the brand profile, 2-5 relevant hashtags at the end). Titles must reflect actual
  content.
- **Where**: reels stage, after picking the top N: copywriter generates title+description
  per reel (replaces the reel_scorer title; scorer keeps only scores). Project-level:
  a "Publish" info block (video title suggestion + description for the main cut),
  generated on demand via a button and stored as project["publish"].
- **UI**: reel cards show title + collapsible description with a copy-to-clipboard
  button; a "Regenerate copy" button per reel; the Reel Editor's Reel tab includes
  editable title/description fields (manual override wins, stored on the reel).
- Export filenames (previous addendum) use the copywriter title.

---

# v6 backlog — macOS packaging (.app/.dmg) (owner request 2026-07-25)
Goal: send the app to other Macs, 100% local.
- PyInstaller (preferred; py2app fallback) -> "Magic Video Editor.app" bundling python,
  pywebview, all wheels incl. the imageio-ffmpeg static binary; `make dist` target builds
  the .app and wraps it in a .dmg (hdiutil/create-dmg, Applications symlink).
- First-run onboarding: doctor screen — if Ollama is missing, guide install (link) and
  offer the in-app model manager to pull a starter model; whisper model auto-downloads.
- Phase 2 option: bundle the ollama binary (MIT) and manage `ollama serve` as a child
  process for a true single-artifact install.
- Signing: unsigned initially (document right-click→Open / xattr for recipients);
  Developer ID + notarization later if distribution gets serious.
- Intel Macs: mlx unavailable -> faster-whisper fallback path must stay working.

## v6 packaging — Option B CONFIRMED + auto-update (owner decisions 2026-07-25)
- **Bundle Ollama**: ship the ollama binary (MIT license — include its LICENSE) inside the
  .app; the app manages `ollama serve` as a child process when no external Ollama is
  reachable on the configured URL (prefer an already-running system Ollama; fall back to
  the bundled one with OLLAMA_MODELS under the app data dir). Bundled serve must join the
  ffmpeg child-registry-style lifecycle (terminate on quit).
- **Auto-update via GitHub Releases**: on launch (non-blocking), GET
  api.github.com/repos/carlosedm10/magic-video-editor/releases/latest; if semver > local
  __version__, show an update banner; "Update now" downloads the release .dmg asset,
  verifies its sha256 (published as a release asset), mounts/copies the new .app over the
  current one (helper script handles the swap + relaunch), with user confirmation.
  Sparkle is the future option once code signing exists — note in README.
- **Release pipeline**: make dist builds .app (PyInstaller) + .dmg + sha256; a GitHub
  Actions workflow on tag push builds and attaches assets to the release.
- ~~Configurable inference endpoint~~ — DROPPED (owner decision 2026-07-25): Ollama is
  the one and only inference path. Do not build llama-server/MLX endpoint switching.

## v5.1 — Model manager fixes (owner feedback on the live UI, 2026-07-25)
The library search currently shows junk: every tag reads "latest / size unknown", no
descriptions. Fix properly:
1. **Real tag data**: the ollama.com search page lacks sizes — enrich results by fetching
   each model's tags page (https://ollama.com/library/<name>/tags) lazily (on result
   expand, or eagerly for the first ~6 results), parsing tag names + sizes (e.g. 7b 4.7GB,
   14b 9.0GB); cache per model 24h. Show the model DESCRIPTION from the search page (the
   parser currently drops it). Filter out obviously exotic/non-LLM entries when a query is
   empty (default view = curated popular list, not whatever the scrape returns first).
2. **Hardware-aware recommendation block** at the top of "Get more models": read the
   machine specs (psutil RAM + sysctl machdep.cpu.brand_string for the chip name) and
   show "Tu Mac: <chip>, <ram>GB" plus two one-click picks from a curated ranking:
   - "Best overall" — strongest general model that STILL FITS (<= ram*0.5): e.g. 48GB ->
     qwen2.5:32b (or qwen3 equivalent if listed), 24-32GB -> qwen2.5:14b, 16GB ->
     qwen2.5:7b-instruct, 8GB -> llama3.2:3b.
   - "Optimal for this Mac" — best speed/quality balance (one tier below best): 48GB ->
     qwen2.5:14b, etc.
   Each with an Install button (existing pull flow) and a one-line why. The curated
   ranking lives in code with a comment (structured-output-friendly models preferred —
   they must handle NativeOutput/JSON well, which qwen2.5 does).

## v5.2 — Projects home dashboard (owner request, 2026-07-25)
Before entering the editor there is a PROJECTS HOME view (the app's landing screen when
no project is open, replacing the current bare empty-state; a "home" button in the top
bar returns to it):
- Grid of project cards: name (inline-renamable), a poster thumbnail (first clip's
  filmstrip frame via the existing thumbs endpoint; placeholder gradient when missing),
  clip count, total footage duration, created date.
- **Delete project** on each card (danger style, confirm dialog "This deletes transcripts
  and renders, not your original footage"). DELETE endpoint already exists.
- **Processing level** (AUTOMATIC, derived from stages): "Por empezar" (no stages done),
  "En proceso" (some done or queue busy), "Finalizado" (render done) — shown as a badge.
- **Project status** (MANUAL, user-set for organization): todo | in_progress | done |
  uploaded — a small dropdown/chips on the card, persisted as project["workflow_status"]
  (new PATCH field on the project update endpoint). Cards filterable by this status
  (filter chips row on top: All / To do / In progress / Done / Uploaded).
- Sidebar project switcher stays for quick jumps; "New project" lives prominently here.

## v5.3 — Media import UX: drag & drop + real file picker (owner request)
The paste-a-path input is a dev artifact — replace it as the browser-mode fallback:
1. **Drag & drop from Finder** onto the media bin (and onto the Projects Home card area
   for the open project): accept files AND folders (webkitGetAsEntry traversal; a dropped
   folder = one camera group named after it). Browser drops don't expose absolute paths,
   so add a streaming multipart upload endpoint POST /api/projects/{pid}/upload
   (multiple files, folder-relative names, writes straight into <project>/media/ and
   registers clips exactly like add_clips; show per-file upload progress in the bin).
   Big-file friendly (GB iPhone clips over loopback are fast; stream to disk, never
   buffer whole files in memory).
2. **Native-style picker in browser mode**: hidden <input type="file" multiple
   accept="video/*,audio/*"> for + Files and <input webkitdirectory> for + Folder,
   feeding the same upload endpoint. In pywebview mode keep the true native dialogs
   (no copy needed — hardlink import stays).
3. Dropzone affordance: dashed highlight on dragover with "Drop clips or a camera
   folder"; remove the paste-path input from primary UI (keep it hidden behind a tiny
   "add by path" link for power users).

## v5.4 — Activity & Export UX redesign (owner feedback: the Queue view is confusing)
The current Queue drawer mixes launch buttons + queue list + raw logs. Separate the three
concerns the way FCP/Premiere do:
1. **Actions move to an Export flow**: a prominent "Export" button top-right (replaces
   the render buttons inside the queue view) opening a small EXPORT DIALOG: radio choices
   (Final video / All reels / A specific reel dropdown), destination folder line (from
   settings, click = open folder), and one primary "Export" CTA that enqueues and closes.
   "Render preview" is contextual only (render-bar click / preview toggle), never a
   drawer button.
2. **Monitoring becomes ambient**: a compact ACTIVITY CHIP in the top bar — invisible
   when idle; while busy shows spinner + current task label + % (e.g. "Rendering final…
   38%"). Click = FCP-style **Background Tasks popover** (anchored, ~380px): running item
   with progress + cancel, pending list (reorder/remove), last 3 finished (subtle). No
   full-page destination.
3. **Logs are two clicks away**: each task row in the popover has a "details" disclosure
   expanding an inline capped-height monospace log. The raw log wall is never the default
   view.
4. The "Queue" drawer tab disappears (Takes and Reels drawers stay). The persistent
   run-all per-stage progress strip stays as-is (it's good) — the chip complements it for
   non-pipeline tasks.

## v5.5 — Lucide icons (owner request)
Replace ad-hoc emoji/unicode icons across the UI with Lucide (vanilla UMD build, ISC
license) — ALREADY VENDORED at ui/vendor/lucide.min.js (v1.26.0, loaded first in
index.html; fully local, no CDN at runtime). Usage: <i data-lucide="name"> + a shared
helper that calls lucide.createIcons({attrs:{width:16,height:16}}) after every render
pass (core.js exposes it; each module calls it after injecting HTML). Swap at minimum:
toolbar (scissors=split, trash-2, undo-2/redo-2, history=history panel, magnet=snapping,
maximize-2=fit), transport (play/pause/skip), top bar (sparkles=run pipeline, download=
export, settings gear, home), media bin (film, folder-plus, file-plus), inspector tabs
(video, palette, volume-2, captions, wand-2, lightbulb), queue/activity chip (loader-2
spinning), reels (clapperboard), close (x). Keep sizes consistent (16px chrome, 18px
transport) and inherit currentColor so the garnet/dim palette applies.

## v6 addendum — native macOS menu bar & app identity (owner note)
Real editors populate the native menu bar; ours must too:
1. **App identity**: the menu bar currently reads "python3". In the packaged .app,
   Info.plist CFBundleName/CFBundleDisplayName = "Magic Video Editor" (+ app icon .icns
   from the brand mark) fixes it. In dev mode, best-effort process rename via pyobjc
   (NSBundle infoDictionary CFBundleName trick) — nice-to-have, don't overinvest.
2. **Native menus via pywebview's menu API** (webview.start(menu=[...])): App menu
   (About Magic Video Editor -> opens Settings/About, Check for updates -> update flow,
   Quit), File (New Project cmd+N, Import Files… cmd+I, Import Folder…, Export… cmd+E ->
   opens the Export dialog), Edit (Undo cmd+Z, Redo shift+cmd+Z, Split S, Delete),
   View (Fit Timeline, Zoom In/Out, Takes, Reels, Projects Home), Help (Shortcuts,
   GitHub repo). Menu actions dispatch into the UI through window.evaluate_js calling a
   single JS entrypoint (window.MenuBus.dispatch(action)) so the web app stays the source
   of truth. Menus must no-op gracefully when no project is open.

## PRINCIPLE — app-first, not web-first (owner directive 2026-07-25)
Magic Video Editor is a macOS APP that happens to render its UI in a webview — never
design for "the browser" as the primary surface. Consequences, binding for all agents:
- The pywebview native path is the PRIMARY experience: native file/folder dialogs,
  native menu bar (v6 addendum), dock presence, window title. Browser access stays a
  dev/debug convenience only — fallbacks (v5.3 upload path etc.) must work but never
  drive design decisions or add browser-ish chrome.
- No web-page patterns: no page reload as flow, no visible URLs, no external links
  opening inside the app window (use the OS default browser via pywebview), context
  menus should feel native (disable the default webview right-click menu where it leaks
  "Reload"-style items).
- App lifecycle: quitting with renders in progress warns ("A render is running — quit
  anyway?"); window size/position persist across launches (pywebview save/restore);
  the fullscreen behavior of the player must use the window, not browser fullscreen
  quirks.
- Dock: app icon (v6), and while rendering set the dock badge/progress when pywebview
  exposes it (best-effort via pyobjc; skip quietly if brittle).

## v5.6 — AI take-quality v3 (owner: still missing repeated bad takes + stuck-run muletillas)
The highest-value task. Three levers, all mandatory:
1. **Few-shot prompts**: transcript_cleaner and the new take_sequencer prompts MUST include
   2-3 worked Spanish examples showing input sentences -> expected cuts (including the
   exact patterns the owner reports: halting fragments, then "venga ya"/"ahora sí"/
   "vamos"/"va"/"ya está"/"perfecto, sigo", then the clean retake).
2. **New take_sequencer pass** (runs in takes AFTER the per-sentence cleaner, BEFORE
   fuzzy dedup): sliding windows of ~12 consecutive sentences per clip (2-sentence
   overlap) WITH timestamps and inter-sentence gaps. Flat-ish output {cut_runs:
   [{start_id, end_id, reason}]} targeting: (a) runs of consecutive failed attempts at
   the same line (halting, restarted, incomplete) that end in a self-encouragement
   marker and are followed by the good take — cut the WHOLE run including the marker,
   keep the final take; (b) the same-line-repeated-many-times pattern even without a
   marker (keep the best/last). Conservative outside those patterns.
3. **Model power for analysis tasks**: pull qwen2.5:32b (fits 48GB) and set task_models
   overrides for transcript_cleaner, take_sequencer, dedup_judge to it (settings-level,
   user-changeable); everything else stays on the default 14b for speed.
4. **Eval harness** (decides prompt-vs-model with data): scripts/eval_takes.py — a small
   labeled fixture built from the REAL c7642fc7755e transcripts (copy sentences into the
   fixture; hand-label expected cut ids for the known bloopers/stuck runs), runs the
   cleaner+sequencer against any model, prints precision/recall per category. Run it for
   14b vs 32b and report both in the final report. Future prompt changes must keep the
   eval green.

## v5.7 — Professional color pipeline + LUT import (owner request; researched)
Replace the ad-hoc 4-slider color config with the industry-standard control set and
STANDARD algorithms (our bundled ffmpeg has every needed filter — verified: exposure,
colortemperature, colorbalance, colorlevels, eq, curves, unsharp, vibrance, lut3d,
haldclut). Do NOT invent math; map 1:1 to these filters.

**Controls (Lightroom/Premiere "Basic" panel set) and their ffmpeg mapping, applied in
the standard correction order:**
1. Exposure (EV, -3..+3) -> `exposure=exposure=EV`
2. White balance: Temperature (-1..1 mapped to ~3500K..8500K) -> `colortemperature`;
   Tint (green<->magenta, -1..1) -> `colorbalance` magenta/green axis (mid tones)
3. Tone: Black point / White point (0..0.5 input levels) -> `colorlevels=rimin/gimin/
   bimin` and `rimax/...`; Brightness + Contrast -> `eq`
4. Presence: Saturation -> `eq=saturation`; Vibrance (smart saturation protecting skin)
   -> `vibrance`
5. Sharpness (0..1) -> `unsharp=5:5:amount` (classic unsharp mask; amount 0..1.5)
6. LOOK (last): imported LUT (see below) with an Intensity slider — implement blend
   with the original via split+lut3d+blend all_opacity, the standard "LUT strength"
   trick.

**LUT import — the interchange format the owner asked about**: `.cube` (Adobe/IRIDAS 3D
LUT) is THE cross-app standard (Premiere, Final Cut, Resolve, Lightroom exports, and the
entire free/paid LUT marketplace share it). Support: import .cube (and .3dl via lut3d,
.png haldclut) — file picker + drag&drop into the Color tab; store LUTs in
~/CutRoom/luts/ (settings-level library, reusable across projects); per-project selected
LUT + intensity persisted in project["color"]["lut"]. Preview via the existing
preview-frame endpoint (must accept the new params) and the CSS-approximation divider
keeps approximating only the basic controls (LUT preview requires the server frame —
show a note in the compare overlay when a LUT is active).

**Presets** stay (B&W/Sepia/Cinematic/Vintage) but become parameter presets over the new
controls (+optional built-in LUTs later). Migration: old {preset, brightness, contrast,
saturation, temperature} configs map into the new schema on read (defaults for new
fields). The Color inspector tab groups controls like Lightroom: WB / Tone / Presence /
Detail / Look, each slider with numeric value + double-click-to-reset.

## v5.8 — Reel playhead, multi-segment viral reels, speaker diarization (owner requests)

### a) Reel Editor playhead (bug/UX)
The reel mini-timeline lacks a playhead: add a vertical time cursor over the filmstrip
tracking playback position in real time (+ current-time label), click/drag on the strip
seeks, and the in/out handles show their timecodes while dragging. Same garnet playhead
styling as the main timeline.

### b) Multi-segment viral reels ("the podcast case")
A reel is no longer one continuous window — it becomes a LIST of segments (same shape as
the EDL) with per-junction transitions:
- Data: reel["segments"] = [{clip_id, start, end}], reel["transitions"] (junction list,
  default crossfade 0.4s). Single-window reels migrate to a 1-segment list on read.
- **Viral composer**: after the existing single-window scoring, a composer agent pass
  looks for PAIRS of high-scoring, semantically-connected windows separated in time
  (same idea continued later, setup+payoff, question+answer) — flat task
  {combine: bool, why, order: "ab"|"ba"} over candidate pairs pre-filtered by embedding/
  keyword overlap among the top ~15 windows (cap ~10 agent calls). Combined candidates
  get a bonus and enter the ranking as multi-segment reels (marked "compuesto" in the
  UI card).
- Render: reuse the main-cut segment renderer (cut each segment with the reel crop/subs,
  then crossfade junctions like the main render does).
- Reel Editor: the mini-timeline shows all segments of the reel side by side (each with
  its own in/out handles), junction transition chips, and "add segment" (pick any moment
  from the source clip strip).

### c) Speaker diarization for subtitles (two voices / podcast)
Detect and label speakers, fully local:
- Pipeline: new optional pass in transcribe stage — voice embeddings per transcript
  segment via **resemblyzer** (MIT, pip, d-vector embeddings; no gated models) over the
  analysis wav, then agglomerative/spectral clustering with auto speaker-count estimate
  (2-4 cap; merge below similarity threshold). Store segment["speaker"] = "S1"/"S2"...
  and project["speakers"] = [{id, label (editable), color}].
- **The user declares the speaker count** (owner refinement): a project-level field
  "Locutores" (1 / 2 / 3 / 4 / auto) asked in the project setup UI (media bin header or
  first-run of the pipeline; default 1). When N>1 the clustering runs with K=N fixed —
  a KNOWN speaker count makes diarization far more reliable than estimating it. "auto"
  falls back to the variance heuristic + estimated K. N=1 skips the pass entirely.
- Subtitles: per-speaker styling — color per speaker (assignable in the Subs tab, with
  editable names shown as "Name:" prefix optional toggle); the .ass generator emits one
  style per speaker; the live overlay tints accordingly.
- Takes/reviewer prompts receive speaker labels so cross-speaker "repetition" (host
  echoing guest) is NOT treated as a duplicate take.

## v5.9 — Resizable layout + manual overlay track (owner requests)

### a) Resizable panels
Draggable splitters between every main region (media bin | player | inspector, and
player area | timeline): thin grab handles on the grid gaps (cursor col-resize/
row-resize), live resize of the CSS grid template, double-click a splitter to reset to
default, min sizes so nothing collapses accidentally. Sizes persist per user in
localStorage and restore on launch. The reel editor reuses the same mechanism where it
has panes.

### b) Manual overlay track (video over video)
A second timeline track ABOVE the main one for overlays/PiP — STRICTLY MANUAL: the AI
pipeline must never create or modify overlay items (enforce in code: only the overlay
API endpoints touch them).
- Data: project["overlays"] = [{id, clip_id, t_start (timeline seconds), duration,
  clip_in (source offset), x, y, scale (0..1 fractions of frame), opacity (0..1)}].
  v1 constraints: no audio from overlays (main audio wins), overlay must lie within the
  final cut's duration.
- Timeline: overlay items drawn on the upper track (thinner blocks, draggable
  horizontally, trim handles); created by dragging a clip from the media bin onto the
  overlay track.
- Player (draft mode): overlay rendered as an absolutely-positioned muted <video>
  synced to the EDL clock; on the player, the selected overlay gets a draggable/
  resizable bounding box (move = x/y, corners = scale) like every editor's PiP.
- Render: main cut renders exactly as today; then ONE final ffmpeg pass applies all
  overlays over the concatenated file (per overlay: trim source window, scale, overlay
  x/y with enable='between(t,start,end)', opacity via format+colorchannelmixer alpha or
  blend) — single filter_complex chain, respects the semaphore.
- Preview render includes overlays (same final pass at preview quality).

## v5.10 — Settings > General redesign (owner: "no queda nada claro")
Replace the current export-folder card with a clear, app-like design:
- Row layout: folder icon + label "Export location" + the folder shown as a FRIENDLY
  path ("~/Movies/Magic Video Editor", home abbreviated, never a raw truncated input) +
  a "Change…" button (native picker; auto-SAVES immediately on choose — no Save button
  anywhere in this card) + a subtle "Reveal in Finder" icon button.
- Below, a live STRUCTURE PREVIEW rendered from real data so the behavior is
  self-explanatory: "Your exports:  Magic Video Editor / <current project name> /
  <latest reel or project title>.mp4" styled as a breadcrumb with folder icons.
- Clicking the path text switches it to an editable input (Enter saves, Esc cancels) —
  the power-user path entry, hidden by default. NO mention of "browser mode" anywhere
  (app-first principle).
- Section subtitle fixes: "General" keeps a neutral description; each card carries its
  own title + one-line description. Apply the same no-Save-button, auto-persist pattern
  to the rest of Settings (Brand textarea already autosaves on blur — keep; Performance
  selects save on change with a brief "Saved ✓" toast).

## v5.11 — Settings > Models redesign (owner: too long, model discovery must be encapsulated)
The Models section currently sprawls vertically. Restructure:
1. **"Your models" (compact, what stays on the page)**: a tight two-column grid of the
   assignments — Default model + the per-task selects (short labels + the one-line dim
   description on hover/tooltip instead of always-visible paragraphs); below it,
   "Installed" as a compact horizontal chip list (name + size, x to delete) instead of
   stacked cards. Target: the whole thing fits one screen without scrolling.
2. **"Browse models" becomes a MODAL** (encapsulated, opened from a single prominent
   button next to Installed): the model browser dialog (~720px, own scroll) contains the
   hardware recommendation block (Tu Mac + Best overall / Optimal picks), the search
   field, and the curated/popular results with tag chips + compatibility badges +
   Install progress. Closing the modal never loses an in-flight pull (it continues as a
   queue/activity item, visible in the Activity chip).
3. Pull progress ALSO surfaces in the Activity chip/popover so the modal doesn't need
   to stay open.

## v5.12 — Fold Transcription into Models (owner request)
Remove the separate "Transcription" settings section. Inside Models, add a visually
DISTINCT box "Transcription — Whisper" (separated from the Ollama area, subtle divider +
its own icon) containing:
- A dropdown of common mlx-community Whisper repos instead of the raw text input:
  whisper-large-v3-turbo (Recomendado — mejor equilibrio velocidad/precisión),
  whisper-large-v3 (máxima precisión, más lento), whisper-medium, whisper-small
  (rápido, menos preciso) — plus an "Custom repo…" option revealing the free-text input.
  Auto-save on change (no Save button).
- A two-line explainer in the box (Spanish-friendly tone, since UI copy is English keep
  it in English): "Speech-to-text uses Whisper — the open-source standard for
  transcription — not an Ollama LLM. It produces the word-level timestamps every edit
  decision depends on, and runs on the Apple GPU via MLX. The Ollama models above only
  reason over the resulting text."

## v5.13 — Full rename: CutRoom -> Magic Video Editor (owner request; RUNS FIRST AND SOLO in the next wave)
Rename everything, including code:
- Python package `cutroom` -> `magic_video_editor` (faithful snake_case); console scripts
  `cutroom`/`cutroom-server` -> `mve` / `mve-server` (Makefile app/server targets updated;
  README too). All imports, Makefile smoke list, launch.json args, docs references.
- **Data directory moves to the macOS-correct location** (app-first): ~/CutRoom ->
  `~/Library/Application Support/Magic Video Editor` (projects/, settings.json, luts/).
  AUTO-MIGRATION on startup: if the old dir exists and the new one doesn't, move it
  (os.rename same-volume) and log; never lose projects. The About panel then shows the
  new path.
- Env vars CUTROOM_* -> MVE_* (old names still honored as fallback, warned once).
- User-facing strings: no "CutRoom" left anywhere (grep gate in verification). Repo name
  stays magic-video-editor.
- Update .claude/launch.json (repo) and the workspace launch config that runs
  uv --project ... cutroom-server -> mve-server.
Verification: make lint && make smoke, boot, create+open a project, and confirm a
pre-existing ~/CutRoom gets migrated (test with a fixture dir set via env override).

## v5.14 — BUG: playback dies after editing colors (owner report; diagnosed)
Symptom: edit color sliders -> the player stops playing and never recovers.
Likely root causes (verify all three, fix at the root):
1. player.js AUTO-SWITCHES Draft<->Preview based on manifest freshness. A color edit
   triggers the debounced preview_render auto-enqueue; when it completes, the player can
   flip to Preview mode mid-playback — and if it flips while preview.mp4 is being
   REWRITTEN in place by the render job, the <video> loads a truncated file and stalls
   forever (readyState 0/2, no error).
2. preview_render writes preview.mp4 IN PLACE — must write preview.tmp.mp4 and
   atomically os.replace() at the end (also update the manifest only after the rename).
3. The preview <video> src needs cache-busting (?v=<manifest-hash>) so a previously
   half-loaded/stale file is never reused.
Fix policy: NEVER auto-switch modes during active playback or within a user's editing
session gesture — auto-select mode only on project open and at segment boundaries when
paused; when a fresh preview becomes available show a subtle "Preview ready" affordance
on the mode toggle instead of switching. Manual toggle always wins. Also ensure the
color panel's live CSS-approximation path never pauses/detaches the draft videos (audit
compare.js interactions with #player-stage).
Regression test for the integrator: with a project playing in Draft, change a color
slider, wait for the auto preview render to complete, and assert playback never stopped.

---

# v7 — Source preview, incremental clips, transitions catalog, subtitle inline edit, reel safe zones (owner, 2026-07-25)

Owner decisions locked: reel safety = DETERMINISTIC geometry (face bbox vs platform zones,
NO vision-LLM screenshots); new-clip insertion = PROPOSE + 1-click accept (never auto).

## 7.1 Media bin source preview
Clicking a clip in the media bin loads it into the main player in **Source mode** (plays
the clip's preview proxy from 0, full clip scrubbing on the timeline RULER ONLY — the EDL
track stays untouched/dimmed): a small "Source: <name>" chip appears in the player
controls with an X to return to **Edit mode** (the EDL). Esc / clicking any timeline
segment also returns. Double-click in the bin = same. This mirrors FCP viewer behavior.

## 7.2 Stage pills -> collapsible pipeline chip
The 8 stage pills leave the top bar. Replace with ONE compact chip ("Pipeline ✓" /
"Pipeline 3/8" / error state) that opens a popover listing all stages with status and
per-stage re-run buttons. While run-all is executing, the existing progress strip remains
(unchanged). Less noise, same power.

## 7.3 Incremental clip addition (pipeline already run)
When clips are added to a project whose pipeline already completed:
- Auto-enqueue an `analyze_clip` queue item scoped to the new clip(s): import/proxy/
  thumbs/wav + transcribe + per-clip cleaner/sequencer ONLY for that clip. Existing
  clips are never re-processed.
- Then a **placement agent** (flat: {placement_after_clip_index: int|-1 (-1=start),
  duplicate_of_clip_index: int|-1, confidence: 1-5, message: str}) receives the video
  topic, ordered per-clip summaries of the existing narrative, and the new clip's kept
  transcript. Output becomes a SUGGESTION card (Ideas tab + toast):
  - fits: "Encaja después del clip 3 (explica X antes de Y)" -> Accept splices it into
    clip_order at that position and rebuilds the EDL insertion (existing edits to other
    segments preserved — only insert, never reshuffle).
  - duplicate: "Este clip repite contenido del clip 2 — ¿seguro que quieres añadirlo?"
    -> Accept anyway (places it) / Dismiss (clip stays in the bin, excluded from EDL).
- Message in the transcript's language.

## 7.4 Overlay track discoverability (already built — surface it)
The overlay track exists (v5.9). Fix discoverability: permanent thin label "Overlay —
arrastra un clip aquí" as empty-state on the upper track, highlight on bin-drag, and an
e2e verification that drag→box→render works after the recent waves.

## 7.5 Transitions catalog (FCP-style, ffmpeg xfade)
No .cube-like interchange format exists for transitions (gl-transitions is the GLSL
catalog standard but needs a custom ffmpeg build — rejected). Expose ffmpeg's native
xfade catalog (~50 named transitions) instead:
- Backend: GET /api/transitions -> [{name, label_es, category (Fundidos/Barridos/
  Deslizamientos/Geométricas/Píxel), xfade_name}]; EDL junction transition.type accepts
  any xfade name (validated against the catalog); render maps type -> xfade=transition=
  <name> (audio always acrossfade); "fade"/"crossfade" legacy values keep working.
- UI: FX inspector tab becomes the transitions BROWSER: category sections, animated
  thumbnail per transition (CSS keyframe approximations on two colored tiles — no video
  decoding), click-to-apply to the selected junction AND drag-onto-junction-chip in the
  timeline. Junction chips show the transition name. Draft playback approximates: fades
  via existing overlay; everything else = generic quick crossfade (exact look = preview
  render).

## 7.6 Subtitle inline edit on the player
When subtitles are enabled and the player is PAUSED: the subtitle overlay becomes
interactive — double-click opens inline editing (contenteditable) saving to a NEW
project-level cue override map (project["subtitles"]["cue_overrides"] {cue_index: text},
honored by cue_list + .ass burn for main render AND preview); simultaneously the Subs
inspector tab opens with that cue's row scrolled into view and focused (cue LIST with
per-cue editable text is added to the Subs tab for the main video, like the reel editor
has). Dragging the overlay vertically adjusts the subtitle vertical margin (persisted in
the subtitles config; snaps to bottom/center presets when close). While playing, the
overlay stays non-interactive.

## 7.7 Reel social safe zones + face safety (deterministic)
- New module with PER-PLATFORM zone specs (research current published safe-zone specs
  for TikTok, Instagram Reels, YouTube Shorts on the web; encode as fractions of
  1080x1920: right action rail, bottom caption/description area, top bar, progress bar).
  Each platform also gets a lightweight CSS/SVG MOCKUP overlay (rail icons: heart,
  comment, share; caption lines; username) for the Reel Editor preview — platform toggle
  chips (TikTok / Reels / Shorts / none). Search the web for an existing open-source
  safe-zone template/library first; if a good one exists (svg/png overlays, permissive
  license), vendor it; else hand-build minimal mockups.
- **Face safety check (deterministic)**: reuse the existing face detector on sampled
  frames of the reel window (respecting crop_x/fit): intersect the face bbox (mapped
  into 9:16 output coords) with each platform's occupied zones. Endpoint
  GET /api/projects/{pid}/reels/{rid}/safety?platform=... -> {safe: bool, intervals:
  [{t0,t1,zone}], coverage_pct}. UI: warning badge per platform chip ("La cara queda
  tapada por la UI de TikTok en 0:04–0:12").
- **One-click fix — "Zoom out con fondo blur"**: reel gains fit_mode: "fill" (default)
  | "fit_blur" + fit_scale (0.6..1.0). fit_blur render: background = the same video
  scaled to fill + boxblur (+slight darken), foreground = the video scaled to fit_scale
  centered (classic vertical-video treatment). Preview approximates with CSS (blurred
  underlay). When a safety warning fires, the suggestion offers this fix with a scale
  that clears the zones (computed from the face bbox geometry).

Conventions: same as always (ruff/lint/smoke, vanilla JS, flat schemas, ffmpeg via
ffmpeg_utils, strict ownership, never commit, never wait-for-monitors, live user on 8765
+ real data dir untouchable, MVE_DATA scratch + ports 8840-8858 for tests).

## v7.8 — Export: transcript/audio/video + output format settings (owner, 2026-07-25)

### Export dialog restructure
Three first-class exports: **Vídeo / Audio / Transcripción** (radio or segmented control),
plus the existing reels options.
- Quick path: "Exportar" uses the DEFAULTS (from Settings); advanced path: "Exportar
  como…" expands: for video — resolution picker (2160p/1440p/1080p/720p/Original,
  CAPPED at the source resolution: options above the source res are disabled with a
  tooltip "tu material es 1080p" — never upscale; compressing down is always allowed),
  quality preset (Alta CRF18 / Media CRF23 / Comprimida CRF28), container (mp4 / mov /
  mkv — what our ffmpeg encodes with h264+aac). For audio — m4a / mp3 / wav of the final
  cut's audio (with enhance applied if enabled). For transcript — .txt (plain, narrative
  order, kept sentences only), .srt (re-timed to the FINAL EDL timeline, honoring cue
  overrides + speakers prefixes if enabled), .md (with clip headings). Files land in the
  export dir under the project folder, named "<project name>.<ext>" (dedupe as usual).
- Settings > General gains an "Export defaults" row: default container + quality preset
  + resolution (Original by default). The Export dialog's quick action reads these.

### Backend
- render gains an export profile parameter {container, crf, height|original} threaded
  through final_render (scale only DOWN; audio codec per container: aac for mp4/mov,
  aac in mkv fine); audio-only export = extract/encode from the rendered cut (or render
  audio-only path if no cut exists yet -> require a render first with a clear message).
- New endpoints: POST /api/projects/{pid}/export {kind: video|audio|transcript, profile}
  -> enqueues; GET transcript export generates synchronously (it is cheap).
- Validation: requested height > source height -> 400 with the friendly message.

## v7.9 — Voice enhancement v2 + 8-band EQ (owner: current enhance "es una lata")
The noisereduce spectral gate DEGRADES already-good audio (tinny artifacts). Replace with
a real neural speech enhancer:
- RESEARCH (agent has web): benchmark locally the leading Python-native options —
  **DeepFilterNet** (pip deepfilternet, DNS-grade, CPU-fast, primary candidate),
  **resemble-enhance** (denoise+enhance, torch), demucs vocals stem (heavy; only if DFN
  disappoints), speechbrain MetricGAN+. Pick by: quality on speech w/ mild room noise,
  CPU speed on Apple Silicon, dependency weight. Generate A/B artifacts (original vs
  each candidate on a real-ish fixture) saved to a scratch folder listed in the report
  so the owner can LISTEN and veto.
- New chain: neural enhance (chosen tool) -> loudness normalize -16 LUFS -> peak limit.
  DROP the crude gate/highpass/presence stack (the model handles it). Keep noisereduce
  ONLY as a fallback when the model/deps are unavailable. The A/B preview endpoint stays.
- **8-band EQ**: project["audio_eq"] = 8 gains in dB (-12..+12) at 60/150/400/1k/2.4k/
  6k/12k/16k Hz, default flat. Render/preview-render apply via chained ffmpeg
  `equalizer` biquads (or superequalizer mapped). Draft playback applies it LIVE via
  WebAudio BiquadFilterNodes on the player's video element (peaking filters, same
  freqs) so sliders are heard instantly. Audio inspector tab: 8 vertical sliders +
  value labels + reset + a couple of presets (Voz, Música, Plano).

## v7.5 addendum — FCP/iMovie transition parity + SVG identity (owner)
Research the DEFAULT transition sets of Final Cut Pro and iMovie (lists are documented
online); map them onto our xfade catalog, naming ours after the familiar ones (Cross
Dissolve, Fade to Black, Fade to White, Wipe, Slide, Circle, etc. — label_es included)
and ordering the browser with those familiar ones FIRST. Every transition gets a clear
visual identity: a small inline SVG glyph (two frames + arrow motif per family) PLUS the
existing animated hover preview. Drag-to-junction stays the core gesture.

## v7.10 — Timeline clip context menu + speed/retime (owner)
Research a documented breakdown of Final Cut Pro's clip-level frontend actions (menus/
shortcuts references online) and map what we lack; implement the high-value set as a
RIGHT-CLICK context menu on timeline blocks (custom menu, app-first — the webview default
menu stays disabled): Cambiar velocidad (0.5x/0.75x/1x/1.5x/2x/custom dialog), Duplicar
segmento, Desactivar/Activar (excluded from render, dimmed block), Dividir aquí, Eliminar
(ripple), Transición… (opens FX browser for its junction), Ver origen (Source mode at
that clip time). Speed/retime: segment gains speed (0.25..4.0); render maps to
setpts=PTS/<v> + atempo chain (atempo composed for >2x/<0.5x); duration math updates EDL
timeline lengths everywhere (cumulative, playhead, filmstrip width); draft playback uses
video.playbackRate for that segment. Subtitle cue times for sped segments re-map
accordingly in cue_list/burn.

## ⛔ HARD RULE — no local packaging activity on this machine (2 Cortex XDR kills)
Cortex XDR terminated the dev session twice: (1) launching a freshly built .app,
(2) building + dry-run-simulating the update swap (detached helpers, xattr, bundle
replacement). On THIS machine, agents must NEVER: run PyInstaller/make dist*, execute
anything from dist/, simulate app swaps/self-replacement, or spawn vendored binaries
for tests. Verification for packaging/updater work = static only (ruff, bash -n, code
review, unit tests with pure-python fakes) + the GitHub Actions release build + user
testing on their own hardware. scripts/dry_run_update_helper.sh is CI/other-machine
material only.

## v7.11 — Reel framing v2: direct manipulation (owner: fit+blur "no es lo que me refería")
Replace the {crop_x, fit_mode, fit_scale} trio with a proper TRANSFORM model. Output is
ALWAYS the 9:16 frame; the transform defines which source window fills it:
- reel["transform"] = {zoom: 0.5..3.0 (1.0 = the classic full-height 9:16 crop exactly
  covers the frame; >1 punches in; <1 opens the window WIDER than the frame),
  offset_x, offset_y: -1..1 (pan, clamped to available room)}.
- **Blur background is not a mode** — it appears automatically wherever the zoomed-out
  source stops covering the 9:16 frame (zoom < cover threshold), exactly like CapCut.
  THE BUG being fixed: today's zoom-out letterboxes the ORIGINAL 16:9 frame; the new
  semantics zoom out FROM the 9:16 crop window, never switching aspect logic.
- **Direct manipulation on the preview** (the main ask): drag the video to pan
  (offset_x/y), scroll-wheel/trackpad-pinch over the preview OR a zoom slider to zoom,
  double-click = reset to auto (face-centered, zoom 1.0). Live CSS preview (transform:
  translate/scale on the video + blurred underlay element). Keep the side handles ("el
  barrido") working as a secondary affordance but the canvas gesture is primary.
- Migration on read: crop_x -> offset_x equivalent; fit_blur+fit_scale -> zoom=fit_scale;
  plain fill -> zoom 1.0.
- Render: derive the source crop rect from the transform (fg crop+scale; blurred cover
  bg only when needed); PATCH accepts transform; safety analysis maps face boxes through
  the SAME transform (single shared mapping helper so UI/render/safety can't drift —
  the safety mapping update may need coordination with the in-flight safety agent).
- Safety's one-click fix becomes "reduce zoom to X" (adjusts transform.zoom).

## v7.12 — Preview mode broken + process logs unreachable (owner field report)
1. **Preview mode does not play**: the player's Draft/Preview toggle fails to play
   <project>/preview/preview.mp4. Diagnose the full chain: does preview.mp4 exist for
   the project (preview_render ran?); the media/file endpoint URL the player builds
   (cache-busted src correctness); mode-switch wiring after the stutter-fix changes;
   and whether the WebAudio graph swallows the preview element's audio. Preview mode
   must ALSO show a clear empty-state when no preview render exists yet ("Genera la
   previsualización" CTA that enqueues preview_render) instead of failing silently.
2. **Process logs are gone**: since the Activity popover redesign the run logs are
   invisible in practice. Requirements: the popover's per-task "Details" disclosure must
   work for running AND recent items (verify after the row redesign); the pipeline chip
   popover gains a "Ver registro" link per stage opening the same log view; and while a
   run-all is executing, the progress strip gains a small log icon opening the live log
   (tail -f behavior, auto-scroll, monospace). A user must never wonder "where did the
   logs go".

## v7.13 — Subtitles in edit mode, kill the Draft/Preview concept, audio-preview UX (owner)
1. **Subtitles overlay broken in edit (draft) playback**: enabled subtitles do not show
   over the player. Diagnose (cue fetch? overlay z-index vs the new layers? gating?) and
   fix: enabled subtitles always render live during draft playback and update on config
   changes.
2. **Kill the user-facing Draft/Preview distinction** — the owner rightly says they feel
   like the same thing. New model: ONE play experience; the app AUTO-chooses the freshest
   representation (rendered preview when its manifest matches, virtual otherwise), never
   switching mid-playback. The explicit toggle leaves the primary UI; in its place a
   subtle quality badge ("Borrador" dim / "Final ✓" green) with a tooltip explaining
   background rendering, plus the v7.12 empty-state CTA when a preview render would help
   (e.g. transitions/LUT active which draft can't show exactly). Keyboard/debug toggle
   can stay hidden behind alt-click.
3. **Audio enhance preview UX**: the clip dropdown + naked "0.5" number are opaque (the
   number is the sample timestamp — the owner had to guess). Replace with: "La mejora se
   aplica a TODO el audio del vídeo al renderizar" copy; the A/B sample controls become
   "Probar desde: [posición actual del cursor] (botón) o [mm:ss] del vídeo final" — the
   final-timeline time maps internally to clip+local offset via the EDL. No raw clip
   selector, no unitless numbers.

## v7.14 — Reel preview render (owner top complaint: dead `<video>` in the drawer)

The Reels drawer had nothing decodable to point a player at: a suggestion has no
rendered file yet, and pointing a `<video>` at the raw source clip fails outright for
iPhone HEVC/10-bit sources (Chromium can't decode them — the same reason clips already
get an H.264 720p preview proxy, `/media/preview/{cid}` in `magic_video_editor/server.py`) and,
even where the proxy does decode, shows the wrong framing (no transform crop/pan/zoom,
no blur background) for that specific reel. Every reel suggestion now gets its own cheap
low-res 9:16 preview render, produced in the background via the per-project job queue,
and the drawer plays THAT — never the raw source. Export/full render quality is
untouched.

- **Shared composition path**: `magic_video_editor/pipeline/reels.py`'s `_compose_reel(log, project,
  reel, work, width, height, crf, preset)` factors the segment/filter construction
  (multi-segment concat, the v7.11 framing transform `{zoom, offset_x, offset_y}` with its
  blur background, subtitle burn, junction crossfades — via the SAME `render.py`
  `_encode_segment`/`_merge_crossfades` reel rendering already reused) out of `render_reel`,
  so both the full-quality render and the new preview render call the one implementation
  with different width/height/crf/preset — no duplicated crop/subs/transition logic.
- **Preview quality**: `PREVIEW_W, PREVIEW_H = 480, 854`, `PREVIEW_CRF = 30`,
  `PREVIEW_PRESET = "ultrafast"`. No audio enhance (expensive, doesn't change
  composition) and no export-dir placement — the file is an internal work artifact at
  `<project_dir>/previews/reels/<reel_id>.mp4`, streamed straight from there.
  Deviation from the original ask: the shared `render.py._encode_segment` hardcodes AAC
  at 192kbps/48kHz (not parameterized, and `render.py` is out of this task's file
  ownership) — the preview's audio stays at that bitrate rather than 96k; negligible
  next to the video-size savings and not worth forking the encode path.
- **Invalidation**: `reel_content_hash(reel)` hashes exactly `{segments, transform,
  transitions}` (NOT title/description/cue text/subtitle style — those don't change a
  single frame) and is stored in a sidecar `<reel_id>.json` next to the mp4, plus mirrored
  onto `reel["preview_hash"]`/`reel["preview_ready"]` in `project.json`. The "reel_previews"
  job (`render_all_reel_previews`) skips any reel whose sidecar hash still matches.
  `PATCH /api/projects/{pid}/reels/{rid}` (`magic_video_editor/api/reels.py`) computes the hash
  before and after applying the patch; if a composition-affecting field actually changed,
  it flips `preview_ready` false and auto-enqueues `"reel_previews"` (deduped on kind) —
  AFTER its own `store.save`, not before, to close a real race where the queue's
  load-mutate-save (and a racing background worker) could otherwise clobber the just-
  applied edit (caught live by `scripts/test_reel_previews.py`).
- **Queue job**: kind `"reel_previews"` registered in `KIND_RUNNERS` (see
  `magic_video_editor/queue.py` + the registration at the bottom of `pipeline/reels.py`, mirroring
  `pipeline/thumbs.py`'s own registration pattern) — one job per project renders every
  reel missing a fresh preview, logging and skipping (not failing the whole job) on a
  per-reel ffmpeg error. Auto-enqueued right after the reels pipeline stage completes,
  whether via `run-all` or a standalone `stage:reels` re-run (`queue.py`'s
  `_run_auto_enqueue_hooks`), same spirit as thumbs/proxies auto-enqueuing after ingest.
- **Endpoint**: `GET /api/projects/{pid}/media/reel-preview/{reel_id}` (`server.py`, route only
  — reuses the existing `_stream` Range helper exactly like `/media/preview/{cid}`) — 206 on a
  Range request, 404 while the reel has no rendered preview yet or doesn't exist.
- **Frontend** (`ui/tabs/reels.js`): the filmstrip poster (`_reelsThumbEntry`/
  `_reelsApplyPoster`) shows immediately regardless of preview state. Once
  `reel.preview_ready` (a flag in the reels payload, no endpoint probing needed), hovering
  the card lazily creates a muted looping `<video>` against the preview endpoint; clicking
  unmutes it (sound-on, the explicit "play this" gesture) — at most 1-2 live decoders at
  rest, created on hover/click and torn down on mouseleave, same as before. Before that,
  the card shows a subtle "Generando previsualización…" badge and no video at all (never
  a dead one). Refreshing on completion reuses the EXISTING queue poll
  (`ui/core.js`'s `pollQueue`/`refreshProject`, triggered when the queue's running-item
  count drops to zero) — no new poller was added.

### v7.14 addendum — drawer never plays the export, and existing projects get backfilled

Two seams left the drawer black in exactly the same way the original bug did, found by
testing live against a real project:

- **Drawer always plays the low-res preview, never the export.** A rendered reel
  (`r.path` set, `r.status === "rendered"`) used to point the card's `<video>` at
  `GET /media/file?path=<absolute export path>` instead of the poster/preview flow.
  Exports land under `settings.export_dir` (`~/Movies/...` by default), which is outside
  the project dir — `media_file()` (`server.py`) 403s anything outside it, so that player
  was dead (`MediaError` code 4). Product decision: the drawer plays the cheap 9:16
  preview unconditionally, rendered or not — full quality is an export-only concern.
  `ui/tabs/reels.js` no longer branches on `r.path` at all; every card goes through the
  same poster + `GET /media/reel-preview/{id}` path, and a rendered reel is distinguished
  only by a "Rendered <time>" badge and the button reading "Re-render 9:16" instead of
  "Render 9:16".
- **Backfill for projects whose reels predate this feature.** `"reel_previews"` was only
  ever auto-enqueued right after a reels-producing stage or a composition-changing PATCH
  — any project that already had reels before v7.14 landed (or whose render never
  finished) had no path to ever get one; `preview_ready` is simply absent and the
  endpoint 404s forever. `GET /api/projects/{pid}` (`api/projects.py`'s `project_get`,
  via `_backfill_reel_previews_once`) now self-heals this on read: for each reel, after
  `reels.ensure_segments()` normalizes legacy shapes, `reels._preview_is_current()`
  checks the on-disk mp4 + hash sidecar exactly like the queue job does; if any reel is
  stale or missing one, `"reel_previews"` is enqueued once (`queue.enqueue(..., dedupe=
  True)`). A module-level guard (same `_healed_once`-style pattern as `store.py`'s
  legacy-path self-heal) makes sure a given project is only ever inspected once per
  process, so this never re-checks on every poll/`refreshProject()` tick and never
  enqueues when everything is already fresh. Deliberately NOT hooked into `store.load()`
  itself: the `"reel_previews"` job's own runner calls `store.load()` while running,
  which would let the read-path hook re-enqueue itself mid-run; the HTTP read path isn't
  on that call graph. Covered by `scripts/test_reel_previews.py`'s
  `ReelPreviewBackfillE2ETest` (legacy-shaped reel gets backfilled exactly once; an
  already-fresh reel never enqueues, even on its very first GET).

## vNext — hardware-aware default model + LLM preflight guard (2026-07-25)

Root cause of two separate field reports ("Ollama never works on a fresh machine" and
"the app hangs COMPLETELY every so often"): `settings.DEFAULT_MODEL` was a static
`"qwen2.5:14b"` (~9GB, needs ~10-12GB RAM), seeded into `settings.json` on **every**
first run regardless of hardware, with no check anywhere that the resolved model was
installed or fit RAM before a pipeline stage actually invoked it. On a clean 8GB M2 that
meant either a raw ollama error (model never pulled) or, worse, ollama loading a model
that can't fit — the Mac swaps to death and the whole app freezes.

- **Hardware-aware first-run default** (`magic_video_editor/settings.py`): `load()` now
  seeds `default_model` from `api/ollama.py`'s `recommended_default_model()` — the same
  RAM-tier table `/api/ollama/recommendation` already used — instead of the static
  default, but ONLY when `settings.json` doesn't exist yet. An existing settings.json is
  never touched; the static `"qwen2.5:14b"` remains the ultimate fallback if the
  recommendation helper itself fails (e.g. psutil unavailable).
- **Preflight guard** (`magic_video_editor/api/ollama.py`'s `preflight_check_models()`,
  called from `api/pipeline.py`'s `_preflight_stage`): before an LLM-backed stage
  (`takes`/`order`/`review`/`reels` — the only `STAGES` entries whose pipeline module
  calls `get_agent()`) actually runs, its resolved model(s) are checked for reachability,
  installation (`GET /api/tags`, 5s timeout so a hung ollama can't block the job), and
  RAM fit (`_compatibility()`, not `"too_big"`). Runs from `_run_stage_kind` and
  `_run_all_kind` — the ONE chokepoint both single-stage and run-all queue runners go
  through — so the failure surfaces as a clear, actionable job/stage error (Spanish,
  names a fitting alternative) instead of a raw ollama error or a silent hang. Non-LLM
  stages (`ingest`/`sync`/`transcribe`/`render`) are skipped entirely.
- **Settings > Models UI hint** (`ui/tabs/settings.js`): `GET /api/ollama/models` now
  also returns each installed model's RAM `compatibility` (great/tight/too_big, same
  table); the default-model and per-task `<select>` options show it inline, and a small
  hint next to the Default model picker surfaces the hardware-recommended pick — so an
  oversized model is visible before it's ever selected, not just at preflight time.
- Covered by `scripts/test_ollama_preflight.py` (first-run default on 8GB vs 48GB,
  existing settings.json left alone, preflight passes/fails for
  installed-and-fits/too-big/not-installed/unreachable).
- Deliberately out of scope: auto-pulling a model without user action (stays
  user-initiated via the existing Settings pull flow), `ollama_manager.py`'s spawn
  logic, packaging.

---

# vNext — Main audio track (music bed with auto-ducking)

FCP/Premiere-style third track, the AUDIO analogue of the existing manual video
overlay/PiP track (spec v5.9b): a single music bed the user drops onto the timeline,
which mixes UNDER the clips' own (program) audio and automatically ducks under it.
Chosen behavior (owner decision): **music bed with auto-ducking** — the imported
audio's volume drops while there's voice/program audio and recovers in gaps — sourced
by **importing audio files** (.mp3/.wav/.m4a) into the media bin. Strictly separate
from the video overlay track; the two features don't interact.

## Data contract
- `project["audio_assets"]` = `[{id, path, filename, duration}]` — imported music
  files, probed (ffprobe) for duration and copied/hardlinked into the project dir
  exactly like camera clips (same macOS-TCC hardlink-import sidestep). **Deliberately
  a separate list from `project["clips"]`**: `build_edl`/`ordering`/`takes` all filter
  `role=="camera"` over `project["clips"]`, and an audio_assets entry is never
  appended there at all — it structurally cannot leak into the AI pipeline. No
  proxy/thumbs/transcribe (camera-clip-only concerns).
- `project["audio_track"]` = `{asset_id, start_s, gain_db, ducking: true}` — the ONE
  main-audio-track placement for MVP (single track, single item). `null` when none.
  CRUD lives in `api/audio.py` (additive alongside the existing voice-enhance/EQ
  endpoints there): `GET/POST/DELETE /api/projects/{pid}/audio-assets` (+`/upload` for
  the browser-mode/drag&drop fallback), `GET/PUT/DELETE /api/projects/{pid}/audio-track`.

## Import (media bin)
`pipeline/ingest.py`'s `add_audio_assets`/`register_uploaded_audio_assets` (mirroring
`add_clips`/`register_uploaded_clips`'s import-copy, but appending to
`audio_assets`) accept `.mp3`/`.wav`/`.m4a` via: the native pywebview file picker (a
dedicated "+ Music" button, `ui/editor/mediabin.js`), the "add by path…" power-user
link (routed by extension alongside the existing clip-path box), and Finder
drag&drop onto the bin (routed by extension in `_handleDrop`). Rendered as a
visually distinct "Audio" section in the bin (music icon, garnet/`--accent2`
accent), draggable with the private MIME type `application/x-mve-audio` (asset id
payload) — parallel to clips' own `application/x-mve-clip`.

## Timeline (audio lane)
A third lane, BELOW the main video track (mirrors the overlay lane's pattern, which
sits above it) — `ui/editor/timeline.js`'s `_ensureAudioTrack`/`renderAudioTrack`.
Dropping an audio-bin asset onto it sets `project["audio_track"]` (`asset_id` +
`start_s` from the drop x-position); the placed block shows the filename, a small
gain (dB) number input, and a remove (✕) control that clears `audio_track`
entirely. Deliberately NOT routed through `ui/editor/state.js`'s `Editor`/undo-history
stack (a single small config object, no per-field undo requirement) — mutations PUT
straight to `api/audio.py` then reload the project.

## Render (`pipeline/render.py`'s `_apply_music_bed`, reused by `pipeline/reels.py`'s
`render_reel`)
Runs as a **follow-up ffmpeg pass** (`-c:v copy`, one audio filter graph — no video
re-encode) placed AFTER audio-enhance in both `_build` (final render + preview
render) and `render_reel`: ducking should key off the FINAL, already-enhanced
program audio, not a pre-enhance version of it. Skipped entirely (documented
no-op) when there's no `audio_track`, the referenced asset is missing, the program
has no audio stream, or `start_s` is already past the program's end. Reel
*previews* skip it (like they already skip audio-enhance) — it doesn't affect a
reel's composition hash.

Filtergraph (one `ffmpeg` invocation, inputs: `0` = the assembled program video,
`1` = the music file with `-stream_loop -1`):
```
[1:a]atrim=0:{program_duration-start_s},asetpts=PTS-STARTPTS,
     adelay={start_s*1000}|{start_s*1000},volume={gain_db}dB[mus]
[mus][0:a]sidechaincompress=threshold=T:ratio=R:attack=A:release=Rl[ducked]   # ducking=true only
[0:a][ducked]amix=inputs=2:duration=first:normalize=0[amix]                   # or [0:a][mus]amix=... when ducking=false
[amix]alimiter=limit=L[aout]
```
- The music is looped (`-stream_loop -1`) and `atrim`+`adelay`ed to land exactly at
  `[start_s, program_duration)` regardless of the source file's own (often shorter)
  duration — no dependency on the music file being as long as the cut.
- `sidechaincompress` takes the music as its signal and the PROGRAM audio as the
  sidechain key — the standard "duck A under B" wiring, not the other way around.
- `amix`'s `duration=first` pads the (delayed, hence shorter) music stream with
  silence to match the program's length — no manual `apad` needed.
- `amix normalize=0` keeps the program at full level (the usual `normalize=1` halves
  loudness per extra input, wrong for a bed mix); `alimiter` guards against clipping
  from the sum instead.
- Named constants in `config.py`: `MUSIC_DUCK_THRESHOLD`, `MUSIC_DUCK_RATIO`,
  `MUSIC_DUCK_ATTACK_MS`, `MUSIC_DUCK_RELEASE_MS`, `MUSIC_GAIN_DEFAULT_DB`,
  `MUSIC_MIX_LIMIT`.
- `_preview_manifest`'s staleness hash now includes `audio_track` alongside
  edl/color/subtitles/audio_enhance.

## Known limitation (documented, not a bug)
**No live Draft-mode audio preview.** Mixing/ducking into the un-rendered virtual
playback path would require touching `ui/editor/player.js` (owned by a different
task/agent). The music is only actually heard on a real render (final export or
"Render preview") for now — a `TODO` comment marks the exact spot in
`ui/editor/timeline.js`. Draft playback continues to play only the program audio,
unchanged from before this feature.

Covered by `scripts/test_audio_track.py`: import via the API lands in
`audio_assets` (never `clips`); audio-track CRUD; one real end-to-end final render
with the track set, asserting the output has an audio stream and the music's own
frequency band is measurably louder than a no-audio-track baseline (loudness
comparison via ffprobe/`volumedetect`) even though the source file is shorter than
the program (loop/trim path); ducking on/off toggles `sidechaincompress` in the
built filtergraph; missing-asset/past-end `start_s` are safe no-ops; a structural
guard that `pipeline/ordering.py` has no notion of `audio_assets`/`audio_track` at
all.

# vNext — Reel dedup analyst (owner request, 2026-07-25: "el creador de reels no
debería hacer 20 siempre. 20 es el MÁXIMO")

`config.REEL_SUGGESTIONS` (20) is a **ceiling**, not a target. `pipeline/reels.py`'s
`suggest()` used to always pad its pick loop up to 20 candidates whenever enough
existed, which meant weak/near-identical windows got promoted just to fill the
count — practically identical reels suggested side by side.

**The distinction that matters (owner said it twice):** talking about a similar
TOPIC is not the same as repeating the same clip.
- **Duplicate (collapse it):** two reels built from the SAME underlying source
  moment — same clip(s), an overlapping/near-identical source time window, and/or
  near-identical transcript wording. The same moment packaged twice.
- **Not a duplicate (keep both):** two reels from DIFFERENT moments/segments that
  merely discuss a similar theme. Different footage/wording, related subject —
  these are legitimately distinct suggestions.

## Mechanism (mirrors `pipeline/takes.py`'s cross-clip dedup design exactly)
1. **Gather a buffered pool.** The existing top-N/mutual-overlap gather (`_overlap`
   < 0.45) now collects up to `REEL_SUGGESTIONS * REEL_DEDUP_POOL_MULTIPLIER`
   candidates instead of stopping at the ceiling — otherwise there'd be nothing left
   to dedup once the ceiling truncated the list.
2. **Structural pre-filter for candidate PAIRS** (`_reel_dedup_candidate_pairs`):
   flags a pair only when it shares a clip_id with an overlapping/near-adjacent
   source window (reusing `_overlap`) and/or has near-identical transcript text
   (rapidfuzz `token_set_ratio` ≥ `REEL_DEDUP_TEXT_SIM_CANDIDATE`). Deliberately
   never filters on shared keywords/topic — that's exactly the signal that must
   NOT trigger a merge. Capped at `REEL_DEDUP_MAX_PAIRS` candidate pairs.
3. **`reel_dedup` LLM agent** (`agents/agents.py` AGENT_SPECS, prompt in
   `agents/prompts.py`, schema `ReelDedup` in `agents/schemas.py`) judges each
   pre-filtered pair: same underlying moment (duplicate) vs. same theme/different
   footage (keep both), plus `keep: "a"|"b"` (prefer the higher score/longer/
   stronger hook) and a 1-5 `confidence`.
4. **Two-tier confidence gate**, same shape as `CROSS_DEDUP_AUTOCUT_CONFIDENCE`/
   `CROSS_DEDUP_SUGGEST_CONFIDENCE`: `same_content` + confidence ≥
   `REEL_DEDUP_COLLAPSE_CONFIDENCE` auto-collapses the pair (the weaker reel is
   dropped from the pool); confidence ≥ `REEL_DEDUP_FLAG_CONFIDENCE` (but below
   collapse) keeps BOTH and sets the weaker reel's `reel["dedup_flag"]` to the
   agent's reason instead of cutting anything. Biased toward keeping when
   uncertain — a false merge silently destroys a good, distinct suggestion, which
   is worse than a near-dup slipping through for a human to dismiss.
5. **Ceiling applied last.** Only after dedup collapses true duplicates is the pool
   sorted by score and truncated to `REEL_SUGGESTIONS` — `project["reels"]` can end
   up with far fewer than 20 when that's all the distinct content supports.

New config (`config.py`): `REEL_DEDUP_MIN_WINDOW_OVERLAP` (0.25),
`REEL_DEDUP_TEXT_SIM_CANDIDATE` (70), `REEL_DEDUP_MAX_PAIRS` (30),
`REEL_DEDUP_COLLAPSE_CONFIDENCE` (4), `REEL_DEDUP_FLAG_CONFIDENCE` (2),
`REEL_DEDUP_POOL_MULTIPLIER` (2).

Covered by `scripts/test_reel_dedup.py` (mocked `reel_dedup` agent, scratch
`MVE_DATA`): two reels from the same clip with overlapping windows + near-identical
text are flagged as a candidate pair and, on a high-confidence mock verdict,
collapsed to one; two reels from different source windows sharing only a topic are
NOT flagged and both survive; the returned count is the number of distinct reels,
never padded to the ceiling; the structural pre-filter keys on source-window
overlap/text similarity, not topic keywords.

# vNext — Paragraph-break suggestions (owner feature, 2026-07-25): NON-DESTRUCTIVE
cuts at "punto y aparte"

Besides removing bloopers, the pipeline now marks (never removes) the spots where
the conversation changes topic/paragraph — a genuine "punto y aparte" (new
paragraph), never every "punto y seguido" (plain sentence end within the same
idea). These are the places an editor would naturally drop a transition, an
intro, or an effect.

**Detection.** New `paragraph_break` agent (`agents/agents.py`, prompt in
`agents/prompts.py`, schema `ParagraphBreaks`/`ParagraphBreakPoint` in
`agents/schemas.py`, flat and small-model friendly): given a numbered window of
consecutive KEPT sentences from one clip, in order, it returns `breaks` — boundary
points (`after_id`, `confidence` 1-5, `reason`) where a clear new topic begins.
The prompt hammers conservatism (worked examples of both a real topic shift and a
plain same-idea sentence continuation) — when in doubt, it returns nothing.

**Pass** (`pipeline/paragraphs.py`, new stage `"paragraphs"` in
`api/pipeline.py::STAGES`, inserted between `"order"` and `"review"`): slides a
`config.PARAGRAPH_BREAK_WINDOW_SIZE`/`_OVERLAP` window (same shape as
`take_sequencer`) over each clip's kept sentences, keeps only boundaries strictly
interior to a window (never the last sentence — the overlap lets a later window
judge it as interior instead) and at or above
`config.PARAGRAPH_BREAK_MIN_CONFIDENCE`, and stores the resulting sentence ids as
`project["paragraph_break_after_ids"]`. Gated by `config.PARAGRAPH_BREAK_ENABLED`
and fails open (ollama down / disabled → empty list, today's EDL unchanged).

**Non-destructive application** (`pipeline/ordering.py::build_edl`, new
`paragraph_break_after` parameter defaulting to reading
`project["paragraph_break_after_ids"]` — every existing caller, `api/edl.py` and
`pipeline/render.py`, picks it up automatically): when the sentence right before a
would-be merge is a recorded break, the merge is suppressed and a segment boundary
is forced there instead. Content, order, and timestamps are otherwise identical —
this only ever SPLITS an already-merged segment, never re-merges or reorders
anything, so the intra-clip chronological invariant
(`scripts/test_intra_clip_order.py`) still holds. The segment right after the
break is tagged `paragraph_break: true` (round-tripped through
`api/edl.py::EdlSegment`, cleared on a manual mid-clip split since that's a
different, user-driven cut); its `transition` is untouched (still `"none"` by
default) — a suggestion, never auto-applied.

**Frontend** (`ui/editor/timeline.js`): the junction chip at a paragraph-break
segment gets a subtle dashed accent ring (`.tl-chip-parabreak`, `--accent2`, no
fill unless a real transition is also set) and a `¶ cambio de párrafo — buen punto
para transición` hint appended to its tooltip. Purely additive — the existing
click-to-open-FX-browser / drag-a-transition-here behavior on that same chip is
unchanged.

New config (`config.py`): `PARAGRAPH_BREAK_ENABLED` (True),
`PARAGRAPH_BREAK_WINDOW_SIZE` (12), `PARAGRAPH_BREAK_WINDOW_OVERLAP` (3),
`PARAGRAPH_BREAK_MIN_CONFIDENCE` (4).

Covered by `scripts/test_paragraph_cuts.py` (mocked `paragraph_break` agent,
scratch `MVE_DATA`): a flagged break forces an extra segment boundary with no
sentence text lost (space-joined text across the split matches the unsplit
baseline byte-for-byte) and tags the right segment; no break leaves the EDL
byte-identical to today's; a flagged break never auto-applies a transition
(`EdlSegment.transition.type` stays `"none"`); intra-clip chronology survives the
split; a below-threshold flag and the `PARAGRAPH_BREAK_ENABLED=False` toggle both
skip detection entirely. Also re-verified `scripts/test_intra_clip_order.py`
still passes unmodified.

## vNext — Shorts are a separate, explicit step (owner mental model)

The AI-first-pass line at the top of this doc ("transcribe → sync → best takes
→ order → render → reels") and the "Pipeline orchestration UX" section's
run-all stage list/labels above are now stale on one point: **reels/shorts
generation is no longer part of run-all.** The owner's model is sequential in
two clearly separated phases: (1) edit the main video — pipeline stages +
manual Studio edits — until the FINAL cut is right; (2) only THEN, as a
separate explicit action, generate shorts FROM that finished cut.

- **run-all** (`magic_video_editor/api/pipeline.py`): `STAGE_ORDER` (the
  run-all sequence) now ends at `"render"` and no longer includes `"reels"` —
  the old "reels stage failure must not kill the pipeline (it's last)"
  carve-out is gone along with it, since reels no longer runs inside run-all
  at all. `STAGES` (the full runnable-stage registry `run_stage()`/
  `_run_stage_kind` validate against) still lists `"reels"` unchanged, so
  `POST /projects/{pid}/run/reels` (queue kind `"stage:reels"`) keeps working
  exactly as any other standalone per-stage re-run always has. The existing
  `reel_previews` auto-enqueue hook (`magic_video_editor/queue.py`'s
  `_run_auto_enqueue_hooks`) is unaffected — it already keyed off the queue
  item's own kind (`"stage:reels"` done, or `"run-all"` done with reels
  present), not off "last stage of run-all", so it still fires for a manual
  reels run.
- **UI** (`ui/core.js`'s `STAGES`/run-all progress list, `ui/tabs/reels.js`):
  the run-all chip/popover/progress strip no longer show a "Making shorts"
  row. The Reels tab now shows an explicit empty state when
  `project["reels"]` is empty — "Generar shorts a partir del vídeo final" — 
  that POSTs the standalone reels stage (reusing the existing generic
  `runStage()` helper, same queue endpoint every other per-stage re-run
  uses) and shows progress inline while it runs. Nothing auto-generates reels
  on project open or as part of run-all.
- **Sourcing from the final cut** (`magic_video_editor/pipeline/reels.py`'s
  `_candidate_windows`): sliding reel-candidate windows are now constrained to
  `project["edl"]` (the persisted final-cut segment list, `pipeline/
  ordering.py::build_edl`'s output) when it exists — a sentence only enters a
  window if it falls fully inside one of the EDL's own kept ranges for that
  clip, and a window may never bridge across a boundary between two different
  kept ranges even when they sit close together in time (that gap is exactly
  content the user cut). This means a moment trimmed out of the main video by
  a manual EDL edit can never surface in a short, even if its sentences are
  still flagged `kept: true` at the sentence-analysis level. Falls back to the
  old sentence-`kept`-only behavior only when there's no EDL yet (a project
  that hasn't reached the render stage).
- **Subtitles don't carry over** (`_effective_subtitle_cfg`/`_compose_reel` in
  the same file): a reel's subtitle burn-in is no longer merged over
  `project["subtitles"]` — reels gain their own `reel["subtitles_enabled"]`
  flag (new field, **default `False`**), and `_compose_reel` only writes/burns
  a segment's `.ass` file when that flag is explicitly `True` on the reel
  itself. `reel["subtitle_style"]` (already existed) is now normalized against
  `subtitles.DEFAULTS` directly rather than layered over the project's config,
  so an unset style field never silently inherits the main video's
  size/font/position — social format and sizing genuinely differ from the
  landscape main edit. `ensure_segments` migrates any pre-existing reel
  missing the field to `subtitles_enabled: False` (never inheriting whatever
  the main project's subtitles happened to be). `magic_video_editor/api/
  reels.py`'s `ReelPatch` gained a matching optional `subtitles_enabled: bool`
  field so the Reel Editor can turn a reel's own subtitles on per-reel.

Covered by `scripts/test_shorts_pipeline.py` (LLM mocked, scratch `MVE_DATA`,
one real tiny ffmpeg clip for the subtitle-burn assertions): run-all's
`STAGE_ORDER` excludes `"reels"` and ends at `"render"`; `_run_all_kind` never
invokes the reels stage even though it's still in `STAGES`; a standalone
`"stage:reels"` run through the real queue still produces suggestions and
still auto-enqueues `reel_previews`; `_candidate_windows` never returns a
window spanning a range excluded from `project["edl"]` (and does span it when
there's no EDL yet, proving the fallback); and `_compose_reel` writes zero
`.ass` files for a reel with `subtitles_enabled` unset/False (even with the
project's own subtitles enabled) but does write one once the reel opts in.
Re-verified `scripts/test_reel_dedup.py`, `test_reel_previews.py`, and
`test_reel_transform.py` all still pass unmodified.
