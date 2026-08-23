"""SQLModel ORM models — see spec.md §4 for the original data model, and
docs/adr/0001-role-independent-company-versioned-profiles.md for how it has
since evolved: Company is role-independent identity only; PdfDigest and
CompanyProfile are versioned per ingestion run, one of each per upload."""

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
    record: raw transcript, tables, and the ingestion agent's merged summary."""

    id: int | None = Field(default=None, primary_key=True)
    company_id: str = Field(foreign_key="company.id")
    raw_text: str = Field(default="", description="Full annotated transcript, incl. [[IMAGE:id]] markers")
    tables_json: str = Field(default="[]", description="Extracted tables, JSON-serialized")
    digest_text: str = Field(default="")
    key_facts: str = Field(default="[]", description="JSON list of short factual bullets")
    document_type: str = Field(default="")
    images_reviewed: int = Field(default=0)
    images_cap_hit: bool = Field(default=False)
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
    created_at: datetime = Field(default_factory=_utcnow)


class CompanyProfile(SQLModel, table=True):
    """Versioned: one row per ingestion run, never updated in place. /generate
    always reads the latest version for a company (highest id)."""

    id: int | None = Field(default=None, primary_key=True)
    company_id: str = Field(foreign_key="company.id")
    pdf_digest_id: int | None = Field(
        default=None,
        foreign_key="pdfdigest.id",
        description="The PDF ingestion run this profile version was produced from, if any — None when this "
        "version came from a name/description-only research run with no PDF uploaded",
    )
    description: str | None = Field(
        default=None, description="User-provided free-text company description supplied for this run, if any"
    )
    offerings: str
    industry: str
    pain_points: str = Field(default="[]", description="JSON list")
    tone_signals: str
    summary: str
    web_sources: str = Field(default="[]", description="JSON list of {url, note}")
    brand_primary_color: str | None = Field(default=None)
    brand_accent_color: str | None = Field(default=None)
    brand_font_family: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)


class GeneratedArticle(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    sender_id: str = Field(foreign_key="company.id")
    receiver_id: str = Field(foreign_key="company.id")
    sender_profile_id: int = Field(foreign_key="companyprofile.id", description="Exact profile version used")
    receiver_profile_id: int = Field(foreign_key="companyprofile.id", description="Exact profile version used")
    prompt: str
    template_id: str
    result_json: str
    pdf_path: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_utcnow)
