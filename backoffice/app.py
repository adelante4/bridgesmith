"""Streamlit backoffice — browse companies and their stored artifacts (profiles,
PDF digests, images, generated articles) straight from the SQLite DB, and drive
the FastAPI endpoints (/context upload, /generate) from a simple UI.

Run: streamlit run backoffice/app.py
Env: DATA_DIR (default "data"), API_URL (default "http://localhost:8000").
"""

import json
import os
from pathlib import Path

import requests
import streamlit as st
from sqlmodel import Session, select

from app.db import engine
from app.models import Company, CompanyProfile, GeneratedArticle, Image, PdfDigest

API_URL = os.environ.get("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Marketing Automation Backoffice", layout="wide")


def load_companies() -> list[Company]:
    with Session(engine) as session:
        return list(session.exec(select(Company).order_by(Company.created_at)))


def latest_profile(session: Session, company_id: str) -> CompanyProfile | None:
    return session.exec(
        select(CompanyProfile)
        .where(CompanyProfile.company_id == company_id)
        .order_by(CompanyProfile.id.desc())
    ).first()


def _json_list(raw: str) -> list:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def render_profile(profile: CompanyProfile) -> None:
    st.markdown(f"**Summary:** {profile.summary}")
    st.markdown(f"**Industry:** {profile.industry}")
    st.markdown(f"**Offerings:** {profile.offerings}")
    st.markdown(f"**Tone signals:** {profile.tone_signals}")
    if profile.description:
        st.markdown(f"**User description:** {profile.description}")
    pain_points = _json_list(profile.pain_points)
    if pain_points:
        st.markdown("**Pain points:**")
        for point in pain_points:
            st.markdown(f"- {point}")
    web_sources = _json_list(profile.web_sources)
    if web_sources:
        st.markdown("**Web sources:**")
        for source in web_sources:
            if isinstance(source, dict):
                st.markdown(f"- [{source.get('url', '')}]({source.get('url', '')}) — {source.get('note', '')}")
            else:
                st.markdown(f"- {source}")
    st.caption(f"Profile v{profile.id} · created {profile.created_at:%Y-%m-%d %H:%M} UTC")


def render_article(article: GeneratedArticle) -> None:
    try:
        result = json.loads(article.result_json)
    except json.JSONDecodeError:
        st.error("Stored result_json is not valid JSON")
        st.code(article.result_json)
        return
    st.markdown(f"## {result.get('headline', '')}")
    st.markdown(f"*{result.get('subheadline', '')}*")
    for section in result.get("sections", []):
        st.markdown(section.get("text", ""))
        if section.get("truncated"):
            st.caption(f"Section `{section.get('id')}` truncated to {section.get('max_words')} words")
    if result.get("pull_quote"):
        st.markdown(f"> {result['pull_quote']}")
    if result.get("cta"):
        st.markdown(f"**CTA:** {result['cta']}")
    placeholders = result.get("image_placeholders", [])
    if placeholders:
        st.markdown("**Image placeholders:**")
        for placeholder in placeholders:
            hint = placeholder.get("source_hint")
            detail = (
                f"asset #{placeholder.get('asset_id')}"
                if hint == "asset"
                else f"stock query: {placeholder.get('stock_query')}"
            )
            st.markdown(f"- `{placeholder.get('slot')}` ({hint}, {detail}) — {placeholder.get('alt_text')}")
    with st.expander("Raw JSON"):
        st.json(result)


# ---------------------------------------------------------------------------
# Sidebar: pick page
# ---------------------------------------------------------------------------

page = st.sidebar.radio("Page", ["Companies", "Generate", "Upload context", "Articles"])
companies = load_companies()
company_labels = {c.id: f"{c.name or '(unnamed)'} ({c.id})" for c in companies}

# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

if page == "Companies":
    st.title("Companies")
    if not companies:
        st.info("No companies yet — upload context first.")
        st.stop()

    with Session(engine) as session:
        overview = []
        for company in companies:
            profile = latest_profile(session, company.id)
            digests = session.exec(
                select(PdfDigest).where(PdfDigest.company_id == company.id)
            ).all()
            profile_versions = session.exec(
                select(CompanyProfile).where(CompanyProfile.company_id == company.id)
            ).all()
            overview.append(
                {
                    "id": company.id,
                    "name": company.name,
                    "created": company.created_at.strftime("%Y-%m-%d %H:%M"),
                    "profile versions": len(profile_versions),
                    "pdf uploads": len(digests),
                    "industry": profile.industry if profile else "",
                }
            )
    st.dataframe(overview, use_container_width=True, hide_index=True)

    selected = st.selectbox(
        "Inspect company", [c.id for c in companies], format_func=company_labels.get
    )
    if selected:
        with Session(engine) as session:
            profiles = session.exec(
                select(CompanyProfile)
                .where(CompanyProfile.company_id == selected)
                .order_by(CompanyProfile.id.desc())
            ).all()
            digests = session.exec(
                select(PdfDigest)
                .where(PdfDigest.company_id == selected)
                .order_by(PdfDigest.id.desc())
            ).all()
            images = session.exec(select(Image).where(Image.company_id == selected)).all()

        tab_profile, tab_digests, tab_images = st.tabs(
            [f"Profiles ({len(profiles)})", f"PDF digests ({len(digests)})", f"Images ({len(images)})"]
        )

        with tab_profile:
            if not profiles:
                st.info("No profile stored.")
            for i, profile in enumerate(profiles):
                header = f"v{profile.id}" + (" (latest — used by /generate)" if i == 0 else "")
                with st.expander(header, expanded=(i == 0)):
                    render_profile(profile)

        with tab_digests:
            if not digests:
                st.info("No PDF uploads.")
            for digest in digests:
                with st.expander(
                    f"Digest #{digest.id} · {digest.document_type or 'unknown type'} · "
                    f"{digest.created_at:%Y-%m-%d %H:%M}"
                ):
                    st.markdown(digest.digest_text or "_empty_")
                    key_facts = _json_list(digest.key_facts)
                    if key_facts:
                        st.markdown("**Key facts:**")
                        for fact in key_facts:
                            st.markdown(f"- {fact}")
                    st.caption(
                        f"{digest.images_reviewed} images reviewed"
                        + (" (cap hit)" if digest.images_cap_hit else "")
                    )
                    with st.expander("Raw transcript"):
                        st.text(digest.raw_text)

        with tab_images:
            if not images:
                st.info("No images extracted.")
            cols = st.columns(4)
            for i, image in enumerate(images):
                with cols[i % 4]:
                    path = Path(image.file_path)
                    if path.exists():
                        st.image(str(path), use_container_width=True)
                    else:
                        st.warning(f"File missing: {image.file_path}")
                    st.caption(f"`{image.image_id}` · {image.tag.value} · p{image.page_number}")
                    if image.description:
                        st.caption(image.description)

# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

elif page == "Generate":
    st.title("Generate article")
    if len(companies) < 1:
        st.info("Need at least one company with a stored profile.")
        st.stop()

    ids = [c.id for c in companies]
    col1, col2 = st.columns(2)
    sender_id = col1.selectbox("Sender (writing company)", ids, format_func=company_labels.get)
    receiver_id = col2.selectbox("Receiver (target company)", ids, format_func=company_labels.get)
    prompt = st.text_area(
        "Prompt", placeholder="e.g. Write a newsletter pitching our analytics platform to their ops team"
    )
    template_id = st.text_input("Template ID", value="b2b_newsletter_v1")

    if st.button("Generate", type="primary", disabled=not prompt):
        with st.spinner("Generating — this calls the LLM and can take a minute…"):
            try:
                response = requests.post(
                    f"{API_URL}/generate",
                    json={
                        "sender_id": sender_id,
                        "receiver_id": receiver_id,
                        "prompt": prompt,
                        "template_id": template_id,
                    },
                    timeout=600,
                )
            except requests.ConnectionError:
                st.error(f"Cannot reach API at {API_URL} — is the FastAPI server running?")
                st.stop()
        if response.ok:
            st.success("Generated")
            result = response.json()
            st.markdown(f"## {result['headline']}")
            st.markdown(f"*{result['subheadline']}*")
            for section in result["sections"]:
                st.markdown(section["text"])
            st.markdown(f"> {result['pull_quote']}")
            st.markdown(f"**CTA:** {result['cta']}")
            with st.expander("Raw JSON"):
                st.json(result)
        else:
            try:
                detail = response.json().get("detail", response.text)
            except json.JSONDecodeError:
                detail = response.text
            st.error(f"API error {response.status_code}: {detail}")

# ---------------------------------------------------------------------------
# Upload context
# ---------------------------------------------------------------------------

elif page == "Upload context":
    st.title("Upload company context")
    existing = st.selectbox(
        "Company (leave as 'New company' to create one)",
        ["New company"] + [c.id for c in companies],
        format_func=lambda v: v if v == "New company" else company_labels.get(v, v),
    )
    default_name = "" if existing == "New company" else (company_labels.get(existing, "").split(" (")[0])
    name = st.text_input("Name (required)", value=default_name)
    description = st.text_area("Description (required when no PDF)")
    file = st.file_uploader("Company PDF", type=["pdf"])

    if st.button("Ingest", type="primary", disabled=not name or (not file and not description)):
        data = {"name": name}
        if existing != "New company":
            data["company_id"] = existing
        if description:
            data["description"] = description
        files = {"file": (file.name, file.getvalue(), "application/pdf")} if file else None
        with st.spinner("Ingesting — PDF extraction, vision review, and profiling can take a few minutes…"):
            try:
                response = requests.post(f"{API_URL}/context", data=data, files=files, timeout=1800)
            except requests.ConnectionError:
                st.error(f"Cannot reach API at {API_URL} — is the FastAPI server running?")
                st.stop()
        if response.ok:
            result = response.json()
            st.success(f"Ingested — company_id `{result['company_id']}`")
            st.markdown(f"**Profile summary:** {result['profile_summary']}")
            if result.get("digest_preview"):
                st.markdown(f"**Digest preview:** {result['digest_preview']}")
            with st.expander("Raw response"):
                st.json(result)
        else:
            try:
                detail = response.json().get("detail", response.text)
            except json.JSONDecodeError:
                detail = response.text
            st.error(f"API error {response.status_code}: {detail}")

# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------

elif page == "Articles":
    st.title("Generated articles")
    with Session(engine) as session:
        articles = session.exec(
            select(GeneratedArticle).order_by(GeneratedArticle.id.desc())
        ).all()
    if not articles:
        st.info("No articles generated yet.")
        st.stop()
    for article in articles:
        sender = company_labels.get(article.sender_id, article.sender_id)
        receiver = company_labels.get(article.receiver_id, article.receiver_id)
        with st.expander(
            f"#{article.id} · {sender} → {receiver} · {article.template_id} · "
            f"{article.created_at:%Y-%m-%d %H:%M}"
        ):
            st.caption(
                f"Prompt: {article.prompt} · profiles used: sender v{article.sender_profile_id}, "
                f"receiver v{article.receiver_profile_id}"
            )
            render_article(article)
