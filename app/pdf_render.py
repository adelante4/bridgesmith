"""Render the brochure_v1 template to a PDF via weasyprint. Only wired for
brochure_v1 — the older b2b_newsletter_v1 template has no print-oriented
HTML/CSS counterpart and stays JSON-only (see routes/generate.py).

Font handling: theme.font_family is a best-effort name gathered by the deep
research agent (app/deep_research.py), not a verified Google Fonts match. We
always emit a Google Fonts @import for it — if the name isn't a real family,
the browser/weasyprint just falls back to the next entry in the CSS font
stack, which is always a safe system sans-serif. No network lookup needed to
validate the name up front.
"""

import logging
import os
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from sqlmodel import Session
from weasyprint import HTML

from app.models import Image
from app.schemas import BrandGuide, GenerateResponse, ThemeColors

logger = logging.getLogger(__name__)

RENDERS_DIR = os.environ.get("RENDERS_DIR", "data/renders")
PDF_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "pdf_templates")

_env = Environment(
    loader=FileSystemLoader(PDF_TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)

# Every family here must actually exist in the runtime image (see Dockerfile),
# ending in a real family name rather than the generic keyword. WeasyPrint
# resolves through fontconfig, and in a slim container with no generic-alias
# rules installed `sans-serif` itself resolved to Liberation Mono — which is
# why every brochure rendered in Docker came out monospace regardless of the
# detected brand font.
_FALLBACK_FONT_STACK = "'DejaVu Sans', 'Liberation Sans', Arial, Helvetica, sans-serif"

# Brand colors/font are LLM-derived from web content (app/prompts.py) and get
# interpolated straight into a raw <style> block (brochure_v1.html) — an
# unvalidated value could break out of its CSS declaration. Only accept
# values that look like what they claim to be; anything else is treated the
# same as "no signal found" and falls back to the template default.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# No quote characters: the font stack is emitted into a raw CSS declaration
# without HTML-escaping (see _css_value), so a name carrying a quote could
# close the family string and inject further CSS.
_FONT_NAME_RE = re.compile(r"^[A-Za-z0-9 \-]{1,60}$")


def _clean_color(value: str | None) -> str | None:
    return value if value and _HEX_COLOR_RE.match(value) else None


def _clean_font(value: str | None) -> str | None:
    return value if value and _FONT_NAME_RE.match(value) else None


def resolve_theme(template_theme: ThemeColors, brand: BrandGuide) -> ThemeColors:
    """Sender brand signals win where present and well-formed; otherwise fall
    back to the template's own defaults. Never leaves a field unset."""
    return ThemeColors(
        primary_color=_clean_color(brand.primary_color) or template_theme.primary_color,
        accent_color=_clean_color(brand.accent_color) or template_theme.accent_color,
        font_family=_clean_font(brand.font_family) or template_theme.font_family,
    )


def _font_stack(font_family: str | None) -> str:
    """The stack must name the font the way the stylesheet declares it. Google
    serves 'Space Grotesk'; a stack asking for 'SpaceGrotesk' matches nothing
    even when the stylesheet loads fine, so use the same display name the
    @import was built from."""
    if not font_family:
        return _FALLBACK_FONT_STACK
    display_name = _google_font_family_param(font_family).replace("+", " ")
    return f"'{display_name}', {_FALLBACK_FONT_STACK}"


_PASCAL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _google_font_family_param(font_family: str) -> str:
    """Google Fonts wants the display family name ('Space+Grotesk'). What we
    detect is the PostScript name embedded in the source PDF, which has the
    spaces stripped ('SpaceGrotesk') and is a 400 from the CSS API — so split
    PascalCase back apart before asking."""
    spaced = _PASCAL_BOUNDARY_RE.sub(" ", font_family) if " " not in font_family else font_family
    return "+".join(spaced.split())


def _google_font_url(font_family: str | None) -> str | None:
    if not font_family:
        return None
    family_param = _google_font_family_param(font_family)
    return f"https://fonts.googleapis.com/css2?family={family_param}:wght@400;600;700&display=swap"


def _css_value(value: str) -> Markup:
    """Mark a CSS value as not-to-be-HTML-escaped.

    The template autoescapes (it is a .html file), which is right for the body
    text but wrong inside <style>: it rewrites the quotes in a font stack to
    &#39;, making the whole font-family declaration invalid CSS. WeasyPrint then
    drops the declaration and falls back to its own default family — which is
    how every brochure ended up in a font nobody chose, regardless of what was
    detected or which fonts were installed.

    Safe only because every value reaching here has been through _clean_color /
    _clean_font, which reject anything that isn't a plain hex colour or a
    quote-free font name."""
    return Markup(value)


def _resolve_asset_path(session: Session, asset_id: int | None) -> str | None:
    if asset_id is None:
        return None
    image = session.get(Image, asset_id)
    return image.file_path if image else None


def render_brochure_pdf(
    response: GenerateResponse,
    theme: ThemeColors,
    session: Session,
    company_id: str,
    article_id: int,
) -> str:
    """Renders response to a PDF at data/renders/{company_id}/{article_id}.pdf
    and returns that path. Raises on failure — the caller decides whether a
    render failure should block the JSON response (it shouldn't; see
    routes/generate.py, which wraps this call and degrades gracefully)."""
    images = {
        placeholder.slot: _resolve_asset_path(session, placeholder.asset_id)
        for placeholder in response.image_placeholders
        if placeholder.source_hint == "asset"
    }

    html = _env.get_template("brochure_v1.html").render(
        headline=response.headline,
        subheadline=response.subheadline,
        sections=response.sections,
        pull_quote=response.pull_quote,
        cta=response.cta,
        images=images,
        primary_color=_css_value(theme.primary_color),
        accent_color=_css_value(theme.accent_color),
        font_stack=_css_value(_font_stack(theme.font_family)),
        google_font_url=_google_font_url(theme.font_family),
    )

    out_dir = os.path.join(RENDERS_DIR, company_id)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{article_id}.pdf")
    HTML(string=html, base_url=os.getcwd()).write_pdf(out_path)
    return out_path
