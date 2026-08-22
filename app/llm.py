"""LLM model construction, per agent.

Each agent (generate_draft, ingestion_agent, vision, deep_research) picks its
model independently via its own env var, falling back to DEFAULT_MODEL (Luna)
when unset. Provider is inferred from the model name by langchain's
init_chat_model (`claude-*` -> anthropic, `gpt-*` -> openai, etc.) — there's
no separate LLM_PROVIDER switch anymore.

deep_research and vision additionally need a provider-specific web search
tool / multimodal content-block format; see get_web_search_tool below and
vision.py's use of standard (provider-agnostic) content blocks.
"""

import os

from langchain.chat_models import init_chat_model

DEFAULT_MODEL = "gpt-5.6-luna"

GENERATION_MODEL_ENV = "GENERATION_MODEL"
INGESTION_AGENT_MODEL_ENV = "INGESTION_AGENT_MODEL"
VISION_MODEL_ENV = "VISION_MODEL"
DEEP_RESEARCH_MODEL_ENV = "DEEP_RESEARCH_MODEL"


def get_agent_model_name(env_var: str) -> str:
    return os.environ.get(env_var, DEFAULT_MODEL)


def get_agent_model(env_var: str, **kwargs):
    """Provider-agnostic model for one agent, selected via its own env var.
    Falls back to DEFAULT_MODEL (Luna, openai) when the env var isn't set."""
    model_name = get_agent_model_name(env_var)
    if model_name.startswith("gpt") or model_name.startswith("o1") or model_name.startswith("o3"):
        # OpenAI reasoning models reject reasoning_effort on /v1/chat/completions
        # when function tools are bound; the Responses API supports both.
        kwargs.setdefault("use_responses_api", True)
        kwargs.setdefault("reasoning_effort", "medium")
    return init_chat_model(model_name, **kwargs)


def get_web_search_tool(env_var: str) -> dict:
    """Hosted web-search tool spec for whichever provider the given agent's
    model name resolves to. Anthropic and OpenAI expose this as different
    dict-shaped hosted tools; no equivalent is wired for other providers yet."""
    model_name = get_agent_model_name(env_var)
    if model_name.startswith("claude"):
        return {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}
    if model_name.startswith("gpt") or model_name.startswith("o1") or model_name.startswith("o3"):
        return {"type": "web_search"}
    raise ValueError(
        f"no web_search tool binding for model '{model_name}' "
        f"(env var {env_var}) — only claude-*/gpt-*/o1-*/o3-* are wired"
    )
