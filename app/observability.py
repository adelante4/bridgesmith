"""Langfuse tracing. Self-hosted locally via the `langfuse-*` services in
docker-compose.yml — see README's Langfuse section. No-ops if
LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY aren't set, so this is safe to leave
wired in for environments that don't run the local stack.

Tracing shape: one root span per API call (started here via `traced_route`,
which becomes the trace root), one child span per LangGraph node (via the
`@observe` decorator on each node function), and GENERATION spans for actual
LLM calls nested under whichever span is current when the call happens.
Nesting is carried by Langfuse's OTEL context, not by threading a shared
CallbackHandler/RunnableConfig through the graph — any node can call
`get_langfuse_callbacks()` fresh and it lands in the right place.
"""

import logging
import os
from contextlib import contextmanager

from langfuse import Langfuse, get_client
from langfuse.langchain import CallbackHandler as _LangchainCallbackHandler

logger = logging.getLogger(__name__)

_ENABLED = bool(os.environ.get("LANGFUSE_PUBLIC_KEY")) and bool(os.environ.get("LANGFUSE_SECRET_KEY"))
_client: Langfuse | None = None


class GenerationsOnlyCallbackHandler(_LangchainCallbackHandler):
    """Suppresses CHAIN-type observations (RunnableSequence, ChatPromptTemplate,
    create_react_agent's/deepagents' internal graph, ...), keeping only
    GENERATION spans for actual LLM calls. Without this, LangChain's
    node-wrapping inflates each trace 5-10x with duplicate copies of the same
    prompt. Graph-level structure (which node ran, in what order) is carried
    by the `@observe`-decorated node functions instead, not by these chain
    spans."""

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
    """Fresh callback handler for one LLM/agent invocation. Safe to call at any
    nested call site (node function, tool, sub-agent): with no explicit
    trace_context, it attaches its GENERATION spans under whatever Langfuse
    span is current in the OTEL context (the enclosing `traced_route` /
    `@observe` span), so no manual threading is required for correct nesting."""
    if not _ENABLED:
        return []
    return [GenerationsOnlyCallbackHandler()]


def new_trace_config() -> dict:
    """RunnableConfig carrying a fresh callback handler for a single .invoke()
    call. Nesting under the enclosing trace/span is automatic (see
    get_langfuse_callbacks) — this no longer needs to be the same instance
    threaded through the whole call tree."""
    return {"callbacks": get_langfuse_callbacks()}


@contextmanager
def traced_route(name: str, **trace_attrs):
    """Wrap a top-level route handler's body in this to start ONE root trace
    for the whole request — every `@observe`-decorated graph node and every
    GENERATION spun up underneath (however deep) nests as its child/descendant
    instead of becoming its own disconnected root trace. No-op if Langfuse
    isn't enabled."""
    if not _ENABLED:
        yield
        return
    with get_client().start_as_current_observation(as_type="span", name=name) as span:
        span.update_trace(name=name, **trace_attrs)
        yield
