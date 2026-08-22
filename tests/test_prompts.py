"""Guard the prompt templates: every *_USER_PROMPT must .format() cleanly.

An unescaped { or } accidentally introduced into a template (e.g. a pasted
JSON example) makes .format() raise KeyError/IndexError on every request at
runtime. Braces inside substituted *values* are safe — .format() only parses
the template — which these tests also demonstrate by passing brace-laden
values.
"""

from app import prompts

BRACES = 'text with {braces} and {"json": true}'


def test_vision_user_prompt_formats():
    out = prompts.VISION_SUBAGENT_USER_PROMPT.format(context_hint=BRACES)
    assert BRACES in out


def test_ingestion_user_prompt_formats():
    out = prompts.INGESTION_AGENT_USER_PROMPT.format(transcript=BRACES)
    assert BRACES in out


def test_deep_research_user_prompt_formats():
    out = prompts.DEEP_RESEARCH_USER_PROMPT.format(
        name="Acme", pdf_section=BRACES, description_section=BRACES
    )
    assert "Acme" in out and BRACES in out


def test_generate_draft_user_prompt_formats():
    out = prompts.GENERATE_DRAFT_USER_PROMPT.format(
        sender_profile='{"offerings": "x"}',
        receiver_profile='{"offerings": "y"}',
        user_prompt=BRACES,
        template_constraints="headline: max 12 words",
        image_slots="hero, inline_1",
        sender_assets="A1: [logo] Acme company logo",
    )
    assert '{"offerings": "x"}' in out and BRACES in out


def test_repair_turn_user_prompt_formats():
    out = prompts.REPAIR_TURN_USER_PROMPT.format(violations=BRACES)
    assert BRACES in out


def test_system_prompts_have_no_placeholders():
    # System prompts are static instructions — they must never be .format()ed
    # and must contain no substitution fields that a call site could forget.
    for name in (
        "VISION_SUBAGENT_SYSTEM_PROMPT",
        "INGESTION_AGENT_SYSTEM_PROMPT",
        "DEEP_RESEARCH_SYSTEM_PROMPT",
        "COMPANY_RESEARCH_SUBAGENT_PROMPT",
        "GENERATE_DRAFT_SYSTEM_PROMPT",
    ):
        text = getattr(prompts, name)
        assert "{" not in text and "}" not in text, f"{name} contains braces"
