"""SQLModel ORM models — see spec.md §4 for the original data model, and
docs/adr/0001-role-independent-company-versioned-profiles.md for how it has
since evolved: Company is role-independent identity only. Context accumulates
as three independent, append-only per-company logs — PdfDigest, Description,
ResearchRun — each one row per add, nothing ever overwritten. There is no
synthesized "profile" row: /generate reads all three logs directly at
generation time (see docs/adr/0005-decouple-context-from-research.md)."""

import enum
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ImageTag(str, enum.Enum):
    logo = "logo"
    product_image = "product_image"
    chart = "chart"
    generic = "generic"


class Company(SQLModel, table=True):
    """Role-independent identity. No per-run artifacts here — those live on
    PdfDigest, one row per ingestion run. No role — sender/receiver are
    request-time labels (see CompanyNotFoundError), not entity attributes."""

    id: str = Field(primary_key=True)
    name: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class PdfDigest(SQLModel, table=True):
    """One row per ingestion run (one per PDF upload) — the per-run artifact
    record: raw transcript, tables, and the ingestion agent's merged summary.
    Also carries the per-PDF style signals gathered at ingestion time:
    tone_signals from the text-reading ingestion agent, and
    design_notes/brand colors from a vision pass over the PDF's first pages
    (font_family is cross-checked deterministically from embedded PDF font
    metadata). /generate's style selector always reads the company's newest
    PdfDigest row for these — no separate profile/style table."""

    id: int | None = Field(default=None, primary_key=True)
    company_id: str = Field(foreign_key="company.id")
    raw_text: str = Field(default="", description="Full annotated transcript, incl. [[IMAGE:id]] markers")
    tables_json: str = Field(default="[]", description="Extracted tables, JSON-serialized")
    digest_text: str = Field(default="")
    key_facts: str = Field(default="[]", description="JSON list of short factual bullets")
    document_type: str = Field(default="")
    tone_signals: str = Field(default="", description="Writing/voice tone observed in the document's own text")
    images_reviewed: int = Field(default=0)
    images_cap_hit: bool = Field(default=False)
    design_notes: str = Field(default="", description="Visual style notes from the page-1-2 vision pass")
    brand_primary_color: str | None = Field(default=None)
    brand_accent_color: str | None = Field(default=None)
    brand_font_family: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class Image(SQLModel, table=True):
    """Scoped to the ingestion run (PdfDigest) that extracted it — not a flat
    company-wide pool. A later upload's images don't collide with an earlier
    upload's, even if extraction assigns the same image_id within each run."""

    id: int | None = Field(default=None, primary_key=True)
    company_id: str = Field(foreign_key="company.id")
    pdf_digest_id: int = Field(foreign_key="pdfdigest.id")
    image_id: str = Field(description="Stable id from extraction, e.g. img_03_01 (unique within its run only)")
    file_path: str
    page_number: int
    description: str = Field(default="")
    tag: ImageTag = Field(default=ImageTag.generic)
    is_own_brand: bool = Field(
        default=False,
        description=(
            "True only when this mark belongs to the company that uploaded the document. "
            "A company deck is full of OTHER companies' logos — customers, partners, cloud "
            "providers — and `tag` cannot tell them apart: Acme's own mark and its customer "
            "Bolt's are both tag=logo. Rendering a customer's logo where the sender's belongs "
            "misrepresents both, so the brochure only ever uses a logo with this set. Defaults "
            "to False: an unclassified mark is not usable."
        ),
    )
    created_at: datetime = Field(default_factory=_utcnow)


class Description(SQLModel, table=True):
    """One row per free-text "add context" submission with no PDF attached.
    Append-only, same pattern as PdfDigest — every description a company has
    ever been given is kept and folded into the context blob at generate time."""

    id: int | None = Field(default=None, primary_key=True)
    company_id: str = Field(foreign_key="company.id")
    text: str
    created_at: datetime = Field(default_factory=_utcnow)


class ResearchRun(SQLModel, table=True):
    """One row per deep-research run, triggered independently of PDF/description
    uploads (POST /context/{company_id}/research). All runs are kept; /generate
    always reads only the newest one for a company."""

    id: int | None = Field(default=None, primary_key=True)
    company_id: str = Field(foreign_key="company.id")
    offerings: str
    industry: str
    target_customers: str = Field(default="", description="Segments/buyer roles the company sells to")
    pain_points: str = Field(default="[]", description="JSON list — pains the company itself faces")
    differentiators: str = Field(default="[]", description="JSON list")
    proof_points: str = Field(default="[]", description="JSON list")
    recent_developments: str = Field(default="[]", description="JSON list, each item dated")
    summary: str
    web_sources: str = Field(default="[]", description="JSON list of {url, note}")
    created_at: datetime = Field(default_factory=_utcnow)


class GeneratedArticle(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    sender_id: str = Field(foreign_key="company.id")
    receiver_id: str = Field(foreign_key="company.id")
    sender_pdf_digest_id: int | None = Field(
        default=None, foreign_key="pdfdigest.id", description="Sender's newest PdfDigest at generation time, if any"
    )
    receiver_pdf_digest_id: int | None = Field(
        default=None, foreign_key="pdfdigest.id", description="Receiver's newest PdfDigest at generation time, if any"
    )
    prompt: str
    template_id: str
    result_json: str
    pdf_path: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
