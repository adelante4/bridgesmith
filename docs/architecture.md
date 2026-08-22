# Cloud Architecture (AWS) — Design Doc

Not deployed — design only. Maps the prototype's SQLite/local-filesystem/synchronous design onto managed AWS services for production. See `spec.md` §11 for the source of this content.

For the actual LangGraph node/edge shape being deployed here, see [`docs/graphs.md`](graphs.md).

## API layer

API Gateway → Lambda, or Fargate if cold-start/latency from LangGraph's import weight becomes an issue. **Decision point to flag:** Fargate is likely the safer real answer given how heavy LangChain's import graph is — worth validating cold-start numbers before committing to Lambda for the API layer itself (the async ingestion worker, below, is a better fit for Lambda regardless).

## Storage

S3 for raw PDFs and extracted images, one prefix per company: `s3://.../companies/{company_id}/`.

## Metadata store

DynamoDB. `Company` and `CompanyProfile` tables keyed by `company_id` (partition key) — a direct swap-in for the prototype's SQLite tables, same shape.

## LLM

Amazon Bedrock, model-agnostic within Bedrock's catalog — matches the provider-agnostic LangChain design already used for `generate_draft`, so swapping local API keys for Bedrock there is a config change, not a rewrite.

**Caveat to flag explicitly:** Bedrock's Claude does not expose the same native server-side web search tool used in `profile_company` on the direct Anthropic API. Production options:
1. Call the direct Anthropic API specifically for the profiling step while using Bedrock for everything else.
2. Bind an external search tool (Tavily/Brave/SerpAPI) to the Bedrock-hosted model to replicate the same research behavior.

Either way, this needs to be a deliberate, documented decision rather than silently losing the research capability on a Bedrock migration.

**Cost lever:** prompt caching on the company profile context, which is large and reused across every `generate` call for the same company.

## Async ingestion

S3 upload event → SQS → Lambda worker runs the ingestion graph. This decouples the slow PDF/LLM profiling work from the upload request, so `/context` can return fast with a "processing" status if productionized. The prototype itself keeps ingestion synchronous for simplicity — this is the first thing to change for production, since a document with several images can take `/context` tens of seconds today (see Non-Functional Notes below).

## Orchestration at scale

Step Functions wrapping the LangGraph-equivalent steps if the pipeline needs cross-service coordination — e.g. a human approval step before publishing. Natural evolution path, not needed at current scope.

## Security

- S3 bucket encryption at rest (SSE-KMS).
- Per-tenant IAM scoping / prefix isolation — uploaded PDFs are real client business documents.
- VPC endpoints for Bedrock/S3/DynamoDB to avoid public internet egress.

## Observability

CloudWatch structured logs. Specifically log `validate`/`repair` outcomes (how often articles need repair) as a product-quality metric worth tracking over time.

## Cost drivers

LLM calls dominate. Two levers:
1. Profile-once-reuse-many company profiling (already in the design — a company's profile is researched once at ingestion, not re-derived on every generate call).
2. Prompt caching on the large, repeated company profile context across generate calls for the same company.

## Non-Functional Notes

- **No RAG/vector DB at this scale, deliberately.** At 1-2 PDFs per company, full text fits in context. Trigger condition for adding it: PDF context exceeding a practical token budget, or many PDFs per company.
- **Grounding is instruction-based only** — no separate fact-checking pass. Known limitation; natural next step is a verification LLM call cross-checking generated claims against source text.
- **Repair loop caps at 2 attempts, then truncates.** Bounded cost/latency, never loops forever.
- **Data isolation between customers' uploaded PDFs is not enforced in the prototype** (SQLite, no auth). Real production concern given this touches real client business data — first thing to add before this touches real customer data.
- **`POST /context` is synchronous and noticeably heavy**: extraction + an agentic tool loop (each `describe_image` call is its own Claude round trip) + web-search-grounded profiling. For a document with several images this can take tens of seconds — fine for a demo, and the concrete justification for the async S3→SQS→Lambda ingestion design above.
- **Two deliberate cost/latency bounds on the vision subagent:** the `describe_image` cap (15 images/document) and per-image downsizing to ~1568px longest edge before it's sent to the model.
