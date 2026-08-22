"""POST /generate — generate a tailored article as structured JSON (spec.md §5.2)."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from app.db import get_session
from app.graphs.generation_graph import CompanyNotFoundError, build_generation_graph
from app.models import GeneratedArticle
from app.observability import new_trace_config
from app.schemas import GenerateRequest, GenerateResponse
from app.templates import TemplateNotFoundError, load_template

logger = logging.getLogger(__name__)
router = APIRouter()


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
        final_state = graph.invoke(initial_state, config=new_trace_config())
    except CompanyNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:
        logger.exception(
            "Generation failed for sender=%s receiver=%s", request.sender_id, request.receiver_id
        )
        raise HTTPException(502, f"LLM generation failed: {e}") from e

    response = GenerateResponse(
        template_id=template.template_id,
        headline=final_state["final_headline"],
        subheadline=final_state["final_subheadline"],
        sections=final_state["final_sections"],
        pull_quote=final_state["final_pull_quote"],
        cta=final_state["final_cta"],
        image_placeholders=final_state["image_placeholders"],
        theme=template.theme,
        grounding_notes="All claims sourced from sender/receiver company profiles (uploaded context, supplemented by web search performed once at ingestion time); no unverified figures included.",
    )

    session.add(
        GeneratedArticle(
            sender_id=request.sender_id,
            receiver_id=request.receiver_id,
            sender_profile_id=final_state["sender_profile_id"],
            receiver_profile_id=final_state["receiver_profile_id"],
            prompt=request.prompt,
            template_id=template.template_id,
            result_json=response.model_dump_json(),
        )
    )
    session.commit()

    return response
