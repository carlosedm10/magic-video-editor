"""Pydantic output models for the editorial agents."""

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


class ClipOrder(BaseModel):
    """Narrative order for separately recorded clips."""

    order: list[int] = Field(
        ...,
        description="Clip indices in the order they should be assembled; "
        "must be a permutation of the given indices",
    )
    notes: str = Field(default="", description="One-line rationale for the ordering")


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
