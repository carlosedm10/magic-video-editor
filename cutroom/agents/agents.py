"""Agent instances (pydantic_ai). All agents run against the local Ollama
server; the model is resolved per-task from cutroom/settings.py (Settings tab)
falling back to config.OLLAMA_MODEL only if settings has no opinion, so
swapping models never touches prompts or schemas.

`AGENT_SPECS` is the single source of truth for what an agent's prompt and
output schema are; `get_agent(task)` builds (and caches) the actual
pydantic_ai Agent lazily, per (task, model) pair, so a Settings change takes
effect on the next call without a restart."""

from pydantic_ai import Agent
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.output import NativeOutput, PromptedOutput
from pydantic_ai.providers.ollama import OllamaProvider

from .. import config, settings
from .prompts import (
    CLIP_ORDER_SYSTEM_PROMPT,
    CONTEXT_CHECK_SYSTEM_PROMPT,
    COPYWRITER_SYSTEM_PROMPT,
    DEDUP_JUDGE_SYSTEM_PROMPT,
    REEL_SCORER_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
    TAKE_JUDGE_SYSTEM_PROMPT,
    TAKE_SEQUENCER_SYSTEM_PROMPT,
    TRANSCRIPT_CLEANER_SYSTEM_PROMPT,
    VIDEO_TOPIC_SYSTEM_PROMPT,
)
from .schemas import (
    ClipOrder,
    ContextCheck,
    CopywriterOutput,
    DedupJudge,
    ReelScore,
    ReviewFindings,
    TakePick,
    TakeSequencer,
    TranscriptCleanup,
    VideoTopic,
)

_MODEL_SETTINGS = {"temperature": 0.2}

# v5 addendum ("pydantic_ai native OllamaModel migration"): every task uses
# NativeOutput (Ollama >=0.5 grammar-constrained json_schema decoding) by
# default. Tasks that were verified live to misbehave under NativeOutput are
# listed here and fall back to PromptedOutput instead -- see the per-task
# comments in AGENT_SPECS for what broke and how it was verified.
_PROMPTED_OUTPUT_TASKS: set[str] = set()

AGENT_SPECS: dict[str, dict] = {
    "take_judge": {"prompt": TAKE_JUDGE_SYSTEM_PROMPT, "output_type": TakePick},
    "clip_order": {"prompt": CLIP_ORDER_SYSTEM_PROMPT, "output_type": ClipOrder},
    "reel_scorer": {"prompt": REEL_SCORER_SYSTEM_PROMPT, "output_type": ReelScore},
    "transcript_cleaner": {
        "prompt": TRANSCRIPT_CLEANER_SYSTEM_PROMPT,
        "output_type": TranscriptCleanup,
    },
    "take_sequencer": {
        "prompt": TAKE_SEQUENCER_SYSTEM_PROMPT,
        "output_type": TakeSequencer,
    },
    "reviewer": {"prompt": REVIEWER_SYSTEM_PROMPT, "output_type": ReviewFindings},
    "video_topic": {"prompt": VIDEO_TOPIC_SYSTEM_PROMPT, "output_type": VideoTopic},
    "context_check": {"prompt": CONTEXT_CHECK_SYSTEM_PROMPT, "output_type": ContextCheck},
    "dedup_judge": {"prompt": DEDUP_JUDGE_SYSTEM_PROMPT, "output_type": DedupJudge},
    "copywriter": {"prompt": COPYWRITER_SYSTEM_PROMPT, "output_type": CopywriterOutput},
}

_cache: dict[tuple[str, str], Agent] = {}


def _model(model_name: str) -> OllamaModel:
    # Native pydantic_ai Ollama model (subclasses OpenAIChatModel, same
    # request/response behavior) instead of a hand-built
    # OpenAIChatModel(provider=OllamaProvider(...)) -- see docs/PLATFORM-SPEC.md
    # v5 addendum "pydantic_ai native OllamaModel migration".
    return OllamaModel(
        model_name,
        provider=OllamaProvider(base_url=f"{config.OLLAMA_URL}/v1"),
    )


def get_agent(task: str) -> Agent:
    """Resolve the model for `task` from settings (task_models[task] or
    default_model) and return a cached Agent for that (task, model) pair,
    constructing it lazily on first use."""
    if task not in AGENT_SPECS:
        raise KeyError(f"unknown agent task {task!r}")
    model_name = settings.model_for(task) or config.OLLAMA_MODEL
    key = (task, model_name)
    agent = _cache.get(key)
    if agent is None:
        spec = AGENT_SPECS[task]
        if task in _PROMPTED_OUTPUT_TASKS:
            output_type = PromptedOutput(spec["output_type"])
        else:
            output_type = NativeOutput(spec["output_type"])
        agent = Agent(
            model=_model(model_name),
            system_prompt=spec["prompt"].strip(),
            output_type=output_type,
            model_settings=_MODEL_SETTINGS,
            retries=2,
        )
        _cache[key] = agent
    return agent
