"""Stage 2 — Sync: find clips recorded simultaneously (multi-cam / external audio)
via audio cross-correlation. Pure signal processing, no LLM.

Two passes:
  1. Coarse: loudness envelopes at SYNC_ENV_RATE Hz, FFT cross-correlation.
     Strong normalized peak => same take; the lag gives the offset.
  2. Fine: full-rate correlation in a +/-1s window around the coarse lag.

Offsets are stored on a per-group timeline: member.offset = seconds between the
group timeline zero (earliest clip) and that clip's local zero.
"""

import numpy as np
from scipy import signal

from .. import config, ffmpeg_utils, store


def _envelope(wav: np.ndarray) -> np.ndarray:
    hop = config.ANALYSIS_SR // config.SYNC_ENV_RATE
    n = len(wav) // hop
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    env = np.abs(wav[: n * hop]).reshape(n, hop).mean(axis=1)
    env -= env.mean()
    std = env.std()
    return env / std if std > 1e-8 else env


def _coarse_offset(env_a: np.ndarray, env_b: np.ndarray) -> tuple[float, float]:
    """Returns (offset_seconds_of_b_relative_to_a, normalized_correlation)."""
    corr = signal.fftconvolve(env_a, env_b[::-1])
    peak = int(np.argmax(corr))
    lag = peak - (len(env_b) - 1)  # positive => b starts after a
    overlap = min(len(env_a), len(env_b))
    score = float(corr[peak] / overlap) if overlap else 0.0
    return lag / config.SYNC_ENV_RATE, score


def _fine_offset(wav_a: np.ndarray, wav_b: np.ndarray, coarse_s: float) -> float:
    """Refine the lag at full sample rate within +/-1s of the coarse estimate,
    using a 30s window of overlapping audio. coarse_s is where clip b's local
    zero falls on clip a's timeline; returns the refined value."""
    sr = config.ANALYSIS_SR
    pad = sr  # +/-1s search
    win = 30 * sr
    a_start = max(pad, int(coarse_s * sr))  # b_start position on a, clamped
    b_start = max(0, int(-coarse_s * sr))  # first overlapped sample in b
    seg_b = wav_b[b_start : b_start + win]
    search = wav_a[a_start - pad : a_start + len(seg_b) + pad]
    if len(seg_b) < sr or len(search) < len(seg_b) + pad:
        return coarse_s
    corr = signal.fftconvolve(search, seg_b[::-1], mode="valid")
    a_pos = (a_start - pad) + int(np.argmax(corr))
    return a_pos / sr - b_start / sr


def run(log, project: dict) -> None:
    clips = [c for c in project["clips"] if c.get("wav")]
    if len(clips) < 2:
        project["sync_groups"] = []
        store.mark_stage(project, "sync", "done", "single clip / nothing to sync")
        log("Fewer than 2 clips with audio — nothing to sync.")
        return

    log("Loading audio envelopes...")
    wavs, envs = {}, {}
    for c in clips:
        wavs[c["id"]] = ffmpeg_utils.load_wav_mono(c["wav"])
        envs[c["id"]] = _envelope(wavs[c["id"]])

    # Pairwise correlation + union-find grouping
    parent = {c["id"]: c["id"] for c in clips}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    offsets: dict[tuple[str, str], float] = {}
    n = len(clips)
    pair_i = 0
    total_pairs = n * (n - 1) // 2
    for i in range(n):
        for j in range(i + 1, n):
            a, b = clips[i], clips[j]
            off, score = _coarse_offset(envs[a["id"]], envs[b["id"]])
            pair_i += 1
            log.progress(pair_i / max(1, total_pairs))
            log(f"{a['filename']} vs {b['filename']}: corr={score:.2f} offset={off:.2f}s")
            if score >= config.SYNC_MIN_CORR:
                off = _fine_offset(wavs[a["id"]], wavs[b["id"]], off)
                offsets[(a["id"], b["id"])] = off
                parent[find(a["id"])] = find(b["id"])

    groups: dict[str, list[str]] = {}
    for c in clips:
        groups.setdefault(find(c["id"]), []).append(c["id"])

    sync_groups = []
    for gi, members in enumerate(groups.values()):
        if len(members) < 2:
            continue
        # Anchor timeline at the first member; place the rest via pairwise offsets.
        anchor = members[0]
        pos = {anchor: 0.0}
        changed = True
        while changed:
            changed = False
            for (a, b), off in offsets.items():
                if a in pos and b not in pos and a in members and b in members:
                    pos[b] = pos[a] + off
                    changed = True
                elif b in pos and a not in pos and a in members and b in members:
                    pos[a] = pos[b] - off
                    changed = True
        base = min(pos.values())
        group = {
            "id": f"g{gi}",
            "members": [{"clip_id": cid, "offset": round(p - base, 3)} for cid, p in pos.items()],
        }
        sync_groups.append(group)
        names = ", ".join(store.get_clip(project, m)["filename"] for m in pos)
        log(f"Sync group {group['id']}: {names}")

    project["sync_groups"] = sync_groups
    store.save(project)
    if not sync_groups:
        log("No simultaneous recordings detected — treating clips as independent takes.")
    else:
        log(f"Found {len(sync_groups)} sync group(s).")


def audio_source_for(project: dict, clip_id: str) -> tuple[str, float] | None:
    """If this camera clip is in a sync group that also contains a dedicated
    audio recording, return (audio_path, time_delta) where
    audio_time = clip_time + time_delta."""
    for g in project.get("sync_groups", []):
        members = {m["clip_id"]: m["offset"] for m in g["members"]}
        if clip_id not in members:
            continue
        for cid, off in members.items():
            clip = store.get_clip(project, cid)
            if clip["role"] == "audio":
                return clip["path"], members[clip_id] - off
    return None
