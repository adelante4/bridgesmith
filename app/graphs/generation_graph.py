"""Generation LangGraph: load_profiles -> plan_research -> research -> outline
-> draft_sections -> polish -> critique -> (revise -> critique)? -> validate
-> (repair -> validate)* -> finalize_article.

STORM, trimmed to its core: plan_research derives 2-3 perspectives of people
who will judge THIS article (perspective-guided question asking) and the
questions each needs answered beyond what the ingestion-time profiles already
say; one research agent (deepagents.create_deep_agent, same idiom as
app/deep_research.py) answers them with web search. Outline-then-per-unit
drafting commits each fact to a section before any prose is written. See
spec.md §3.2 and docs/adr/ for the redesign rationale.
"""

import json
import logging
from typing import Any, Literal, TypedDict

from deepagents import FilesystemMiddleware, create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langfuse import observe
from sqlmodel import Session, select

from app.llm import (
    GENERATION_MODEL_ENV,
    GENERATION_RESEARCH_MODEL_ENV,
    get_agent_model,
    get_web_search_tool,
)
from app.context_store import all_descriptions, all_pdf_digests, latest_pdf_digest, latest_research
from app.models import Company, Description
from app.models import Image
from app.models import PdfDigest as PdfDigestRow
from app.models import ResearchRun as ResearchRunRow
from app.observability import new_trace_config
from app.prompts import (
    CRITIQUE_SYSTEM_PROMPT,
    CRITIQUE_USER_PROMPT,
    GENERATION_FACT_FINDER_SUBAGENT_PROMPT,
    GENERATION_RESEARCH_SYSTEM_PROMPT,
    GENERATION_RESEARCH_USER_PROMPT,
    NO_ASSETS_PLACEHOLDER,
    OUTLINE_SYSTEM_PROMPT,
    OUTLINE_USER_PROMPT,
    POLISH_SYSTEM_PROMPT,
    POLISH_USER_PROMPT,
    REPAIR_TURN_USER_PROMPT,
    RESEARCH_PLAN_SYSTEM_PROMPT,
    RESEARCH_PLAN_USER_PROMPT,
    REVISE_TURN_USER_PROMPT,
    SECTION_WRITER_SYSTEM_PROMPT,
    SECTION_WRITER_USER_PROMPT,
)
from app.schemas import (
    ArticleSchema,
    ArticleOutlineSchema,
    BrandGuide,
    CompressedResearchSchema,
    CritiqueSchema,
    GenerateImagePlaceholder,
    GenerateSection,
    HeadlineSubheadlineDraft,
    QuoteCtaDraft,
    ResearchPlanSchema,
    SectionTextDraft,
    StyleBundle,
    TemplateConfig,
)

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2
MAX_REVISE_ATTEMPTS = 1
MAX_RESEARCH_SEARCHES = 6  # enforced via prompt budget, not code — see GENERATION_RESEARCH_SYSTEM_PROMPT


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
    sender_name: str
    receiver_name: str
    sender_context_blob: str
    receiver_context_blob: str
    sender_style: StyleBundle
    receiver_style: StyleBundle
    sender_summary: str
    receiver_summary: str
    sender_pdf_digest_id: int | None
    receiver_pdf_digest_id: int | None
    asset_catalog: dict[str, int]  # alias (e.g. "A2") -> Image.id
    sender_assets_block: str
    research_plan: ResearchPlanSchema
    research: CompressedResearchSchema
    outline: ArticleOutlineSchema
    section_drafts: dict[str, str]  # template section id -> drafted text
    headline_draft: str
    subheadline_draft: str
    pull_quote_draft: str
    cta_draft: str
    polish_messages: list  # system+user+assistant turns; revise appends here for prompt caching
    draft: ArticleSchema
    critique: CritiqueSchema
    revise_attempts: int
    draft_messages: list  # polish/revise conversation; repair appends here for prompt caching
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


NO_CONTEXT_PLACEHOLDER = "(no context available for this company yet — no PDFs, descriptions, or research on file)"
NO_RESEARCH_SUMMARY_PLACEHOLDER = "(no web research run yet for this company — see the context above for what's on file)"


def _style_bundle(digest: PdfDigestRow | None) -> StyleBundle:
    """Deterministic selection, no LLM call: the company's newest PdfDigest
    already carries tone_signals (ingestion agent, text-based) and
    design_notes/brand colors (vision pass over pages 1-2). A company with no
    PdfDigest yet gets placeholder tone text and an empty BrandGuide — no
    fallback search through older digests (there are none to prefer over)."""
    if digest is None:
        return StyleBundle(tone_signals="(unknown — no PDF uploaded for this company yet)", design_notes="")
    return StyleBundle(
        tone_signals=digest.tone_signals or "(unknown)",
        design_notes=digest.design_notes,
        brand=BrandGuide(
            primary_color=digest.brand_primary_color,
            accent_color=digest.brand_accent_color,
            font_family=digest.brand_font_family,
        ),
    )


def _context_blob(
    digests: list[PdfDigestRow], descriptions: list[Description], research: ResearchRunRow | None
) -> str:
    """Plain concatenation of every context source on file for this company —
    no LLM synthesis. All PdfDigests and Descriptions ever added, plus the
    single newest ResearchRun (see docs/adr/0005-decouple-context-from-research.md)."""
    parts: list[str] = []
    for d in digests:
        parts.append(
            f"## PDF context ({d.document_type or 'document'}, added {d.created_at:%Y-%m-%d})\n{d.digest_text}"
        )
    for desc in descriptions:
        parts.append(f"## User-provided description (added {desc.created_at:%Y-%m-%d})\n{desc.text}")
    if research is not None:
        lines = [
            f"## Web research (added {research.created_at:%Y-%m-%d})",
            f"Offerings: {research.offerings}",
            f"Industry: {research.industry}",
        ]
        if research.target_customers:
            lines.append(f"Target customers: {research.target_customers}")
        pain_points = ", ".join(json.loads(research.pain_points))
        if pain_points:
            lines.append(f"Pain points the company faces: {pain_points}")
        for label, raw in (
            ("Differentiators", research.differentiators),
            ("Proof points", research.proof_points),
            ("Recent developments", research.recent_developments),
        ):
            items = json.loads(raw)
            if items:
                lines.append(f"{label}: " + "; ".join(items))
        lines.append(f"Summary: {research.summary}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts) if parts else NO_CONTEXT_PLACEHOLDER


def _load_company_context(session: Session, company_id: str):
    """Fetches each of the three context logs for one company exactly once,
    then derives the blob/style/summary from that single fetch (fixes the
    earlier version's duplicate per-field queries)."""
    digest = latest_pdf_digest(session, company_id)
    research = latest_research(session, company_id)
    digests = all_pdf_digests(session, company_id)
    descriptions = all_descriptions(session, company_id)

    return {
        "digest": digest,
        "blob": _context_blob(digests, descriptions, research),
        "style": _style_bundle(digest),
        "summary": research.summary if research else NO_RESEARCH_SUMMARY_PLACEHOLDER,
    }


def make_load_profiles_node(session: Session):
    @observe(name="load_profiles", capture_input=False, capture_output=False)
    def load_profiles_node(state: GenerationState) -> dict:
        sender_company = session.get(Company, state["sender_id"])
        if sender_company is None:
            raise CompanyNotFoundError(state["sender_id"], "sender")
        receiver_company = session.get(Company, state["receiver_id"])
        if receiver_company is None:
            raise CompanyNotFoundError(state["receiver_id"], "receiver")

        sender_ctx = _load_company_context(session, state["sender_id"])
        receiver_ctx = _load_company_context(session, state["receiver_id"])
        sender_digest = sender_ctx["digest"]

        # Sender-only asset catalog, scoped to the sender's newest PdfDigest.
        # Every Image row was described at ingestion time, so the catalog only
        # ever contains assets the draft model can reason about.
        sender_images: list[Image] = []
        if sender_digest is not None:
            sender_images = list(
                session.exec(
                    select(Image).where(Image.pdf_digest_id == sender_digest.id).order_by(Image.id)
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
            "sender_name": sender_company.name or state["sender_id"],
            "receiver_name": receiver_company.name or state["receiver_id"],
            "sender_context_blob": sender_ctx["blob"],
            "receiver_context_blob": receiver_ctx["blob"],
            "sender_style": sender_ctx["style"],
            "receiver_style": receiver_ctx["style"],
            "sender_summary": sender_ctx["summary"],
            "receiver_summary": receiver_ctx["summary"],
            "sender_pdf_digest_id": sender_digest.id if sender_digest else None,
            "receiver_pdf_digest_id": receiver_ctx["digest"].id if receiver_ctx["digest"] else None,
            "asset_catalog": asset_catalog,
            "sender_assets_block": sender_assets_block,
        }

    return load_profiles_node


# ---------------------------------------------------------------------------
# plan_research — STORM perspective-guided question asking: derive who will
# judge this article, and what each of them would need answered first.
# ---------------------------------------------------------------------------


@observe(name="plan_research", capture_input=False, capture_output=False)
def plan_research_node(state: GenerationState) -> dict:
    model = get_agent_model(GENERATION_MODEL_ENV).with_structured_output(ResearchPlanSchema)
    prompt = RESEARCH_PLAN_USER_PROMPT.format(
        sender_name=state["sender_name"],
        receiver_name=state["receiver_name"],
        sender_profile=state["sender_context_blob"],
        receiver_profile=state["receiver_context_blob"],
        user_prompt=state["prompt"],
    )
    plan = model.invoke(
        [
            {"role": "system", "content": RESEARCH_PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        config=new_trace_config(),
    )
    return {"research_plan": plan}


# ---------------------------------------------------------------------------
# research — one deep agent answers the perspective-guided questions
# ---------------------------------------------------------------------------


def _build_researcher():
    web_search_tool = get_web_search_tool(GENERATION_RESEARCH_MODEL_ENV)
    # Filesystem tools are pure context overhead for a search-and-summarize
    # agent; read_file is the mandatory minimum (large tool results get
    # evicted to files the model must be able to read back).
    fact_finder_subagent = {
        "name": "fact-finder",
        "description": "Delegate a focused web search on one or a few related research questions to this sub-agent.",
        "system_prompt": GENERATION_FACT_FINDER_SUBAGENT_PROMPT,
        "tools": [web_search_tool],
        "middleware": [FilesystemMiddleware(tools=["read_file"])],
    }
    return create_deep_agent(
        model=get_agent_model(GENERATION_RESEARCH_MODEL_ENV),
        tools=[web_search_tool],
        system_prompt=GENERATION_RESEARCH_SYSTEM_PROMPT,
        subagents=[fact_finder_subagent],
        middleware=[TodoListMiddleware(), FilesystemMiddleware(tools=["read_file"])],
        response_format=CompressedResearchSchema,
    )


def _questions_block(plan: ResearchPlanSchema) -> str:
    lines = []
    for perspective in plan.perspectives:
        lines.append(f"{perspective.name}:")
        lines.extend(f"- {q}" for q in perspective.questions)
    return "\n".join(lines)


@observe(name="research", capture_input=False, capture_output=False)
def research_node(state: GenerationState) -> dict:
    agent = _build_researcher()
    prompt = GENERATION_RESEARCH_USER_PROMPT.format(
        sender_name=state["sender_name"],
        receiver_name=state["receiver_name"],
        sender_profile=state["sender_context_blob"],
        receiver_profile=state["receiver_context_blob"],
        questions=_questions_block(state["research_plan"]),
    )
    result = agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config=new_trace_config())
    research = result.get("structured_response")
    if research is None:
        logger.error("Generation research agent did not return a structured_response")
        raise RuntimeError("generation research agent did not return structured research")
    return {"research": research}


def _facts_block(research: CompressedResearchSchema) -> str:
    if not research.facts:
        return "(no additional facts found)"
    return "\n".join(
        f"- {f.fact}" + (f" (source: {f.source_url})" if f.source_url else "") for f in research.facts
    )


# ---------------------------------------------------------------------------
# outline
# ---------------------------------------------------------------------------


@observe(name="outline", capture_input=False, capture_output=False)
def outline_node(state: GenerationState) -> dict:
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

    model = get_agent_model(GENERATION_MODEL_ENV).with_structured_output(ArticleOutlineSchema)
    prompt = OUTLINE_USER_PROMPT.format(
        sender_profile=state["sender_context_blob"],
        receiver_profile=state["receiver_context_blob"],
        research=_facts_block(state["research"]),
        user_prompt=state["prompt"],
        template_constraints="\n".join(lines),
    )
    outline = model.invoke(
        [
            {"role": "system", "content": OUTLINE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        config=new_trace_config(),
    )
    return {"outline": outline}


# ---------------------------------------------------------------------------
# draft_sections — one call per template unit (STORM-style section drafting)
# ---------------------------------------------------------------------------


def _write_unit(unit_label: str, word_limit: int, guidance: str, angle: str, fact_refs: list[str], state: GenerationState) -> str:
    schema = SectionTextDraft
    model = get_agent_model(GENERATION_MODEL_ENV, temperature=0.4).with_structured_output(schema)
    prompt = SECTION_WRITER_USER_PROMPT.format(
        unit_label=unit_label,
        word_limit=word_limit,
        guidance=guidance,
        angle=angle,
        facts="\n".join(f"- {f}" for f in fact_refs) if fact_refs else "(none assigned — write from the profiles only)",
        sender_summary=state["sender_summary"],
        receiver_summary=state["receiver_summary"],
        receiver_tone=state["receiver_style"].tone_signals,
    )
    result = model.invoke(
        [
            {"role": "system", "content": SECTION_WRITER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        config=new_trace_config(),
    )
    return result.text


@observe(name="draft_sections", capture_input=False, capture_output=False)
def draft_sections_node(state: GenerationState) -> dict:
    template = state["template"]
    fields = template.fields
    outline = state["outline"]

    hs_model = get_agent_model(GENERATION_MODEL_ENV, temperature=0.4).with_structured_output(
        HeadlineSubheadlineDraft
    )
    hs_prompt = SECTION_WRITER_USER_PROMPT.format(
        unit_label="headline + subheadline",
        word_limit=f"headline max {fields.headline.max_words}, subheadline max {fields.subheadline.max_words}",
        guidance="Headline + subheadline together introduce the article's hook.",
        angle=f"Headline angle: {outline.headline_angle}\nSubheadline angle: {outline.subheadline_angle}",
        facts="(headline/subheadline draw on the article's overall angle, not a specific fact list)",
        sender_summary=state["sender_summary"],
        receiver_summary=state["receiver_summary"],
        receiver_tone=state["receiver_style"].tone_signals,
    )
    headline_subheadline = hs_model.invoke(
        [
            {"role": "system", "content": SECTION_WRITER_SYSTEM_PROMPT},
            {"role": "user", "content": hs_prompt},
        ],
        config=new_trace_config(),
    )

    outline_by_id = {s.id: s for s in outline.sections}
    section_drafts: dict[str, str] = {}
    for constraint in fields.sections:
        plan = outline_by_id.get(constraint.id)
        section_drafts[constraint.id] = _write_unit(
            unit_label=f"section '{constraint.id}'",
            word_limit=constraint.max_words,
            guidance=constraint.guidance,
            angle=plan.angle if plan else "(no outline entry — write to the section guidance)",
            fact_refs=plan.fact_refs if plan else [],
            state=state,
        )

    qc_model = get_agent_model(GENERATION_MODEL_ENV, temperature=0.4).with_structured_output(QuoteCtaDraft)
    qc_prompt = SECTION_WRITER_USER_PROMPT.format(
        unit_label="pull quote + CTA",
        word_limit=f"pull_quote max {fields.pull_quote.max_words}, cta max {fields.cta.max_words}",
        guidance="Pull quote is a short standalone highlight; CTA drives the reader to act.",
        angle=f"Pull quote angle: {outline.pull_quote_angle}\nCTA angle: {outline.cta_angle}",
        facts="(draw on the drafted sections' claims, not new facts)",
        sender_summary=state["sender_summary"],
        receiver_summary=state["receiver_summary"],
        receiver_tone=state["receiver_style"].tone_signals,
    )
    quote_cta = qc_model.invoke(
        [
            {"role": "system", "content": SECTION_WRITER_SYSTEM_PROMPT},
            {"role": "user", "content": qc_prompt},
        ],
        config=new_trace_config(),
    )

    return {
        "headline_draft": headline_subheadline.headline,
        "subheadline_draft": headline_subheadline.subheadline,
        "section_drafts": section_drafts,
        "pull_quote_draft": quote_cta.pull_quote,
        "cta_draft": quote_cta.cta,
    }


# ---------------------------------------------------------------------------
# polish — assembles drafted pieces into the final ArticleSchema
# ---------------------------------------------------------------------------


def _polish_model():
    return get_agent_model(GENERATION_MODEL_ENV, temperature=0.2).with_structured_output(
        ArticleSchema, include_raw=True
    )


def _invoke_polish(messages: list) -> tuple[ArticleSchema, Any]:
    result = _polish_model().invoke(messages, config=new_trace_config())
    if result.get("parsing_error") or result.get("parsed") is None:
        raise RuntimeError(f"polish structured output failed to parse: {result.get('parsing_error')}")
    return result["parsed"], result["raw"]


@observe(name="polish", capture_input=False, capture_output=False)
def polish_node(state: GenerationState) -> dict:
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

    sections_block = "\n".join(f"[{sid}] {text}" for sid, text in state["section_drafts"].items())
    all_facts = _facts_block(state["research"])

    user_prompt = POLISH_USER_PROMPT.format(
        headline=state["headline_draft"],
        subheadline=state["subheadline_draft"],
        sections=sections_block,
        pull_quote=state["pull_quote_draft"],
        cta=state["cta_draft"],
        template_constraints="\n".join(lines),
        image_slots=", ".join(template.image_slots),
        sender_assets=state["sender_assets_block"],
        all_facts=all_facts,
    )
    messages = [
        {"role": "system", "content": POLISH_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    draft, raw = _invoke_polish(messages)
    return {"draft": draft, "polish_messages": messages + [raw], "revise_attempts": 0}


# ---------------------------------------------------------------------------
# critique / revise
# ---------------------------------------------------------------------------


@observe(name="critique", capture_input=False, capture_output=False)
def critique_node(state: GenerationState) -> dict:
    draft = state["draft"]
    article_text = (
        f"Headline: {draft.headline}\nSubheadline: {draft.subheadline}\n"
        + "\n".join(f"[{s.id}] {s.text}" for s in draft.sections)
        + f"\nPull quote: {draft.pull_quote}\nCTA: {draft.cta}"
    )
    model = get_agent_model(GENERATION_MODEL_ENV).with_structured_output(CritiqueSchema)
    prompt = CRITIQUE_USER_PROMPT.format(
        article=article_text,
        research=_facts_block(state["research"]),
    )
    critique = model.invoke(
        [
            {"role": "system", "content": CRITIQUE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        config=new_trace_config(),
    )
    return {"critique": critique}


def should_revise(state: GenerationState) -> Literal["revise", "validate"]:
    critique = state["critique"]
    if critique.required_edits and state["revise_attempts"] < MAX_REVISE_ATTEMPTS:
        return "revise"
    return "validate"


@observe(name="revise", capture_input=False, capture_output=False)
def revise_node(state: GenerationState) -> dict:
    required_edits = "\n".join(f"- {e}" for e in state["critique"].required_edits)
    messages = state["polish_messages"] + [
        {"role": "user", "content": REVISE_TURN_USER_PROMPT.format(required_edits=required_edits)}
    ]
    draft, raw = _invoke_polish(messages)
    return {
        "draft": draft,
        "polish_messages": messages + [raw],
        "revise_attempts": state["revise_attempts"] + 1,
    }


# ---------------------------------------------------------------------------
# validate / repair / finalize (unchanged word-limit machinery)
# ---------------------------------------------------------------------------


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

    return {"validation_errors": errors, "draft_messages": state["polish_messages"]}


def should_repair(state: GenerationState) -> Literal["repair", "finalize_article"]:
    if state["validation_errors"] and state.get("repair_attempts", 0) < MAX_REPAIR_ATTEMPTS:
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
    # Extending the same conversation keeps the (large) polish prompt a stable
    # prefix, so the model provider's prompt cache pays for each repair round.
    violations = "\n".join(_violation_line(e) for e in state["validation_errors"])
    messages = state["draft_messages"] + [
        {"role": "user", "content": REPAIR_TURN_USER_PROMPT.format(violations=violations)}
    ]
    draft, raw = _invoke_polish(messages)
    return {
        "draft": draft,
        "draft_messages": messages + [raw],
        "repair_attempts": state.get("repair_attempts", 0) + 1,
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
    graph.add_node("plan_research", plan_research_node)
    graph.add_node("research", research_node)
    graph.add_node("outline", outline_node)
    graph.add_node("draft_sections", draft_sections_node)
    graph.add_node("polish", polish_node)
    graph.add_node("critique", critique_node)
    graph.add_node("revise", revise_node)
    graph.add_node("validate", validate_node)
    graph.add_node("repair", repair_node)
    graph.add_node("finalize_article", make_finalize_article_node(session))

    graph.set_entry_point("load_profiles")
    graph.add_edge("load_profiles", "plan_research")
    graph.add_edge("plan_research", "research")
    graph.add_edge("research", "outline")
    graph.add_edge("outline", "draft_sections")
    graph.add_edge("draft_sections", "polish")
    graph.add_edge("polish", "critique")
    graph.add_conditional_edges("critique", should_revise, {"revise": "revise", "validate": "validate"})
    graph.add_edge("revise", "critique")
    graph.add_conditional_edges(
        "validate", should_repair, {"repair": "repair", "finalize_article": "finalize_article"}
    )
    graph.add_edge("repair", "validate")

    return graph.compile()
