# Automated Generative Marketing Collateral

Prototype backend for the ML6 Senior AI Engineer Challenge ("Automated Generative Marketing Collateral"). Automates the manual process of researching a Sender and Receiver company and writing a bridging B2B marketing article: upload PDF context for each company, then generate a tailored article as structured JSON that maps onto a pre-defined publishing template (word limits, image slots, theme colors).

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

SQLite database and extracted PDF assets persist under `./data` on the host (volume-mounted).

## Example usage

Upload context for a sender company:

```bash
curl -X POST http://localhost:8000/context \
  -F role=sender \
  -F name="Acme Corp" \
  -F file=@path/to/acme_onepager.pdf
```

Response includes a generated `company_id` (e.g. `sender_a1b2c3d4`) — save it. Repeat for the receiver:

```bash
curl -X POST http://localhost:8000/context \
  -F role=receiver \
  -F name="Globex Inc" \
  -F file=@path/to/globex_brief.pdf
```

Generate a tailored article:

```bash
curl -X POST http://localhost:8000/generate \
  -H "Content-Type: application/json" \
  -d '{
    "sender_id": "sender_a1b2c3d4",
    "receiver_id": "receiver_e5f6g7h8",
    "prompt": "Focus on how Acme'\''s real-time fleet optimization reduces fuel costs for large logistics operators."
  }'
```

`template_id` is optional in the request body and defaults to `b2b_newsletter_v1` (see `config/templates/b2b_newsletter_v1.json`).

**Note on latency:** `/context` runs the full ingestion chain synchronously — extraction, an agentic tool loop (each image description is its own Claude round trip), and web-search-grounded profiling. For a document with several images this can take tens of seconds. This is expected for a demo; `docs/architecture.md` describes the async S3→SQS→Lambda design that would decouple this in production.

## Explicitly out of scope

Per the challenge brief, deliberately not built:

- Rendering the final newsletter/brochure (InDesign/Figma/HTML) — the deliverable is the JSON contract only.
- Vector search / embeddings / RAG — at 1-2 PDFs per company, full text fits in context. See `spec.md` §12 for the upgrade trigger.
- Auth / multi-tenancy enforcement.
- A full automated test suite (a few smoke tests are included, non-blocking — see below).
- Real image generation — placeholders reference extracted assets or a `stock_query` hint.
- A frontend UI — `/docs` (FastAPI's OpenAPI UI) is the demo surface.

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
config/templates/            layout template configs (fixed input contract)
data/                          gitignored: sqlite db + extracted assets
docs/architecture.md          AWS cloud architecture design (not deployed)
```
