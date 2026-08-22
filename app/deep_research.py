"""profile_company via LangChain's deep research agent (deepagents.create_deep_agent).

Replaces a hand-rolled two-step "web-search then structure" call with the
todo-planning + sub-agent-delegation pattern LangChain ships for exactly this
kind of "research one company, produce a structured profile" task (see
https://docs.langchain.com/oss/python/deepagents/deep-research, whose own
example is a company marketing-profile research task).

Uses whichever model DEEP_RESEARCH_MODEL_ENV resolves to, via create_deep_agent's
`tools` accepting raw dict tool specs alongside callables: the hosted web_search
tool spec is picked per-provider by app.llm.get_web_search_tool (Anthropic's
web_search_20250305 vs OpenAI's web_search) — same tool used by both the main
agent and the research sub-agent. No Tavily/Brave fallback wired for other
providers; get_web_search_tool raises for those (spec.md §7).
"""

import logging

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.runnables import RunnableConfig

from app.llm import DEEP_RESEARCH_MODEL_ENV, get_agent_model, get_web_search_tool
from app.observability import new_trace_config
from app.prompts import (
    COMPANY_RESEARCH_SUBAGENT_PROMPT,
    DEEP_RESEARCH_SYSTEM_PROMPT,
    DEEP_RESEARCH_USER_PROMPT,
    NO_DESCRIPTION_PLACEHOLDER,
    NO_PDF_DIGEST_PLACEHOLDER,
)
from app.schemas import CompanyProfileSchema, PdfDigestSchema

logger = logging.getLogger(__name__)


def _build_agent():
    web_search_tool = get_web_search_tool(DEEP_RESEARCH_MODEL_ENV)
    company_research_subagent = {
        "name": "company-research-agent",
        "description": "Delegate deep research on a single company to this sub-agent. Give it one company at a time.",
        "system_prompt": COMPANY_RESEARCH_SUBAGENT_PROMPT,
        "tools": [web_search_tool],
    }
    return create_deep_agent(
        model=get_agent_model(DEEP_RESEARCH_MODEL_ENV),
        tools=[web_search_tool],
        system_prompt=DEEP_RESEARCH_SYSTEM_PROMPT,
        subagents=[company_research_subagent],
        middleware=[TodoListMiddleware()],
        response_format=CompanyProfileSchema,
    )


def research_company_profile(
    name: str,
    digest: PdfDigestSchema | None = None,
    description: str | None = None,
    config: RunnableConfig | None = None,
) -> CompanyProfileSchema:
    """`config` should be the caller's RunnableConfig, threaded down so this agent's
    spans nest under the caller's Langfuse trace. Falls back to a fresh (root)
    trace only for standalone/direct calls outside the ingestion graph."""
    agent = _build_agent()
    config = config or new_trace_config()

    if digest is not None:
        pdf_section = (
            f"Document type: {digest.document_type}\n"
            f"Digest: {digest.digest_text}\n"
            f"Key facts: {', '.join(digest.key_facts)}"
        )
    else:
        pdf_section = NO_PDF_DIGEST_PLACEHOLDER

    description_section = description if description else NO_DESCRIPTION_PLACEHOLDER

    prompt = DEEP_RESEARCH_USER_PROMPT.format(
        name=name, pdf_section=pdf_section, description_section=description_section
    )

    result = agent.invoke(
        {"messages": [{"role": "user", "content": prompt}]},
        config=config,
    )

    profile = result.get("structured_response")
    if profile is None:
        logger.error("Deep research agent did not return a structured_response")
        raise RuntimeError("deep research agent did not return a structured company profile")

    return profile
