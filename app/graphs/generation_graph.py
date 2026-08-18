"""Generation LangGraph: load_profiles -> build_prompt -> generate_draft -> validate
-> (repair -> validate)* -> select_assets. See spec.md §3.2.
"""

import json
import logging
from typing import Any, Literal, TypedDict

from sqlmodel import Session, select

from app.llm import get_provider_agnostic_model
from app.models import CompanyProfile as CompanyProfileRow
from app.models import Image
from app.prompts import GENERATE_DRAFT_SYSTEM_PROMPT, REPAIR_FIELD_PROMPT
from app.schemas import (
    ArticleSchema,
    ArticleSectionDraft,
    CompanyProfileSchema,
    GenerateImagePlaceholder,
    GenerateSection,
    TemplateConfig,
    WebSource,
)

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2


class CompanyNotFoundError(Exception):
    def __init__(self, company_id: str, role: str):
        self.company_id = company_id
        self.role = role
        super().__init__(f"{role} company '{company_id}' not found")


class GenerationState(TypedDict, total=False):
    sender_id: str
    receiver_id: str
    prompt: str
    template: TemplateConfig
    sender_profile: CompanyProfileSchema
    receiver_profile: CompanyProfileSchema
    system_prompt: str
    draft: ArticleSchema
    validation_errors: list[dict]
    repair_attempts: int
    final_sections: list[GenerateSection]
    final_headline: str
    final_subheadline: str
    final_pull_quote: str
    final_cta: str
    image_placeholders: list[GenerateImagePlaceholder]


def _word_count(text: str) -> int:
    return len(text.split())


def _word_truncate(text: str, max_words: int) -> str:
    return " ".join(text.split()[:max_words])


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _profile_row_to_schema(row: CompanyProfileRow) -> CompanyProfileSchema:
    return CompanyProfileSchema(
        offerings=row.offerings,
        industry=row.industry,
        pain_points=json.loads(row.pain_points),
        tone_signals=row.tone_signals,
        summary=row.summary,
        web_sources=[WebSource(**s) for s in json.loads(row.web_sources)],
    )


def make_load_profiles_node(session: Session):
    def load_profiles_node(state: GenerationState) -> dict:
        sender_row = session.get(CompanyProfileRow, state["sender_id"])
        if sender_row is None:
            raise CompanyNotFoundError(state["sender_id"], "sender")
        receiver_row = session.get(CompanyProfileRow, state["receiver_id"])
        if receiver_row is None:
            raise CompanyNotFoundError(state["receiver_id"], "receiver")

        return {
            "sender_profile": _profile_row_to_schema(sender_row),
            "receiver_profile": _profile_row_to_schema(receiver_row),
        }

    return load_profiles_node


def build_prompt_node(state: GenerationState) -> dict:
    template = state["template"]
    fields = template.fields

    lines = [
        f"headline: max {fields.headline.max_words} words",
        f"subheadline: max {fields.subheadline.max_words} words",
    ]
    for s in fields.sections:
        min_part = f"min {s.min_words}, " if s.min_words else ""
        lines.append(f"section '{s.id}': {min_part}max {s.max_words} words — {s.guidance}")
    lines.append(f"pull_quote: max {fields.pull_quote.max_words} words")
    lines.append(f"cta: max {fields.cta.max_words} words")

    system_prompt = GENERATE_DRAFT_SYSTEM_PROMPT.format(
        sender_profile=state["sender_profile"].model_dump_json(),
        receiver_profile=state["receiver_profile"].model_dump_json(),
        user_prompt=state["prompt"],
        template_constraints="\n".join(lines),
        image_slots=", ".join(template.image_slots),
    )
    return {"system_prompt": system_prompt}


def generate_draft_node(state: GenerationState) -> dict:
    # Fresh, clean model instance — deliberately no tools bound (see spec §3.2).
    model = get_provider_agnostic_model(temperature=0.4).with_structured_output(ArticleSchema)
    draft = model.invoke([{"role": "user", "content": state["system_prompt"]}])
    return {"draft": draft, "repair_attempts": 0}


def validate_node(state: GenerationState) -> dict:
    template = state["template"]
    draft = state["draft"]
    errors: list[dict] = []

    def check(field_id: str, text: str, max_words: int, min_words: int | None = None, guidance: str = ""):
        wc = _word_count(text)
        if wc > max_words:
            errors.append(
                {"field_id": field_id, "actual_words": wc, "limit": max_words, "kind": "max", "guidance": guidance}
            )
        elif min_words and wc < min_words:
            errors.append(
                {"field_id": field_id, "actual_words": wc, "limit": min_words, "kind": "min", "guidance": guidance}
            )

    check("headline", draft.headline, template.fields.headline.max_words)
    check("subheadline", draft.subheadline, template.fields.subheadline.max_words)

    for constraint in template.fields.sections:
        section = next((s for s in draft.sections if s.id == constraint.id), None)
        if section is None:
            errors.append(
                {
                    "field_id": constraint.id,
                    "actual_words": 0,
                    "limit": constraint.max_words,
                    "kind": "missing",
                    "guidance": constraint.guidance,
                }
            )
            continue
        check(constraint.id, section.text, constraint.max_words, constraint.min_words, constraint.guidance)

    check("pull_quote", draft.pull_quote, template.fields.pull_quote.max_words)
    check("cta", draft.cta, template.fields.cta.max_words)

    draft_slots = {p.slot for p in draft.image_placeholders}
    for slot in template.image_slots:
        if slot not in draft_slots:
            errors.append({"field_id": f"image_slot:{slot}", "actual_words": 0, "limit": 0, "kind": "missing_slot", "guidance": ""})

    return {"validation_errors": errors}


def should_repair(state: GenerationState) -> Literal["repair", "select_assets"]:
    if state["validation_errors"] and state["repair_attempts"] < MAX_REPAIR_ATTEMPTS:
        return "repair"
    return "select_assets"


def repair_node(state: GenerationState) -> dict:
    draft = state["draft"]
    template = state["template"]
    errors = state["validation_errors"]
    model = get_provider_agnostic_model(temperature=0.2)

    guidance_map = {s.id: s.guidance for s in template.fields.sections}
    updated_sections = {s.id: s.text for s in draft.sections}
    updated_top = {
        "headline": draft.headline,
        "subheadline": draft.subheadline,
        "pull_quote": draft.pull_quote,
        "cta": draft.cta,
    }

    for err in errors:
        field_id = err["field_id"]
        if err["kind"] in ("missing", "missing_slot"):
            continue  # not a word-limit rewrite case
        current_text = updated_sections.get(field_id, updated_top.get(field_id))
        if current_text is None:
            continue

        prompt = REPAIR_FIELD_PROMPT.format(
            field_id=field_id,
            actual_words=err["actual_words"],
            limit_kind=err["kind"],
            limit=err["limit"],
            guidance=guidance_map.get(field_id, ""),
            current_text=current_text,
        )
        result = model.invoke([{"role": "user", "content": prompt}])
        new_text = _flatten_content(result.content).strip()

        if field_id in updated_sections:
            updated_sections[field_id] = new_text
        else:
            updated_top[field_id] = new_text

    new_sections = [
        ArticleSectionDraft(id=s.id, text=updated_sections.get(s.id, s.text)) for s in draft.sections
    ]
    new_draft = draft.model_copy(
        update={
            "headline": updated_top["headline"],
            "subheadline": updated_top["subheadline"],
            "pull_quote": updated_top["pull_quote"],
            "cta": updated_top["cta"],
            "sections": new_sections,
        }
    )

    return {"draft": new_draft, "repair_attempts": state["repair_attempts"] + 1}


_SLOT_TAG_HINTS = {"hero": "logo"}


def _match_image(slot: str, alt_text: str, images: list[Image]) -> Image | None:
    if not images:
        return None

    preferred_tag = _SLOT_TAG_HINTS.get(slot)
    if preferred_tag:
        for img in images:
            if img.tag.value == preferred_tag:
                return img

    alt_words = {w.lower() for w in alt_text.split()}
    best, best_score = None, 0
    for img in images:
        desc_words = {w.lower() for w in img.description.split()}
        score = len(alt_words & desc_words)
        if score > best_score:
            best, best_score = img, score

    return best if best_score > 0 else None


def make_select_assets_node(session: Session):
    def select_assets_node(state: GenerationState) -> dict:
        template = state["template"]
        draft = state["draft"]
        error_by_field = {e["field_id"]: e for e in state["validation_errors"]}

        def finalize(field_id: str, text: str, max_words: int) -> tuple[str, bool]:
            err = error_by_field.get(field_id)
            if err and err["kind"] == "max":
                return _word_truncate(text, max_words), True
            return text, False

        headline_text, _ = finalize("headline", draft.headline, template.fields.headline.max_words)
        subheadline_text, _ = finalize("subheadline", draft.subheadline, template.fields.subheadline.max_words)
        pull_quote_text, _ = finalize("pull_quote", draft.pull_quote, template.fields.pull_quote.max_words)
        cta_text, _ = finalize("cta", draft.cta, template.fields.cta.max_words)

        final_sections = []
        for constraint in template.fields.sections:
            section = next((s for s in draft.sections if s.id == constraint.id), None)
            text = section.text if section else ""
            text, truncated = finalize(constraint.id, text, constraint.max_words)
            final_sections.append(
                GenerateSection(
                    id=constraint.id,
                    text=text,
                    word_count=_word_count(text),
                    max_words=constraint.max_words,
                    truncated=truncated,
                )
            )

        sender_images = session.exec(select(Image).where(Image.company_id == state["sender_id"])).all()
        receiver_images = session.exec(select(Image).where(Image.company_id == state["receiver_id"])).all()
        all_images = list(sender_images) + list(receiver_images)

        draft_placeholders = {p.slot: p for p in draft.image_placeholders}
        image_placeholders = []
        for slot in template.image_slots:
            placeholder_draft = draft_placeholders.get(slot)
            alt_text = placeholder_draft.alt_text if placeholder_draft else slot
            match = _match_image(slot, alt_text, all_images)
            if match:
                image_placeholders.append(
                    GenerateImagePlaceholder(slot=slot, source_hint="asset", asset_id=match.id, alt_text=alt_text)
                )
            else:
                image_placeholders.append(
                    GenerateImagePlaceholder(
                        slot=slot, source_hint="stock_query", alt_text=alt_text, stock_query=alt_text
                    )
                )

        return {
            "final_sections": final_sections,
            "final_headline": headline_text,
            "final_subheadline": subheadline_text,
            "final_pull_quote": pull_quote_text,
            "final_cta": cta_text,
            "image_placeholders": image_placeholders,
        }

    return select_assets_node


def build_generation_graph(session: Session):
    from langgraph.graph import StateGraph

    graph = StateGraph(GenerationState)
    graph.add_node("load_profiles", make_load_profiles_node(session))
    graph.add_node("build_prompt", build_prompt_node)
    graph.add_node("generate_draft", generate_draft_node)
    graph.add_node("validate", validate_node)
    graph.add_node("repair", repair_node)
    graph.add_node("select_assets", make_select_assets_node(session))

    graph.set_entry_point("load_profiles")
    graph.add_edge("load_profiles", "build_prompt")
    graph.add_edge("build_prompt", "generate_draft")
    graph.add_edge("generate_draft", "validate")
    graph.add_conditional_edges("validate", should_repair, {"repair": "repair", "select_assets": "select_assets"})
    graph.add_edge("repair", "validate")

    return graph.compile()
