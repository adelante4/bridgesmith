"""LLM model construction. Provider-agnostic path (init_chat_model) for
generate_draft; direct ChatAnthropic for the Anthropic-specific web search
and native multimodal image features (see spec.md §7)."""

import os

from langchain.chat_models import init_chat_model
from langchain_anthropic import ChatAnthropic

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"


def get_anthropic_model_name() -> str:
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)


def get_openai_model_name() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)


def get_chat_anthropic(**kwargs) -> ChatAnthropic:
    """Fresh ChatAnthropic instance — no shared state/history across callers."""
    return ChatAnthropic(model=get_anthropic_model_name(), **kwargs)


def get_provider_agnostic_model(**kwargs):
    """Provider-agnostic model selection via LLM_PROVIDER env var, per spec §7/§9.
    Used only by generate_draft, which deliberately has no provider-specific tools bound."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    if provider == "anthropic":
        model_name = get_anthropic_model_name()
        return init_chat_model(model_name, model_provider="anthropic", **kwargs)
    if provider == "openai":
        model_name = get_openai_model_name()
        return init_chat_model(model_name, model_provider="openai", **kwargs)
    return init_chat_model(model_provider=provider, **kwargs)
