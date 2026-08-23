# 3. Per-generation company research, outline-first STORM-style drafting

Date: 2026-08-23

## Status

Accepted

## Context

The generation graph wrote the whole article in one structured-output call, fed only the sender/receiver
`CompanyProfile` rows captured once at ingestion time (`load_profiles -> build_prompt -> generate_draft ->
validate -> repair* -> finalize_article`). Nothing was researched *for this specific sender/receiver
pairing* — a stale ingestion-time profile was the ceiling on how specific the article could get, and the
draft prompt explicitly forbade claims beyond the profile ("do not invent facts ... if unsure, omit it"),
which starved the writer rather than sending it to find more.

A survey of current OSS research/writing agents (deepagents, `open_deep_research`, `gpt-researcher`, Stanford's
STORM, Anthropic's multi-agent research system) converged on a consistent shape for this kind of task: scope
research into an explicit brief, run isolated per-subject researcher agents with hard search budgets, outline
before writing, draft section-by-section against the outline's assigned facts, then critique with a concrete
rubric before finalizing. The pipeline here is fixed-shape (always: brief, research two named companies,
outline, draft, polish, critique) — the "known workflow, want fine-grained control" case the deepagents docs
themselves route to a custom LangGraph graph, not their open-ended harness. `app/deep_research.py` already
established the `deepagents.create_deep_agent` idiom for "research one company, produce structured output";
reusing it per-company here keeps one pattern in the codebase instead of two.

## Decision

- New nodes fan out from `load_profiles`: `research_brief` (no-tool LLM call turning both profiles + the
  creative brief into a per-company research dimension list) `-> research_sender` and `research_receiver` in
  parallel, each an isolated `create_deep_agent` (web_search tool + a `fact-finder` sub-agent, `TodoListMiddleware`)
  budgeted in-prompt to ~5 searches total, `-> outline` (waits on both).
- No separate "compress" LLM call: the researcher's own structured output (`CompressedResearchSchema`, a list
  of `{fact, source_url}`) already forces the clean, source-preserving shape a compression pass would produce.
  Folding it into the researcher's final answer saves a call without losing the property that mattered.
- `outline` produces `ArticleOutlineSchema`: an angle per template unit, plus the exact fact strings assigned
  to each — committing to which fact goes where before any prose exists (STORM's core move).
- `draft_sections` makes one structured-output call per template unit (headline+subheadline together, each
  body section, pull_quote+cta together), each fed only its outline-assigned facts, not the full research
  dump — a section with no assigned facts is written from the profiles alone rather than reaching for
  something irrelevant.
- `polish` (renamed from `generate_draft`) assembles the drafted pieces into one `ArticleSchema`: fixes
  transitions/dedup/lead, still does asset-alias selection (ADR 0002), and now also populates a new
  `sources: list[str]` field with the URLs actually behind claims that survived into the final text.
- `critique` scores the polished draft against the research on four named dimensions (fact grounding,
  personalization, tone match, structure) and lists concrete `required_edits`; `revise` (capped at
  `MAX_REVISE_ATTEMPTS = 1`) applies them as one more turn on the same `polish_messages` conversation before
  falling through to `validate`.
- `validate -> repair* -> finalize_article` is otherwise unchanged: same word-limit/slot checks, same
  conversation-turn repair pattern, now continuing from `polish_messages` instead of a from-scratch draft
  conversation. The `ArticleSchema` contract, DB `result_json`, and backoffice rendering are unaffected except
  for the additive `sources` field.
- No time budget — search/revise/repair attempt counts are the only caps (per-project decision: quality over
  latency for this submission).

## Consequences

A generation run is now roughly: 1 brief call + 2 parallel research agents (each up to ~5 searches across
itself and its sub-agent) + 1 outline call + up to 5 section-writer calls + 1 polish call + 1 critique call +
up to 1 revise call + up to 2 repair calls — meaningfully more LLM calls and latency than the old single-call
draft, traded for facts that are actually about this sender/receiver pairing instead of whatever ingestion
happened to capture, and for auditable per-claim sourcing. A pairing with a thin research yield still degrades
gracefully: `_facts_block` renders "(no additional facts found)" and downstream nodes fall back to writing
from the profiles alone, the same way they always could.
