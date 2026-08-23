# LangGraph Agent Graphs

Rendered from the compiled graphs (`.get_graph().draw_mermaid_png()`), not hand-drawn — regenerate after any node/edge change:

```
.venv/bin/python -c "
from app.graphs.generation_graph import build_generation_graph
from app.graphs.ingestion_graph import build_ingestion_graph
open('docs/graphs/generation_graph.png','wb').write(build_generation_graph(None).get_graph().draw_mermaid_png())
open('docs/graphs/ingestion_graph.png','wb').write(build_ingestion_graph(None).get_graph().draw_mermaid_png())
"
```

## Ingestion graph (`app/graphs/ingestion_graph.py`)

Runs on `POST /context`, only when a PDF is attached — a description-only "add context" call skips this graph
entirely (just inserts a `Description` row). Deep-research (web search) is a fully separate action
(`POST /context/{company_id}/research`, `app/deep_research.py`) never invoked from here — see
`docs/adr/0005-decouple-context-from-research.md`.

![ingestion graph](graphs/ingestion_graph.png)

- `extract_pdf_structure` — parses the PDF (transcript, tables, image map).
- `create_digest_shell` — inserts the `PdfDigest` row early so `describe_image` has a `pdf_digest_id` to scope `Image` rows to this run.
- `ingestion_agent` — agentic loop (`app/graphs/ingestion_agent.py`) merging text + image summaries into a digest, plus `tone_signals` (writing/voice tone read from the document's own text).
- `persist_digest` — writes `digest_text`/`key_facts`/`document_type`/`tone_signals` back onto the `PdfDigest` row.
- `extract_brand` — deterministic in shape, not agentic: renders the PDF's first 1-2 pages to images, one vision call for `primary_color`/`accent_color`/`design_notes`, plus embedded-PDF-metadata font detection (no LLM) for `brand_font_family`. Writes all four onto the same `PdfDigest` row.

## Generation graph (`app/graphs/generation_graph.py`)

Runs on `POST /generate`. STORM trimmed to its core: perspectives generate the questions, questions drive the
searches; outline before writing, draft section by section, then polish/critique/validate. `critique` loops back
through `revise` (at most `MAX_REVISE_ATTEMPTS`, 2) before `validate`; `validate` loops back through `repair`
(at most `MAX_REPAIR_ATTEMPTS`, 2) before falling through to `finalize_article`. See
`docs/adr/0003-generation-research-and-storm-drafting.md`, `docs/adr/0004-perspective-guided-research.md`,
`docs/adr/0005-decouple-context-from-research.md`, and `docs/adr/0006-evidence-caveat-split.md`.

Two invariants run through the whole graph, both added after the first end-to-end run came out as hedged,
evidence-free prose (ADR 0006):

1. **Only `evidence` facts reach a writer.** The researcher also records `caveat` facts — things it could not
   verify. Those go to `outline` as judgement context and to `critique` as an accuracy check, never into a
   "write only from these facts" prompt.
2. **Anything that comments on the article is written after it.** `draft_sections` writes the body first, then
   hands the finished body to the headline and pull-quote/CTA writers.

![generation graph](graphs/generation_graph.png)

- `load_profiles` — no LLM call: builds each company's `sender_context_blob`/`receiver_context_blob` (plain markdown concatenation of every `PdfDigest` + `Description` + the newest `ResearchRun`) and `sender_style`/`receiver_style` (`StyleBundle` deterministically read off the company's *newest* `PdfDigest` — tone/design/brand; null-safe, no fallback to older digests).
- `plan_research` — STORM perspective-guided question asking: one no-tool LLM call derives 2-3 perspectives of people who will judge this article (e.g. budget decision-maker at the receiver, technical evaluator, sender proof-point verifier) with 2-4 searchable questions each — explicitly excluding anything the ingestion-time profiles already answer.
- `research` — one `deepagents.create_deep_agent` (same idiom as `app/deep_research.py`) with a `web_search` tool and a `fact-finder` sub-agent, budgeted in-prompt to ~6 searches, answers the perspective questions. Structured output is a list of facts with source URLs — the research pass and the "compress" step are the same call: the agent's final structured answer *is* the cleaned fact list.
- `outline` — LLM call mapping each body section to a specific subset of the researched **evidence**, before any prose exists. Caveats are shown as context but must never be assigned to a section. Every section is required to get at least one concrete fact, since anything left unassigned cannot appear in the article.
- `draft_sections` — body first: one structured-output call per body section, each fed only its outline-assigned facts, the sender/receiver briefs, and the **sender's** tone_signals (the sender is the one speaking; passing the receiver's tone stripped the client's voice out of its own brochure). Each section also drafts its own `heading` when the template sets `max_heading_words`. The finished body text is then passed to two further calls — headline+subheadline, and pull_quote+cta — which read the real article rather than an outline angle. Writers mark their strongest measured result with a two-marker Markdown subset (`app/richtext.py`) that the print template renders as emphasis.
- `polish` — assembles the drafted pieces into one `ArticleSchema`: smooths transitions, dedupes, strengthens the lead, picks image placeholder asset aliases, and lists the `sources` actually used. Keeps a `polish_messages` conversation for `revise` to extend.
- `critique` — rubric LLM call (fact grounding, personalization, tone match, specificity, structure) producing `required_edits`, each tagged `blocking` or `advisory`. It is explicitly forbidden from asking for a hedge or qualifier on a claim the research already supports — that instruction is what the rubric previously rewarded.
- `revise` — branches from `polish_base_messages` (the *original* polish turn, not the previous revision) applying only the blocking edits, and re-emits the corrected `ArticleSchema`. Loops back to `critique`. The loop stops when the lowest rubric score clears `REVISE_SCORE_FLOOR`, when no blocking edits remain, or at the attempt cap — gating on "the critic still has an opinion" never terminates.
- `validate` — no LLM call: checks word limits/min-lengths per field, required image slots and per-slot asset eligibility, plus a concrete-anchor gate (every section must carry a number, date or name) and filler/hedge word detection. All checks run on markup-stripped text, since a word limit is a promise about what a reader sees.
- `repair` — appends one turn to the draft conversation (continuing from `polish_messages`) batching every current violation (word limits, missing sections/slots, invalid asset aliases) and re-emits the full corrected article; the stable conversation prefix makes each repair round a prompt-cache hit. Loops back to `validate`.
- `finalize_article` — truncates any field still over limit after repair attempts are exhausted, and resolves the draft's chosen asset aliases to `Image` rows (sender assets only; the model picks from a catalog in its prompt) or falls back to a stock-query hint. Identity slots (`hero`, `masthead`) additionally require `Image.is_own_brand`, so a customer's logo extracted from the sender's own deck can never stand in as the sender's mark — see `docs/adr/0007-brochure-v2-print-design.md`.
