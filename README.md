# Automated Generative Marketing Collateral

Backend that automates the manual process of researching a Sender and Receiver company and writing a bridging B2B marketing article: upload PDF context for each company, then generate a tailored article as structured JSON that maps onto a pre-defined publishing template (word limits, image slots, theme colors).

Full technical design: [`spec.md`](spec.md). Cloud architecture design: [`docs/architecture.md`](docs/architecture.md).

## Architecture in one paragraph

Two LangGraph-orchestrated pipelines. **Ingestion** (`POST /context`): deterministic PyMuPDF extraction of an image-annotated transcript → a tool-calling Claude agent that reads the transcript and selectively calls a vision subagent to describe relevant images → a structured digest → a LangChain deep research agent (`deepagents.create_deep_agent`, plans with a todo list, delegates to a company-research sub-agent bound to Claude's native web search) that produces a structured company profile, persisted once and reused by every future `/generate` call for that company. **Generation** (`POST /generate`): load both company profiles → build a grounded prompt against the template's constraints → generate structured output → validate word limits in code → targeted repair loop (max 2 attempts, then hard-truncate) → match image slots to extracted assets. See `spec.md` §3 and `app/deep_research.py` for the full node-by-node design and rationale.

## Prerequisites

- Python 3.12+ (or Docker)
- An Anthropic API key with web search tool access

## Setup (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY
```

Run:

```bash
uvicorn app.main:app --reload
```

The API is now at `http://localhost:8000`, interactive docs at `http://localhost:8000/docs`.

## Setup (with Docker)

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY
docker compose up --build
```

SQLite database and extracted PDF assets persist under `./data` on the host (volume-mounted). The backoffice UI comes up alongside the API at `http://localhost:8501`.

## Langfuse (local LLM tracing)

`docker compose up` also brings up a self-hosted Langfuse (v4) stack — `langfuse-web`/`langfuse-worker` + its postgres/clickhouse/redis/minio dependencies — vendored from [langfuse/langfuse's docker-compose.yml](https://github.com/langfuse/langfuse/blob/main/docker-compose.yml). `.env.example`'s `LANGFUSE_INIT_*` vars auto-provision a project matching `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` on first boot, so tracing works with no manual signup.

- UI: `http://localhost:3000` (login `dev@localhost` / `changeme123`, from `.env.example` — change these before using anywhere but a local box)
- Traces: every LLM/agent call in `app/vision.py`, `app/graphs/ingestion_agent.py`, `app/deep_research.py`, and `app/graphs/generation_graph.py` is wired with a Langfuse callback (`app/observability.py`) — hit `/context` or `/generate` and the trace shows up in the UI within a few seconds.
- Running `uvicorn` directly on the host (no Docker) instead? Still works — start just the Langfuse services with `docker compose up langfuse-web langfuse-worker postgres clickhouse redis minio`, then set `LANGFUSE_HOST=http://localhost:3000` in `.env`.
- Unset `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` to disable tracing entirely — `app/observability.py` no-ops when they're absent.

## Example usage

Upload context for a company. `role` isn't a request field — Company is role-independent; sender/receiver is just which slot you pass its `company_id` into on `/generate` (see `CONTEXT.md`):

```bash
curl -X POST http://localhost:8000/context \
  -F name="Acme Corp" \
  -F file=@path/to/acme_onepager.pdf
```

Response includes a generated `company_id` (e.g. `co_a1b2c3d4`) — save it. Repeat for the other company:

```bash
curl -X POST http://localhost:8000/context \
  -F name="Globex Inc" \
  -F file=@path/to/globex_brief.pdf
```

Generate a tailored article:

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "sender_id": "co_a1b2c3d4",
    "receiver_id": "co_e5f6g7h8",
    "prompt": "Focus on how Acme'\''s real-time fleet optimization reduces fuel costs for large logistics operators."
  }'
```

`template_id` is optional in the request body and defaults to `b2b_newsletter_v1` (see `config/templates/b2b_newsletter_v1.json`).

**Note on latency:** `/context` runs the full ingestion chain synchronously — extraction, an agentic tool loop (each image description is its own Claude round trip), and web-search-grounded profiling. For a document with several images this can take tens of seconds. This is expected for a demo; `docs/architecture.md` describes the async S3→SQS→Lambda design that would decouple this in production.

## Explicitly out of scope

Deliberately not built:

- Rendering the final newsletter/brochure (InDesign/Figma/HTML) — the deliverable is the JSON contract only.
- Vector search / embeddings / RAG — at 1-2 PDFs per company, full text fits in context. See `spec.md` §12 for the upgrade trigger.
- Auth / multi-tenancy enforcement.
- A full automated test suite (a few smoke tests are included, non-blocking — see below).
- Real image generation — placeholders reference extracted assets or a `stock_query` hint.

A minimal Streamlit backoffice is included for browsing stored companies and driving `/context`/`/generate` (see below) — `/docs` (FastAPI's OpenAPI UI) remains the API demo surface.

## Backoffice UI

A Streamlit app for browsing companies (profiles, PDF digests, extracted images), triggering `/context` uploads and `/generate` calls, and reviewing past generated articles — reads the SQLite DB directly and calls the API over HTTP.

```bash
uvicorn app.main:app --reload &      # API must be running for Generate/Upload pages
streamlit run backoffice/streamlit_app.py
```

Opens at `http://localhost:8501`. Set `API_URL` if the API isn't at `http://localhost:8000`; `DATA_DIR` must match the API's (default `data`).

## Tests

A couple of non-blocking smoke tests are included:

```bash
pytest tests/
```

## Repo structure

```
app/
  main.py               FastAPI app, route registration
  models.py              SQLModel ORM models
  schemas.py              Pydantic request/response + LLM structured-output schemas
  db.py                   SQLite session setup
  pdf_extraction.py       PyMuPDF parsing -> annotated transcript + saved images
  vision.py                describe_image vision subagent
  llm.py                   LLM model construction (provider-agnostic + Anthropic-specific)
  deep_research.py          profile_company via deepagents.create_deep_agent
  prompts.py                module-level prompt string constants
  templates.py               layout template config loader
  graphs/
    ingestion_agent.py       tool-calling agent: describe_image + submit_digest tools
    ingestion_graph.py       extract -> agent -> persist_digest -> profile_company -> persist_profile
    generation_graph.py      load_profiles -> ... -> select_assets
  routes/
    context.py                POST /context
    generate.py                POST /generate
backoffice/streamlit_app.py             Streamlit backoffice UI
config/templates/            layout template configs (fixed input contract)
data/                          gitignored: sqlite db + extracted assets
docs/architecture.md          AWS cloud architecture design (not deployed)
```
