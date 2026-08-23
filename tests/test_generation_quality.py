"""Deterministic guards for the copy-quality and render fixes.

Everything here runs without a live LLM: the pieces under test are exactly the
parts of the pipeline that were made deterministic on purpose, because relying
on a model to police its own specificity is what failed the first time.
"""

import pytest

from app.graphs.generation_graph import (
    REVISE_SCORE_FLOOR,
    _caveats_block,
    _concrete_anchors,
    _evidence_block,
    _filler_hits,
    _hedge_hits,
    should_revise,
)
from app.pdf_render import _font_stack, _google_font_url
from app.schemas import CompressedResearchSchema, CritiqueEdit, CritiqueSchema, ResearchFact


# ---------------------------------------------------------------------------
# Evidence / caveat routing — writers must never see a negative finding.
# ---------------------------------------------------------------------------


def _research() -> CompressedResearchSchema:
    return CompressedResearchSchema(
        facts=[
            ResearchFact(fact="Sage reported 300+ third-party apps in May 2026", source_url="http://a", kind="evidence"),
            ResearchFact(fact="No independent audit of Acme governance was found", kind="caveat"),
        ]
    )


def test_evidence_block_excludes_caveats():
    block = _evidence_block(_research())
    assert "300+ third-party apps" in block
    assert "No independent audit" not in block


def test_caveats_block_excludes_evidence():
    block = _caveats_block(_research())
    assert "No independent audit" in block
    assert "300+ third-party apps" not in block


def test_facts_default_to_evidence():
    # A researcher that omits `kind` must not silently poison the writer's
    # input — the safe default is the usable one.
    assert ResearchFact(fact="x").kind == "evidence"


# ---------------------------------------------------------------------------
# Specificity gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The live workflow routed 80% of tickets correctly.",
        "At Bolt Energie, we deployed a customer-service system.",
        "On April 28, 2026, Sage announced a shift.",
    ],
)
def test_concrete_text_has_anchors(text):
    assert _concrete_anchors(text) > 0


@pytest.mark.parametrize(
    "text",
    [
        "The challenge is to expand innovation without compromising control or trust.",
        "This brings a bridge between platform capability and production delivery.",
    ],
)
def test_abstract_text_has_no_anchors(text):
    assert _concrete_anchors(text) == 0


def test_sentence_initial_capital_is_not_an_anchor():
    # Otherwise every sentence would trivially pass the gate.
    assert _concrete_anchors("The team shipped it. Then they moved on.") == 0


def test_filler_and_hedges_are_detected():
    assert _filler_hits("We leverage robust, seamless tooling.") == ["leverage", "robust", "seamless"]
    assert _hedge_hits("Acme could deliver reported gains.") == ["could", "reported"]
    assert _filler_hits("We routed 80% of tickets correctly.") == []
    assert _hedge_hits("We routed 80% of tickets correctly.") == []


# ---------------------------------------------------------------------------
# Revise loop termination — the runaway-hedging bug.
# ---------------------------------------------------------------------------


def _critique(score: float, edits: list[CritiqueEdit]) -> CritiqueSchema:
    return CritiqueSchema(
        fact_grounding=score,
        personalization=score,
        tone_match=score,
        structure=score,
        specificity=score,
        required_edits=edits,
    )


def test_advisory_edits_alone_do_not_trigger_revision():
    critique = _critique(0.5, [CritiqueEdit(edit="Consider tightening the opening", severity="advisory")])
    assert should_revise({"critique": critique, "revise_attempts": 0}) == "validate"


def test_blocking_edit_triggers_revision():
    critique = _critique(0.5, [CritiqueEdit(edit="Headline claims an unsupported partnership", severity="blocking")])
    assert should_revise({"critique": critique, "revise_attempts": 0}) == "revise"


def test_good_scores_stop_the_loop_despite_blocking_edits():
    critique = _critique(REVISE_SCORE_FLOOR, [CritiqueEdit(edit="Anything", severity="blocking")])
    assert should_revise({"critique": critique, "revise_attempts": 0}) == "validate"


def test_attempt_cap_stops_the_loop():
    critique = _critique(0.1, [CritiqueEdit(edit="Still wrong", severity="blocking")])
    assert should_revise({"critique": critique, "revise_attempts": 99}) == "validate"


# ---------------------------------------------------------------------------
# Font resolution
# ---------------------------------------------------------------------------


def test_postscript_font_name_is_split_for_google_fonts():
    # 'SpaceGrotesk' is what PDF metadata carries; Google Fonts 404s on it.
    assert "family=Space+Grotesk" in _google_font_url("SpaceGrotesk")
    assert "family=Space+Grotesk" in _google_font_url("Space Grotesk")


def test_font_stack_uses_the_display_name_and_real_fallbacks():
    stack = _font_stack("SpaceGrotesk")
    assert stack.startswith("'Space Grotesk'")
    # Must end in families that actually exist in the runtime image, not just
    # the bare generic keyword — see the Dockerfile's fc-match guard.
    assert "DejaVu Sans" in stack


def test_css_values_survive_template_autoescaping():
    """The regression that made every brochure monospace.

    The template is a .html file, so Jinja autoescapes — which rewrote the
    quotes in the font stack to &#39;, invalidating the whole font-family
    declaration and sending WeasyPrint to its own default font.
    """
    from app.pdf_render import _css_value, _env

    rendered = _env.from_string("body { font-family: {{ font_stack }}; }").render(
        font_stack=_css_value(_font_stack("SpaceGrotesk"))
    )
    assert "&#39;" not in rendered
    assert "'Space Grotesk'" in rendered
