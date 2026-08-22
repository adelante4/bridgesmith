"""Langfuse tracing. Self-hosted locally via the `langfuse-*` services in
docker-compose.yml — see README's Langfuse section. No-ops if
LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY aren't set, so this is safe to leave
wired in for environments that don't run the local stack.
"""

import logging
import os

from langfuse import Langfuse
from langfuse.langchain import CallbackHandler as _LangchainCallbackHandler

logger = logging.getLogger(__name__)

_ENABLED = bool(os.environ.get("LANGFUSE_PUBLIC_KEY")) and bool(os.environ.get("LANGFUSE_SECRET_KEY"))
_client: Langfuse | None = None


class GenerationsOnlyCallbackHandler(_LangchainCallbackHandler):
    """Suppresses CHAIN-type observations (RunnableSequence, ChatPromptTemplate,
    create_react_agent's internal graph, ...), keeping only GENERATION spans for
    actual LLM calls. Without this, LangGraph/LangChain's node-wrapping inflates
    each trace 5-10x with duplicate copies of the same prompt."""

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._child_to_parent_run_id_map[run_id] = parent_run_id
        self._register_langfuse_prompt(run_id=run_id, parent_run_id=parent_run_id, metadata=metadata)

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        self._deregister_langfuse_prompt(run_id)
        if parent_run_id is None:
            self._reset()

    def on_chain_error(self, error, *, run_id, parent_run_id=None, tags=None, **kwargs):
        if parent_run_id is None:
            self._reset()


def init_langfuse() -> None:
    """Call once at app startup. No-op if credentials aren't set."""
    global _client
    if not _ENABLED:
        logger.info("Langfuse tracing disabled (LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY not set)")
        return
    _client = Langfuse()
    logger.info("Langfuse tracing initialized, host=%s", os.environ.get("LANGFUSE_HOST"))


def get_langfuse_callbacks() -> list:
    """Callback list to pass as config={"callbacks": ...} at each LLM/agent invoke site."""
    if not _ENABLED:
        return []
    return [GenerationsOnlyCallbackHandler()]
