"""Shared subtitle support (spec v4 §3 + §6): a single .ass generator used by
final render, preview render and reels, parameterized by project["subtitles"]:

    {
      "enabled": bool, "style": "clean"|"bold"|"karaoke",
      "font": str, "size": "S"|"M"|"L",
      "color": "#RRGGBB", "outline_color": "#RRGGBB",
      "position": "bottom"|"center", "words_per_cue": int,
    }

Two entry points:
- `ass_for_range` / `write_ass`: per-segment burn-in (render.py, reels.py) —
  cue times are re-based to 0 = `start` of the given clip window, and styled
  for the target `play_res` (the encode's output width/height).
- `cue_list`: maps word timings through the whole persisted EDL into a flat
  list of {edl_t_start, edl_t_end, text} cues in RENDERED-TIMELINE seconds,
  for the frontend's Draft-mode DOM subtitle overlay (api/subtitles.py
  exposes this)."""

from .. import store

DEFAULTS: dict = {
    "enabled": False,
    "style": "clean",
    "font": "Helvetica Neue",
    "size": "M",
    "color": "#FFFFFF",
    "outline_color": "#000000",
    "position": "bottom",
    "words_per_cue": 4,
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
    return cfg


def _hex_to_ass(hex_color: str, alpha_hex: str = "00") -> str:
    """'#RRGGBB' -> ASS '&HAABBGGRR' (BGR order, alpha inverted-transparency:
    00=opaque, FF=fully transparent)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha_hex}{b}{g}{r}".upper()


def _style_line(cfg: dict, play_res: tuple[int, int]) -> str:
    _, play_h = play_res
    sp = _STYLE_PARAMS[cfg["style"]]

    fontsize = max(10, round(play_h * _SIZE_FRACTION[cfg["size"]] * sp["size_mult"]))
    outline = max(1, round(_BASE_OUTLINE * sp["outline_mult"]))

    primary_hex = cfg["color"]
    if sp["karaoke_highlight"] and primary_hex == DEFAULTS["color"]:
        # v1 karaoke has no per-word timing (spec: "approximate with bold +
        # primary colour highlight") — fall back to a highlight colour
        # unless the user already picked their own.
        primary_hex = _KARAOKE_HIGHLIGHT

    primary = _hex_to_ass(primary_hex, "00")
    outline_color = _hex_to_ass(cfg["outline_color"], "80")  # ~50% transparent
    back = "&H00000000"

    alignment = 5 if cfg["position"] == "center" else 2
    margin_lr = max(0, round(play_res[0] * 0.0556))
    margin_v = max(0, round(play_h * 0.0729)) if cfg["position"] == "bottom" else 0

    font = cfg["font"]
    return (
        f"Style: Default,{font},{fontsize},{primary},{primary},{outline_color},{back},"
        f"{sp['bold']},0,0,0,100,100,0,0,1,{outline},0,{alignment},"
        f"{margin_lr},{margin_lr},{margin_v},1"
    )


def _header(cfg: dict, play_res: tuple[int, int]) -> str:
    w, h = play_res
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
        f"{_style_line(cfg, play_res)}\n\n"
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
    words = []
    for seg in (clip.get("transcript") or {}).get("segments", []):
        words.extend(w for w in seg["words"] if start <= w["s"] < end)
    words.sort(key=lambda w: w["s"])
    return words


def _group_words_into_cues(
    words: list[dict], ref_time: float, words_per_cue: int, max_gap: float = _MAX_CUE_GAP
) -> list[tuple[float, float, str]]:
    """Group consecutive words into cues of up to `words_per_cue` words,
    starting a new cue early if the gap to the next word exceeds `max_gap`.
    Returned times are re-based so 0 == ref_time."""
    cues, cue = [], []
    for i, w in enumerate(words):
        cue.append(w)
        at_end = i + 1 == len(words)
        gap_too_big = (not at_end) and (words[i + 1]["s"] - w["e"] > max_gap)
        if len(cue) >= words_per_cue or gap_too_big or at_end:
            text = " ".join(x["w"] for x in cue).strip().replace("\n", " ")
            if text:
                cues.append((cue[0]["s"] - ref_time, cue[-1]["e"] - ref_time, text))
            cue = []
    return cues


def ass_for_range(
    clip: dict,
    start: float,
    end: float,
    cfg: dict | None,
    play_res: tuple[int, int],
    cue_overrides: dict | None = None,
) -> str:
    """Full .ass file content for one render segment/reel window [start, end)
    of `clip`'s transcript. Times are re-based (0 == start), matching how the
    caller will burn it via a `-ss start -t (end-start)` cut. `cue_overrides`
    (spec v5 reel data model, {cue_index: text}) substitutes cue TEXT by
    index — typo fixes from the Reel Editor's Subs tab — keeping timing."""
    cfg = normalize_config(cfg)
    words = _words_in_range(clip, start, end)
    cues = _group_words_into_cues(words, start, cfg["words_per_cue"])
    cues = _apply_cue_overrides(cues, cue_overrides)
    events = [
        f"Dialogue: 0,{_ts(t0)},{_ts(t1)},Default,,0,0,0,,{text}"
        for t0, t1, text in cues
        if text
    ]
    return _header(cfg, play_res) + "\n".join(events) + ("\n" if events else "")


def write_ass(
    path: str,
    clip: dict,
    start: float,
    end: float,
    cfg: dict | None,
    play_res: tuple[int, int],
    cue_overrides: dict | None = None,
) -> str:
    """ass_for_range(...) written to `path`. Returns `path` for convenience."""
    content = ass_for_range(clip, start, end, cfg, play_res, cue_overrides)
    with open(path, "w") as f:
        f.write(content)
    return path


def _apply_cue_overrides(
    cues: list[tuple[float, float, str]], cue_overrides: dict | None
) -> list[tuple[float, float, str]]:
    """Replace cue text by index. Indices may arrive as JSON-round-tripped
    strings ("0") as well as ints — both are honored. Unknown/out-of-range
    indices are ignored rather than raising, since the window/style may have
    changed since the override was recorded."""
    if not cue_overrides:
        return cues
    out = list(cues)
    for i, (t0, t1, _text) in enumerate(cues):
        override = cue_overrides.get(i, cue_overrides.get(str(i)))
        if override is not None:
            text = str(override).strip().replace("\n", " ")
            out[i] = (t0, t1, text)
    return out


def cues_for_range(clip: dict, start: float, end: float, cfg: dict | None = None) -> list[dict]:
    """[{index, start, end, text}] cues for a clip window using the same
    word-grouping as `ass_for_range`, WITHOUT rendering an .ass file —
    exposes the index each cue would have so callers (the Reel Editor's Subs
    tab, reels.py's cues endpoint) can key `cue_overrides` correctly. Times
    are re-based so 0 == `start`, matching `ass_for_range`."""
    cfg = normalize_config(cfg)
    words = _words_in_range(clip, start, end)
    cues = _group_words_into_cues(words, start, cfg["words_per_cue"])
    return [
        {"index": i, "start": round(t0, 3), "end": round(t1, 3), "text": text}
        for i, (t0, t1, text) in enumerate(cues)
    ]


def cue_list(project: dict) -> list[dict]:
    """[{edl_t_start, edl_t_end, text}] mapping every kept word through the
    persisted EDL into rendered-timeline seconds, for the Draft-mode DOM
    subtitle overlay (both the overlay and the burn-in group words the same
    way, so Draft preview matches the real burn). Words_per_cue/gap come from
    project["subtitles"]; a cue never spans an EDL segment boundary."""
    cfg = normalize_config(project.get("subtitles"))
    segments = project.get("edl") or []
    cues: list[dict] = []
    t_cursor = 0.0
    for seg in segments:
        seg_len = max(0.0, seg["end"] - seg["start"])
        try:
            clip = store.get_clip(project, seg["clip_id"])
        except KeyError:
            t_cursor += seg_len
            continue
        words = _words_in_range(clip, seg["start"], seg["end"])
        for t0, t1, text in _group_words_into_cues(words, seg["start"], cfg["words_per_cue"]):
            cues.append(
                {
                    "edl_t_start": round(t_cursor + max(0.0, t0), 3),
                    "edl_t_end": round(t_cursor + max(0.0, t1), 3),
                    "text": text,
                }
            )
        t_cursor += seg_len
    return cues
