"""POST /context — upload a PDF as context for a company (spec.md §5.1)."""

import logging
import os
from uuid import uuid4

import pymupdf as fitz
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session

from app.db import get_session
from app.graphs.ingestion_graph import build_ingestion_graph
from app.models import Company
from app.observability import new_trace_config
from app.schemas import ContextUploadResponse

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

    graph = build_ingestion_graph(session)
    initial_state = {
        "pdf_bytes": pdf_bytes,
        "company_id": resolved_company_id,
        "name": name,
        "description": description,
        "assets_dir": ASSETS_DIR,
    }

    try:
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

    digest = final_state.get("digest")
    profile = final_state["profile"]

    return ContextUploadResponse(
        company_id=resolved_company_id,
        page_count=final_state.get("page_count"),
        images_extracted=len(final_state["image_map"]) if "image_map" in final_state else None,
        images_described=final_state.get("images_reviewed"),
        digest_preview=digest.digest_text[:280] if digest is not None else None,
        profile_summary=profile.summary,
        web_sources=[s.url for s in profile.web_sources],
    )
