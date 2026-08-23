"""Tests for the decoupled context/research split (docs/adr/0005-...): PDF/
description upload never triggers research, brand/font extraction is
deterministic-shape, and /generate's context aggregation is pure (no LLM)."""

import json

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine
from app.graphs.generation_graph import _load_company_context, _style_bundle
from app.main import app
from app.models import Company, Description, PdfDigest, ResearchRun
from app.pdf_extraction import detect_embedded_font, render_first_pages
from app.schemas import BrandGuide, StyleBundle


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _make_pdf_bytes(text: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------------------
# /context: description-only add never calls an LLM (no file -> ingestion
# graph never runs), so this is safe to exercise against the real route.
# ---------------------------------------------------------------------------


def test_description_only_context_creates_company_and_description(client):
    response = client.post(
        "/context",
        data={"name": "Acme Corp", "description": "We sell rocket-powered staplers."},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["description_added"] is True
    assert body["digest_preview"] is None
    company_id = body["company_id"]

    with Session(engine) as session:
        assert session.get(Company, company_id) is not None
        rows = session.exec(select(Description).where(Description.company_id == company_id)).all()
        assert len(rows) == 1
        assert rows[0].text == "We sell rocket-powered staplers."


def test_context_requires_name(client):
    response = client.post("/context", data={"description": "no name given"})
    assert response.status_code == 400


def test_context_requires_description_when_no_file(client):
    response = client.post("/context", data={"name": "No Content Co"})
    assert response.status_code == 400


def test_research_missing_company_returns_404(client):
    response = client.post("/context/does_not_exist/research")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Deterministic PDF-derived signals — no LLM involved.
# ---------------------------------------------------------------------------


def test_detect_embedded_font_reads_pdf_metadata():
    pdf_bytes = _make_pdf_bytes("Hello brand font")
    font = detect_embedded_font(pdf_bytes)
    assert font is not None
    assert isinstance(font, str)


def test_render_first_pages_caps_at_max_pages():
    doc = pymupdf.open()
    for i in range(4):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {i}")
    pdf_bytes = doc.tobytes()
    doc.close()

    pages = render_first_pages(pdf_bytes, max_pages=2)
    assert len(pages) == 2
    for png_bytes in pages:
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Generation-time aggregation is pure concatenation/selection — no LLM.
# ---------------------------------------------------------------------------


def test_context_blob_concatenates_all_sources_and_only_latest_research():
    with Session(engine) as session:
        company = Company(id="blob_co", name="Blob Co")
        session.add(company)
        session.commit()

        session.add(
            PdfDigest(company_id="blob_co", digest_text="First PDF digest.", document_type="one-pager")
        )
        session.add(
            PdfDigest(company_id="blob_co", digest_text="Second PDF digest.", document_type="brochure")
        )
        session.add(Description(company_id="blob_co", text="A free-text note."))
        session.add(
            ResearchRun(
                company_id="blob_co",
                offerings="Widgets",
                industry="Manufacturing",
                pain_points=json.dumps(["slow shipping"]),
                summary="Old research summary.",
                web_sources="[]",
            )
        )
        session.add(
            ResearchRun(
                company_id="blob_co",
                offerings="Widgets 2.0",
                industry="Manufacturing",
                pain_points=json.dumps(["slow shipping", "high cost"]),
                summary="Newest research summary.",
                web_sources="[]",
            )
        )
        session.commit()

        blob = _load_company_context(session, "blob_co")["blob"]

    assert "First PDF digest." in blob
    assert "Second PDF digest." in blob
    assert "A free-text note." in blob
    assert "Newest research summary." in blob
    assert "Old research summary." not in blob


def test_context_blob_placeholder_when_no_context():
    with Session(engine) as session:
        session.add(Company(id="empty_co", name="Empty Co"))
        session.commit()
        blob = _load_company_context(session, "empty_co")["blob"]
    assert "no context available" in blob.lower()


def test_company_context_summary_falls_back_when_no_research_but_pdfs_exist():
    with Session(engine) as session:
        session.add(Company(id="summary_co", name="Summary Co"))
        session.add(PdfDigest(company_id="summary_co", digest_text="Some PDF content."))
        session.commit()
        ctx = _load_company_context(session, "summary_co")
    assert "no pdfs" not in ctx["summary"].lower()
    assert "no web research run yet" in ctx["summary"].lower()


def test_style_bundle_reads_given_digest_null_safe():
    digest = PdfDigest(
        company_id="style_co",
        tone_signals="playful",
        design_notes="minimal",
        brand_primary_color="#00FF00",
        brand_accent_color="#0000FF",
        brand_font_family="Poppins",
    )

    bundle = _style_bundle(digest)
    assert bundle == StyleBundle(
        tone_signals="playful",
        design_notes="minimal",
        brand=BrandGuide(primary_color="#00FF00", accent_color="#0000FF", font_family="Poppins"),
    )


def test_style_bundle_null_safe_with_no_digest():
    bundle = _style_bundle(None)
    assert bundle.brand.primary_color is None
    assert bundle.brand.accent_color is None
    assert bundle.brand.font_family is None
    assert "no pdf" in bundle.tone_signals.lower()
