"""Non-blocking smoke tests. Only cover paths that don't require a live LLM call
(input validation, missing-company 404, deterministic PDF extraction) — per
spec.md §13 item 10, these are explicitly optional and not meant to block the
core deliverable."""

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pdf_extraction import extract_pdf_structure


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


def test_upload_context_rejects_non_pdf(client):
    response = client.post(
        "/context",
        data={"role": "sender"},
        files={"file": ("notes.txt", b"not a pdf", "text/plain")},
    )
    assert response.status_code == 400


def test_upload_context_rejects_invalid_role(client):
    response = client.post(
        "/context",
        data={"role": "vendor"},
        files={"file": ("doc.pdf", _make_pdf_bytes("hi"), "application/pdf")},
    )
    assert response.status_code == 422


def test_generate_missing_sender_returns_404(client):
    response = client.post(
        "/generate",
        json={
            "sender_id": "sender_does_not_exist",
            "receiver_id": "receiver_does_not_exist",
            "prompt": "test prompt",
        },
    )
    assert response.status_code == 404
    assert "sender" in response.json()["detail"]


def test_generate_invalid_template_returns_422(client):
    response = client.post(
        "/generate",
        json={
            "sender_id": "sender_x",
            "receiver_id": "receiver_x",
            "prompt": "test prompt",
            "template_id": "does_not_exist",
        },
    )
    assert response.status_code == 422


def test_pdf_extraction_preserves_image_marker_order(tmp_path):
    import io

    from PIL import Image

    img = Image.new("RGB", (50, 50), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Header text before image.")
    page.insert_image(pymupdf.Rect(72, 100, 172, 200), stream=buf.getvalue())
    page.insert_text((72, 220), "Footer text after image.")
    pdf_bytes = doc.tobytes()
    doc.close()

    result = extract_pdf_structure(pdf_bytes, "smoke_co", str(tmp_path))

    assert "[[IMAGE:img_00_00]]" in result.annotated_transcript
    transcript = result.annotated_transcript
    assert transcript.index("Header") < transcript.index("[[IMAGE") < transcript.index("Footer")
    assert "img_00_00" in result.image_map
