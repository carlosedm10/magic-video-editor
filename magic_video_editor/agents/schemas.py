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


class ClipOrder(BaseModel):
    """Narrative order for separately recorded clips."""

    order: list[int] = Field(
        ...,
        description="Clip indices in the order they should be assembled; "
        "must be a permutation of the given indices",
    )
    notes: str = Field(default="", description="One-line rationale for the ordering")


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
