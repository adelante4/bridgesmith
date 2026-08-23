# Automated Generative Marketing Collateral

Backend that ingests PDF context for companies and generates a tailored, structurally-constrained B2B marketing article bridging two of them.

## Language

**Company**:
A role-independent business entity that has uploaded PDF context. Identity only (`id`, `name`, `created_at`) — carries no notion of sender/receiver, and no per-run artifacts (transcript, tables). The same company can appear as a Sender in one generate request and a Receiver in another.
_Avoid_: Account, Organization.

**Sender** / **Receiver**:
Roles a Company plays for the duration of a single `/generate` request, not properties of the Company itself. `sender_id`/`receiver_id` are just two positional slots in a request; nothing on the referenced Company is checked or constrained by which slot it's passed into.
_Avoid_: treating these as company attributes, "the sender company" as a fixed identity.

**PdfDigest**:
The record of one PDF upload: the annotated transcript (text + `[[IMAGE:id]]` markers), extracted tables, the ingestion agent's merged text+image summary (`digest_text`, `key_facts`, `document_type`), its `tone_signals` (writing/voice tone read from the document's own text), and its `design_notes`/`brand_primary_color`/`brand_accent_color`/`brand_font_family` (visual style, from a vision pass over the PDF's first pages plus deterministic embedded-font detection). A Company accumulates one `PdfDigest` per upload, append-only — this is where per-upload artifacts *and* style signals live, not on Company. `/generate`'s style selector deterministically reads a company's *newest* `PdfDigest`.
_Avoid_: Transcript (too narrow — a digest includes tables and image summaries, not just text), Profile (a digest is one company's-own-document artifact; it never includes web research).

**Description**:
One free-text "add context" submission for a company with no PDF attached. Append-only, same pattern as `PdfDigest` — every description ever added for a company is kept, not just the latest.
_Avoid_: Profile, Summary.

**ResearchRun**:
One deep-research (web-search-augmented) run for a company, triggered independently of any upload via `POST /context/{company_id}/research` — never fired automatically by adding a PdfDigest or Description. Every run is kept; `/generate` reads only the newest one for a company. See `docs/adr/0005-decouple-context-from-research.md`.
_Avoid_: CompanyProfile (retired — see below), "the" research (there can be several, ordered by recency).

**Context blob**:
The plain markdown concatenation of everything on file for a company — every `PdfDigest.digest_text`, every `Description.text`, and the newest `ResearchRun`'s findings — built fresh at generation time (`app/graphs/generation_graph.py::_context_blob`, no LLM call). What the old, now-retired `CompanyProfile` synthesized once per upload; now assembled from *all* accumulated context every time, not just the most recent upload's artifact.
_Avoid_: Profile, Summary (a specific `ResearchRun.summary` field, not the blob).

**Image**:
An extracted image asset, scoped to the specific `PdfDigest` run that produced it — not a company-wide flat pool. `/generate`'s asset matching only considers images from a company's *newest* `PdfDigest`. Carries `is_own_brand`: whether the mark belongs to the company whose document it came from. A company deck is full of *other* companies' logos (customers, partners, cloud providers) and `tag` cannot tell them apart — Acme's own mark and its customer Bolt's are both `tag=logo` — so ownership is a separate signal set by the ingestion vision pass. See `docs/adr/0007-brochure-v2-print-design.md`.
_Avoid_: Asset (used loosely elsewhere for "extracted image or stock query hint" generically — Image specifically means a row with a `file_path`).

**Template**:
A fixed publishing-layout contract (word limits, section and heading caps, image slots, theme colors) supplied by Customer C's design team, loaded from `config/templates/*.json`. Never derived from company data, never invented by the LLM. A template also names its own print stylesheet in `html_template` (`null` = JSON-only, renders no PDF), so which templates produce a PDF is a property of the template rather than a list maintained in the route.
_Avoid_: Schema (that's the Pydantic/ORM sense elsewhere in the codebase), Layout.

**Evidence** / **Caveat**:
The two kinds of `ResearchFact` a generation-time research run produces. **Evidence** states something that is the case — a number, a date, a named customer, a shipped capability. A **caveat** records an absence or a limit: something that could not be verified, or a boundary on what another fact supports. Only evidence ever reaches a writer; caveats go to the outline as judgement context and to the critique node as an accuracy check. Handing a copywriter a majority of negations under a "write only from these facts" instruction is what produced the pipeline's hedged-prose failure. See `docs/adr/0006-evidence-caveat-split.md`.
_Avoid_: "negative fact" (a caveat is not a fact about the company, it is a fact about the research), Limitation.

**Palette**:
The derived print colour set (`app/palette.py`) — paper, ink, brand tint/rule/text, accent — computed from a `PdfDigest`'s two detected brand hexes. Distinct from **theme**, which is the raw `primary_color`/`accent_color`/`font_family` triple carried on the template and the article. A theme is the input; a palette is what a page is allowed to paint with, and no detected colour is ever painted across a full page.
_Avoid_: Theme (that's the raw detected triple), Brand colors (ambiguous between the two).

**Article** / **GeneratedArticle**:
The generated JSON output for one sender/receiver/prompt/template combination. Records which `PdfDigest` (sender + receiver, nullable — a company can be generated for with zero PDF uploads) was the company's newest at generation time, so a past article stays traceable even after either company's context has since changed.
_Avoid_: Draft (that's the in-flight `ArticleSchema` before validation/repair; an Article/GeneratedArticle is the final persisted result), CompanyProfile version (retired terminology).
