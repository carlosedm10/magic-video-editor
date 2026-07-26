"""Token-budget heuristics for pipeline/ordering.py's hierarchical clip
listing: decide whether the FULL kept-text of every clip fits a model's
context window, or must be compressed via the clip_digest agent instead.

`estimate_tokens`, `fits_context`, and `num_ctx_for` are a CONTRACT other
workstreams import -- keep these signatures exactly as documented, changing
only their internals.

LIVE-VERIFICATION FINDING (2026-07-26): `num_ctx_for`'s result is passed by
pipeline/ordering.py's clip_order call as
`model_settings={"extra_body": {"options": {"num_ctx": n}}}` (pydantic_ai's
generic OpenAI-compat passthrough), but this is a NO-OP against a real
Ollama daemon. Verified empirically against Ollama v0.32.1: neither
`extra_body.options.num_ctx` nor a bare top-level `options`/`num_ctx` field
sent to `/v1/chat/completions` (the OpenAI-compatible endpoint pydantic_ai's
OllamaModel/OpenAIChatModel always uses) changes the context window Ollama
actually loads the model with (`ollama ps`'s CONTEXT column stayed at
whatever the daemon's own default was, e.g. 32768, no matter what was sent).
Ollama's NATIVE `/api/chat` endpoint DOES honor `options.num_ctx` (confirmed
live: passing 2048 there actually shrank the loaded context and resident
size) -- but nothing in this app's pydantic_ai-based agent layer talks to
that endpoint.

The `extra_body` passthrough in ordering.py is kept anyway as harmless
forward-compat (a future Ollama/pydantic_ai release may start honoring it,
or a non-Ollama OpenAI-compatible backend swapped in later might), and
because it costs nothing today. The REAL guard keeping prompts within a
model's actual window is `fits_context` (the go/no-go decision that picks
full-text vs. digest in pipeline/ordering.py's `_build_clip_listing`) --
that decision is made in OUR code before the call, so it doesn't depend on
Ollama accepting a per-call override at all. Separately,
`ollama_manager.py`'s `_spawn_binary` sets `OLLAMA_CONTEXT_LENGTH=32768` in
the environment of any daemon THIS APP spawns (never an already-running
"system" daemon), so the daemon's own default context window matches this
module's `_FAMILY_CONTEXT_TOKENS` assumption instead of drifting from it."""


# A rough, DOCUMENTED-APPROXIMATE chars-per-token heuristic (not an exact
# tokenizer count -- good enough for a budget go/no-go decision, not for
# billing). English/Spanish prose tends to average ~4 characters per token
# across the model families this app targets (qwen/deepseek/llama
# tokenizers), but this can vary +/-30% by language and content; the actual
# safety comes from `fits_context`'s `margin`, not this constant's precision.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token estimate for `text` (chars / 4, see module docstring).
    Never raises: an empty/falsy `text` estimates to 0."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


# Curated per-model-family context windows, in tokens. These are best-effort,
# CONSERVATIVE figures from each family's public model card -- actual context
# length can vary by quantization/fine-tune/Ollama build, and Ollama itself
# often defaults num_ctx far below what a model supports regardless of this
# table (see num_ctx_for, which is what actually sets the per-call window).
# Matched by prefix of the (lowercased) model name, e.g. "qwen3:14b" matches
# "qwen3". An unrecognized family falls back to _DEFAULT_CONTEXT_TOKENS
# (llama3.2's conservative 8k) rather than guessing high and overflowing.
_FAMILY_CONTEXT_TOKENS: dict[str, int] = {
    "qwen3": 32_000,
    "deepseek-r1": 32_000,
    "qwen2.5": 32_000,
    "llama3.2": 8_000,  # deliberately conservative -- see module docstring
}
_DEFAULT_CONTEXT_TOKENS = 8_000


def _context_window_for(model_name: str) -> int:
    name = (model_name or "").lower()
    for family, window in _FAMILY_CONTEXT_TOKENS.items():
        if name.startswith(family):
            return window
    return _DEFAULT_CONTEXT_TOKENS


def fits_context(
    total_tokens: int,
    model_name: str,
    reserve_thinking: int = 4000,
    reserve_output: int = 500,
    margin: float = 0.10,
) -> bool:
    """True if `total_tokens` (the estimated size of the content we want to
    send, e.g. a clip-order listing) plus `reserve_thinking` +
    `reserve_output` headroom fits within `model_name`'s curated context
    window, minus a safety `margin` (fraction of the window held back for
    estimate_tokens's documented approximation error). Never raises -- an
    unrecognized model family falls back to a conservative default window
    (see _context_window_for) instead of crashing the caller."""
    window = _context_window_for(model_name)
    usable = window * (1 - margin)
    return (total_tokens + reserve_thinking + reserve_output) <= usable


# Coarse tiers to round num_ctx up to -- num_ctx directly drives Ollama's
# KV-cache memory allocation, so a smaller number when it's enough saves
# real RAM instead of always requesting the model's full window.
_NUM_CTX_TIERS = (2_000, 4_000, 8_000, 16_000, 32_000)


def num_ctx_for(total_tokens: int, model_name: str) -> int:
    """Tier-capped num_ctx to pass per-call (e.g. via model_settings=
    {'extra_body': {'options': {'num_ctx': n}}}) -- big enough for
    `total_tokens` plus generous thinking/output headroom, but capped at
    `model_name`'s curated context window so Ollama is never asked for more
    than the model actually supports. Never raises."""
    window = _context_window_for(model_name)
    needed = total_tokens + 4000 + 500  # same headroom defaults as fits_context
    for tier in _NUM_CTX_TIERS:
        if needed <= tier:
            return min(tier, window)
    return window
