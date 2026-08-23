"""Pydantic v2 schemas — single source of truth shared between LangGraph nodes,
LLM structured-output targets, and FastAPI request/response models."""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Template config schemas (mirror config/templates/*.json)
# ---------------------------------------------------------------------------


class SectionConstraint(BaseModel):
    id: str = Field(description="Section identifier, e.g. 'body_intro'")
    min_words: Optional[int] = Field(default=None, description="Minimum word count, if any")
    max_words: int = Field(description="Maximum word count allowed for this section")
    guidance: str = Field(description="Writing guidance for this section")


class FieldWordLimit(BaseModel):
    max_words: int = Field(description="Maximum word count allowed")


class FieldConstraints(BaseModel):
    headline: FieldWordLimit
    subheadline: FieldWordLimit
    sections: list[SectionConstraint]
    pull_quote: FieldWordLimit
    cta: FieldWordLimit


class ThemeColors(BaseModel):
    primary_color: str
    accent_color: str
    font_family: Optional[str] = Field(
        default=None, description="Font family name, e.g. 'Poppins'; null falls back to a system sans-serif stack"
    )


class TemplateConfig(BaseModel):
    template_id: str
    fields: FieldConstraints
    image_slots: list[str]
    theme: ThemeColors


# ---------------------------------------------------------------------------
# Ingestion agent structured-output schemas
# ---------------------------------------------------------------------------


class PdfDigestSchema(BaseModel):
    """Argument schema for the ingestion agent's `submit_digest` tool."""

    digest_text: str = Field(
        description="Comprehensive prose summary merging document text and described image content"
    )
    key_facts: list[str] = Field(description="List of short factual bullets extracted from the document")
    document_type: str = Field(
        description="Kind of document, e.g. 'company one-pager', 'product brochure'"
    )


class ImageDescription(BaseModel):
    """Structured output of the describe_image vision subagent."""

    image_type: Literal["logo", "product_screenshot", "chart", "diagram", "photo", "other"] = Field(
        description="Classification of the image"
    )
    visible_text: str = Field(default="", description="Any text or numbers visible in the image, or empty")
    summary: str = Field(description="One to two sentence summary of relevance to a company profile")


# ---------------------------------------------------------------------------
# Company profiling structured-output schema
# ---------------------------------------------------------------------------


class WebSource(BaseModel):
    url: str = Field(description="URL cited during web search")
    note: str = Field(description="Short note on what this source supports/confirms")


class BrandGuide(BaseModel):
    """Best-effort brand signals gathered alongside the company profile. Any
    field may be null when no clear signal was found — callers must fall back
    to a template's own defaults rather than treat a null as an error."""

    primary_color: Optional[str] = Field(
        default=None, description="Company's primary brand hex color (e.g. '#0B5FFF'), or null if none found"
    )
    accent_color: Optional[str] = Field(
        default=None, description="Company's secondary/accent brand hex color, or null if none found"
    )
    font_family: Optional[str] = Field(
        default=None, description="Company's brand font family name (e.g. 'Poppins'), or null if none found"
    )


class CompanyProfileSchema(BaseModel):
    """profile_company's with_structured_output target."""

    offerings: str = Field(description="What the company offers/sells")
    industry: str = Field(description="Industry the company operates in")
    pain_points: list[str] = Field(description="Target pain points this company addresses or has")
    tone_signals: str = Field(description="Tone/voice signals observed for this company's communications")
    summary: str = Field(description="Overall summary of the company")
    web_sources: list[WebSource] = Field(
        default_factory=list, description="URLs cited by web search during profiling, for traceability"
    )
    brand: BrandGuide = Field(
        default_factory=BrandGuide, description="Best-effort brand colors/font gathered during profiling"
    )


# ---------------------------------------------------------------------------
# Generation-time research schemas (research_sender / research_receiver nodes)
# ---------------------------------------------------------------------------


class ResearchBriefSchema(BaseModel):
    """research_brief's with_structured_output target: what each per-company
    researcher should go find before the article gets written."""

    sender_dimensions: list[str] = Field(
        description="Concrete facts to confirm/find about the sender: proof points, offerings, recent news"
    )
    receiver_dimensions: list[str] = Field(
        description="Concrete facts to confirm/find about the receiver: pain points, recent news, industry context"
    )


class ResearchFact(BaseModel):
    fact: str = Field(description="A single researched fact, cleaned up, not summarized away")
    source_url: Optional[str] = Field(default=None, description="URL the fact was found at, if any")


class CompressedResearchSchema(BaseModel):
    """research_sender/research_receiver's with_structured_output target — the
    per-company researcher's final structured answer, already in the
    fact-plus-source shape the outline/writer stages consume directly."""

    facts: list[ResearchFact] = Field(description="Researched facts about this company, each with its source")


# ---------------------------------------------------------------------------
# Outline schema (outline node)
# ---------------------------------------------------------------------------


class OutlineSectionPlan(BaseModel):
    id: str = Field(description="Section identifier matching the template's section id")
    angle: str = Field(description="What this section will argue/cover")
    fact_refs: list[str] = Field(
        default_factory=list, description="Verbatim fact strings (from the research notes) this section will use"
    )


class ArticleOutlineSchema(BaseModel):
    """outline node's with_structured_output target — a plan mapping template
    slots to the specific facts each will use, written before any prose."""

    headline_angle: str = Field(description="The angle/hook the headline should take")
    subheadline_angle: str = Field(description="What the subheadline should add beyond the headline")
    sections: list[OutlineSectionPlan] = Field(description="One plan entry per template section id")
    pull_quote_angle: str = Field(description="What the pull quote should capture")
    cta_angle: str = Field(description="What action the CTA should drive toward")


# ---------------------------------------------------------------------------
# Section-drafting schemas (draft_sections node — one call per template unit)
# ---------------------------------------------------------------------------


class HeadlineSubheadlineDraft(BaseModel):
    headline: str = Field(description="Article headline")
    subheadline: str = Field(description="Article subheadline")


class SectionTextDraft(BaseModel):
    text: str = Field(description="The section's body text")


class QuoteCtaDraft(BaseModel):
    pull_quote: str = Field(description="Short pull quote")
    cta: str = Field(description="Call to action")


# ---------------------------------------------------------------------------
# Critique schema (critique node)
# ---------------------------------------------------------------------------


class CritiqueSchema(BaseModel):
    """critique node's with_structured_output target — a rubric score plus
    concrete edits, not a vague verdict."""

    fact_grounding: float = Field(description="0-1: every company claim traces to a researched fact or profile")
    personalization: float = Field(description="0-1: how specifically the article connects sender to receiver")
    tone_match: float = Field(description="0-1: how well the writing matches the receiver's tone_signals")
    structure: float = Field(description="0-1: flow, lead strength, lack of duplication across sections")
    required_edits: list[str] = Field(
        default_factory=list, description="Concrete, actionable edits; empty when no revision is needed"
    )


# ---------------------------------------------------------------------------
# Generation structured-output schema (Article)
# ---------------------------------------------------------------------------


class ArticleSectionDraft(BaseModel):
    id: str = Field(description="Section identifier matching the template's section id")
    text: str = Field(description="Section body text")


class ImagePlaceholderDraft(BaseModel):
    slot: str = Field(description="Image slot identifier matching the template's image_slots")
    alt_text: str = Field(description="Alt text describing the desired image for this slot")
    asset_alias: Optional[str] = Field(
        default=None,
        description=(
            "Alias of the chosen asset from the sender asset catalog (e.g. 'A2'), "
            "or null when no listed asset fits and a stock image should be sourced instead"
        ),
    )


class ArticleSchema(BaseModel):
    """polish node's with_structured_output target. Never includes code-computed
    fields like word_count/truncated/theme — those are added downstream."""

    headline: str = Field(description="Article headline")
    subheadline: str = Field(description="Article subheadline")
    sections: list[ArticleSectionDraft] = Field(description="Body sections, one per template section id")
    pull_quote: str = Field(description="Short pull quote")
    cta: str = Field(description="Call to action")
    image_placeholders: list[ImagePlaceholderDraft] = Field(
        description="One entry per template image slot, with alt text describing the desired image"
    )
    sources: list[str] = Field(
        default_factory=list, description="URLs of researched facts actually used in the article, for traceability"
    )


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------


class ContextUploadResponse(BaseModel):
    company_id: str
    page_count: Optional[int] = None
    images_extracted: Optional[int] = None
    images_described: Optional[int] = None
    digest_preview: Optional[str] = None
    profile_summary: str
    web_sources: list[str]


class GenerateRequest(BaseModel):
    sender_id: str
    receiver_id: str
    prompt: str
    template_id: str = "brochure_v1"


class GenerateSection(BaseModel):
    id: str
    text: str
    word_count: int
    max_words: int
    truncated: bool


class GenerateImagePlaceholder(BaseModel):
    slot: str
    source_hint: Literal["asset", "stock_query"]
    asset_id: Optional[int] = None
    alt_text: str
    stock_query: Optional[str] = None


class GenerateResponse(BaseModel):
    template_id: str
    headline: str
    subheadline: str
    sections: list[GenerateSection]
    pull_quote: str
    cta: str
    image_placeholders: list[GenerateImagePlaceholder]
    theme: ThemeColors
    grounding_notes: str
    sources: list[str] = Field(default_factory=list)
    pdf_path: Optional[str] = Field(
        default=None,
        description="Path to a rendered PDF, when the template supports PDF rendering (brochure_v1 only)",
    )


class CompanyProfileResponse(BaseModel):
    id: int
    company_id: str
    offerings: str
    industry: str
    pain_points: list[str]
    tone_signals: str
    summary: str
    web_sources: list[WebSource]
    created_at: datetime
