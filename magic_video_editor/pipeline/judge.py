"""Stage — Pre-render Judge (runs after `review`, before `render`): a last,
text-only editorial pass comparing the EDITED transcript (kept sentences
only, narrative/clip_order, globally numbered) against the ORDERED-BUT-UNCUT
originals (every sentence of every camera clip, kept AND cut, marked, same
clip_order) -- spec point 6.

Unlike review.py, this stage runs the edit_judge agent config.JUDGE_RUNS
times and only acts on a MAJORITY consensus (config.JUDGE_MAJORITY runs
agreeing kind + overlapping sentence_ids): a finding that only shows up once
is discarded outright, never even surfaced as a suggestion -- conservative,
since keeping content is always safer than losing something the viewer
needed ("suggest, don't delete", same doctrine as review.py, with one narrow
exception below).

The one exception: a majority "kept_blooper" finding whose severity is
config.JUDGE_AUTOCUT_SEVERITY or higher in EVERY contributing run (the MIN
severity across those runs) DOES get auto-applied -- the flagged sentence is
flipped kept=false and project["edl"] is invalidated (same invalidation
pattern as pipeline/ordering.py's reorder hook), so a genuinely blooper take
that survived every earlier pass doesn't make it into the final render.
Every other finding (lost_content, order_issue, incoherent_transition, and
any kept_blooper below the severity bar) becomes an open entry in
project["suggestions"] for a human to accept/dismiss, exactly like
review.py's findings -- order_issue in particular is REPORT ONLY, the judge
can never reorder anything itself.

If an auto-cut happened, one more full JUDGE_RUNS-run pass runs over the
now-changed transcript (a cut can surface new problems); config.
MAX_JUDGE_ITERATIONS bounds the total number of passes unconditionally, even
against an agent that keeps returning fresh findings forever.

Fail-open throughout: an individual edit_judge call that raises is logged
and simply doesn't contribute to that pass's consensus -- it never breaks
the stage.

Concurrency: run() can hold its in-memory `project` snapshot across up to
MAX_JUDGE_ITERATIONS * JUDGE_RUNS sequential Ollama calls (minutes) before
the final store.save(). To avoid silently reverting any concurrent HTTP
write (suggestion accept, EDL PUT/split, rename, ...) that landed during
that window, the save at the end of run() reloads a FRESH copy of the
project from store and re-applies only judge's own deltas onto it (see
_save_judge_deltas) instead of saving the long-held in-memory snapshot
wholesale -- the same spirit as store.save()'s own queue-field
preservation, just for judge's specific deltas.

KNOWN RESIDUAL LIMITATION (auto-cut vs. a stale EDL PUT): auto-cut flips
sentences to kept=false and invalidates project["edl"] (sets it to None) so
it gets rebuilt without the cut content. But a browser tab that fetched the
OLD edl before the auto-cut ran can still PUT that stale edl back afterward
(api/edl.py's PUT has no version/etag check to reject it -- there is no
cheap versioning mechanism there to hook into), which would silently
reinstate the cut content into the render. Full optimistic concurrency
(etag/version on the project or the edl) is out of scope here. The cheap
mitigation implemented: project["judge_autocut_at"] records the epoch time
of the last auto-cut, and the (non-open, informational) suggestion entry
judge appends for an auto-cut names the actual cut sentence text, so a
human inspecting the project can always see what was removed and when, even
if a stale PUT brings it back."""

import time
import uuid

from .. import config, llm, store
from .speakers import speaker_prefix


def _numbered_transcripts(project: dict) -> tuple[str, str, dict[int, str]]:
    """Own local numbered-transcript helpers (does NOT reuse review.py's
    private _numbered_clips). Builds ONE shared numbering over every
    sentence (kept and cut) of every camera clip, in clip_order (falling
    back to camera clip file order) -- this is deliberate: a single id_map
    covering both listings means a finding's sentence_ids are unambiguous
    regardless of whether they point at a kept or a cut sentence, instead of
    juggling two independent numberings.

    Returns (edited_transcript_text, original_transcript_text, {number: real
    sentence id}). The EDITED listing only prints kept sentences (still
    using their shared number, so a number found in the EDITED text always
    matches the same number in the ORIGINAL text); the ORIGINAL listing
    prints every sentence, each tagged [KEPT] or [CUT]."""
    order = project.get("clip_order") or [
        c["id"] for c in project["clips"] if c["role"] == "camera"
    ]
    id_map: dict[int, str] = {}
    edited_lines: list[str] = []
    original_lines: list[str] = []
    n = 0
    for ci, cid in enumerate(order):
        all_sents = sorted(
            (s for s in project["sentences"] if s["clip_id"] == cid),
            key=lambda s: s["start"],
        )
        if not all_sents:
            continue
        original_lines.append(f"CLIP {ci}:")
        edited_clip_lines = []
        for s in all_sents:
            n += 1
            id_map[n] = s["id"]
            text = f"{speaker_prefix(project, s)}{s['text']}"
            tag = "[KEPT]" if s["kept"] else "[CUT]"
            original_lines.append(f'{n}: {tag} "{text}"')
            if s["kept"]:
                edited_clip_lines.append(f'{n}: "{text}"')
        if edited_clip_lines:
            edited_lines.append(f"CLIP {ci}:")
            edited_lines.extend(edited_clip_lines)
    return "\n".join(edited_lines), "\n".join(original_lines), id_map


def _dedupe_key(kind: str, sentence_ids: list) -> tuple:
    return (kind, tuple(sorted(sentence_ids)))


def _overlaps(a: list, b: list) -> bool:
    return bool(set(a) & set(b))


def _aggregate(runs_findings: list[list[dict]]) -> list[dict]:
    """Cluster findings across the JUDGE_RUNS runs of one pass: same kind +
    overlapping sentence_ids join the same cluster (simple union-find over
    all findings from all runs). A cluster only survives if it was
    contributed by at least config.JUDGE_MAJORITY DISTINCT runs -- a
    singleton (one run only) is discarded outright, never surfaced.

    Returns merged findings: {kind, sentence_ids (union, real ids -- the
    human-facing suggestion payload, useful context even for the ids that
    didn't make the autocut bar), autocut_sentence_ids (INTERSECTION of
    sentence_ids across contributing runs -- see below), message,
    min_severity (MIN severity across contributing runs), eligible_autocut
    (kept_blooper AND every contributing run's [min] severity >= config.
    JUDGE_AUTOCUT_SEVERITY AND autocut_sentence_ids is non-empty)}.

    Why intersection for autocut: one run mentioning an extra sentence_id
    alongside the true blooper (e.g. it lumps a neighbouring sentence into
    the same finding) must NOT get that extra id auto-cut just because it
    overlapped enough to join the cluster -- the majority never actually
    agreed on that id. Auto-cut only ever touches ids every contributing run
    flagged; if that intersection is empty (no id survives across all
    contributing runs) the finding is downgraded to a suggestion instead,
    still carrying the union for human context."""
    flat = [
        {**f, "_run": ridx} for ridx, findings in enumerate(runs_findings) for f in findings
    ]
    n = len(flat)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if flat[i]["kind"] == flat[j]["kind"] and _overlaps(
                flat[i]["sentence_ids"], flat[j]["sentence_ids"]
            ):
                union(i, j)

    clusters: dict[int, list[dict]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(flat[i])

    merged = []
    for members in clusters.values():
        run_idxs = {m["_run"] for m in members}
        if len(run_idxs) < config.JUDGE_MAJORITY:
            continue
        kind = members[0]["kind"]
        sentence_ids = sorted({sid for m in members for sid in m["sentence_ids"]})
        # Per-run union first (a single run can in principle contribute more
        # than one member to this cluster), THEN intersect across runs -- an
        # id only counts toward autocut if every contributing run agreed on
        # it, not just some member of some run.
        ids_by_run: dict[int, set] = {}
        for m in members:
            ids_by_run.setdefault(m["_run"], set()).update(m["sentence_ids"])
        autocut_sentence_ids = sorted(set.intersection(*ids_by_run.values()))
        message = max((m["message"] for m in members), key=len, default="")
        severity_by_run: dict[int, int] = {}
        for m in members:
            r = m["_run"]
            severity_by_run[r] = min(severity_by_run.get(r, m["severity"]), m["severity"])
        min_severity = min(severity_by_run.values())
        eligible_autocut = (
            kind == "kept_blooper"
            and bool(autocut_sentence_ids)
            and all(v >= config.JUDGE_AUTOCUT_SEVERITY for v in severity_by_run.values())
        )
        merged.append(
            {
                "kind": kind,
                "sentence_ids": sentence_ids,
                "autocut_sentence_ids": autocut_sentence_ids,
                "message": message,
                "min_severity": min_severity,
                "eligible_autocut": eligible_autocut,
            }
        )
    return merged


_PROPOSED_ACTION = {
    "lost_content": "restore",
    "kept_blooper": "cut",
    "order_issue": "reorder",
    "incoherent_transition": "review",
}


def _run_one_pass(log, project: dict, topic_line: str) -> list[dict]:
    """Runs edit_judge config.JUDGE_RUNS times over the CURRENT transcript
    state and returns the aggregated (majority-consensus) findings, real
    sentence ids resolved. Fail-open per individual call."""
    edited_text, original_text, id_map = _numbered_transcripts(project)
    if not id_map:
        return []

    from ..agents.agents import get_agent

    prompt = (
        f"{topic_line}EDITED transcript (kept sentences only, in narrative order):\n"
        f"{edited_text}\n\n"
        f"ORIGINAL transcript (ALL sentences, kept and cut, same clip order):\n"
        f"{original_text}"
    )

    runs_findings: list[list[dict]] = []
    for i in range(config.JUDGE_RUNS):
        try:
            result = get_agent("edit_judge").run_sync(prompt).output
        except Exception as e:
            log(f"Judge run {i + 1}/{config.JUDGE_RUNS} failed, skipping: {e}")
            runs_findings.append([])
            continue
        run_findings = []
        for f in result.findings[:10]:
            real_ids = [id_map[n] for n in f.sentence_ids if n in id_map]
            if not real_ids:
                continue
            run_findings.append(
                {
                    "kind": f.kind,
                    "sentence_ids": real_ids,
                    "message": (f.message or "")[:300],
                    "severity": f.severity,
                }
            )
        runs_findings.append(run_findings)

    return _aggregate(runs_findings)


def run(log, project: dict) -> None:
    if not project.get("sentences"):
        log("No sentences yet; skipping judge.")
        return
    if not any(s.get("kept") for s in project["sentences"]):
        log("No kept sentences to judge; skipping.")
        return
    if not llm.available():
        log("Judge skipped (ollama unavailable).")
        return

    topic = project.get("topic") or ""
    topic_line = f'Video topic: "{topic}"\n\n' if topic else ""

    all_new_suggestions: list[dict] = []
    seen_keys: set[tuple] = set()
    any_autocut = False
    # Judge's own deltas, tracked as we go so the final save can re-apply
    # them onto a FRESH reload of the project instead of saving this
    # long-held in-memory snapshot wholesale (see module docstring,
    # "Concurrency"). cut_sentence_ids: real sentence ids judge itself
    # flipped kept=false (by id, so a reload-and-reapply is a no-op if the
    # sentence no longer exists). autocut_happened: whether ANY pass in this
    # run() call auto-cut something (drives the project["edl"] = None
    # delta and project["judge_autocut_at"] stamp).
    cut_sentence_ids: set = set()
    autocut_happened = False

    for iteration in range(1, config.MAX_JUDGE_ITERATIONS + 1):
        log(f"Judge pass {iteration}/{config.MAX_JUDGE_ITERATIONS} "
            f"({config.JUDGE_RUNS} run(s), majority {config.JUDGE_MAJORITY})...")
        merged = _run_one_pass(log, project, topic_line)

        autocut_this_pass = False
        for m in merged:
            if m["eligible_autocut"]:
                flipped_any = False
                cut_texts = []
                for sid in m["autocut_sentence_ids"]:
                    for s in project["sentences"]:
                        if s["id"] == sid and s.get("kept"):
                            s["kept"] = False
                            flipped_any = True
                            cut_sentence_ids.add(sid)
                            cut_texts.append(s.get("text", ""))
                if flipped_any:
                    autocut_this_pass = True
                    any_autocut = True
                    autocut_happened = True
                    log(
                        f"Auto-cut kept blooper sentence(s) {m['autocut_sentence_ids']} "
                        f"(severity {m['min_severity']})."
                    )
                    # Informational record only (status != "open"), so a
                    # human can see exactly what text was auto-cut (and
                    # when, via project["judge_autocut_at"]) even though
                    # there's nothing for them to accept/dismiss here --
                    # see module docstring's "KNOWN RESIDUAL LIMITATION".
                    all_new_suggestions.append(
                        {
                            "id": uuid.uuid4().hex[:8],
                            "kind": m["kind"],
                            "sentence_ids": m["autocut_sentence_ids"],
                            "message": (
                                f"Auto-cut (severity {m['min_severity']}): "
                                + " / ".join(t for t in cut_texts if t)
                            )[:300],
                            "proposed_action": "cut",
                            "status": "auto_applied",
                            "source": "judge",
                            "severity": m["min_severity"],
                        }
                    )
                    continue  # auto-applied, not also an open suggestion

            key = _dedupe_key(m["kind"], m["sentence_ids"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_new_suggestions.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "kind": m["kind"],
                    "sentence_ids": m["sentence_ids"],
                    "message": m["message"],
                    "proposed_action": _PROPOSED_ACTION.get(m["kind"], "review"),
                    "status": "open",
                    "source": "judge",
                    "severity": m["min_severity"],
                }
            )

        if autocut_this_pass:
            project["edl"] = None

        if not autocut_this_pass:
            break

    fresh = _save_judge_deltas(
        log,
        project["id"],
        cut_sentence_ids=cut_sentence_ids,
        new_suggestions=all_new_suggestions,
        autocut_happened=autocut_happened,
    )
    if fresh is not None:
        # Callers (api/pipeline.py's _run_stage_kind / _run_all_kind) do
        # their OWN store.mark_stage(project, ...) -> store.save(project)
        # right after fn(log, project) returns, using this SAME in-memory
        # `project` object -- a plain save, no reload-merge. If we left
        # `project` holding its old, stale (pre-reload) contents, that next
        # save would immediately re-clobber the fresh, concurrency-safe
        # state we just persisted, defeating the whole point above. So:
        # sync `project`'s contents in place (same dict identity, callers
        # already hold a reference to it) to match exactly what we saved,
        # narrowing any remaining race to that one small, fast, LLM-free
        # mark_stage save -- the same residual-risk shape store.py's own
        # queue-field docstring accepts elsewhere in this codebase.
        project.clear()
        project.update(fresh)
    log(f"auto-cut this run: {any_autocut}.")


def _save_judge_deltas(
    log,
    project_id: str,
    *,
    cut_sentence_ids: set,
    new_suggestions: list[dict],
    autocut_happened: bool,
) -> dict | None:
    """Persists judge's own deltas from this run() call by RELOADING a fresh
    copy of the project from store and re-applying only those deltas onto
    it, instead of saving the long-held in-memory `project` snapshot
    wholesale -- see module docstring, "Concurrency". This is the piece that
    stops judge from silently reverting a concurrent HTTP write (suggestion
    accept, EDL PUT/split, rename, ...) that landed during judge's up-to-
    MAX_JUDGE_ITERATIONS * JUDGE_RUNS sequential Ollama calls.

    Deltas re-applied, each independently safe to no-op if its target has
    moved on since judge started:
      - cut_sentence_ids: flip kept=false by sentence id, skipped if the
        sentence no longer exists (e.g. deleted in the meantime).
      - new_suggestions: judge's own OPEN suggestions from this run replace
        judge's previous OPEN suggestions (same convention review.py uses),
        skipped by (kind, sentence_ids) if an equivalent one is already
        present; auto_applied (informational, non-actionable) entries are
        additive, skipped only on an exact id collision. Every other
        suggestion (other stages', or ones a human already accepted/
        dismissed) -- including ones added/changed concurrently -- is left
        untouched.
      - project["edl"] = None + project["judge_autocut_at"] stamp, applied
        ONLY if this run() call auto-cut something, regardless of what the
        freshly reloaded edl currently is (a concurrent stale PUT that
        raced back in is exactly the case this delta needs to win over --
        see the module docstring's "KNOWN RESIDUAL LIMITATION" for what
        this does NOT cover).

    No existing reload-merge helper exists elsewhere in the codebase (ordering.py
    and takes.py, the other long-running stages, both do a single plain
    store.save() like judge used to) -- this is judge-local.

    Returns the freshly reloaded + merged project dict (so run() can sync
    its own in-memory `project` to it -- see the call site), or None if the
    project had disappeared by save time (nothing was saved)."""
    try:
        fresh = store.load(project_id)
    except FileNotFoundError:
        log("Judge: project disappeared before save; discarding this run's deltas.")
        return None

    if cut_sentence_ids:
        for s in fresh["sentences"]:
            if s["id"] in cut_sentence_ids:
                s["kept"] = False

    existing = fresh.get("suggestions", [])
    # Same convention as before: re-running replaces judge's own OPEN
    # suggestions but keeps ones the user already accepted/dismissed AND
    # leaves every other stage's (e.g. review.py's) open suggestions
    # untouched -- just now computed against the FRESH suggestions list
    # rather than the stale in-memory one.
    kept_existing = [
        s for s in existing if s.get("status") != "open" or s.get("source") != "judge"
    ]
    existing_keys = {_dedupe_key(s["kind"], s["sentence_ids"]) for s in kept_existing}
    existing_ids = {s["id"] for s in existing}
    deduped_new = []
    for f in new_suggestions:
        if f["status"] == "open":
            if _dedupe_key(f["kind"], f["sentence_ids"]) in existing_keys:
                continue
        elif f["id"] in existing_ids:
            continue
        deduped_new.append(f)

    fresh["suggestions"] = kept_existing + deduped_new

    if autocut_happened:
        fresh["edl"] = None
        fresh["judge_autocut_at"] = time.time()

    store.save(fresh)
    log(
        f"{len(deduped_new)} suggestion(s) ({len(kept_existing)} previously decided/other-stage "
        "kept)."
    )
    return fresh
