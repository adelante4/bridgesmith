"""PDF ingestion LangGraph: extract_pdf_structure -> ingestion_agent -> persist_digest
-> extract_brand. See spec.md §3.1 and docs/adr/0005-decouple-context-from-research.md.

Only ever invoked for a PDF upload — a description-only "add context" doesn't
need any of this and is handled directly in routes/context.py. Deep-research
(web search) is a fully separate action (POST /context/{company_id}/research,
app/deep_research.py) — this graph never triggers it.
"""

import json
import logging
from typing import TypedDict

from langfuse import observe
from sqlmodel import Session

from app.graphs.ingestion_agent import run_ingestion_agent
from app.models import PdfDigest
from app.pdf_extraction import ImageMeta, detect_embedded_font, extract_pdf_structure, render_first_pages
from app.schemas import PdfDigestSchema
from app.vision import extract_brand_from_pages

logger = logging.getLogger(__name__)


class IngestionState(TypedDict, total=False):
    pdf_bytes: bytes
    company_id: str
    assets_dir: str
    annotated_transcript: str
    image_map: dict[str, ImageMeta]
    page_count: int
    tables_json: str
    digest_id: int
    digest: PdfDigestSchema
    images_reviewed: int
    images_cap_hit: bool


@observe(name="extract_pdf_structure", capture_input=False, capture_output=False)
def extract_pdf_structure_node(state: IngestionState) -> dict:
    result = extract_pdf_structure(state["pdf_bytes"], state["company_id"], state["assets_dir"])
    return {
        "annotated_transcript": result.annotated_transcript,
        "image_map": result.image_map,
        "page_count": result.page_count,
        "tables_json": result.tables_json,
    }


def make_create_digest_shell_node(session: Session):
    """Inserts the PdfDigest row for this run before the ingestion agent starts,
    so the describe_image tool has a pdf_digest_id to scope Image rows to this
    run (see docs/adr/0001-...). digest_text/key_facts/document_type/tone_signals
    are filled in later by persist_digest, once submit_digest reports them."""

    @observe(name="create_digest_shell", capture_input=False, capture_output=False)
    def create_digest_shell_node(state: IngestionState) -> dict:
        row = PdfDigest(
            company_id=state["company_id"],
            raw_text=state["annotated_transcript"],
            tables_json=state["tables_json"],
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {"digest_id": row.id}

    return create_digest_shell_node


def make_ingestion_agent_node(session: Session):
    @observe(name="ingestion_agent", capture_input=False, capture_output=False)
    def ingestion_agent_node(state: IngestionState) -> dict:
        digest, images_reviewed, cap_hit = run_ingestion_agent(
            state["annotated_transcript"],
            session,
            state["company_id"],
            state["digest_id"],
            state["image_map"],
        )
        return {"digest": digest, "images_reviewed": images_reviewed, "images_cap_hit": cap_hit}

    return ingestion_agent_node


def make_persist_digest_node(session: Session):
    @observe(name="persist_digest", capture_input=False, capture_output=False)
    def persist_digest_node(state: IngestionState) -> dict:
        digest = state["digest"]
        row = session.get(PdfDigest, state["digest_id"])
        if row is None:
            raise RuntimeError(f"PdfDigest {state['digest_id']} missing before digest persist")

        row.digest_text = digest.digest_text
        row.key_facts = json.dumps(digest.key_facts)
        row.document_type = digest.document_type
        row.tone_signals = digest.tone_signals
        row.images_reviewed = state["images_reviewed"]
        row.images_cap_hit = state["images_cap_hit"]
        session.add(row)
        session.commit()
        return {}

    return persist_digest_node


def make_extract_brand_node(session: Session):
    """Deterministic in shape (fixed 2-page render -> one vision call -> font
    metadata lookup), not agentic — kept separate from the ingestion_agent
    tool-loop, which is scoped to digest text + per-image descriptions."""

    @observe(name="extract_brand", capture_input=False, capture_output=False)
    def extract_brand_node(state: IngestionState) -> dict:
        page_images = render_first_pages(state["pdf_bytes"], max_pages=2)
        brand = extract_brand_from_pages(page_images)
        font_family = detect_embedded_font(state["pdf_bytes"], max_pages=2)

        row = session.get(PdfDigest, state["digest_id"])
        if row is None:
            raise RuntimeError(f"PdfDigest {state['digest_id']} missing before brand persist")

        row.design_notes = brand.design_notes
        row.brand_primary_color = brand.primary_color
        row.brand_accent_color = brand.accent_color
        row.brand_font_family = font_family
        session.add(row)
        session.commit()
        return {}

    return extract_brand_node


def build_ingestion_graph(session: Session):
    from langgraph.graph import StateGraph

    graph = StateGraph(IngestionState)
    graph.add_node("extract_pdf_structure", extract_pdf_structure_node)
    graph.add_node("create_digest_shell", make_create_digest_shell_node(session))
    graph.add_node("ingestion_agent", make_ingestion_agent_node(session))
    graph.add_node("persist_digest", make_persist_digest_node(session))
    graph.add_node("extract_brand", make_extract_brand_node(session))

    graph.set_entry_point("extract_pdf_structure")
    graph.add_edge("extract_pdf_structure", "create_digest_shell")
    graph.add_edge("create_digest_shell", "ingestion_agent")
    graph.add_edge("ingestion_agent", "persist_digest")
    graph.add_edge("persist_digest", "extract_brand")

    return graph.compile()
