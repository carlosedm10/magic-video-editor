"""Stage 6 — Render the main cut: EDL segments -> per-segment frame-accurate
re-encode (normalized to the main camera's format) -> lossless concat.
If a camera clip is synced with an external audio recording, its audio is
replaced by the aligned external track.

Junction transitions (project["edl"][i]["transition"]): "fade" is baked into
the per-segment encode as a video fade + audio afade on the outgoing tail of
the previous segment and the incoming head of this one (cheap, no extra
pass). "crossfade" is applied after all segments are encoded by merging each
crossfade junction's two adjacent files with ffmpeg xfade/acrossfade,
processing junctions left-to-right so a merged file can chain into the next
junction.

Two render profiles (spec v4 §3, §5):
- `run()` — the final render, current quality (RENDER_CRF/RENDER_PRESET),
  written under the user's configured export directory
  (settings.export_dir, default ~/Movies/Magic Video Editor) in a
  per-project subfolder; also recorded in project["renders"].
- `render_preview()` — a fast 540p/crf32/ultrafast render with every effect
  included (color, transitions, subtitles, audio enhance), written to
  <project_dir>/preview/preview.mp4, plus a manifest hash of the config it
  corresponds to (project["preview"] = {path, manifest}) so the timeline
  render-bar (spec §3) can tell whether the preview is stale.

Both profiles share `_build`/`_encode_segment` so subtitle burn-in, color,
transitions and audio-enhance behave identically between them; only
resolution/crf/preset/output-location differ.
"""

import hashlib
import json
import re
import time
from pathlib import Path

from .. import config, ffmpeg_utils, settings, store
from . import audio_enhance, filters, ordering, subtitles, sync

PREVIEW_HEIGHT = 540
PREVIEW_CRF = 32
PREVIEW_PRESET = "ultrafast"
PREVIEW_MAX_FPS = 30.0


def _target_format(project: dict) -> tuple[int, int, float]:
    mains = [c for c in project["clips"] if c["is_main"] and c["info"] and c["info"]["has_video"]]
    cams = mains or [c for c in project["clips"] if c["info"] and c["info"]["has_video"]]
    if not cams:
        raise RuntimeError("No video clips in project.")
    info = cams[0]["info"]
    return info["width"], info["height"], info["fps"] or 30.0


def _normalize_transition(t: dict | None) -> dict:
    """Defensive normalization mirroring api/edl.py's validation, in case the
    persisted EDL predates the transition field or was written some other
    way."""
    if not t:
        return {"type": "none", "duration": 0.5}
    ttype = t.get("type") or "none"
    if ttype not in ("none", "fade", "crossfade"):
        ttype = "none"
    if ttype == "none":
        return {"type": "none", "duration": 0.5}
    duration = float(t.get("duration") or 0.5)
    duration = min(1.5, max(0.2, duration))
    return {"type": ttype, "duration": duration}


def _safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip()
    return cleaned or "project"


def _sanitize_export_title(name: str) -> str:
    """Filename stem sanitization for exported files (spec v5 addendum
    "export filenames"): strip /:\\ and control chars, collapse whitespace.
    Unlike `_safe_folder_name` (folder names, replaces with "_"), separators
    are simply dropped so "A: B" -> "A B", not "A_ B"."""
    cleaned = re.sub(r'[/:\\\x00-\x1f]', "", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "project"


def _unique_export_path(export_dir: Path, stem: str, ext: str = ".mp4") -> Path:
    """Dedupe an export filename against what's already on disk: "<stem>.mp4",
    then "<stem> (2).mp4", "<stem> (3).mp4", ... (spec v5 addendum)."""
    candidate = export_dir / f"{stem}{ext}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = export_dir / f"{stem} ({n}){ext}"
        if not candidate.exists():
            return candidate
        n += 1


def _export_dir_for(project: dict) -> Path:
    """settings.export_dir (owned/added by the settings agent in parallel) —
    read defensively so this keeps working whether or not that key exists
    yet."""
    try:
        raw = settings.load().get("export_dir")
    except Exception:
        raw = None
    root = Path(raw).expanduser() if raw else (Path.home() / "Movies" / "Magic Video Editor")
    d = root / _safe_folder_name(project.get("name") or project["id"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _preview_manifest(project: dict) -> str:
    """Stable hash of the config a preview render corresponds to (edl + color
    + subtitles + audio_enhance) so the UI render-bar can compare it against
    current project state and know if the preview is stale."""
    payload = {
        "edl": project.get("edl"),
        "color": project.get("color"),
        "subtitles": project.get("subtitles"),
        "audio_enhance": project.get("audio_enhance"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _encode_segment(
    src: str,
    start: float,
    end: float,
    dst: str,
    width: int,
    height: int,
    fps: float,
    audio_src: str | None,
    audio_start: float | None,
    vf_extra: str,
    ass_path: str | None,
    fade_in: float,
    fade_out: float,
    crf: int,
    preset: str,
) -> None:
    """Normalized re-encode (same geometry contract as ffmpeg_utils.
    cut_segment) plus optional subtitle burn-in and head/tail fade — all in
    one pass. Self-contained (rather than calling cut_segment) so crf/preset
    can vary per render profile without touching ffmpeg_utils.py."""
    dur = max(0.05, end - start)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}"
    )
    if vf_extra:
        vf = f"{vf_extra},{vf}"
    if ass_path:
        vf += f",ass={ass_path}"

    af_parts = []
    if fade_in > 0:
        vf += f",fade=t=in:st=0:d={fade_in:.3f}"
        af_parts.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        st = max(0.0, dur - fade_out)
        vf += f",fade=t=out:st={st:.3f}:d={fade_out:.3f}"
        af_parts.append(f"afade=t=out:st={st:.3f}:d={fade_out:.3f}")

    cmd = [ffmpeg_utils.ffmpeg_bin(), "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    if audio_src is not None:
        cmd += [
            "-ss",
            f"{audio_start:.3f}",
            "-t",
            f"{dur:.3f}",
            "-i",
            audio_src,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    cmd += ["-vf", vf]
    if af_parts:
        cmd += ["-af", ",".join(af_parts)]
    cmd += [
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-video_track_timescale",
        "90000",
        "-threads",
        str(ffmpeg_utils.ffmpeg_threads()),
        dst,
    ]
    # heavy=True: a full re-encode, must respect the RAM guard + concurrency
    # gate (resource-safety spec) just like ffmpeg_utils.cut_segment.
    ffmpeg_utils.run(cmd, heavy=True)


def _merge_crossfades(
    seg_paths: list[str], transitions: list[dict], work: Path, log, crf: int, preset: str
) -> list[str]:
    """Merge crossfade junctions left-to-right. transitions[i] is the
    transition INTO seg_paths[i]; a crossfade at index i merges paths[i-1]
    and paths[i] via xfade+acrossfade, offset = prevDuration - duration. The
    merged file replaces both entries and can itself be the left side of the
    next junction (chaining), so durations are re-probed off disk each time."""
    paths = list(seg_paths)
    trans = list(transitions)
    merge_idx = 0
    i = 1
    while i < len(paths):
        if trans[i].get("type") != "crossfade":
            i += 1
            continue
        left, right = paths[i - 1], paths[i]
        d = trans[i]["duration"]
        left_dur = ffmpeg_utils.clip_info(left)["duration"]
        right_dur = ffmpeg_utils.clip_info(right)["duration"]
        d = min(d, max(0.1, left_dur - 0.1), max(0.1, right_dur - 0.1))
        offset = max(0.0, left_dur - d)
        merged = work / f"xfade_{merge_idx:04d}.mp4"
        merge_idx += 1
        log(f"Crossfading junction ({d:.2f}s)...")
        cmd = [
            ffmpeg_utils.ffmpeg_bin(),
            "-y",
            "-i",
            left,
            "-i",
            right,
            "-filter_complex",
            f"[0:v][1:v]xfade=transition=fade:duration={d:.3f}:offset={offset:.3f}[v];"
            f"[0:a][1:a]acrossfade=d={d:.3f}[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            preset,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-video_track_timescale",
            "90000",
            "-threads",
            str(ffmpeg_utils.ffmpeg_threads()),
            str(merged),
        ]
        # heavy=True: same reasoning as _encode_segment above -- a real
        # re-encode, must go through the RAM guard + concurrency gate.
        ffmpeg_utils.run(cmd, heavy=True)
        paths[i - 1 : i + 1] = [str(merged)]
        del trans[i]
        # don't advance i: the newly merged block may chain into the next junction
    return paths


def _build(
    log,
    project: dict,
    out_path: Path,
    *,
    width: int,
    height: int,
    fps: float,
    crf: int,
    preset: str,
    work_tag: str,
) -> None:
    """Shared encode pipeline for both render profiles: per-segment encode
    (color + fades + subtitle burn-in) -> crossfade merges -> concat ->
    optional audio enhance. Writes the final file to `out_path`."""
    segments = project["edl"]
    pdir = store.project_dir(project["id"])
    work = pdir / "work" / work_tag
    work.mkdir(parents=True, exist_ok=True)

    color_vf = filters.build_vf(project.get("color"))
    transitions = [_normalize_transition(seg.get("transition")) for seg in segments]

    sub_cfg = subtitles.normalize_config(project.get("subtitles"))
    burn_subs = bool(sub_cfg["enabled"]) and ffmpeg_utils.supports_subtitles()
    if sub_cfg["enabled"] and not burn_subs:
        log("Subtitles enabled but ffmpeg has no libass — rendering without burned subtitles")

    log(f"Rendering {len(segments)} segments at {width}x{height}@{fps:g}...")
    seg_paths = []
    for i, seg in enumerate(segments):
        clip = store.get_clip(project, seg["clip_id"])
        audio = sync.audio_source_for(project, seg["clip_id"])
        audio_src, audio_start = (None, None)
        if audio:
            audio_src, delta = audio
            audio_start = seg["start"] + delta
            if audio_start < 0:
                audio_src, audio_start = None, None  # external track starts later

        ass_path = None
        if burn_subs:
            ass_path = str(work / f"sub_{i:04d}.ass")
            subtitles.write_ass(ass_path, clip, seg["start"], seg["end"], sub_cfg, (width, height))

        out = work / f"seg_{i:04d}.mp4"

        # fade-in on this segment's head (transitions[0] == fade means fade-from-black;
        # crossfade on the very first segment has no predecessor and is ignored here).
        fade_in = transitions[i]["duration"] if transitions[i]["type"] == "fade" else 0.0
        # fade-out on this segment's tail, driven by the NEXT segment's transition.
        fade_out = 0.0
        if i + 1 < len(segments) and transitions[i + 1]["type"] == "fade":
            fade_out = transitions[i + 1]["duration"]
        seg_len = max(0.05, seg["end"] - seg["start"])
        if fade_in + fade_out > seg_len:
            scale = seg_len / (fade_in + fade_out)
            fade_in *= scale
            fade_out *= scale

        _encode_segment(
            clip["path"],
            seg["start"],
            seg["end"],
            str(out),
            width,
            height,
            fps,
            audio_src,
            audio_start,
            color_vf,
            ass_path,
            fade_in,
            fade_out,
            crf,
            preset,
        )
        seg_paths.append(str(out))
        log.progress((i + 1) / (len(segments) + 1))
        log(
            f"seg {i + 1}/{len(segments)}: {clip['filename']} "
            f"{seg['start']:.1f}-{seg['end']:.1f}s" + (" [external audio]" if audio_src else "")
        )

    seg_paths = _merge_crossfades(seg_paths, transitions, work, log, crf, preset)

    log("Concatenating...")
    ffmpeg_utils.concat_segments(seg_paths, str(out_path), work)

    if project.get("audio_enhance"):
        log("Enhancing voice audio...")
        raw_wav = work / "final_audio.wav"
        enhanced_wav = work / "final_audio_enhanced.wav"
        remuxed = work / "final_remuxed.mp4"
        ffmpeg_utils.extract_wav(str(out_path), str(raw_wav))
        audio_enhance.enhance(str(raw_wav), str(enhanced_wav))
        ffmpeg_utils.mux_audio(str(out_path), str(enhanced_wav), str(remuxed))
        remuxed.replace(out_path)


def _ensure_edl(project: dict) -> list[dict]:
    segments = project.get("edl")
    if not segments:
        segments = ordering.build_edl(project)
        project["edl"] = segments
        store.save(project)
    return segments


def run(log, project: dict) -> None:
    """Final render: current quality, written under the configured export
    directory in a per-project subfolder (spec v4 §5 General settings)."""
    segments = _ensure_edl(project)
    if not segments:
        raise RuntimeError("EDL is empty — no kept sentences to render.")

    width, height, fps = _target_format(project)
    export_dir = _export_dir_for(project)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    title = _sanitize_export_title(project.get("name") or project["id"])
    final = _unique_export_path(export_dir, title)

    _build(
        log,
        project,
        final,
        width=width,
        height=height,
        fps=fps,
        crf=config.RENDER_CRF,
        preset=config.RENDER_PRESET,
        work_tag=f"render_{int(time.time())}",
    )

    total = sum(s["end"] - s["start"] for s in segments)
    project.setdefault("renders", []).append(
        {
            "path": str(final),
            "at": stamp,
            "segments": len(segments),
            "duration": round(total, 1),
        }
    )
    store.save(project)
    log(f"Done: {final.name} ({total:.0f}s from {len(segments)} segments) -> {export_dir}")


def render_preview(log, project: dict) -> None:
    """Preview render (spec v4 §3): 540p/crf32/ultrafast, every effect
    included, written to <project_dir>/preview/preview.mp4. Records
    project["preview"] = {path, manifest} so the timeline render-bar can
    detect staleness by comparing manifests."""
    segments = _ensure_edl(project)
    if not segments:
        raise RuntimeError("EDL is empty — nothing to preview.")

    width, height, fps = _target_format(project)
    preview_h = PREVIEW_HEIGHT
    preview_w = max(2, round(width * preview_h / height / 2) * 2)
    preview_fps = min(fps or 30.0, PREVIEW_MAX_FPS)

    pdir = store.project_dir(project["id"])
    preview_dir = pdir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    out_path = preview_dir / "preview.mp4"

    log(f"Rendering draft preview {preview_w}x{preview_h}...")
    _build(
        log,
        project,
        out_path,
        width=preview_w,
        height=preview_h,
        fps=preview_fps,
        crf=PREVIEW_CRF,
        preset=PREVIEW_PRESET,
        work_tag=f"preview_{int(time.time())}",
    )

    manifest = _preview_manifest(project)
    project["preview"] = {"path": str(out_path), "manifest": manifest}
    store.save(project)
    log(f"Preview ready: {out_path.name} (manifest {manifest})")


# ---------- queue integration (spec v4 §2) ----------
# cutroom/queue.py dispatches every registered runner as
# runner(log, project, payload) (see queue._run_item) -- run()/render_preview()
# themselves only take (log, project), so they're wrapped rather than
# registered directly (a bare `KIND_RUNNERS["final_render"] = run` blew up at
# runtime with "takes 2 positional arguments but 3 were given").
try:
    from .. import queue  # type: ignore[import-not-found]

    queue.register_runner("final_render", lambda log, project, payload=None: run(log, project))
    queue.register_runner(
        "preview_render", lambda log, project, payload=None: render_preview(log, project)
    )
except ImportError:
    # cutroom/queue.py doesn't exist in this working tree.
    pass
