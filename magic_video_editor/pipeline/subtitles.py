"""Shared subtitle support (spec v4 §3 + §6): a single .ass generator used by
final render, preview render and reels, parameterized by project["subtitles"]:

    {
      "enabled": bool, "style": "clean"|"bold"|"karaoke",
      "font": str, "size": "S"|"M"|"L",
      "color": "#RRGGBB", "outline_color": "#RRGGBB",
      "position": "bottom"|"center", "words_per_cue": int,
      "speaker_names": bool,  # v5.8c: "Name:" prefix on diarized cues
    }

Two entry points:
- `ass_for_range` / `write_ass`: per-segment burn-in (render.py, reels.py) —
  cue times are re-based to 0 = `start` of the given clip window, and styled
  for the target `play_res` (the encode's output width/height).
- `cue_list`: maps word timings through the whole persisted EDL into a flat
  list of {edl_t_start, edl_t_end, text} cues in RENDERED-TIMELINE seconds,
  for the frontend's Draft-mode DOM subtitle overlay (api/subtitles.py
  exposes this).

v5.8c speaker diarization: when pipeline/speakers.py has tagged a clip's
transcript segments with `segment["speaker"]`, every cue-producing function
below additionally resolves which speaker a cue belongs to and emits one ASS
style per speaker (PrimaryColour = that speaker's color) instead of the
single "Default" style, and cue dicts/cue_list entries gain a "speaker"
key. Callers may pass the project's `speakers` ([{id, label, color}], from
project["speakers"]) for user-edited labels/colors; omitting it (existing
call sites don't) still works -- a palette is derived on the fly from
pipeline.speakers.DEFAULT_PALETTE in first-appearance order, so this is a
zero-touch upgrade for render.py/reels.py until they're wired to pass the
project's speaker list through.

v7 §7.6 project-level subtitle inline edit: project["subtitles"]["cue_overrides"]
({cue_index: text}) is a NEW config field (normalize_config below). Its index
is the GLOBAL, flat position in cue_list()'s output (the same numbering the
player's inline editor and the Subs tab's cue list both use) -- cue_list()
applies it directly. The .ass burn-in (write_ass/ass_for_range) is called
PER EDL SEGMENT by render.py, where cue indices are LOCAL to that segment's
window, so the global map can't be handed to write_ass as-is; use
`segment_cue_overrides(project, seg_index)` to slice out the right LOCAL
sub-map for a given EDL segment (render.py integration is a follow-up outside
this file's ownership for this task -- see the final report)."""

from .. import store
from .speakers import DEFAULT_PALETTE

DEFAULTS: dict = {
    "enabled": False,
    "style": "clean",
    "font": "Helvetica Neue",
    "size": "M",
    "color": "#FFFFFF",
    "outline_color": "#000000",
    "position": "bottom",
    "words_per_cue": 4,
    "speaker_names": False,  # v5.8c: prefix diarized cues with "<Name>: "
    "vpos": 0.0,  # v7 §7.6: vertical nudge, frame-height fraction, from the position preset
    "cue_overrides": {},  # v7 §7.6: {cue_index: text}, GLOBAL index (see cue_list())
}

STYLES = ("clean", "bold", "karaoke")
SIZES = ("S", "M", "L")
POSITIONS = ("bottom", "center")

# Curated macOS system fonts (spec v4 §5/§6) — surfaced via GET /api/fonts.
FONTS = [
    "Helvetica Neue",
    "Arial Black",
    "Futura",
    "Impact",
    "Avenir Next",
    "SF Pro",
]

# fontsize = play_res height * fraction, per size bucket. M @ 1920 -> ~74px,
# matching the reel style this replaces (keeps default reel output identical).
_SIZE_FRACTION = {"S": 0.0300, "M": 0.03854, "L": 0.0500}

# Per-style multipliers/flags. "clean"/"bold" numbers reproduce the exact
# look the old hardcoded reel ASS_HEADER used (Bold on, Outline 4 @ 1920) as
# the "clean" default, so reels.py stays pixel-identical unless the project
# opts into a different style.
_STYLE_PARAMS = {
    "clean": {"bold": -1, "size_mult": 1.0, "outline_mult": 1.0, "karaoke_highlight": False},
    "bold": {"bold": -1, "size_mult": 1.22, "outline_mult": 1.4, "karaoke_highlight": False},
    "karaoke": {"bold": -1, "size_mult": 1.08, "outline_mult": 1.15, "karaoke_highlight": True},
}

_BASE_OUTLINE = 4.0  # px @ PlayResY=1920, style "clean" (matches old reel header)
_KARAOKE_HIGHLIGHT = "#FFC93C"  # gold — used when the user hasn't customized color
_MAX_CUE_GAP = 0.6  # seconds; a bigger gap between words always starts a new cue


def normalize_config(raw: dict | None) -> dict:
    """Merge `raw` (project.get("subtitles"), possibly None/partial/stale)
    over DEFAULTS and clamp enum-ish fields to known values."""
    cfg = dict(DEFAULTS)
    cfg.update(raw or {})
    if cfg.get("style") not in STYLES:
        cfg["style"] = DEFAULTS["style"]
    if cfg.get("size") not in SIZES:
        cfg["size"] = DEFAULTS["size"]
    if cfg.get("position") not in POSITIONS:
        cfg["position"] = DEFAULTS["position"]
    try:
        wpc = int(cfg.get("words_per_cue") or DEFAULTS["words_per_cue"])
    except (TypeError, ValueError):
        wpc = DEFAULTS["words_per_cue"]
    cfg["words_per_cue"] = max(1, min(12, wpc))
    if not (isinstance(cfg.get("color"), str) and cfg["color"].startswith("#")):
        cfg["color"] = DEFAULTS["color"]
    if not (isinstance(cfg.get("outline_color"), str) and cfg["outline_color"].startswith("#")):
        cfg["outline_color"] = DEFAULTS["outline_color"]
    if cfg.get("font") not in FONTS:
        cfg["font"] = DEFAULTS["font"]
    cfg["speaker_names"] = bool(cfg.get("speaker_names", DEFAULTS["speaker_names"]))
    try:
        vpos = float(cfg.get("vpos") or 0.0)
    except (TypeError, ValueError):
        vpos = 0.0
    cfg["vpos"] = max(-0.35, min(0.35, vpos))
    cue_overrides = cfg.get("cue_overrides")
    cfg["cue_overrides"] = cue_overrides if isinstance(cue_overrides, dict) else {}
    return cfg


def _hex_to_ass(hex_color: str, alpha_hex: str = "00") -> str:
    """'#RRGGBB' -> ASS '&HAABBGGRR' (BGR order, alpha inverted-transparency:
    00=opaque, FF=fully transparent)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha_hex}{b}{g}{r}".upper()


def _style_name(speaker_id: str) -> str:
    """ASS style names may not contain spaces/commas; speaker ids ("S1"...)
    are already safe, this just namespaces them away from "Default"."""
    return f"Speaker_{speaker_id}"


def _style_line(
    cfg: dict,
    play_res: tuple[int, int],
    *,
    name: str = "Default",
    color_override: str | None = None,
) -> str:
    _, play_h = play_res
    sp = _STYLE_PARAMS[cfg["style"]]

    fontsize = max(10, round(play_h * _SIZE_FRACTION[cfg["size"]] * sp["size_mult"]))
    outline = max(1, round(_BASE_OUTLINE * sp["outline_mult"]))

    primary_hex = color_override or cfg["color"]
    if color_override is None and sp["karaoke_highlight"] and primary_hex == DEFAULTS["color"]:
        # v1 karaoke has no per-word timing (spec: "approximate with bold +
        # primary colour highlight") — fall back to a highlight colour
        # unless the user already picked their own.
        primary_hex = _KARAOKE_HIGHLIGHT

    primary = _hex_to_ass(primary_hex, "00")
    outline_color = _hex_to_ass(cfg["outline_color"], "80")  # ~50% transparent
    back = "&H00000000"

    alignment = 5 if cfg["position"] == "center" else 2
    margin_lr = max(0, round(play_res[0] * 0.0556))
    # v7 §7.6: "vpos" is the live vertical-drag nudge (fraction of frame
    # height, positive = dragged DOWN, see ui/editor/player.js's convention).
    # Only wired into the "bottom" alignment, where ASS MarginV is a plain
    # distance-from-the-bottom-edge -- subtracting the nudge moves the text
    # down, exactly like the DOM overlay does. "center" alignment (5) mostly
    # ignores MarginV in libass, so vpos there stays a DOM-preview-only
    # approximation (documented, not attempted here to avoid guessing at
    # renderer-specific behavior for a rarely-dragged-from preset).
    vpos = float(cfg.get("vpos") or 0.0)
    margin_v = max(0, round(play_h * 0.0729 - vpos * play_h)) if cfg["position"] == "bottom" else 0

    font = cfg["font"]
    return (
        f"Style: {name},{font},{fontsize},{primary},{primary},{outline_color},{back},"
        f"{sp['bold']},0,0,0,100,100,0,0,1,{outline},0,{alignment},"
        f"{margin_lr},{margin_lr},{margin_v},1"
    )


def _header(
    cfg: dict, play_res: tuple[int, int], speaker_colors: dict[str, str] | None = None
) -> str:
    """`speaker_colors` ({speaker_id: "#RRGGBB"}), when given, adds one extra
    [V4+ Styles] row per speaker (v5.8c) -- "Default" is kept too, used for
    any cue whose speaker couldn't be resolved."""
    w, h = play_res
    style_lines = [_style_line(cfg, play_res)]
    for speaker_id, color in (speaker_colors or {}).items():
        style_lines.append(
            _style_line(cfg, play_res, name=_style_name(speaker_id), color_override=color)
        )
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\n"
        f"PlayResY: {h}\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(style_lines)
        + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _ts(t: float) -> str:
    """ASS timestamp: H:MM:SS.CS (centiseconds)."""
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{int(s):02d}.{round((s % 1) * 100):02d}"


def _words_in_range(clip: dict, start: float, end: float) -> list[dict]:
    """Words are tagged with `_speaker` (transient, not persisted) from
    their whisper segment's `segment["speaker"]` when diarization has run
    (pipeline/speakers.py), so downstream cue-grouping can break on speaker
    changes and pick the right ASS style."""
    words = []
    for seg in (clip.get("transcript") or {}).get("segments", []):
        spk = seg.get("speaker")
        for w in seg["words"]:
            if start <= w["s"] < end:
                words.append({**w, "_speaker": spk} if spk else w)
    words.sort(key=lambda w: w["s"])
    return words


def _speaker_palette(
    clip: dict, speakers: list[dict] | None = None
) -> dict[str, dict[str, str]]:
    """{speaker_id: {"color": "#RRGGBB", "label": str}} for every speaker id
    tagged on `clip`'s transcript segments. Uses `speakers` (project
    ["speakers"], user-edited labels/colors) when a matching id is found;
    otherwise falls back to pipeline.speakers.DEFAULT_PALETTE assigned in
    first-appearance order, labeled with the raw id (e.g. "S1")."""
    by_id = {sp["id"]: sp for sp in (speakers or [])}
    palette: dict[str, dict[str, str]] = {}
    for seg in (clip.get("transcript") or {}).get("segments", []):
        sid = seg.get("speaker")
        if not sid or sid in palette:
            continue
        known = by_id.get(sid)
        default_color = DEFAULT_PALETTE[len(palette) % len(DEFAULT_PALETTE)]
        palette[sid] = {
            "color": known["color"] if known else default_color,
            "label": known["label"] if known else sid,
        }
    return palette


def _group_words_into_cues(
    words: list[dict], ref_time: float, words_per_cue: int, max_gap: float = _MAX_CUE_GAP
) -> list[tuple[float, float, str, str | None]]:
    """Group consecutive words into cues of up to `words_per_cue` words,
    starting a new cue early if the gap to the next word exceeds `max_gap`
    OR the speaker changes (v5.8c, see `_words_in_range`) -- a cue always
    belongs to exactly one speaker so it can use that speaker's ASS style.
    Returned times are re-based so 0 == ref_time; the 4th tuple element is
    the cue's speaker id, or None when unresolved/undiarized."""
    cues, cue = [], []
    for i, w in enumerate(words):
        cue.append(w)
        at_end = i + 1 == len(words)
        gap_too_big = (not at_end) and (words[i + 1]["s"] - w["e"] > max_gap)
        speaker_changes = (not at_end) and (words[i + 1].get("_speaker") != w.get("_speaker"))
        if len(cue) >= words_per_cue or gap_too_big or speaker_changes or at_end:
            text = " ".join(x["w"] for x in cue).strip().replace("\n", " ")
            if text:
                speaker = cue[0].get("_speaker")
                cues.append((cue[0]["s"] - ref_time, cue[-1]["e"] - ref_time, text, speaker))
            cue = []
    return cues


def _cue_display_text(cfg: dict, text: str, speaker_id: str | None, palette: dict) -> str:
    """`text`, prefixed with "<Label>: " when `cfg["speaker_names"]` is on
    and `speaker_id` resolves to a known label (v5.8c "Name:" prefix
    toggle)."""
    if cfg["speaker_names"] and speaker_id and speaker_id in palette:
        return f"{palette[speaker_id]['label']}: {text}"
    return text


def ass_for_range(
    clip: dict,
    start: float,
    end: float,
    cfg: dict | None,
    play_res: tuple[int, int],
    cue_overrides: dict | None = None,
    speakers: list[dict] | None = None,
) -> str:
    """Full .ass file content for one render segment/reel window [start, end)
    of `clip`'s transcript. Times are re-based (0 == start), matching how the
    caller will burn it via a `-ss start -t (end-start)` cut. `cue_overrides`
    (spec v5 reel data model, {cue_index: text}) substitutes cue TEXT by
    index — typo fixes from the Reel Editor's Subs tab — keeping timing.
    `speakers` (v5.8c, project["speakers"]) supplies user-edited labels/
    colors for the per-speaker styles; omit it to fall back to a derived
    default palette (see `_speaker_palette`)."""
    cfg = normalize_config(cfg)
    words = _words_in_range(clip, start, end)
    cues = _group_words_into_cues(words, start, cfg["words_per_cue"])
    cues = _apply_cue_overrides(cues, cue_overrides)
    palette = _speaker_palette(clip, speakers)
    speaker_colors = {sid: p["color"] for sid, p in palette.items()}
    events = [
        f"Dialogue: 0,{_ts(t0)},{_ts(t1)},"
        f"{_style_name(speaker) if speaker in palette else 'Default'},,0,0,0,,"
        f"{_cue_display_text(cfg, text, speaker, palette)}"
        for t0, t1, text, speaker in cues
        if text
    ]
    header = _header(cfg, play_res, speaker_colors)
    return header + "\n".join(events) + ("\n" if events else "")


def write_ass(
    path: str,
    clip: dict,
    start: float,
    end: float,
    cfg: dict | None,
    play_res: tuple[int, int],
    cue_overrides: dict | None = None,
    speakers: list[dict] | None = None,
) -> str:
    """ass_for_range(...) written to `path`. Returns `path` for convenience."""
    content = ass_for_range(clip, start, end, cfg, play_res, cue_overrides, speakers)
    with open(path, "w") as f:
        f.write(content)
    return path


def _apply_cue_overrides(
    cues: list[tuple[float, float, str, str | None]], cue_overrides: dict | None
) -> list[tuple[float, float, str, str | None]]:
    """Replace cue text by index. Indices may arrive as JSON-round-tripped
    strings ("0") as well as ints — both are honored. Unknown/out-of-range
    indices are ignored rather than raising, since the window/style may have
    changed since the override was recorded."""
    if not cue_overrides:
        return cues
    out = list(cues)
    for i, (t0, t1, _text, speaker) in enumerate(cues):
        override = cue_overrides.get(i, cue_overrides.get(str(i)))
        if override is not None:
            text = str(override).strip().replace("\n", " ")
            out[i] = (t0, t1, text, speaker)
    return out


def cues_for_range(
    clip: dict,
    start: float,
    end: float,
    cfg: dict | None = None,
    speakers: list[dict] | None = None,
) -> list[dict]:
    """[{index, start, end, text, speaker, speaker_label, speaker_color}]
    cues for a clip window using the same word-grouping as `ass_for_range`,
    WITHOUT rendering an .ass file — exposes the index each cue would have
    so callers (the Reel Editor's Subs tab, reels.py's cues endpoint) can key
    `cue_overrides` correctly. Times are re-based so 0 == `start`, matching
    `ass_for_range`. `text` already carries the "Name:" prefix when
    `cfg["speaker_names"]` is on (v5.8c) -- speaker/speaker_label/
    speaker_color are still exposed separately for UI tinting."""
    cfg = normalize_config(cfg)
    words = _words_in_range(clip, start, end)
    cues = _group_words_into_cues(words, start, cfg["words_per_cue"])
    palette = _speaker_palette(clip, speakers)
    out = []
    for i, (t0, t1, text, speaker) in enumerate(cues):
        info = palette.get(speaker)
        out.append(
            {
                "index": i,
                "start": round(t0, 3),
                "end": round(t1, 3),
                "text": _cue_display_text(cfg, text, speaker, palette),
                "speaker": speaker,
                "speaker_label": info["label"] if info else None,
                "speaker_color": info["color"] if info else None,
            }
        )
    return out


CueTuple = tuple[float, float, str, str | None]


def _grouped_cues_per_segment(project: dict, cfg: dict) -> list[list[CueTuple]]:
    """Per-EDL-segment grouped (pre-override, pre-display-text) cues, in
    exactly the order/grouping `cue_list` iterates -- the shared building
    block behind both cue_list's GLOBAL flat index and
    `segment_cue_overrides`'s per-segment slicing, so the two indexing
    schemes can never drift apart from each other."""
    segments = project.get("edl") or []
    out = []
    for seg in segments:
        try:
            clip = store.get_clip(project, seg["clip_id"])
        except KeyError:
            out.append([])
            continue
        words = _words_in_range(clip, seg["start"], seg["end"])
        out.append(_group_words_into_cues(words, seg["start"], cfg["words_per_cue"]))
    return out


def cue_list(project: dict) -> list[dict]:
    """[{index, edl_t_start, edl_t_end, text, speaker, speaker_label,
    speaker_color}] mapping every kept word through the persisted EDL into
    rendered-timeline seconds, for the Draft-mode DOM subtitle overlay (both
    the overlay and the burn-in group words the same way, so Draft preview
    matches the real burn). Words_per_cue/gap/speaker_names come from
    project["subtitles"]; a cue never spans an EDL segment boundary (v5.8c:
    nor a speaker change). speaker_label/speaker_color use project["speakers"]
    when present.

    `index` (v7 §7.6) is this cue's GLOBAL, flat position across the whole
    EDL (0, 1, 2, ... in the order cues are emitted here) -- it's the key
    space `project["subtitles"]["cue_overrides"]` uses, and `text` already
    reflects any override for that index. The player's inline edit and the
    Subs tab's cue list both address cues by this same `index`."""
    cfg = normalize_config(project.get("subtitles"))
    cue_overrides = cfg["cue_overrides"]
    speakers = project.get("speakers")
    segments = project.get("edl") or []
    per_segment = _grouped_cues_per_segment(project, cfg)
    cues: list[dict] = []
    t_cursor = 0.0
    global_i = 0
    for seg, grouped in zip(segments, per_segment, strict=True):
        seg_len = max(0.0, seg["end"] - seg["start"])
        try:
            clip = store.get_clip(project, seg["clip_id"])
        except KeyError:
            t_cursor += seg_len
            continue
        palette = _speaker_palette(clip, speakers)
        for t0, t1, text, speaker in grouped:
            override = cue_overrides.get(global_i, cue_overrides.get(str(global_i)))
            if override is not None:
                text = str(override).strip().replace("\n", " ")
            info = palette.get(speaker)
            cues.append(
                {
                    "index": global_i,
                    "edl_t_start": round(t_cursor + max(0.0, t0), 3),
                    "edl_t_end": round(t_cursor + max(0.0, t1), 3),
                    "text": _cue_display_text(cfg, text, speaker, palette),
                    "speaker": speaker,
                    "speaker_label": info["label"] if info else None,
                    "speaker_color": info["color"] if info else None,
                }
            )
            global_i += 1
        t_cursor += seg_len
    return cues


def segment_cue_overrides(project: dict, seg_index: int) -> dict[int, str]:
    """LOCAL cue_overrides slice (0-based WITHIN that segment's own cue
    window, matching what `ass_for_range`/`write_ass`'s `cue_overrides` param
    already expects) for EDL segment `seg_index`, derived from the project-
    level GLOBAL map (project["subtitles"]["cue_overrides"], keyed by
    cue_list()'s flat `index`).

    Intended caller: render.py's per-segment .ass burn-in loop (final render
    + preview render share one `_build`), e.g.:
        overrides = subtitles.segment_cue_overrides(project, i)
        subtitles.write_ass(ass_path, clip, seg["start"], seg["end"], sub_cfg,
                             (width, height), cue_overrides=overrides,
                             speakers=project.get("speakers"))
    That one-line wiring is outside this file's ownership for this task --
    flagged for the integrator/render.py owner. Always safe to call: returns
    {} when there's nothing to override or `seg_index` is out of range."""
    cfg = normalize_config(project.get("subtitles"))
    raw_overrides = cfg["cue_overrides"]
    if not raw_overrides:
        return {}
    segments = project.get("edl") or []
    if not (0 <= seg_index < len(segments)):
        return {}
    per_segment = _grouped_cues_per_segment(project, cfg)
    offset = sum(len(c) for c in per_segment[:seg_index])
    count = len(per_segment[seg_index])
    out: dict[int, str] = {}
    for local_i in range(count):
        global_i = offset + local_i
        val = raw_overrides.get(global_i, raw_overrides.get(str(global_i)))
        if val is not None:
            out[local_i] = val
    return out
