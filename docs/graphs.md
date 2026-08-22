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

Runs on `POST /context`. `extract_pdf_structure` / `profile_company` fork depending on whether a PDF was uploaded — see `route_from_entry`.

![ingestion graph](graphs/ingestion_graph.png)

- `extract_pdf_structure` — parses the PDF (transcript, tables, image map). Skipped on a no-PDF (name/description-only) run.
- `create_digest_shell` — inserts the `PdfDigest` row early so `describe_image` has a `pdf_digest_id` to scope `Image` rows to this run.
- `ingestion_agent` — agentic loop (`app/graphs/ingestion_agent.py`) merging text + image summaries into a digest.
- `persist_digest` — writes `digest_text`/`key_facts`/`document_type` back onto the `PdfDigest` row.
- `profile_company` — LangChain deep-research agent (`app/deep_research.py`), web-search-grounded; entry point directly on a no-PDF run.
- `persist_profile` — writes the new `CompanyProfile` version (never overwrites a prior one).

## Generation graph (`app/graphs/generation_graph.py`)

Runs on `POST /generate`. `validate` loops back through `repair` up to `MAX_REPAIR_ATTEMPTS` (2) before falling through to `select_assets`.

![generation graph](graphs/generation_graph.png)

- `load_profiles` — loads latest sender/receiver `CompanyProfile` rows.
- `build_prompt` — assembles the system prompt from profiles, template word-limit/section constraints, and the user prompt.
- `generate_draft` — clean model instance (no tools bound), structured output into `ArticleSchema`.
- `validate` — checks word limits/min-lengths per field and required image slots.
- `repair` — rewrites only the fields that failed validation; loops back to `validate`.
- `select_assets` — truncates any field still over limit after repair attempts are exhausted, matches image placeholders to extracted `Image` rows or falls back to a stock-query hint.
