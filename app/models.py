"""SQLModel ORM models — see spec.md §4 for the data model this mirrors."""

import enum
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CompanyRole(str, enum.Enum):
    sender = "sender"
    receiver = "receiver"


class ImageTag(str, enum.Enum):
    logo = "logo"
    product_image = "product_image"
    chart = "chart"
    generic = "generic"


class Company(SQLModel, table=True):
    id: str = Field(primary_key=True)
    role: CompanyRole
    name: str | None = Field(default=None)
    raw_text: str = Field(default="")
    tables_json: str = Field(default="[]")
    created_at: datetime = Field(default_factory=_utcnow)


class PdfDigest(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company_id: str = Field(foreign_key="company.id")
    digest_text: str
    key_facts: str = Field(default="[]", description="JSON list of short factual bullets")
    document_type: str
    images_reviewed: int = Field(default=0)
    images_cap_hit: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)


class Image(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company_id: str = Field(foreign_key="company.id")
    image_id: str = Field(description="Stable id from extraction, e.g. img_03_01")
    file_path: str
    page_number: int
    description: str = Field(default="")
    tag: ImageTag = Field(default=ImageTag.generic)
    created_at: datetime = Field(default_factory=_utcnow)


class CompanyProfile(SQLModel, table=True):
    # One profile per company: company_id is both PK and FK, not a redundant autoincrement id.
    company_id: str = Field(foreign_key="company.id", primary_key=True)
    offerings: str
    industry: str
    pain_points: str = Field(default="[]", description="JSON list")
    tone_signals: str
    summary: str
    web_sources: str = Field(default="[]", description="JSON list of {url, note}")
    created_at: datetime = Field(default_factory=_utcnow)


class GeneratedArticle(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    sender_id: str = Field(foreign_key="company.id")
    receiver_id: str = Field(foreign_key="company.id")
    prompt: str
    template_id: str
    result_json: str
    created_at: datetime = Field(default_factory=_utcnow)
