"""Pydantic output models for the editorial agents."""

from typing import Literal

from pydantic import BaseModel, Field


class TakePick(BaseModel):
    """Which of the presented takes of the same line is best."""

    best: int = Field(..., ge=0, description="Index of the best take in the given list")


class TranscriptCleanup(BaseModel):
    """Sentence ids to cut from one clip's transcript: restart markers,
    abandoned takes superseded by a later retake, or meta-asides to the
    camera/editor. Kept flat and small-model friendly."""

    cut_ids: list[int] = Field(
        default_factory=list,
        description="Sentence numbers to cut (restarts, abandoned takes, meta-asides). "
        "Empty list if nothing should be cut.",
    )
    reason: str = Field(default="", description="One-line rationale for the cuts")


class CutRun(BaseModel):
    """One contiguous run of sentence ids to cut as a single stuck/repeated
    take (v5.6 take_sequencer). Flat object, held in a short list -- NOT a
    deeply nested/batch schema, small models handle a short list of flat
    objects fine."""

    start_id: int = Field(..., description="First sentence number (inclusive) of the run to cut")
    end_id: int = Field(..., description="Last sentence number (inclusive) of the run to cut")
    reason: str = Field(default="", description="One-line rationale for this run")


class TakeSequencer(BaseModel):
    """Sliding-window verdict over ~12 consecutive sentences: contiguous runs
    of failed/halting attempts at the same line (optionally ending in a
    self-encouragement marker like 'venga ya' / 'ahora sí') that are
    superseded by a later clean take in the SAME window, or the same line
    repeated many times without a marker. At most ~4 runs per window."""

    cut_runs: list[CutRun] = Field(
        default_factory=list,
        max_length=4,
        description="Contiguous sentence-id runs to cut as stuck/repeated takes. "
        "Empty list if nothing in this window qualifies.",
    )


class VideoTopic(BaseModel):
    """One-line topic summary of a video's transcript, used to judge whether a
    sentence is an out-of-context aside. Flat and cheap on purpose — a small
    model runs this once per clip batch."""

    topic: str = Field(..., description="One short line describing what the video is about")


class ContextFlag(BaseModel):
    """One sentence (by its local number in the numbered chunk) that is a
    meta-aside / out-of-context for the video's topic, with a confidence
    used to gate auto-cut vs. suggestion (same idea as DedupJudge.confidence).
    Flat object held in a short list -- same pattern as TakeSequencer's
    cut_runs, NOT a deeply nested/batch schema."""

    id: int = Field(..., description="Local sentence number (from the numbered input)")
    confidence: int = Field(
        ..., ge=1, le=5, description="Confidence this sentence doesn't belong, 1-5"
    )
    reason: str = Field(default="", description="One-line rationale")


class ContextCheck(BaseModel):
    """Batched chunk verdict: given a numbered list of consecutive sentences
    from one clip and the video's topic, which ones are meta-asides /
    out-of-context? Only the sentences that DON'T belong are returned (with a
    confidence); everything else is implicitly in-context. Batched per chunk
    instead of one call per sentence -- small-model friendly, mirrors
    TakeSequencer's cut_runs list."""

    out_of_context: list[ContextFlag] = Field(
        default_factory=list,
        max_length=15,
        description="Sentences that are meta-asides or out-of-context, with confidence. "
        "Empty list if every sentence belongs.",
    )


class BlooperFlag(BaseModel):
    """One sentence (by its number in the numbered full-clip transcript)
    that is an abandoned/bad take of something said better LATER in the
    SAME clip -- a repeat far enough apart that the chunked windowed passes
    (transcript_cleaner/take_sequencer/context_check) can miss it. Flat
    object, mirrors ContextFlag -- small-model friendly.

    `superseded_by` (required, 2026-07-26 precision fix): a live-verification
    run found the model auto-cutting topic-setup/transition sentences with
    no genuine later restatement -- structurally forcing it to NAME the
    specific later sentence it claims says the same thing better makes the
    pass's actual job ("catch far-apart repeats the windowed passes missed")
    explicit in the schema, not just the prompt; takes.py's
    _full_clip_review then code-verifies this claim (real, later, kept, and
    textually similar) before trusting it, rather than acting on confidence
    alone."""

    sentence_number: int = Field(
        ..., description="Sentence number (from the numbered clip transcript)"
    )
    superseded_by: int = Field(
        ...,
        description="Sentence number (same numbered clip transcript) of the LATER "
        "sentence that says essentially the same content better -- the specific "
        "restatement this flag claims supersedes `sentence_number`. Never the same "
        "number as sentence_number, never earlier, never invented.",
    )
    confidence: int = Field(
        ..., ge=1, le=5, description="Confidence this is a superseded take, 1-5"
    )
    reason: str = Field(default="", description="One-line rationale")


class BlooperReview(BaseModel):
    """Whole-clip verdict (WS-C, "full-clip blooper-review pass"): given the
    ENTIRE not-yet-cut, numbered sentence list of ONE clip, which sentences
    are abandoned/bad takes of something said better later in this SAME
    clip -- repeats far enough apart that the chunked 12/15/40-sentence
    windowed passes can miss them? Flat, capped list -- same "small model,
    small job" pattern as ContextCheck/TakeSequencer."""

    flags: list[BlooperFlag] = Field(
        default_factory=list,
        max_length=20,
        description="Sentences that are superseded/abandoned takes of a later, better "
        "delivery in this same clip. Empty list if nothing qualifies.",
    )


class DedupJudge(BaseModel):
    """Judges whether two sentences from DIFFERENT clips say the same thing,
    and if so which one to keep. Flat schema, small-model friendly."""

    same_content: bool = Field(
        ..., description="True if both sentences convey the same content/idea"
    )
    keep: Literal["a", "b"] = Field(
        ..., description="Which of the two sentences reads better and should be kept"
    )
    confidence: int = Field(..., ge=1, le=5, description="Confidence in this judgement, 1-5")
    reason: str = Field(default="", description="One-line rationale")


class ParagraphBreakPoint(BaseModel):
    """One boundary where a NEW paragraph/topic clearly begins ("punto y
    aparte"), expressed as the local sentence number AFTER which the break
    falls (the break sits between that sentence and the next one). Flat
    object held in a short list -- same pattern as ContextFlag/CutRun."""

    after_id: int = Field(
        ...,
        description="Local sentence number; the break falls between this sentence and the next",
    )
    confidence: int = Field(
        ..., ge=1, le=5, description="Confidence this is a genuine paragraph/topic change, 1-5"
    )
    reason: str = Field(default="", description="One-line rationale for this break")


class ParagraphBreaks(BaseModel):
    """Windowed verdict: given a numbered list of consecutive KEPT sentences
    from one clip, in spoken order, which boundaries mark a genuine new
    paragraph / topic shift ("punto y aparte")? NEVER a plain sentence end
    within the same idea ("punto y seguido"). Empty list when nothing in the
    window qualifies -- when in doubt, don't mark. Batched per window, same
    shape as TakeSequencer.cut_runs."""

    breaks: list[ParagraphBreakPoint] = Field(
        default_factory=list,
        max_length=6,
        description="Boundaries where a new paragraph/topic clearly begins. "
        "Empty list if nothing in this window is a genuine topic change.",
    )


class ClipOrder(BaseModel):
    """Narrative order for separately recorded clips."""

    order: list[int] = Field(
        ...,
        description="Clip indices in the order they should be assembled; "
        "must be a permutation of the given indices",
    )
    notes: str = Field(default="", description="One-line rationale for the ordering")


class ClipDigest(BaseModel):
    """Cheap ~400-character digest of ONE clip's kept transcript, used by
    pipeline/ordering.py's hierarchical clip listing when the full transcripts
    of every clip together would not fit the clip_order model's context
    window (see pipeline/token_budget.fits_context). Flat and single-clip on
    purpose -- same "small model, small job" pattern as VideoTopic."""

    summary: str = Field(
        ...,
        description="Concise summary of what this clip covers, ~400 characters, "
        "in the transcript's own language -- enough for a narrative-ordering "
        "judgement without the full text",
    )


class ReviewFinding(BaseModel):
    """One suggested issue in the full kept transcript (redundancy, repeated
    idea, off-topic tangent, or incoherent transition). Report-only — never
    applied automatically; the editor accepts or dismisses it."""

    kind: Literal["redundant", "repeated_idea", "off_topic", "incoherent"] = Field(
        ..., description="redundant, repeated_idea, off_topic, or incoherent"
    )
    sentence_ids: list[int] = Field(
        default_factory=list,
        description="Global sentence numbers (from the numbered input) this finding is about",
    )
    message: str = Field(
        default="",
        description="Short, concrete one-line explanation, in the transcript's own language",
    )
    proposed_action: Literal["cut", "reorder", "merge"] = Field(
        ..., description="Suggested fix: cut, reorder, or merge"
    )


class ReviewFindings(BaseModel):
    """Up to 8 conservative findings about the full kept transcript across
    all clips. Suggest, don't delete: the reviewer never cuts anything
    itself."""

    findings: list[ReviewFinding] = Field(default_factory=list, max_length=8)


class EditJudgeFinding(BaseModel):
    """One finding from comparing the EDITED transcript against the
    ORDERED-BUT-UNCUT originals (pipeline/judge.py, spec point 6). Report
    only -- the judge never reorders and, outside the kept_blooper autocut
    gate in judge.py itself, never cuts anything either."""

    kind: Literal["lost_content", "kept_blooper", "order_issue", "incoherent_transition"] = Field(
        ..., description="lost_content, kept_blooper, order_issue, or incoherent_transition"
    )
    sentence_ids: list[int] = Field(
        default_factory=list,
        description="Global sentence numbers (from the numbered input) this finding is about",
    )
    message: str = Field(
        default="",
        description="Short, concrete one-line explanation, in the transcript's own language",
    )
    severity: int = Field(..., ge=1, le=5, description="How serious this finding is, 1-5")


class EditJudgeVerdict(BaseModel):
    """Up to 10 conservative findings from one edit_judge pass. Aggregated
    across several runs by pipeline/judge.py before anything is acted on."""

    findings: list[EditJudgeFinding] = Field(default_factory=list, max_length=10)


class CopywriterOutput(BaseModel):
    """SEO/viral copy for a reel or the full video. Flat schema, small-model
    friendly. Written in the content's own language."""

    title: str = Field(
        ..., description="Scroll-stopping but truthful title, <=70 characters"
    )
    description: str = Field(
        default="",
        description="SEO-structured description: keywords early, line breaks, "
        "brand-aligned CTA, hashtags at the end",
    )
    hashtags: str = Field(
        default="", description="2-5 relevant hashtags, space-separated, '#' prefixed"
    )


class ClipPlacement(BaseModel):
    """Where a newly added clip fits into the already-edited narrative (spec
    v7.3 "Incremental clip addition"). Flat schema, small-model friendly."""

    placement_after_clip_index: int = Field(
        ...,
        description="Existing clip index (0-based) the new clip should play AFTER, "
        "or -1 to place it at the very start, before every existing clip",
    )
    duplicate_of_clip_index: int = Field(
        default=-1,
        description="Existing clip index (0-based) whose content the new clip clearly "
        "repeats, or -1 if it is not a duplicate",
    )
    confidence: int = Field(..., ge=1, le=5, description="Confidence in this judgement, 1-5")
    message: str = Field(
        default="",
        description="Short, concrete one-line explanation, in the transcript's own language",
    )


class ReelComposer(BaseModel):
    """Verdict on whether two high-scoring short-form windows continue the
    SAME idea apart in time (setup+payoff, question+answer, ...) and should
    be spliced into one multi-segment reel (spec v5.8b "the podcast case").
    Flat schema, small-model friendly."""

    combine: bool = Field(
        ..., description="True if the two windows should be combined into one reel"
    )
    why: str = Field(default="", description="One-line rationale for the verdict")
    order: Literal["ab", "ba"] = Field(
        default="ab",
        description="Playback order once combined: 'ab' = A then B, 'ba' = B then A",
    )


class ReelDedup(BaseModel):
    """Judges whether two REEL suggestions are essentially the SAME
    underlying source moment repackaged twice (a true duplicate -- same
    clip(s), an overlapping/near-identical source time window, and/or
    near-identical transcript text) or merely cover a similar TOPIC with
    DIFFERENT footage/wording (NOT a duplicate -- both should survive). Flat
    schema, mirrors DedupJudge (takes.py's cross-clip dedup)."""

    same_content: bool = Field(
        ...,
        description="True ONLY if both reels are the SAME moment/clip packaged twice -- "
        "sharing a topic or discussing similar ideas is NOT enough on its own",
    )
    keep: Literal["a", "b"] = Field(
        ...,
        description="Which reel to keep when same_content is true -- prefer the higher "
        "score, the longer duration, or the stronger hook",
    )
    confidence: int = Field(..., ge=1, le=5, description="Confidence in this judgement, 1-5")
    reason: str = Field(default="", description="One-line rationale")


class ReelScore(BaseModel):
    """Scores for one short-form candidate window. Kept flat and
    single-candidate on purpose: small local models fill this schema far more
    reliably than a nested batch."""

    hook: int = Field(..., ge=0, le=10, description="Does the first line grab attention?")
    self_contained: int = Field(
        ..., ge=0, le=10, description="Understandable without surrounding context?"
    )
    payoff: int = Field(..., ge=0, le=10, description="Delivers value, insight, or a punchline?")
    title: str = Field(..., description="Catchy 5-8 word title for the clip")
