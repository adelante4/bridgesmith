"""POST /context — add PDF and/or free-text context for a company (spec.md §5.1).
POST /context/{company_id}/research — separately trigger deep web research.

Decoupled on purpose (docs/adr/0005-decouple-context-from-research.md): adding
context never fires an LLM web-search call, and research never requires a
fresh upload — each is its own append-only log, both read together by
/generate at generation time.
"""

import json
import logging
import os
from uuid import uuid4

import pymupdf as fitz
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session

from app.context_store import all_descriptions, latest_pdf_digest
from app.db import get_session
from app.deep_research import research_company_profile
from app.graphs.ingestion_graph import build_ingestion_graph
from app.models import Company, Description, ResearchRun
from app.observability import new_trace_config, traced_route
from app.schemas import ContextUploadResponse, PdfDigestSchema, ResearchRunResponse, WebSource

logger = logging.getLogger(__name__)
router = APIRouter()

ASSETS_DIR = os.path.join(os.environ.get("DATA_DIR", "data"), "assets")


@router.post("/context", response_model=ContextUploadResponse)
async def upload_context(
    company_id: str | None = Form(None),
    name: str | None = Form(None),
    description: str | None = Form(None),
    file: UploadFile | None = File(None),
    session: Session = Depends(get_session),
) -> ContextUploadResponse:
    if not name:
        raise HTTPException(400, "name is required")

    pdf_bytes: bytes | None = None
    if file is not None:
        filename = (file.filename or "").lower()
        if file.content_type != "application/pdf" and not filename.endswith(".pdf"):
            raise HTTPException(400, "file must be a PDF")

        pdf_bytes = await file.read()
        if not pdf_bytes.startswith(b"%PDF"):
            raise HTTPException(400, "file is not a valid PDF")
    elif not description:
        raise HTTPException(400, "description is required when no file is uploaded")

    resolved_company_id = company_id or f"co_{uuid4().hex[:8]}"

    if session.get(Company, resolved_company_id) is None:
        session.add(Company(id=resolved_company_id, name=name))
        session.commit()

    page_count = images_extracted = images_described = None
    digest_preview = None

    if pdf_bytes is not None:
        graph = build_ingestion_graph(session)
        initial_state = {
            "pdf_bytes": pdf_bytes,
            "company_id": resolved_company_id,
            "assets_dir": ASSETS_DIR,
        }
        try:
            with traced_route("context", metadata={"company_id": resolved_company_id}):
                final_state = graph.invoke(initial_state, config=new_trace_config())
        except fitz.FileDataError as e:
            logger.exception("PDF extraction failed for company_id=%s", resolved_company_id)
            raise HTTPException(500, f"PDF extraction failed: {e}") from e
        except RuntimeError as e:
            logger.exception("Ingestion agent failed for company_id=%s", resolved_company_id)
            raise HTTPException(500, f"Ingestion failed: {e}") from e
        except Exception as e:
            logger.exception("Unexpected ingestion failure for company_id=%s", resolved_company_id)
            raise HTTPException(500, f"Ingestion failed: {e}") from e

        page_count = final_state.get("page_count")
        images_extracted = len(final_state["image_map"]) if "image_map" in final_state else None
        images_described = final_state.get("images_reviewed")
        digest = final_state.get("digest")
        digest_preview = digest.digest_text[:280] if digest is not None else None

    description_added = False
    if description:
        session.add(Description(company_id=resolved_company_id, text=description))
        session.commit()
        description_added = True

    return ContextUploadResponse(
        company_id=resolved_company_id,
        page_count=page_count,
        images_extracted=images_extracted,
        images_described=images_described,
        digest_preview=digest_preview,
        description_added=description_added,
    )


@router.post("/context/{company_id}/research", response_model=ResearchRunResponse)
def run_research(company_id: str, session: Session = Depends(get_session)) -> ResearchRunResponse:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(404, f"company '{company_id}' not found")

    latest_digest = latest_pdf_digest(session, company_id)
    digest_arg: PdfDigestSchema | None = None
    if latest_digest is not None:
        digest_arg = PdfDigestSchema(
            digest_text=latest_digest.digest_text,
            key_facts=json.loads(latest_digest.key_facts),
            document_type=latest_digest.document_type,
            tone_signals=latest_digest.tone_signals,
        )

    descriptions = all_descriptions(session, company_id)
    description_arg = "\n\n".join(d.text for d in descriptions) or None

    try:
        with traced_route("context_research", metadata={"company_id": company_id}):
            result = research_company_profile(
                name=company.name or company_id,
                digest=digest_arg,
                description=description_arg,
                config=new_trace_config(),
            )
    except Exception as e:
        logger.exception("Deep research failed for company_id=%s", company_id)
        raise HTTPException(500, f"Research failed: {e}") from e

    row = ResearchRun(
        company_id=company_id,
        offerings=result.offerings,
        industry=result.industry,
        target_customers=result.target_customers,
        pain_points=json.dumps(result.pain_points),
        differentiators=json.dumps(result.differentiators),
        proof_points=json.dumps(result.proof_points),
        recent_developments=json.dumps(result.recent_developments),
        summary=result.summary,
        web_sources=json.dumps([s.model_dump() for s in result.web_sources]),
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    return ResearchRunResponse(
        company_id=company_id,
        offerings=row.offerings,
        industry=row.industry,
        target_customers=row.target_customers,
        pain_points=result.pain_points,
        differentiators=result.differentiators,
        proof_points=result.proof_points,
        recent_developments=result.recent_developments,
        summary=row.summary,
        web_sources=[WebSource(**s) for s in json.loads(row.web_sources)],
        created_at=row.created_at,
    )
