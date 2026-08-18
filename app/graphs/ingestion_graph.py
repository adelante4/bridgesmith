"""Ingestion LangGraph: extract_pdf_structure -> ingestion_agent -> persist_digest
-> profile_company -> persist_profile. See spec.md §3.1.
"""

import json
import logging
from typing import Any, TypedDict

from sqlmodel import Session

from app.graphs.ingestion_agent import run_ingestion_agent
from app.llm import get_chat_anthropic
from app.models import Company, CompanyProfile, PdfDigest
from app.pdf_extraction import ImageMeta, extract_pdf_structure
from app.prompts import PROFILE_COMPANY_SYSTEM_PROMPT
from app.schemas import CompanyProfileSchema, PdfDigestSchema, WebSource

logger = logging.getLogger(__name__)


class IngestionState(TypedDict, total=False):
    pdf_bytes: bytes
    company_id: str
    role: str
    name: str | None
    assets_dir: str
    annotated_transcript: str
    image_map: dict[str, ImageMeta]
    page_count: int
    tables_json: str
    digest: PdfDigestSchema
    images_reviewed: int
    images_cap_hit: bool
    profile: CompanyProfileSchema


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _extract_web_sources(content: Any) -> list[WebSource]:
    sources: list[WebSource] = []
    seen: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            url = node.get("url")
            if url and url not in seen:
                seen.add(url)
                note = node.get("title") or node.get("encrypted_content", "")[:0] or "cited during web search"
                sources.append(WebSource(url=url, note=str(note)))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(content)
    return sources


def extract_pdf_structure_node(state: IngestionState) -> dict:
    result = extract_pdf_structure(state["pdf_bytes"], state["company_id"], state["assets_dir"])
    return {
        "annotated_transcript": result.annotated_transcript,
        "image_map": result.image_map,
        "page_count": result.page_count,
        "tables_json": result.tables_json,
    }


def make_ingestion_agent_node(session: Session):
    def ingestion_agent_node(state: IngestionState) -> dict:
        digest, images_reviewed, cap_hit = run_ingestion_agent(
            state["annotated_transcript"], session, state["company_id"], state["image_map"]
        )
        return {"digest": digest, "images_reviewed": images_reviewed, "images_cap_hit": cap_hit}

    return ingestion_agent_node


def make_persist_digest_node(session: Session):
    def persist_digest_node(state: IngestionState) -> dict:
        digest = state["digest"]
        row = PdfDigest(
            company_id=state["company_id"],
            digest_text=digest.digest_text,
            key_facts=json.dumps(digest.key_facts),
            document_type=digest.document_type,
            images_reviewed=state["images_reviewed"],
            images_cap_hit=state["images_cap_hit"],
        )
        session.add(row)
        session.commit()
        return {}

    return persist_digest_node


def profile_company_node(state: IngestionState) -> dict:
    digest = state["digest"]

    # Two-step call: (1) a web-search-bound research pass, (2) a plain structuring
    # pass with with_structured_output. Splitting these avoids relying on binding a
    # server-side tool AND forced structured output in a single call, which is a
    # version-fragile combination in the LangChain/Anthropic integration.
    research_model = get_chat_anthropic(temperature=0).bind_tools(
        [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    )
    research_prompt = PROFILE_COMPANY_SYSTEM_PROMPT.format(
        document_type=digest.document_type,
        digest_text=digest.digest_text,
        key_facts=", ".join(digest.key_facts),
    )
    research_result = research_model.invoke([{"role": "user", "content": research_prompt}])
    research_text = _flatten_content(research_result.content)
    web_sources = _extract_web_sources(research_result.content)

    structuring_model = get_chat_anthropic(temperature=0).with_structured_output(CompanyProfileSchema)
    structuring_prompt = (
        "Based on the research below, produce a structured company profile.\n\n"
        f"Research:\n{research_text}\n\n"
        f"Web sources cited: {[s.url for s in web_sources]}"
    )
    profile = structuring_model.invoke([{"role": "user", "content": structuring_prompt}])
    if not profile.web_sources:
        profile.web_sources = web_sources

    return {"profile": profile}


def make_persist_profile_node(session: Session):
    def persist_profile_node(state: IngestionState) -> dict:
        profile = state["profile"]

        company = session.get(Company, state["company_id"])
        if company is None:
            raise RuntimeError(f"Company {state['company_id']} missing before profile persist")

        company.raw_text = state["annotated_transcript"]
        company.tables_json = state["tables_json"]
        if not company.name and state.get("name"):
            company.name = state["name"]
        session.add(company)

        profile_row = CompanyProfile(
            company_id=state["company_id"],
            offerings=profile.offerings,
            industry=profile.industry,
            pain_points=json.dumps(profile.pain_points),
            tone_signals=profile.tone_signals,
            summary=profile.summary,
            web_sources=json.dumps([s.model_dump() for s in profile.web_sources]),
        )
        session.add(profile_row)
        session.commit()
        return {}

    return persist_profile_node


def build_ingestion_graph(session: Session):
    from langgraph.graph import StateGraph

    graph = StateGraph(IngestionState)
    graph.add_node("extract_pdf_structure", extract_pdf_structure_node)
    graph.add_node("ingestion_agent", make_ingestion_agent_node(session))
    graph.add_node("persist_digest", make_persist_digest_node(session))
    graph.add_node("profile_company", profile_company_node)
    graph.add_node("persist_profile", make_persist_profile_node(session))

    graph.set_entry_point("extract_pdf_structure")
    graph.add_edge("extract_pdf_structure", "ingestion_agent")
    graph.add_edge("ingestion_agent", "persist_digest")
    graph.add_edge("persist_digest", "profile_company")
    graph.add_edge("profile_company", "persist_profile")

    return graph.compile()
