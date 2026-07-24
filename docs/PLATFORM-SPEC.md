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
- New `cutroom/queue.py`: per-project FIFO persisted in project.json ("queue": [...]),
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
  `cutroom/pipeline/subtitles.py`, parameterized by the style config; final render and
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
Replace the OpenAIChatModel+OllamaProvider construction in cutroom/agents/agents.py with
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
