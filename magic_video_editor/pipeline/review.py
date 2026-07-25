"""Stage — Reviewer (runs after `order`): reads the full KEPT transcript
across all clips, in narrative (clip_order) order, and proposes suggestions
for a human to accept/dismiss in the Studio. NEVER auto-cuts anything — see
docs/PLATFORM-SPEC.md "Suggest, don't delete". Explicit restart/blooper
handling stays entirely in the transcript_cleaner pass (takes stage); this
agent only sees what already survived that pass."""

import uuid

from .. import llm, store
from .speakers import speaker_prefix


def _numbered_clips(project: dict) -> tuple[list[dict], dict[int, str]]:
    """Kept sentences across clips in clip_order (falling back to camera
    clip file order), globally numbered 1..N in narrative order. Returns
    (clips payload for the prompt, {global number: real sentence id})."""
    order = project.get("clip_order") or [
        c["id"] for c in project["clips"] if c["role"] == "camera"
    ]
    id_map: dict[int, str] = {}
    clips_payload = []
    n = 0
    for ci, cid in enumerate(order):
        kept = sorted(
            (s for s in project["sentences"] if s["clip_id"] == cid and s["kept"]),
            key=lambda s: s["start"],
        )
        numbered = []
        for s in kept:
            n += 1
            id_map[n] = s["id"]
            # v5.8c: prefix with the resolved speaker label so cross-speaker
            # "repetition" (host echoing guest) doesn't read as a duplicate
            # take to the reviewer agent.
            numbered.append({"n": n, "text": f"{speaker_prefix(project, s)}{s['text']}"})
        if numbered:
            clips_payload.append({"clip_index": ci, "sentences": numbered})
    return clips_payload, id_map


def _dedupe_key(kind: str, sentence_ids: list) -> tuple:
    return (kind, tuple(sorted(sentence_ids)))


def run(log, project: dict) -> None:
    if not project.get("sentences"):
        log("No sentences yet; skipping review.")
        return

    clips_payload, id_map = _numbered_clips(project)
    if not id_map:
        log("No kept sentences to review; skipping.")
        return

    if not llm.available():
        log("Reviewer skipped (ollama unavailable).")
        return

    lines = []
    for clip in clips_payload:
        lines.append(f"CLIP {clip['clip_index']}:")
        for s in clip["sentences"]:
            lines.append(f'{s["n"]}: "{s["text"]}"')
    listing = "\n".join(lines)

    from ..agents.agents import get_agent

    topic = project.get("topic") or ""
    topic_line = f'Video topic: "{topic}"\n\n' if topic else ""

    log(f"Asking the reviewer agent to check {len(id_map)} kept sentence(s)...")
    try:
        result = get_agent("reviewer").run_sync(
            f"{topic_line}Full kept transcript, in narrative order, grouped by clip:\n{listing}"
        ).output
    except Exception as e:
        log(f"Reviewer failed, skipping: {e}")
        return

    new_findings = []
    for f in result.findings[:8]:
        real_ids = [id_map[n] for n in f.sentence_ids if n in id_map]
        if not real_ids:
            continue
        new_findings.append(
            {
                "id": uuid.uuid4().hex[:8],
                "kind": f.kind,
                "sentence_ids": real_ids,
                "message": (f.message or "")[:300],
                "proposed_action": f.proposed_action,
                "status": "open",
            }
        )

    # Re-running replaces OPEN suggestions but keeps ones the user already
    # accepted/dismissed; dedupe by (kind, sentence_ids) against those so an
    # unchanged transcript doesn't produce the same suggestion twice.
    existing = project.get("suggestions", [])
    kept_existing = [s for s in existing if s.get("status") != "open"]
    existing_keys = {_dedupe_key(s["kind"], s["sentence_ids"]) for s in kept_existing}
    deduped_new = [
        f for f in new_findings if _dedupe_key(f["kind"], f["sentence_ids"]) not in existing_keys
    ]

    project["suggestions"] = kept_existing + deduped_new
    store.save(project)
    log(f"{len(deduped_new)} suggestion(s) ({len(kept_existing)} previously decided kept).")
