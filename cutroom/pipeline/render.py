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
junction."""

import time
from pathlib import Path

from .. import config, ffmpeg_utils, store
from . import audio_enhance, filters, ordering, sync


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


def _encode_segment_with_fades(
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
    fade_in: float,
    fade_out: float,
) -> None:
    """Same normalized re-encode as ffmpeg_utils.cut_segment, plus a video
    `fade` + audio `afade` on the head/tail of this segment. cut_segment has
    no audio-filter hook, so this mirrors it directly (still a single pass,
    still routed through ffmpeg_utils' public helpers)."""
    dur = max(0.05, end - start)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps}"
    )
    if vf_extra:
        vf = f"{vf_extra},{vf}"

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
        str(config.RENDER_CRF),
        "-preset",
        config.RENDER_PRESET,
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
    # heavy=True: this is a full re-encode just like cut_segment, so it must
    # respect the same RAM guard + concurrency gate (resource-safety spec) —
    # not just be registry-tracked.
    ffmpeg_utils.run(cmd, heavy=True)


def _merge_crossfades(seg_paths: list[str], transitions: list[dict], work: Path, log) -> list[str]:
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
            str(config.RENDER_CRF),
            "-preset",
            config.RENDER_PRESET,
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
        # heavy=True: same reasoning as _encode_segment_with_fades above --
        # a real re-encode, must go through the RAM guard + concurrency gate.
        ffmpeg_utils.run(cmd, heavy=True)
        paths[i - 1 : i + 1] = [str(merged)]
        del trans[i]
        # don't advance i: the newly merged block may chain into the next junction
    return paths


def run(log, project: dict) -> None:
    segments = project.get("edl")
    if not segments:
        segments = ordering.build_edl(project)
        project["edl"] = segments
        store.save(project)
    if not segments:
        raise RuntimeError("EDL is empty — no kept sentences to render.")

    width, height, fps = _target_format(project)
    pdir = store.project_dir(project["id"])
    work = pdir / "work" / f"render_{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)

    color_vf = filters.build_vf(project.get("color"))
    transitions = [_normalize_transition(seg.get("transition")) for seg in segments]

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

        if fade_in > 0 or fade_out > 0:
            _encode_segment_with_fades(
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
                fade_in,
                fade_out,
            )
        else:
            ffmpeg_utils.cut_segment(
                clip["path"],
                seg["start"],
                seg["end"],
                str(out),
                width,
                height,
                fps,
                audio_src=audio_src,
                audio_start=audio_start,
                vf_extra=color_vf,
            )
        seg_paths.append(str(out))
        log.progress((i + 1) / (len(segments) + 1))
        log(
            f"seg {i + 1}/{len(segments)}: {clip['filename']} "
            f"{seg['start']:.1f}-{seg['end']:.1f}s" + (" [external audio]" if audio_src else "")
        )

    seg_paths = _merge_crossfades(seg_paths, transitions, work, log)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    final = pdir / f"maincut_{stamp}.mp4"
    log("Concatenating...")
    ffmpeg_utils.concat_segments(seg_paths, str(final), work)

    if project.get("audio_enhance"):
        log("Enhancing voice audio...")
        raw_wav = work / "final_audio.wav"
        enhanced_wav = work / "final_audio_enhanced.wav"
        remuxed = work / "final_remuxed.mp4"
        ffmpeg_utils.extract_wav(str(final), str(raw_wav))
        audio_enhance.enhance(str(raw_wav), str(enhanced_wav))
        ffmpeg_utils.mux_audio(str(final), str(enhanced_wav), str(remuxed))
        remuxed.replace(final)

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
    log(f"Done: {final.name} ({total:.0f}s from {len(segments)} segments).")
