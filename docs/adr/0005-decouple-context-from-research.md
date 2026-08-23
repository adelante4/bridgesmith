# 5. Decouple context ingestion from research; drop CompanyProfile for three append-only logs

Date: 2026-08-23

## Status

Accepted

## Context

`POST /context` conflated two independent concerns: adding context for a company (a PDF, or a plain-text
description) always fired the full deep-research web-search agent (`profile_company_node` in
`ingestion_graph.py`) before the request could return. Uploading a second PDF for the same company re-ran research
from scratch, blocking on an LLM web-search call the user didn't ask for. The output of that call — a single
versioned `CompanyProfile` row (ADR 0001) — was also the *only* source `/generate` read from: `load_profiles`
took the highest-id row and discarded every earlier PDF/description/research run for that company, even though
all of it was still sitting in the DB.

Two problems, one root cause: context accumulation and research were forced into lockstep, and only the most
recent lockstepped result was ever used.

## Decision

Split into three independent, append-only per-company logs — `PdfDigest`, `Description`, `ResearchRun` — and
retire `CompanyProfile` entirely. No row is ever overwritten; nothing is deleted.

- **Ingestion** (`POST /context`) never triggers research. A PDF still runs `extract_pdf_structure ->
  ingestion_agent -> persist_digest` unchanged, plus a new `extract_brand` node (see below). A bare description
  just inserts a `Description` row — no graph, no LLM call.
- **Research** is a separate action: `POST /context/{company_id}/research`. Callable any time, independently of
  uploads; still grounds itself in the company's newest `PdfDigest` + all `Description` rows as prompt context
  (`app/deep_research.py`), but its output (`ResearchResultSchema`: offerings/industry/pain_points/summary/
  web_sources) is its own `ResearchRun` row, not folded back into a profile.
- **Style signals move to where they're produced, not where they're read.** Writing tone comes from the
  `ingestion_agent` (it already reads the full document text) as a new `tone_signals` field on `PdfDigestSchema`.
  Visual brand identity — primary/accent hex color, free-text `design_notes` — comes from a new deterministic-shape
  `extract_brand` node: render the PDF's first 1-2 pages to images (`app.pdf_extraction.render_first_pages`), one
  fixed vision call (`app.vision.extract_brand_from_pages`) for colors + design notes, plus embedded-PDF-metadata
  font detection (`app.pdf_extraction.detect_embedded_font`, no LLM). All five fields land on that PDF's own
  `PdfDigest` row. Web research never touches brand/tone — a company's own documents are the authority on its
  own voice and look, not a general web search.
- **No synthesized profile, ever — not even lazily.** `/generate`'s `load_profiles` node was renamed in spirit but
  kept its name for diff size; it now does two things, both pure aggregation, no LLM call:
  - `_context_blob`: plain markdown concatenation of every `PdfDigest.digest_text`, every `Description.text`, and
    the single newest `ResearchRun`'s findings for a company. Fed directly into `plan_research`/`research`/
    `outline` prompts in place of the old `CompanyProfileSchema.model_dump_json()`.
  - `_style_bundle`: deterministic selection (no search across history) of the company's *newest* `PdfDigest`'s
    tone/design/brand fields, wrapped as `StyleBundle`. Feeds `SECTION_WRITER_USER_PROMPT`'s `receiver_tone` and
    `pdf_render.resolve_theme`'s brand colors, same as the old `CompanyProfileSchema.tone_signals`/`.brand` did.
  - `sender_summary`/`receiver_summary` for section-writer prompts fall back to `NO_CONTEXT_PLACEHOLDER` when no
    `ResearchRun` exists yet — a company with only PDFs/descriptions and no research run is still generatable.
- `GeneratedArticle.sender_profile_id`/`receiver_profile_id` (FK to the now-gone `CompanyProfile`) become
  `sender_pdf_digest_id`/`receiver_pdf_digest_id` (nullable — a company can be generated for with zero PDFs, pure
  description/research context).
- Old `CompanyProfile` rows from before this change are simply never read again; no migration/backfill — this is
  a prototype (spec.md), no migration framework exists (`app/db.py`).

## Consequences

Uploading a second PDF, or adding a plain description, is now instant — no LLM call on the critical path.
Research is opt-in and repeatable on demand (backoffice gets a standalone "Run deep research" button). `/generate`
now genuinely uses *everything* on file for a company instead of only the artifact of the most recent `/context`
call — the actual ask that started this redesign. The cost: `/generate`'s context blob grows unbounded with a
company's PDF/description history (no map-reduce/summarization step — digest texts are already ingestion-time
summaries, not raw transcripts, so this is cheap in practice but not free forever). A company whose latest
`PdfDigest` predates this change has null tone/brand fields with no fallback search to an older digest that might
have them (deliberate — see the "accept null, no fallback" call in the design discussion); the gap self-heals the
next time anyone uploads a PDF for that company.
