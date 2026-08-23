"""POST /generate — generate a tailored article as structured JSON (spec.md §5.2)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.graphs.generation_graph import CompanyNotFoundError, build_generation_graph
from app.models import GeneratedArticle
from app.observability import new_trace_config, traced_route
from app.pdf_render import render_brochure_pdf, resolve_theme
from app.schemas import GenerateRequest, GenerateResponse
from app.templates import TemplateNotFoundError, load_template

logger = logging.getLogger(__name__)
router = APIRouter()

PDF_ENABLED_TEMPLATE_IDS = {"brochure_v1"}


@router.post("/generate", response_model=GenerateResponse)
def generate_article(request: GenerateRequest, session: Session = Depends(get_session)) -> GenerateResponse:
    try:
        template = load_template(request.template_id)
    except TemplateNotFoundError as e:
        raise HTTPException(422, str(e)) from e

    graph = build_generation_graph(session)
    initial_state = {
        "sender_id": request.sender_id,
        "receiver_id": request.receiver_id,
        "prompt": request.prompt,
        "template": template,
    }

    try:
        with traced_route(
            "generate", metadata={"sender_id": request.sender_id, "receiver_id": request.receiver_id}
        ):
            final_state = graph.invoke(initial_state, config=new_trace_config())
    except CompanyNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        logger.exception(
            "Generation failed for sender=%s receiver=%s", request.sender_id, request.receiver_id
        )
        raise HTTPException(502, f"LLM generation failed: {e}") from e

    pdf_supported = template.template_id in PDF_ENABLED_TEMPLATE_IDS
    theme = (
        resolve_theme(template.theme, final_state["sender_style"].brand) if pdf_supported else template.theme
    )

    response = GenerateResponse(
        template_id=template.template_id,
        headline=final_state["final_headline"],
        subheadline=final_state["final_subheadline"],
        sections=final_state["final_sections"],
        pull_quote=final_state["final_pull_quote"],
        cta=final_state["final_cta"],
        image_placeholders=final_state["image_placeholders"],
        theme=theme,
        grounding_notes="All claims sourced from sender/receiver company profiles plus per-generation web research on both companies; no unverified figures included.",
        sources=final_state["draft"].sources,
    )

    article = GeneratedArticle(
        sender_id=request.sender_id,
        receiver_id=request.receiver_id,
        sender_pdf_digest_id=final_state["sender_pdf_digest_id"],
        receiver_pdf_digest_id=final_state["receiver_pdf_digest_id"],
        prompt=request.prompt,
        template_id=template.template_id,
        result_json=response.model_dump_json(),
    )
    session.add(article)
    session.commit()
    session.refresh(article)

    if pdf_supported:
        try:
            pdf_path = render_brochure_pdf(response, theme, session, request.sender_id, article.id)
        except Exception:
            # PDF rendering is a bonus artifact on top of the JSON contract
            # (the actual deliverable, per spec.md) — never let a render
            # failure fail an otherwise-successful generation.
            logger.exception("PDF render failed for article=%s, continuing without it", article.id)
        else:
            response.pdf_path = pdf_path
            article.pdf_path = pdf_path
            article.result_json = response.model_dump_json()
            session.add(article)
            session.commit()

    return response
