# Automated Generative Marketing Collateral — Build Spec

Prepared for: ML6 Senior AI Engineer Challenge (Case: "Automated Generative Marketing Collateral")
Purpose: hand this file to Claude Code as the build spec for a working prototype + design.

---

## 1. Context (from the challenge brief)

Customer C is a marketing automation agency that creates personalized B2B marketing materials (newsletters, brochures) that bridge a **Sender** company (e.g. a software vendor) and a **Receiver** company (e.g. a target account) they're pitching. Today this is manual: research both companies, write a bridging article, gather visual assets, and hand-format everything into a strict publishing template.

We are automating this with GenAI. The system takes PDF context on both companies, uses an LLM to write a tailored, factually-grounded article, and outputs structured **JSON** that maps into a pre-defined layout template (respecting word limits, image placeholders, and theme colors defined by that template).

This spec covers: (a) the technical design (orchestration + cloud architecture), and (b) a lightweight, runnable prototype backend with exactly two HTTP endpoints, per the brief's deliverable requirements.

---

## 2. Scope

### In scope (build this)
- FastAPI backend, Python.
- `POST /context` — upload a PDF as context for a company (sender or receiver role).
- `POST /generate` — generate a tailored article as structured JSON for a sender+receiver pair.
- PDF text/table/image extraction on ingestion.
- Ingestion-time company profiling (LLM summarizes each uploaded PDF into a structured profile once, cached, reused across every `/generate` call involving that company).
- LangGraph-orchestrated generation pipeline: retrieve context → build prompt → generate structured output → validate word limits → repair loop if needed → return.
- Provider-agnostic LLM layer via LangChain (`init_chat_model` / `with_structured_output`), configurable via env var — not hardcoded to one vendor.
- One example pre-defined layout template (`config/templates/b2b_newsletter_v1.json`) with concrete word limits, image slots, and theme colors — treated as a fixed input contract, not something the LLM invents.
- SQLite persistence (via SQLModel or plain SQLAlchemy) for company records, extracted context, and generated outputs.
- Dockerfile + docker-compose for one-command local run.
- Cloud architecture design for AWS (design doc only, not deployed).
- README with setup/run instructions and example `curl` calls for both endpoints.
- Git init + initial commit (candidate pushes to their own private GitHub repo afterward).

### Explicitly out of scope (call this out in the presentation, don't build it)
- Actually rendering the final newsletter/brochure (InDesign/Figma/HTML rendering of the JSON) — the deliverable is the JSON contract only.
- Vector search / embeddings / RAG — with 1-2 PDFs per company, full text fits in context; note this as a deliberate scale-dependent decision, and describe the upgrade path (chunking + embeddings) in the design doc for when PDF volume grows.
- Auth/multi-tenancy enforcement (mention as a production concern in the design doc; not implemented).
- Automated test suite (explicitly deprioritized for the time budget — if time remains, add 2-3 smoke tests for the two endpoints, but do not block on this).
- Real image generation — image placeholders reference extracted assets or a "stock_query" hint, never actually call an image-gen model.
- Frontend UI — `curl`/Postman/OpenAPI docs (`/docs` via FastAPI) is the demo surface.

---

## 3. Orchestration Design (LangGraph)

Two flows, both defined as LangGraph graphs so the orchestration is explicit and inspectable (this is also a natural fit for the "orchestration" requirement in the brief).

### 3.1 Ingestion graph (triggered by `POST /context`)

```
[extract_pdf_structure] -> [ingestion_agent] -> [persist_digest] -> [profile_company] -> [persist_profile]
                                 |  ^
                        describe_image tool
                          (vision subagent)
```

- `extract_pdf_structure`: **deterministic**, no LLM. Use PyMuPDF's block-level extraction (`page.get_text("dict")["blocks"]`) to walk the document in reading order. For each **text block**, append its text to a running transcript. For each **image block**, assign it a stable id (`img_{page:02d}_{index:02d}`), extract the raw bytes (`page.get_images()` + `doc.extract_image(xref)`) to `data/assets/{company_id}/{image_id}.png`, and insert an inline marker `[[IMAGE:img_03_01]]` into the transcript at that exact position. Output: an **annotated transcript** (text interleaved with image markers, preserving document order) + a map of `image_id -> file_path`. This transcript, not raw unmarked text, is what downstream steps consume.
- `ingestion_agent`: a **tool-calling agent** (LangGraph, e.g. built on `create_react_agent` or an equivalent custom tool loop), not a single LLM call. Input: the annotated transcript from `extract_pdf_structure`. System prompt: read the transcript in order; whenever you reach an `[[IMAGE:...]]` marker for an image that looks materially relevant to understanding this company (skip obvious decorative/background images if you can tell from context), call the `describe_image` tool to find out what's in it before continuing; once you've reviewed the document, call `submit_digest` with your final structured summary — that is the only way this agent loop ends. It has exactly two tools:
  - **`describe_image(image_id: str, context_hint: str)`** — this is the **vision subagent**: it loads the image bytes for `image_id`, downsizes them if needed (cap longest edge ~1568px, per Anthropic's guidance, to bound token cost), and makes a *separate*, tightly-scoped Claude call with the image as a native multimodal content block plus `context_hint` (the surrounding transcript text passed in by the calling agent, so the subagent knows what it's looking for). The subagent's job is narrow: describe what's factually visible — image type (logo / product screenshot / chart / diagram / photo / other), any visible text or numbers, and a one/two-sentence summary of relevance to a company profile. It returns this description as plain text back to the `ingestion_agent` as the tool result, **and** persists it to the `Image` table immediately (so a description is never lost even if the outer agent loop later fails) — the tool call itself, not the agent's final digest, is the durability boundary. Calls are idempotent/cached per `image_id`: a repeat call for an already-described image returns the cached row instead of re-invoking the vision subagent. Hard cap of 15 `describe_image` calls per document to bound cost/latency — if hit, the agent must proceed to `submit_digest` with what it has and note the cap was hit.
  - **`submit_digest(digest: PdfDigestSchema)`** — the agent's only way to terminate the loop; forces structured output (fields: `digest_text` — a comprehensive prose summary merging text + described image content, `key_facts` — list of short factual bullets, `document_type` — e.g. "company one-pager", "product brochure") rather than letting the agent end on free text.
- `persist_digest`: write the `PdfDigest` row (from `submit_digest`'s payload) and confirm all `Image` rows from this run are committed.
- `profile_company`: LLM call with **Claude's native web search tool** bound, structured output, producing a `CompanyProfile` (see §5.2) — offerings, industry, target pain points, tone signals. Input is the `PdfDigest` (not raw PDF text — the digest already folds in the image descriptions, so this step reasons over a clean, image-aware summary rather than re-reading marker tokens). The model is explicitly permitted to use web search to supplement/verify the digest (e.g. confirm current positioning, recent news, industry context not present in the PDF). This directly automates the case's manual "Research: visit both companies' websites" step — worth calling out explicitly in the presentation as a 1:1 mapping to a named pain point. Capture every URL the model cites into `CompanyProfile.web_sources` for traceability. Done once per upload so `/generate` never re-parses raw PDF text, re-describes images, or re-searches the web on every generate call.
- `persist_profile`: write `Company` and `CompanyProfile` rows to SQLite (the `Image` rows were already persisted per-call by the `describe_image` tool, and `PdfDigest` by `persist_digest`).

**Why an agent here rather than a fixed pipeline:** the set of images worth inspecting varies per document (a one-pager might have zero relevant images, a brochure might have six), so a fixed "describe every image" or "describe no images" rule is wrong in one direction or the other. Letting the ingestion agent decide, per document, which markers are worth a `describe_image` call is the actual reason this step is agentic rather than a straight-line function chain — call this out explicitly as a design decision, mirroring the repair-loop rationale in §3.2.

### 3.2 Generation graph (triggered by `POST /generate`)

```
[load_profiles] -> [build_prompt] -> [generate_draft] -> [validate] --(fail)--> [repair] -> [validate]
                                                              |
                                                           (pass)
                                                              v
                                                          [finalize_article] -> [return]
```

- `load_profiles`: fetch sender + receiver `CompanyProfile` from SQLite by id (404 if either is missing — see §5, error cases). Also loads the sender's described `Image` rows (latest ingestion run only) into an aliased asset catalog (`A1`, `A2`, …) for the draft prompt — sender assets only, so the receiver's logo can never land in the sender's article.
- `build_prompt`: assemble system prompt containing the layout template's field constraints (word limits per section, required sections, tone guidance) + both company profiles (with `web_sources` stripped — the article can't cite URLs, so they'd be pure prompt noise) + the sender asset catalog (alias, tag, description per asset; an explicit "(none)" when empty) + the user's creative brief (`prompt` field from the request) + an explicit grounding instruction ("only state claims supported by the provided company context; do not invent facts, statistics, or claims about either company").
- `generate_draft`: single LLM call with **enforced structured output** against the `Article` Pydantic schema (via `with_structured_output`) — never parse freeform text. Deliberately **no web search tool bound here** — this call is grounded strictly to the already-vetted `CompanyProfile` (which itself was researched once, with web search, at ingestion time). Keeping research and writing as separate steps means every article generated for the same company draws from one consistent, auditable pool of facts rather than re-researching (and potentially getting different answers) on every request.
- `validate`: pure Python, no LLM — check every section's word count against the template's `max_words`/`min_words`. Also check every declared image slot has an entry and that any chosen `asset_alias` exists in the sender asset catalog.
- `repair` (conditional edge, max 2 attempts): append ONE follow-up turn to the same draft conversation, batching every current violation (word limits, missing sections/slots, invalid asset aliases), and have the model re-emit the full corrected article ("fix only the listed problems; copy every other field verbatim"). Keeping repairs as appended turns makes the large profile prompt a stable prefix, so each repair round is an OpenAI prompt-cache hit rather than a fresh full-price prompt. After 2 failed attempts, hard-truncate word-limit violations at the word boundary and flag `"truncated": true` on that field in the response, and drop any still-invalid asset alias to a stock-query fallback — never silently exceed the contract, never loop forever.
- `finalize_article`: the draft model itself picked an `asset_alias` per `image_placeholders` slot from the sender asset catalog in its prompt (hero slot instructed to prefer a logo-tagged asset); this node just resolves alias → `Image.id`, and falls back to `"source_hint": "stock_query"` (query = the slot's alt text) when the model picked none or the alias is still invalid after repairs.
- `return`: assemble and return the final JSON (see §5.2 response shape).

Note in the presentation: the repair loop and the "check don't trust" validation step are the main things distinguishing this from a naive "call the LLM once and hope" implementation — call this out explicitly as a design decision.

---

## 4. Data Model (SQLite)

```
Company
  id (pk, str, e.g. "sender_acme" or generated uuid)
  role            enum: "sender" | "receiver"
  name            str (nullable until profile extracted)
  raw_text        text   (full annotated transcript, incl. [[IMAGE:id]] markers — for traceability/debugging)
  tables_json     text   (extracted tables, JSON-serialized)
  created_at      datetime

PdfDigest
  id (pk)
  company_id (fk -> Company.id)
  digest_text       text   (ingestion_agent's merged text+image summary, via submit_digest)
  key_facts         text (JSON list of short factual bullets)
  document_type     str
  images_reviewed   int    (how many describe_image calls this run made)
  images_cap_hit    bool   (true if the 15-image cap was reached)
  created_at        datetime

Image
  id (pk)
  company_id (fk -> Company.id)
  image_id          str    (stable id from extraction, e.g. "img_03_01")
  file_path         str
  page_number       int
  description       text   (vision subagent output, from describe_image)
  tag               enum: "logo" | "product_image" | "chart" | "generic"  (derived from the subagent's description)
  created_at        datetime

CompanyProfile
  company_id (fk -> Company.id)
  offerings         text
  industry          str
  pain_points       text (JSON list)
  tone_signals      text
  summary           text
  web_sources       text (JSON list of {url, note} cited by Claude's web search during profiling)
  created_at        datetime

GeneratedArticle   (optional but cheap to add — persist generation history for demo/debugging)
  id (pk)
  sender_id (fk)
  receiver_id (fk)
  prompt            text
  template_id       str
  result_json       text
  created_at        datetime
```

---

## 5. API Spec

### 5.1 `POST /context`

Upload a PDF as context for one company.

**Request:** `multipart/form-data`
| field | type | required | notes |
|---|---|---|---|
| `company_id` | str | no | if omitted, server generates one and returns it |
| `role` | str | yes | `"sender"` or `"receiver"` |
| `name` | str | no | display name; can also be inferred by the profiling step |
| `file` | file | yes | PDF |

**Response `200`:**
```json
{
  "company_id": "sender_acme",
  "role": "sender",
  "page_count": 3,
  "images_extracted": 2,
  "images_described": 2,
  "digest_preview": "Acme Corp one-pager: AI-driven fleet optimization platform. Page 2 includes a product screenshot of the live routing dashboard and the company logo on page 1...",
  "profile_summary": "Acme Corp is a logistics-focused AI software vendor offering real-time fleet optimization...",
  "web_sources": ["https://acme.example.com/product", "https://acme.example.com/news/series-b"]
}
```
Note: this endpoint now runs the full ingestion chain synchronously (extraction → ingestion agent incl. any `describe_image` vision calls → digest persist → web-search-grounded profiling → profile persist) before responding — see the latency callout in §12.

**Error cases:** `400` non-PDF file, `422` missing/invalid `role`, `500` extraction failure (with a clear message — don't swallow parser errors).

### 5.2 `POST /generate`

Generate a tailored article as structured JSON for a sender/receiver pair.

**Request:** `application/json`
```json
{
  "sender_id": "sender_acme",
  "receiver_id": "receiver_globex",
  "prompt": "Focus on how Acme's real-time fleet optimization reduces fuel costs for large logistics operators.",
  "template_id": "b2b_newsletter_v1"
}
```
`template_id` optional, defaults to `b2b_newsletter_v1`.

**Response `200`** (shape driven by the template, see §6):
```json
{
  "template_id": "b2b_newsletter_v1",
  "headline": "Cut Fuel Costs at Scale with Real-Time Fleet Intelligence",
  "subheadline": "How Acme's AI platform helps logistics operators like Globex optimize every mile",
  "sections": [
    {
      "id": "body_intro",
      "text": "...",
      "word_count": 58,
      "max_words": 60,
      "truncated": false
    },
    {
      "id": "body_value",
      "text": "...",
      "word_count": 112,
      "max_words": 120,
      "truncated": false
    }
  ],
  "pull_quote": "...",
  "cta": "See how Acme can cut your fleet's fuel spend — book a demo.",
  "image_placeholders": [
    {"slot": "hero", "source_hint": "asset", "asset_id": 14, "alt_text": "Acme Corp logo"},
    {"slot": "section_1", "source_hint": "stock_query", "alt_text": "logistics fleet trucks highway aerial"}
  ],
  "theme": {"primary_color": "#0B5FFF", "accent_color": "#FFB020"},
  "grounding_notes": "All claims sourced from sender/receiver uploaded context; no unverified figures included."
}
```

**Error cases:** `404` if `sender_id` or `receiver_id` not found (name which one), `422` invalid `template_id`, `502` if the LLM call fails after retries — return a clear error, don't return partial/malformed JSON.

---

## 6. Example Layout Template

File: `config/templates/b2b_newsletter_v1.json` — represents a pre-defined publishing template as Customer C's design team would hand it over (fixed constraints, not derived from the PDFs).

```json
{
  "template_id": "b2b_newsletter_v1",
  "fields": {
    "headline": {"max_words": 12},
    "subheadline": {"max_words": 20},
    "sections": [
      {"id": "body_intro", "min_words": 40, "max_words": 60, "guidance": "Hook connecting receiver's industry challenge to sender's offering"},
      {"id": "body_value", "min_words": 80, "max_words": 120, "guidance": "Concrete value proposition, grounded in provided context only"}
    ],
    "pull_quote": {"max_words": 25},
    "cta": {"max_words": 15}
  },
  "image_slots": ["hero", "section_1"],
  "theme": {"primary_color": "#0B5FFF", "accent_color": "#FFB020"}
}
```

Load this generically (don't hardcode field names in Python logic) so adding a second template later is a config change, not a code change — worth a one-line mention in the presentation as a scalability decision.

---

## 7. LLM / Structured Output

- Use LangChain's model-agnostic `init_chat_model`, selected via env var `LLM_PROVIDER` (`anthropic` | `openai` | `google_genai`, etc.) + corresponding API key env var, so the architecture remains portable. **For now, `LLM_PROVIDER=anthropic` is the active default** — this is the model the prototype is actually built and demoed against.
- `ingestion_agent` (§3.1) is a **tool-calling Claude agent**, not a single completion — bound to exactly two tools, `describe_image` and `submit_digest` (see §3.1 for both). Implement as a LangGraph tool-calling loop (`create_react_agent` or an equivalent explicit loop node) rather than `.with_structured_output`, since it needs to interleave multiple tool calls before producing its final structured payload.
- `describe_image`'s vision subagent call is a **separate, narrowly-scoped Claude call** — same model family (Claude is natively multimodal, so this isn't a different "vision model," just a distinct, tightly-prompted call kept separate from the main agent's context for cleanliness and cost control), given the image as a base64 content block plus the `context_hint` text, returning a short structured/plain-text description. Treat it as a true subagent: it doesn't share the ingestion agent's conversation history, only receives what it needs for that one image.
- `profile_company` (§3.1) uses **Claude with Anthropic's server-side web search tool bound** (`ChatAnthropic` with the `web_search` tool via LangChain's tool binding, or the Anthropic SDK directly if that's more reliable at implementation time), consuming the `PdfDigest` (not raw text) as input. This is a deliberate, named exception to the provider-agnostic design: web search is an Anthropic-specific server tool, so swapping `LLM_PROVIDER` later would require an equivalent search-tool binding for that provider (e.g. a Tavily/Brave search tool call) — call this tradeoff out explicitly rather than pretending it's fully portable today.
- `generate_draft` (§3.2) has no search tool bound — see §3.2 for why — and uses `.with_structured_output(ArticleSchema)` (Pydantic model mirroring §5.2's response, minus fields computed in code like `word_count`/`truncated`). Never regex/parse freeform text.
- `ArticleSchema`, `PdfDigestSchema`, and `CompanyProfileSchema` are Pydantic models shared between the LangGraph nodes and the FastAPI response models — single source of truth.
- System prompt for `generate_draft` must explicitly state the word-limit constraints per field (even though they're also validated in code) and the grounding instruction (no claims beyond the provided `CompanyProfile`).

---

## 8. PDF Parsing

- Library: **PyMuPDF (`fitz`)** — handles text, embedded images, and reasonable table-ish text extraction in one dependency; faster than pdfplumber for image extraction.
- Extract, in reading order, an **annotated transcript**: page/text blocks concatenated in order, with each embedded image replaced inline by a `[[IMAGE:img_id]]` marker at the position it appears (see §3.1's `extract_pdf_structure`). Images themselves are saved to `data/assets/{company_id}/{image_id}.png`, separate from the text.
- Also extract a naive table pass (optional — note as a known limitation if not implemented; complex tables are a stretch goal, not core).
- No heuristic image tagging at extraction time — that used to be a cheap position-based guess (first image on page 1 = logo); it's now superseded by the ingestion agent's actual `describe_image` vision calls (§3.1/§7), which produce a real description instead of a guess. Extraction only needs to assign stable `image_id`s and record file paths + page numbers; classification into `tag` happens downstream from the description text.

---

## 9. Tech Stack

- Python 3.11+, FastAPI, Uvicorn
- LangChain + LangGraph (orchestration, tool-calling agent loop, structured output)
- Anthropic Claude as the active model (`LLM_PROVIDER=anthropic`): the native web search tool bound for `profile_company`, and native multimodal image input for the `describe_image` vision subagent — both Anthropic-specific capabilities, called out as such in §7
- Pillow (`PIL`) for downsizing images before they're sent to the vision subagent (cost/token control, see §3.1)
- PyMuPDF (`pymupdf`) for PDF parsing
- SQLAlchemy or SQLModel + SQLite (file-based, `data/app.db`)
- Pydantic v2 for all schemas
- python-multipart (FastAPI file upload dependency)
- python-dotenv for local env var loading

---

## 10. Repo Structure

```
.
├── README.md
├── spec.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── config/
│   └── templates/
│       └── b2b_newsletter_v1.json
├── app/
│   ├── main.py                # FastAPI app, route registration
│   ├── models.py               # SQLAlchemy/SQLModel ORM models
│   ├── schemas.py               # Pydantic request/response + LLM structured-output schemas
│   ├── db.py                    # SQLite session setup
│   ├── pdf_extraction.py        # PyMuPDF parsing -> annotated transcript + saved images
│   ├── vision.py                 # describe_image vision subagent (scoped Claude call)
│   ├── templates.py              # load/validate layout template configs
│   ├── graphs/
│   │   ├── ingestion_agent.py    # LangGraph tool-calling agent: describe_image + submit_digest tools
│   │   ├── ingestion_graph.py    # LangGraph: extract_pdf_structure -> ingestion_agent -> persist_digest -> profile_company -> persist_profile
│   │   └── generation_graph.py   # LangGraph: load_profiles -> ... -> return
│   └── routes/
│       ├── context.py            # POST /context
│       └── generate.py           # POST /generate
├── data/                          # gitignored: sqlite db + extracted assets
└── docs/
    ├── architecture.md            # cloud architecture design (AWS) — see §11
    └── diagram.png / .drawio      # architecture diagram for the presentation
```

---

## 11. Cloud Architecture (AWS) — Design Doc Content

Write this up in `docs/architecture.md`, and produce one diagram from it for the presentation slide. Not deployed — design only.

- **API layer:** API Gateway → Lambda (or Fargate if cold-start/latency from LangGraph deps becomes an issue — note this as a decision point, Fargate is likely the safer real answer given LangChain's import weight).
- **Storage:** S3 for raw PDFs + extracted images, one prefix per company (`s3://.../companies/{company_id}/`).
- **Metadata store:** DynamoDB, `Company` and `CompanyProfile` tables keyed by `company_id` (partition key) — swap-in replacement for the prototype's SQLite tables, same shape.
- **LLM:** Amazon Bedrock (model-agnostic within Bedrock's catalog) — matches the provider-agnostic LangChain design already in the prototype for the `generate_draft` step, so swapping local API keys for Bedrock there is a config change, not a rewrite. **Caveat to flag explicitly in the presentation:** Bedrock's Claude does not expose the same native server-side web search tool used in `profile_company` (§3.1/§7) on the direct Anthropic API. Production options: (a) call the direct Anthropic API specifically for the profiling step while using Bedrock for everything else, or (b) bind an external search tool (Tavily/Brave/SerpAPI) to the Bedrock-hosted model to replicate the same research behavior. Either way, decide and document this rather than silently losing the research capability on a Bedrock migration. Mention **prompt caching** for the company profiles (reused across many sender×receiver generate calls) as a concrete cost lever.
- **Async ingestion:** S3 upload event → SQS → Lambda worker runs the ingestion graph (decouples slow PDF/LLM profiling work from the upload request, so `/context` can return fast with a "processing" status if this were productionized — for the prototype itself, ingestion stays synchronous for simplicity, call this out as the first thing to change for production).
- **Orchestration at scale:** Step Functions wrapping the LangGraph-equivalent steps if the pipeline needs cross-service coordination (e.g., a human approval step before publishing) — note this as the natural evolution path, not needed at current scope.
- **Security:** S3 bucket encryption at rest (SSE-KMS), per-tenant IAM scoping/prefix isolation since uploaded PDFs are real client business documents, VPC endpoints for Bedrock/S3/DynamoDB to avoid public internet egress.
- **Observability:** CloudWatch structured logs, and specifically log the `validate`/`repair` outcomes (how often articles need repair) as a product-quality metric worth tracking over time.
- **Cost drivers:** LLM calls dominate. Two levers: (1) profile-once-reuse-many company profiling (already in the design), (2) prompt caching on the (large, repeated) company profile context across generate calls for the same company.

---

## 12. Non-Functional Notes to Call Out in the Presentation

- Deliberately no RAG/vector DB at this scale — state why, and the trigger condition for adding it (PDF context exceeding a practical token budget, or many PDFs per company).
- Grounding is instruction-based only (no separate fact-checking pass) — flag as a known limitation and the natural next step (a verification LLM call cross-checking generated claims against source text).
- Word-limit repair loop caps at 2 attempts then truncates — bounded cost/latency, never infinite-loops.
- Data isolation between different customers' uploaded PDFs is a real production concern given this touches real client business data — not enforced in the prototype (SQLite, no auth) but explicitly named as the first thing to add before this touches real customer data.
- `POST /context` is now noticeably heavier: extraction + an agentic tool loop (each `describe_image` call is its own Claude round trip) + web-search-grounded profiling, all synchronous. For a document with several images this could take tens of seconds — expected and fine for a demo, but explicitly flag it as the concrete justification for the async S3→SQS→Lambda ingestion design in §11 rather than something to silently accept in production.
- The `describe_image` cap (15 images/document) and per-image downsizing (§3.1/§9) are the two deliberate cost/latency bounds on the vision subagent — mention both by name if asked "what stops this from being expensive on a 50-page PDF."

---

## 13. Setup & Deliverable Checklist (for Claude Code)

1. Scaffold repo per §10.
2. Implement `POST /context` (§5.1) with PyMuPDF extraction (§8) + the full ingestion LangGraph (§3.1): `extract_pdf_structure` → `ingestion_agent` (tool-calling agent with `describe_image` vision subagent + `submit_digest`) → `persist_digest` → `profile_company` (Claude web search tool bound) → `persist_profile`.
3. Implement `POST /generate` (§5.2) with generation LangGraph incl. repair loop (§3.2).
4. Add `config/templates/b2b_newsletter_v1.json` (§6).
5. Dockerfile + docker-compose for one-command run (`docker compose up`).
6. `.env.example` listing `LLM_PROVIDER=anthropic` (default/active), `ANTHROPIC_API_KEY` (required now; must have web search tool access on the account), plus `OPENAI_API_KEY`/`GOOGLE_API_KEY` as unused placeholders for the provider-agnostic path — real PDFs and real API key(s) to be supplied by the candidate, not committed.
7. README: setup instructions, how to run locally (with and without Docker), and example `curl` calls for both endpoints using placeholder PDFs.
8. `docs/architecture.md` per §11.
9. `git init` + initial commit. (Candidate creates the private GitHub repo and pushes themselves — not part of this build step.)
10. If time remains after the above: 2-3 smoke tests (upload → generate happy path, missing-company 404, word-limit repair triggering, `describe_image` cache hit on a repeated image id). Do not let this block the core deliverable.
