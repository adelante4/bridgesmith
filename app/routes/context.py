"""POST /context — upload a PDF as context for a company (spec.md §5.1)."""

import logging
import os
from uuid import uuid4

import pymupdf as fitz
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session

from app.db import get_session
from app.graphs.ingestion_graph import build_ingestion_graph
from app.models import Company, CompanyRole
from app.schemas import ContextUploadResponse

logger = logging.getLogger(__name__)
router = APIRouter()

ASSETS_DIR = os.path.join(os.environ.get("DATA_DIR", "data"), "assets")


@router.post("/context", response_model=ContextUploadResponse)
async def upload_context(
    role: str = Form(...),
    company_id: str | None = Form(None),
    name: str | None = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> ContextUploadResponse:
    if role not in ("sender", "receiver"):
        raise HTTPException(422, f"role must be 'sender' or 'receiver', got '{role}'")

    filename = (file.filename or "").lower()
    if file.content_type != "application/pdf" and not filename.endswith(".pdf"):
        raise HTTPException(400, "file must be a PDF")

    pdf_bytes = await file.read()
    if not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(400, "file is not a valid PDF")

    resolved_company_id = company_id or f"{role}_{uuid4().hex[:8]}"

    if session.get(Company, resolved_company_id) is None:
        session.add(Company(id=resolved_company_id, role=CompanyRole(role), name=name))
        session.commit()

    graph = build_ingestion_graph(session)
    initial_state = {
        "pdf_bytes": pdf_bytes,
        "company_id": resolved_company_id,
        "role": role,
        "name": name,
        "assets_dir": ASSETS_DIR,
    }

    try:
        final_state = graph.invoke(initial_state)
    except fitz.FileDataError as e:
        logger.exception("PDF extraction failed for company_id=%s", resolved_company_id)
        raise HTTPException(500, f"PDF extraction failed: {e}") from e
    except RuntimeError as e:
        logger.exception("Ingestion agent failed for company_id=%s", resolved_company_id)
        raise HTTPException(500, f"Ingestion failed: {e}") from e
    except Exception as e:
        logger.exception("Unexpected ingestion failure for company_id=%s", resolved_company_id)
        raise HTTPException(500, f"Ingestion failed: {e}") from e

    digest = final_state["digest"]
    profile = final_state["profile"]

    return ContextUploadResponse(
        company_id=resolved_company_id,
        role=role,
        page_count=final_state["page_count"],
        images_extracted=len(final_state["image_map"]),
        images_described=final_state["images_reviewed"],
        digest_preview=digest.digest_text[:280],
        profile_summary=profile.summary,
        web_sources=[s.url for s in profile.web_sources],
    )
