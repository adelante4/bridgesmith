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
through `revise` (at most `MAX_REVISE_ATTEMPTS`, 1) before `validate`; `validate` loops back through `repair`
(at most `MAX_REPAIR_ATTEMPTS`, 2) before falling through to `finalize_article`. See
`docs/adr/0003-generation-research-and-storm-drafting.md`, `docs/adr/0004-perspective-guided-research.md`, and
`docs/adr/0005-decouple-context-from-research.md`.

![generation graph](graphs/generation_graph.png)

- `load_profiles` — no LLM call: builds each company's `sender_context_blob`/`receiver_context_blob` (plain markdown concatenation of every `PdfDigest` + `Description` + the newest `ResearchRun`) and `sender_style`/`receiver_style` (`StyleBundle` deterministically read off the company's *newest* `PdfDigest` — tone/design/brand; null-safe, no fallback to older digests).
- `plan_research` — STORM perspective-guided question asking: one no-tool LLM call derives 2-3 perspectives of people who will judge this article (e.g. budget decision-maker at the receiver, technical evaluator, sender proof-point verifier) with 2-4 searchable questions each — explicitly excluding anything the ingestion-time profiles already answer.
- `research` — one `deepagents.create_deep_agent` (same idiom as `app/deep_research.py`) with a `web_search` tool and a `fact-finder` sub-agent, budgeted in-prompt to ~6 searches, answers the perspective questions. Structured output is a list of facts with source URLs — the research pass and the "compress" step are the same call: the agent's final structured answer *is* the cleaned fact list.
- `outline` — LLM call mapping template slots (headline, each section, pull quote, CTA) to a specific subset of the researched facts, before any prose exists.
- `draft_sections` — one structured-output call per template unit (headline+subheadline together, each body section, pull_quote+cta together), each fed only its outline-assigned facts and the receiver's tone_signals.
- `polish` — assembles the drafted pieces into one `ArticleSchema`: smooths transitions, dedupes, strengthens the lead, picks image placeholder asset aliases, and lists the `sources` actually used. Keeps a `polish_messages` conversation for `revise` to extend.
- `critique` — rubric LLM call (fact grounding, personalization, tone match, structure) producing concrete `required_edits`.
- `revise` — appends one turn to `polish_messages` applying the critique's edits, re-emits the corrected `ArticleSchema`. Loops back to `critique`.
- `validate` — checks word limits/min-lengths per field and required image slots.
- `repair` — appends one turn to the draft conversation (continuing from `polish_messages`) batching every current violation (word limits, missing sections/slots, invalid asset aliases) and re-emits the full corrected article; the stable conversation prefix makes each repair round a prompt-cache hit. Loops back to `validate`.
- `finalize_article` — truncates any field still over limit after repair attempts are exhausted, resolves the draft's chosen asset aliases to `Image` rows (sender assets only; the model picks from a catalog in its prompt) or falls back to a stock-query hint.
