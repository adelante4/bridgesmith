"""Deterministic guards for the brochure_v2 design system.

Palette derivation, the Markdown subset, and asset eligibility are all pure
functions on purpose — they are the parts of the render that must not depend on
a model behaving well.
"""

import pytest

from app.graphs.generation_graph import _eligible_slots
from app.models import Image, ImageTag
from app.palette import PAPER, contrast_ratio, derive
from app.richtext import strip, to_html
from app.templates import load_template

V2_SLOTS = ["masthead", "mark_1", "mark_2", "mark_3"]


def _img(tag: ImageTag, own: bool) -> Image:
    return Image(
        company_id="co_x", pdf_digest_id=1, image_id="i", file_path="/x.png",
        page_number=1, tag=tag, is_own_brand=own,
    )


# ---------------------------------------------------------------------------
# Asset eligibility — the "Bolt's logo in Acme's masthead" bug.
# ---------------------------------------------------------------------------


def test_own_logo_may_fill_the_masthead():
    assert "masthead" in _eligible_slots(_img(ImageTag.logo, own=True), V2_SLOTS)


def test_third_party_logo_may_not_fill_the_masthead():
    # A customer's logo is tag=logo exactly like the sender's own mark; only
    # is_own_brand separates them.
    assert _eligible_slots(_img(ImageTag.logo, own=False), V2_SLOTS) == []


def test_logos_never_fill_content_slots():
    # Even the sender's own wordmark stretched into a content slot reads as a
    # mistake, so identity marks stay in identity slots.
    assert _eligible_slots(_img(ImageTag.logo, own=True), V2_SLOTS) == ["masthead"]


def test_generic_marks_fill_content_slots_only():
    assert _eligible_slots(_img(ImageTag.generic, own=False), V2_SLOTS) == ["mark_1", "mark_2", "mark_3"]


# ---------------------------------------------------------------------------
# Palette — never trust two detected hexes as a working pair.
# ---------------------------------------------------------------------------


def test_achromatic_accent_becomes_ink():
    # Acme: orange primary, black accent. Black is the ink of their own material.
    p = derive("#FF4B00", "#000000")
    assert p.accent_is_hue is False
    assert p.ink == "#000000"


def test_chromatic_distinct_accent_is_kept_as_a_hue():
    p = derive("#0B5FFF", "#FFB020")
    assert p.accent_is_hue is True
    assert p.accent == "#FFB020"


def test_accent_close_in_hue_to_primary_does_not_become_ink():
    # A navy/blue pair must not set the whole body text in blue.
    p = derive("#101828", "#2563EB")
    assert p.accent_is_hue is False
    assert p.ink == "#1A1A1A"


@pytest.mark.parametrize("primary", ["#FF4B00", "#FFE800", "#0B5FFF", "#101828"])
def test_text_variants_are_always_legible_on_paper(primary):
    p = derive(primary, None)
    assert contrast_ratio(p.brand_text, PAPER) >= 4.5


def test_page_is_never_painted_in_a_detected_colour():
    # The v1 failure: primary interpolated across a full A4 bleed.
    p = derive("#FF4B00", "#000000")
    assert p.paper == PAPER
    assert contrast_ratio(p.paper, p.ink) > 10


# ---------------------------------------------------------------------------
# Markdown subset — two tags, and nothing else gets through.
# ---------------------------------------------------------------------------


def test_supported_markers_convert():
    assert to_html("routed **80%** of tickets") == "routed <strong>80%</strong> of tickets"
    assert to_html("replied *three times* faster") == "replied <em>three times</em> faster"


def test_html_in_model_output_is_neutralised():
    out = to_html("hi <script>alert(1)</script> **there**")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<strong>there</strong>" in out


def test_link_and_image_syntax_is_not_markup():
    # The design carries no URLs; Markdown link syntax must stay inert text.
    out = to_html("see [Sage](https://sage.com) and ![x](y.png)")
    assert "<a" not in out and "<img" not in out


def test_strip_removes_markers_for_counting():
    assert strip("routed **80%** and *3x* faster") == "routed 80% and 3x faster"


def test_bold_is_not_mangled_into_nested_emphasis():
    assert to_html("**80%**") == "<strong>80%</strong>"


# ---------------------------------------------------------------------------
# Template registration — adding a template stays a config change.
# ---------------------------------------------------------------------------


def test_v2_declares_its_own_stylesheet():
    assert load_template("brochure_v2").html_template == "brochure_v2.html"


def test_v1_still_declares_its_original_stylesheet():
    assert load_template("brochure_v1").html_template == "brochure_v1.html"


def test_json_only_template_declares_no_stylesheet():
    # b2b_newsletter_v1 has no print counterpart; a null here is what makes the
    # route skip rendering rather than a hardcoded id set.
    assert load_template("b2b_newsletter_v1").html_template is None


def test_v2_sections_carry_heading_caps():
    sections = load_template("brochure_v2").fields.sections
    assert len(sections) == 3
    assert all(s.max_heading_words and s.heading_fallback for s in sections)
