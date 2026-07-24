"""Agent instances (pydantic_ai). All agents run against the local Ollama
server — provider/model are config-driven (CUTROOM_LLM / OLLAMA_URL), so
swapping models never touches prompts or schemas."""

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

from .. import config
from .prompts import (
    CLIP_ORDER_SYSTEM_PROMPT,
    REEL_SCORER_SYSTEM_PROMPT,
    TAKE_JUDGE_SYSTEM_PROMPT,
)
from .schemas import ClipOrder, ReelScore, TakePick

_SETTINGS = {"temperature": 0.2}


def _model() -> OpenAIChatModel:
    return OpenAIChatModel(
        config.OLLAMA_MODEL,
        provider=OllamaProvider(base_url=f"{config.OLLAMA_URL}/v1"),
    )


take_judge_agent = Agent(
    model=_model(),
    system_prompt=TAKE_JUDGE_SYSTEM_PROMPT.strip(),
    output_type=TakePick,
    model_settings=_SETTINGS,
    retries=2,
)

clip_order_agent = Agent(
    model=_model(),
    system_prompt=CLIP_ORDER_SYSTEM_PROMPT.strip(),
    output_type=ClipOrder,
    model_settings=_SETTINGS,
    retries=2,
)

reel_scorer_agent = Agent(
    model=_model(),
    system_prompt=REEL_SCORER_SYSTEM_PROMPT.strip(),
    output_type=ReelScore,
    model_settings=_SETTINGS,
    retries=2,
)
