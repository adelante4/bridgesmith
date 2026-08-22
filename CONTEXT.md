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
The record of one ingestion run: the annotated transcript (text + `[[IMAGE:id]]` markers), extracted tables, and the ingestion agent's merged text+image summary (`digest_text`, `key_facts`, `document_type`) for one PDF upload. A Company accumulates one `PdfDigest` per upload — this is where per-run artifacts live, not on Company.
_Avoid_: Transcript (too narrow — a digest includes tables and image summaries, not just text), Summary (ambiguous with CompanyProfile.summary).

**CompanyProfile**:
A versioned, web-search-augmented research profile for a company, produced once per ingestion run from that run's `PdfDigest`. Every upload creates a new `CompanyProfile` version; `/generate` always reads the latest version for a company. Never mutated in place.
_Avoid_: "the" profile (there can be several, ordered by recency), Digest (a profile is Digest + web research, not the same thing).

**Image**:
An extracted image asset, scoped to the specific `PdfDigest` run that produced it — not a company-wide flat pool. `/generate`'s asset matching only considers images from a company's *latest* run, consistent with always using the latest `CompanyProfile`.
_Avoid_: Asset (used loosely elsewhere for "extracted image or stock query hint" generically — Image specifically means a row with a `file_path`).

**Template**:
A fixed publishing-layout contract (word limits, image slots, theme colors) supplied by Customer C's design team, loaded from `config/templates/*.json`. Never derived from company data, never invented by the LLM.
_Avoid_: Schema (that's the Pydantic/ORM sense elsewhere in the codebase), Layout.

**Article** / **GeneratedArticle**:
The generated JSON output for one sender/receiver/prompt/template combination. Records exactly which `CompanyProfile` version (sender + receiver) it was grounded in, so a past article stays traceable even after either company's profile has since changed.
_Avoid_: Draft (that's the in-flight `ArticleSchema` before validation/repair; an Article/GeneratedArticle is the final persisted result).
