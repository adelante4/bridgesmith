"""Shared read helpers over the three append-only context logs (PdfDigest,
Description, ResearchRun) — used by both routes/context.py (research grounding)
and graphs/generation_graph.py (context blob + style bundle), so "latest for a
company" is defined in exactly one place. See docs/adr/0005-decouple-context-from-research.md."""

from sqlmodel import Session, select

from app.models import Description, PdfDigest, ResearchRun


def latest_pdf_digest(session: Session, company_id: str) -> PdfDigest | None:
    return session.exec(
        select(PdfDigest).where(PdfDigest.company_id == company_id).order_by(PdfDigest.id.desc())
    ).first()


def latest_research(session: Session, company_id: str) -> ResearchRun | None:
    return session.exec(
        select(ResearchRun).where(ResearchRun.company_id == company_id).order_by(ResearchRun.id.desc())
    ).first()


def all_pdf_digests(session: Session, company_id: str) -> list[PdfDigest]:
    return list(
        session.exec(select(PdfDigest).where(PdfDigest.company_id == company_id).order_by(PdfDigest.id)).all()
    )


def all_descriptions(session: Session, company_id: str) -> list[Description]:
    return list(
        session.exec(
            select(Description).where(Description.company_id == company_id).order_by(Description.id)
        ).all()
    )
