"""Generation LangGraph: load_profiles -> build_prompt -> generate_draft -> validate
-> (repair -> validate)* -> finalize_article. See spec.md §3.2.
"""

import json
import logging
from typing import Any, Literal, TypedDict

from langfuse import observe
from sqlmodel import Session, select

from app.llm import GENERATION_MODEL_ENV, get_agent_model
from app.models import CompanyProfile as CompanyProfileRow
from app.models import Image
from app.observability import new_trace_config
from app.prompts import (
    GENERATE_DRAFT_SYSTEM_PROMPT,
    GENERATE_DRAFT_USER_PROMPT,
    NO_ASSETS_PLACEHOLDER,
    REPAIR_TURN_USER_PROMPT,
)
from app.schemas import (
    ArticleSchema,
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
    sender_profile_id: int
    receiver_profile_id: int
    sender_pdf_digest_id: int
    receiver_pdf_digest_id: int
    asset_catalog: dict[str, int]  # alias (e.g. "A2") -> Image.id
    sender_assets_block: str
    generate_user_prompt: str
    draft_messages: list  # system+user+assistant turns; repairs append here for prompt caching
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


def _profile_row_to_schema(row: CompanyProfileRow) -> CompanyProfileSchema:
    return CompanyProfileSchema(
        offerings=row.offerings,
        industry=row.industry,
        pain_points=json.loads(row.pain_points),
        tone_signals=row.tone_signals,
        summary=row.summary,
        web_sources=[WebSource(**s) for s in json.loads(row.web_sources)],
    )


def _latest_profile(session: Session, company_id: str) -> CompanyProfileRow | None:
    return session.exec(
        select(CompanyProfileRow)
        .where(CompanyProfileRow.company_id == company_id)
        .order_by(CompanyProfileRow.id.desc())
    ).first()


def make_load_profiles_node(session: Session):
    @observe(name="load_profiles", capture_input=False, capture_output=False)
    def load_profiles_node(state: GenerationState) -> dict:
        sender_row = _latest_profile(session, state["sender_id"])
        if sender_row is None:
            raise CompanyNotFoundError(state["sender_id"], "sender")
        receiver_row = _latest_profile(session, state["receiver_id"])
        if receiver_row is None:
            raise CompanyNotFoundError(state["receiver_id"], "receiver")

        # Sender-only asset catalog, scoped to the sender's latest ingestion run
        # (matching the CompanyProfile just loaded — see docs/adr/0001-...).
        # Every Image row was described at ingestion time, so the catalog only
        # ever contains assets the draft model can reason about.
        sender_images: list[Image] = []
        if sender_row.pdf_digest_id is not None:
            sender_images = list(
                session.exec(
                    select(Image)
                    .where(Image.pdf_digest_id == sender_row.pdf_digest_id)
                    .order_by(Image.id)
                ).all()
            )

        asset_catalog = {f"A{i}": img.id for i, img in enumerate(sender_images, start=1)}
        if sender_images:
            sender_assets_block = "\n".join(
                f"{alias}: [{img.tag.value}] {img.description}"
                for alias, img in zip(asset_catalog, sender_images)
            )
        else:
            sender_assets_block = NO_ASSETS_PLACEHOLDER

        return {
            "sender_profile": _profile_row_to_schema(sender_row),
            "receiver_profile": _profile_row_to_schema(receiver_row),
            "sender_profile_id": sender_row.id,
            "receiver_profile_id": receiver_row.id,
            "sender_pdf_digest_id": sender_row.pdf_digest_id,
            "receiver_pdf_digest_id": receiver_row.pdf_digest_id,
            "asset_catalog": asset_catalog,
            "sender_assets_block": sender_assets_block,
        }

    return load_profiles_node


@observe(name="build_prompt", capture_input=False, capture_output=False)
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

    # web_sources stay out of the prompt: the article can't cite URLs, and any
    # fact worth using must already live in the profile's summary/offerings.
    user_prompt = GENERATE_DRAFT_USER_PROMPT.format(
        sender_profile=state["sender_profile"].model_dump_json(exclude={"web_sources"}),
        receiver_profile=state["receiver_profile"].model_dump_json(exclude={"web_sources"}),
        user_prompt=state["prompt"],
        template_constraints="\n".join(lines),
        image_slots=", ".join(template.image_slots),
        sender_assets=state["sender_assets_block"],
    )
    return {"generate_user_prompt": user_prompt}


def _draft_model():
    # Fresh, clean model instance — deliberately no tools bound (see spec §3.2).
    # include_raw keeps the assistant message so repair turns can extend the
    # same conversation (stable prefix -> OpenAI prompt cache hits).
    return get_agent_model(GENERATION_MODEL_ENV, temperature=0.4).with_structured_output(
        ArticleSchema, include_raw=True
    )


def _invoke_draft(messages: list) -> tuple[ArticleSchema, Any]:
    result = _draft_model().invoke(messages, config=new_trace_config())
    if result.get("parsing_error") or result.get("parsed") is None:
        raise RuntimeError(f"draft structured output failed to parse: {result.get('parsing_error')}")
    return result["parsed"], result["raw"]


@observe(name="generate_draft", capture_input=False, capture_output=False)
def generate_draft_node(state: GenerationState) -> dict:
    # A fresh callback handler nests under whatever Langfuse span is current
    # (this node's @observe span) via OTEL context — no manual config threading.
    messages = [
        {"role": "system", "content": GENERATE_DRAFT_SYSTEM_PROMPT},
        {"role": "user", "content": state["generate_user_prompt"]},
    ]
    draft, raw = _invoke_draft(messages)
    return {"draft": draft, "draft_messages": messages + [raw], "repair_attempts": 0}


@observe(name="validate", capture_input=False, capture_output=False)
def validate_node(state: GenerationState) -> dict:
    template = state["template"]
    draft = state["draft"]
    catalog = state["asset_catalog"]
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

    draft_placeholders = {p.slot: p for p in draft.image_placeholders}
    for slot in template.image_slots:
        placeholder = draft_placeholders.get(slot)
        if placeholder is None:
            errors.append(
                {"field_id": f"image_slot:{slot}", "actual_words": 0, "limit": 0, "kind": "missing_slot", "guidance": ""}
            )
        elif placeholder.asset_alias is not None and placeholder.asset_alias not in catalog:
            errors.append(
                {
                    "field_id": f"image_slot:{slot}",
                    "actual_words": 0,
                    "limit": 0,
                    "kind": "invalid_asset",
                    "guidance": "",
                    "alias": placeholder.asset_alias,
                }
            )

    return {"validation_errors": errors}


def should_repair(state: GenerationState) -> Literal["repair", "finalize_article"]:
    if state["validation_errors"] and state["repair_attempts"] < MAX_REPAIR_ATTEMPTS:
        return "repair"
    return "finalize_article"


def _violation_line(err: dict) -> str:
    field_id = err["field_id"]
    kind = err["kind"]
    guidance = f" Guidance: {err['guidance']}" if err.get("guidance") else ""
    if kind == "max":
        return f"- '{field_id}' is {err['actual_words']} words; the maximum is {err['limit']}.{guidance}"
    if kind == "min":
        return f"- '{field_id}' is {err['actual_words']} words; the minimum is {err['limit']}.{guidance}"
    if kind == "missing":
        return f"- section '{field_id}' is missing entirely; write it (max {err['limit']} words).{guidance}"
    if kind == "missing_slot":
        slot = field_id.removeprefix("image_slot:")
        return f"- image_placeholders has no entry for slot '{slot}'; add one."
    if kind == "invalid_asset":
        slot = field_id.removeprefix("image_slot:")
        return (
            f"- image_placeholders entry for slot '{slot}' uses asset_alias '{err['alias']}', which is not in "
            f"the sender asset catalog; pick a listed alias or set it to null."
        )
    return f"- '{field_id}': {kind}"


@observe(name="repair", capture_input=False, capture_output=False)
def repair_node(state: GenerationState) -> dict:
    # One appended turn per repair iteration, batching every current violation.
    # Extending the same conversation keeps the (large) profile prompt a stable
    # prefix, so OpenAI's prompt cache pays for each repair round.
    violations = "\n".join(_violation_line(e) for e in state["validation_errors"])
    messages = state["draft_messages"] + [
        {"role": "user", "content": REPAIR_TURN_USER_PROMPT.format(violations=violations)}
    ]
    draft, raw = _invoke_draft(messages)
    return {
        "draft": draft,
        "draft_messages": messages + [raw],
        "repair_attempts": state["repair_attempts"] + 1,
    }


def make_finalize_article_node(session: Session):
    @observe(name="finalize_article", capture_input=False, capture_output=False)
    def finalize_article_node(state: GenerationState) -> dict:
        template = state["template"]
        draft = state["draft"]
        catalog = state["asset_catalog"]
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

        # The draft model picked asset aliases itself (from the sender-only
        # catalog in its prompt); here we only resolve alias -> Image.id and
        # fall back to a stock query when it picked none, the repair loop
        # couldn't fix an invalid alias, or the slot never got an entry.
        draft_placeholders = {p.slot: p for p in draft.image_placeholders}
        image_placeholders = []
        for slot in template.image_slots:
            placeholder_draft = draft_placeholders.get(slot)
            alt_text = placeholder_draft.alt_text if placeholder_draft else slot
            alias = placeholder_draft.asset_alias if placeholder_draft else None
            if alias is not None and alias in catalog:
                image_placeholders.append(
                    GenerateImagePlaceholder(slot=slot, source_hint="asset", asset_id=catalog[alias], alt_text=alt_text)
                )
            else:
                if alias is not None:
                    logger.warning(
                        "slot '%s': asset_alias '%s' still invalid after repairs; falling back to stock query",
                        slot,
                        alias,
                    )
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

    return finalize_article_node


def build_generation_graph(session: Session):
    from langgraph.graph import StateGraph

    graph = StateGraph(GenerationState)
    graph.add_node("load_profiles", make_load_profiles_node(session))
    graph.add_node("build_prompt", build_prompt_node)
    graph.add_node("generate_draft", generate_draft_node)
    graph.add_node("validate", validate_node)
    graph.add_node("repair", repair_node)
    graph.add_node("finalize_article", make_finalize_article_node(session))

    graph.set_entry_point("load_profiles")
    graph.add_edge("load_profiles", "build_prompt")
    graph.add_edge("build_prompt", "generate_draft")
    graph.add_edge("generate_draft", "validate")
    graph.add_conditional_edges(
        "validate", should_repair, {"repair": "repair", "finalize_article": "finalize_article"}
    )
    graph.add_edge("repair", "validate")

    return graph.compile()
