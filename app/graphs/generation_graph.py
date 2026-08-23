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

Two invariants keep the prose from collapsing into hedged, generic filler:

1. Only `evidence` facts reach a writer. The researcher also records `caveat`
   facts (things it could not verify), and those go to the outline as
   judgement context and to the critic as an accuracy check — never into a
   "write only from these facts" prompt. See CompressedResearchSchema.
2. Anything that comments on the article is written after it. draft_sections
   writes the body first, then hands the finished body to the headline and
   pull-quote/CTA writers. Writing them first, off an angle with facts
   withheld, can only produce a paraphrase of that angle.

The revise loop is bounded by rubric score and blocking-severity edits, not by
"the critic still has an opinion" — see should_revise and revise_node.
"""

import json
import logging
import re
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
    FINISHED_BODY_USER_PROMPT,
    HEADLINE_WRITER_SYSTEM_PROMPT,
    QUOTE_CTA_WRITER_SYSTEM_PROMPT,
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
# Repeated self-critique has diminishing and then negative returns: past the
# first pass the critic mostly softens already-correct text rather than fixing
# it. The score floor below, not this cap, is meant to be what stops the loop.
MAX_REVISE_ATTEMPTS = 2
REVISE_SCORE_FLOOR = 0.8
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
    sender_brief: str
    receiver_brief: str
    asset_eligibility: dict[str, list[str]]  # alias -> slots this asset may fill
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
    polish_base_messages: list  # the original polish turn; every revise branches from this, never from the last revision
    polish_messages: list  # most recent polish/revise conversation, handed to the repair loop
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


# A concrete anchor is a digit run (a number, a year, a percentage) or a
# capitalised token that isn't just sentence-initial — a company, product, or
# customer name. Cheap and deterministic on purpose: it runs in validate
# alongside the word-limit checks, with no extra model call, and it is the only
# check that actually forces researched specifics to survive into the prose.
_ANCHOR_NUMBER_RE = re.compile(r"\d")
_ANCHOR_NAME_RE = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][A-Za-z0-9&./-]{1,}\b")

# Words that mark generated filler. Not fatal on their own, but a section built
# out of them and nothing else is the failure mode this whole pass exists to
# stop, so they are reported to the repair turn.
_FILLER_WORDS = frozenset(
    """leverage leveraging unlock unlocking seamless seamlessly streamline streamlining empower
    empowering transformative robust cutting-edge harness harnessing unprecedented landscape elevate
    holistic synergy turnkey best-in-class""".split()
)

_HEDGE_WORDS = frozenset("reported reportedly could would may proposed purportedly".split())


def _concrete_anchors(text: str) -> int:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    names = sum(len(_ANCHOR_NAME_RE.findall(s[1:])) for s in sentences if s)
    return names + len(_ANCHOR_NUMBER_RE.findall(text))


def _filler_hits(text: str) -> list[str]:
    words = {w.strip(".,;:!?\"'").lower() for w in text.split()}
    return sorted(words & _FILLER_WORDS)


def _hedge_hits(text: str) -> list[str]:
    words = {w.strip(".,;:!?\"'").lower() for w in text.split()}
    return sorted(words & _HEDGE_WORDS)


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


NO_BRIEF_PLACEHOLDER = "(no web research on file for this company — write from the profile context only)"


def _sender_brief(research: ResearchRunRow | None) -> str:
    """What the writer needs to sell this company: what it offers, what sets it
    apart, what it has actually proven.

    Deliberately NOT ResearchRun.summary. That field is an analyst's synthesis
    and carries the company's own risks and source discrepancies ("competition
    from much larger consultancies", "the deck reports 100+ experts while the
    announcement cited 140+"). Handing that to a copywriter as its brief on its
    own client teaches it to distrust the thing it is selling."""
    if research is None:
        return NO_BRIEF_PLACEHOLDER
    lines = [f"Offers: {research.offerings}"]
    for label, raw in (("Sets it apart", research.differentiators), ("Proven results", research.proof_points)):
        items = json.loads(raw)
        if items:
            lines.append(f"{label}:")
            lines.extend(f"  - {i}" for i in items)
    return "\n".join(lines)


def _receiver_brief(research: ResearchRunRow | None) -> str:
    """What the writer needs to speak to: the reader's world and the pressures
    the article can credibly address."""
    if research is None:
        return NO_BRIEF_PLACEHOLDER
    lines = [f"Industry: {research.industry}"]
    if research.target_customers:
        lines.append(f"Sells to: {research.target_customers}")
    for label, raw in (
        ("Under pressure to solve", research.pain_points),
        ("Recent moves", research.recent_developments),
    ):
        items = json.loads(raw)
        if items:
            lines.append(f"{label}:")
            lines.extend(f"  - {i}" for i in items)
    return "\n".join(lines)


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
        "sender_brief": _sender_brief(research),
        "receiver_brief": _receiver_brief(research),
    }


HERO_SLOT = "hero"

# Which image slots each asset tag may fill. A brand mark reads fine as a small
# cover mark; it does not read as the photograph a large content slot expects.
# 'generic' covers decorative shapes and arrows ("a decorative graphic showing
# two opposing directional arrows") — never a content image anywhere.
_TAG_ELIGIBILITY = {
    "logo": {HERO_SLOT},
    "product_image": None,  # None = every slot
    "chart": None,
    "generic": set(),
}


def _eligible_slots(image: Image, template_slots: list[str]) -> list[str]:
    allowed = _TAG_ELIGIBILITY.get(image.tag.value, set())
    if allowed is None:
        return list(template_slots)
    return [s for s in template_slots if s in allowed]


def make_load_profiles_node(session: Session):
    @observe(name="load_profiles", capture_input=False, capture_output=False)
    def load_profiles_node(state: GenerationState) -> dict:
        template = state["template"]
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
        asset_eligibility = {
            alias: _eligible_slots(img, template.image_slots)
            for alias, img in zip(asset_catalog, sender_images)
        }
        listable = [
            (alias, img) for alias, img in zip(asset_catalog, sender_images) if asset_eligibility[alias]
        ]
        if listable:
            sender_assets_block = "\n".join(
                f"{alias}: [{img.tag.value}] {img.description} "
                f"(eligible for: {', '.join(asset_eligibility[alias])})"
                for alias, img in listable
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
            "sender_brief": sender_ctx["sender_brief"],
            "receiver_brief": receiver_ctx["receiver_brief"],
            "asset_eligibility": asset_eligibility,
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


def _render_facts(facts: list, empty: str) -> str:
    if not facts:
        return empty
    return "\n".join(f"- {f.fact}" + (f" (source: {f.source_url})" if f.source_url else "") for f in facts)


def _evidence_block(research: CompressedResearchSchema) -> str:
    """Positive, usable findings — the only research a writer ever sees."""
    return _render_facts(research.evidence(), "(no additional evidence found)")


def _caveats_block(research: CompressedResearchSchema) -> str:
    """Negative/limiting findings. Routed to the outline (as judgement context)
    and the critic (as an accuracy check) — never to a writer. Handing these to
    a copywriter under a "write only from these facts" instruction is what
    produced the hedged prose; see CompressedResearchSchema's docstring."""
    return _render_facts(research.caveats(), "(none recorded)")


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
        research=_evidence_block(state["research"]),
        caveats=_caveats_block(state["research"]),
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
        facts="\n".join(f"- {f}" for f in fact_refs) if fact_refs else "(none assigned — write from the briefs only)",
        sender_name=state["sender_name"],
        receiver_name=state["receiver_name"],
        sender_brief=state["sender_brief"],
        receiver_brief=state["receiver_brief"],
        # The Sender is the one speaking, so the article carries the Sender's
        # voice. This used to pass the Receiver's tone_signals, which stripped
        # the client's own voice out of its own brochure and replaced it with
        # the prospect's (typically far more formal) house style.
        sender_tone=state["sender_style"].tone_signals,
    )
    result = model.invoke(
        [
            {"role": "system", "content": SECTION_WRITER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        config=new_trace_config(),
    )
    return result.text


def _body_text(fields, section_drafts: dict[str, str]) -> str:
    """The drafted body as the headline/quote/CTA writers see it."""
    return "\n\n".join(
        f"[{c.id}] {section_drafts[c.id]}" for c in fields.sections if c.id in section_drafts
    )


@observe(name="draft_sections", capture_input=False, capture_output=False)
def draft_sections_node(state: GenerationState) -> dict:
    """Body first, then everything that comments on the body.

    The headline, pull quote, and CTA used to be written before (or blind to)
    the sections, from the outline's angle alone and with facts explicitly
    withheld. A writer handed only an abstraction can do nothing but rephrase
    it, which is exactly what came out. They now read the finished body."""
    template = state["template"]
    fields = template.fields
    outline = state["outline"]
    evidence = _evidence_block(state["research"])

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

    body = _body_text(fields, section_drafts)

    def from_body(system_prompt: str, schema, word_limit: str, angle: str):
        model = get_agent_model(GENERATION_MODEL_ENV, temperature=0.4).with_structured_output(schema)
        return model.invoke(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": FINISHED_BODY_USER_PROMPT.format(
                        body=body,
                        sender_name=state["sender_name"],
                        receiver_name=state["receiver_name"],
                        sender_tone=state["sender_style"].tone_signals,
                        facts=evidence,
                        word_limit=word_limit,
                        angle=angle,
                    ),
                },
            ],
            config=new_trace_config(),
        )

    headline_subheadline = from_body(
        HEADLINE_WRITER_SYSTEM_PROMPT,
        HeadlineSubheadlineDraft,
        f"headline max {fields.headline.max_words} words, subheadline max {fields.subheadline.max_words} words",
        f"Headline angle: {outline.headline_angle}\nSubheadline angle: {outline.subheadline_angle}",
    )
    quote_cta = from_body(
        QUOTE_CTA_WRITER_SYSTEM_PROMPT,
        QuoteCtaDraft,
        f"pull_quote max {fields.pull_quote.max_words} words, cta max {fields.cta.max_words} words",
        f"Pull quote angle: {outline.pull_quote_angle}\nCTA angle: {outline.cta_angle}",
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
    all_facts = _evidence_block(state["research"])

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
    return {
        "draft": draft,
        "polish_base_messages": messages,
        "polish_messages": messages + [raw],
        "revise_attempts": 0,
    }


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
        research=_evidence_block(state["research"]),
        caveats=_caveats_block(state["research"]),
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
    """Two independent stop conditions, both required to keep going.

    Gating on "the critic still has something to say" never terminates: asked
    to review marketing copy against a fact list, a critic will always find one
    more thing worth qualifying, so the loop ran to its attempt cap every time
    and the article got a little more hedged on each pass. Advisory edits no
    longer count, and a rubric that is already good enough stops the loop
    regardless of what else the critic listed."""
    critique = state["critique"]
    if state["revise_attempts"] >= MAX_REVISE_ATTEMPTS:
        return "validate"
    if critique.min_score() >= REVISE_SCORE_FLOOR:
        return "validate"
    if not critique.blocking_edits():
        return "validate"
    return "revise"


@observe(name="revise", capture_input=False, capture_output=False)
def revise_node(state: GenerationState) -> dict:
    """Each pass branches from the ORIGINAL polish turn, not from the previous
    revision's conversation.

    The previous version appended every round's edit list to one growing
    conversation, so round N was still being told to apply rounds 1..N-1
    verbatim. Since most of those edits were "qualify this claim", the
    qualifiers compounded on every pass — one 60-word paragraph came out of a
    four-round loop carrying three separate "Acme-reported" hedges. Keeping the
    base turn fixed also keeps it a stable prompt-cache prefix."""
    required_edits = "\n".join(f"- {e}" for e in state["critique"].blocking_edits())
    messages = state["polish_base_messages"] + [
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
    eligibility = state.get("asset_eligibility", {})
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

        if _concrete_anchors(section.text) == 0:
            errors.append(
                {
                    "field_id": constraint.id,
                    "actual_words": _word_count(section.text),
                    "limit": constraint.max_words,
                    "kind": "no_anchor",
                    "guidance": constraint.guidance,
                }
            )
        filler = _filler_hits(section.text)
        if filler:
            errors.append(
                {
                    "field_id": constraint.id,
                    "actual_words": 0,
                    "limit": 0,
                    "kind": "filler",
                    "guidance": "",
                    "words": filler,
                }
            )
        hedges = _hedge_hits(section.text)
        if hedges:
            errors.append(
                {
                    "field_id": constraint.id,
                    "actual_words": 0,
                    "limit": 0,
                    "kind": "hedge",
                    "guidance": "",
                    "words": hedges,
                }
            )

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
        elif placeholder.asset_alias is not None and slot not in eligibility.get(placeholder.asset_alias, []):
            errors.append(
                {
                    "field_id": f"image_slot:{slot}",
                    "actual_words": 0,
                    "limit": 0,
                    "kind": "ineligible_asset",
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
    if kind == "no_anchor":
        return (
            f"- '{field_id}' contains no concrete anchor — no number, date, or named company, product, or "
            f"customer. Rewrite it around a specific fact from the research instead of describing the "
            f"situation in general terms. Stay within {err['limit']} words.{guidance}"
        )
    if kind == "filler":
        return (
            f"- '{field_id}' uses filler that carries no information: {', '.join(err['words'])}. "
            f"Replace each with the specific thing it is standing in for, or cut the sentence."
        )
    if kind == "hedge":
        return (
            f"- '{field_id}' hedges a supported claim with: {', '.join(err['words'])}. Every fact in this "
            f"article is already verified. State the claim plainly, or drop it and use a different fact — "
            f"do not keep it and qualify it."
        )
    if kind == "missing_slot":
        slot = field_id.removeprefix("image_slot:")
        return f"- image_placeholders has no entry for slot '{slot}'; add one."
    if kind == "invalid_asset":
        slot = field_id.removeprefix("image_slot:")
        return (
            f"- image_placeholders entry for slot '{slot}' uses asset_alias '{err['alias']}', which is not in "
            f"the sender asset catalog; pick a listed alias or set it to null."
        )
    if kind == "ineligible_asset":
        slot = field_id.removeprefix("image_slot:")
        return (
            f"- image_placeholders entry for slot '{slot}' uses asset_alias '{err['alias']}', which is not "
            f"eligible for that slot (check the catalog's 'eligible for' list). Pick an eligible alias or set "
            f"it to null — null is fine and the slot will be filled another way."
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
        eligibility = state.get("asset_eligibility", {})
        # Only over-length errors drive truncation, and a field can now carry
        # several errors at once (over-length AND no anchor AND filler), so key
        # on the "max" ones specifically rather than letting whichever error
        # happens to be last win.
        overlong_fields = {e["field_id"] for e in state["validation_errors"] if e["kind"] == "max"}

        def finalize(field_id: str, text: str, max_words: int) -> tuple[str, bool]:
            if field_id in overlong_fields:
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
            if alias is not None and slot not in eligibility.get(alias, []):
                logger.warning(
                    "slot '%s': asset_alias '%s' is not eligible for this slot; falling back to stock query",
                    slot,
                    alias,
                )
                alias = None
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
