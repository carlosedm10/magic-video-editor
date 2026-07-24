"""Agent instances (pydantic_ai). All agents run against the local Ollama
server; the model is resolved per-task from cutroom/settings.py (Settings tab)
falling back to config.OLLAMA_MODEL only if settings has no opinion, so
swapping models never touches prompts or schemas.

`AGENT_SPECS` is the single source of truth for what an agent's prompt and
output schema are; `get_agent(task)` builds (and caches) the actual
pydantic_ai Agent lazily, per (task, model) pair, so a Settings change takes
effect on the next call without a restart."""

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.output import PromptedOutput
from pydantic_ai.providers.ollama import OllamaProvider

from .. import config, settings
from .prompts import (
    CLIP_ORDER_SYSTEM_PROMPT,
    REEL_SCORER_SYSTEM_PROMPT,
    TAKE_JUDGE_SYSTEM_PROMPT,
    TRANSCRIPT_CLEANER_SYSTEM_PROMPT,
)
from .schemas import ClipOrder, ReelScore, TakePick, TranscriptCleanup

_MODEL_SETTINGS = {"temperature": 0.2}

AGENT_SPECS: dict[str, dict] = {
    "take_judge": {"prompt": TAKE_JUDGE_SYSTEM_PROMPT, "output_type": TakePick},
    "clip_order": {"prompt": CLIP_ORDER_SYSTEM_PROMPT, "output_type": ClipOrder},
    "reel_scorer": {"prompt": REEL_SCORER_SYSTEM_PROMPT, "output_type": ReelScore},
    "transcript_cleaner": {
        "prompt": TRANSCRIPT_CLEANER_SYSTEM_PROMPT,
        "output_type": TranscriptCleanup,
    },
}

_cache: dict[tuple[str, str], Agent] = {}


def _model(model_name: str) -> OpenAIChatModel:
    return OpenAIChatModel(
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
        agent = Agent(
            model=_model(model_name),
            system_prompt=spec["prompt"].strip(),
            # PromptedOutput (JSON-in-text) instead of the pydantic_ai default
            # tool-calling output mode: the local Ollama runtime's llama-server
            # is launched with `--no-jinja --chat-template chatml`, which breaks
            # native tool-call formatting for Qwen models and made every
            # list[int]-shaped schema (transcript_cleaner, clip_order) fail with
            # "Exceeded maximum output retries" regardless of prompt content.
            # PromptedOutput sidesteps tool-calling entirely and is reliable
            # against this runtime. See docs/PLATFORM-SPEC.md agents section.
            output_type=PromptedOutput(spec["output_type"]),
            model_settings=_MODEL_SETTINGS,
            retries=2,
        )
        _cache[key] = agent
    return agent
