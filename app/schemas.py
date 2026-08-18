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


# ---------------------------------------------------------------------------
# Generation structured-output schema (Article)
# ---------------------------------------------------------------------------


class ArticleSectionDraft(BaseModel):
    id: str = Field(description="Section identifier matching the template's section id")
    text: str = Field(description="Section body text")


class ImagePlaceholderDraft(BaseModel):
    slot: str = Field(description="Image slot identifier matching the template's image_slots")
    alt_text: str = Field(description="Alt text describing the desired image for this slot")


class ArticleSchema(BaseModel):
    """generate_draft's with_structured_output target. Never includes code-computed
    fields like word_count/truncated/theme — those are added downstream."""

    headline: str = Field(description="Article headline")
    subheadline: str = Field(description="Article subheadline")
    sections: list[ArticleSectionDraft] = Field(description="Body sections, one per template section id")
    pull_quote: str = Field(description="Short pull quote")
    cta: str = Field(description="Call to action")
    image_placeholders: list[ImagePlaceholderDraft] = Field(
        description="One entry per template image slot, with alt text describing the desired image"
    )


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------


class ContextUploadResponse(BaseModel):
    company_id: str
    role: str
    page_count: int
    images_extracted: int
    images_described: int
    digest_preview: str
    profile_summary: str
    web_sources: list[str]


class GenerateRequest(BaseModel):
    sender_id: str
    receiver_id: str
    prompt: str
    template_id: str = "b2b_newsletter_v1"


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


class CompanyProfileResponse(BaseModel):
    company_id: str
    offerings: str
    industry: str
    pain_points: list[str]
    tone_signals: str
    summary: str
    web_sources: list[WebSource]
    created_at: datetime
