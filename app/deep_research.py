"""profile_company via LangChain's deep research agent (deepagents.create_deep_agent).

Replaces a hand-rolled two-step "web-search then structure" call with the
todo-planning + sub-agent-delegation pattern LangChain ships for exactly this
kind of "research one company, produce a structured profile" task (see
https://docs.langchain.com/oss/python/deepagents/deep-research, whose own
example is a company marketing-profile research task).

Kept Anthropic-only, no Tavily dependency: create_deep_agent's `tools` accepts
raw dict tool specs alongside callables, so Anthropic's native server-side
web_search tool is bound directly — same tool used by both the main agent and
the research sub-agent, consistent with the rest of this codebase's
Anthropic-specific web search usage (spec.md §7).
"""

import logging

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware

from app.llm import get_chat_anthropic
from app.observability import get_langfuse_callbacks
from app.prompts import (
    COMPANY_RESEARCH_SUBAGENT_PROMPT,
    DEEP_RESEARCH_SYSTEM_PROMPT,
    DEEP_RESEARCH_USER_PROMPT,
    NO_DESCRIPTION_PLACEHOLDER,
    NO_PDF_DIGEST_PLACEHOLDER,
)
from app.schemas import CompanyProfileSchema, PdfDigestSchema

logger = logging.getLogger(__name__)

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}

_COMPANY_RESEARCH_SUBAGENT = {
    "name": "company-research-agent",
    "description": "Delegate deep research on a single company to this sub-agent. Give it one company at a time.",
    "system_prompt": COMPANY_RESEARCH_SUBAGENT_PROMPT,
    "tools": [WEB_SEARCH_TOOL],
}


def _build_agent():
    return create_deep_agent(
        model=get_chat_anthropic(temperature=0),
        tools=[WEB_SEARCH_TOOL],
        system_prompt=DEEP_RESEARCH_SYSTEM_PROMPT,
        subagents=[_COMPANY_RESEARCH_SUBAGENT],
        middleware=[TodoListMiddleware()],
        response_format=CompanyProfileSchema,
    )


def research_company_profile(
    name: str, digest: PdfDigestSchema | None = None, description: str | None = None
) -> CompanyProfileSchema:
    agent = _build_agent()

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
        config={"callbacks": get_langfuse_callbacks()},
    )

    profile = result.get("structured_response")
    if profile is None:
        logger.error("Deep research agent did not return a structured_response")
        raise RuntimeError("deep research agent did not return a structured company profile")

    return profile
