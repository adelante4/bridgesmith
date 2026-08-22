# 2. LLM asset selection in the draft, conversation-turn repairs

Date: 2026-08-22

## Status

Accepted

## Context

The generation graph originally kept writing and asset selection fully separate: the draft model wrote
`alt_text` per image slot blind to what assets existed, and a deterministic `select_assets` node matched
placeholders to `Image` rows afterwards via a hero→logo tag hint plus bag-of-words overlap between alt text
and image descriptions. That matcher was the weakest link — a good asset sharing zero surface words with the
alt text lost to a stock-query fallback, and stopwords counted as overlap. Separately, the repair loop made
isolated per-field LLM calls with their own tiny prompts, and the generate prompt carried each profile's
`web_sources` (URLs the article can never cite).

## Decision

- The draft model selects assets itself. `load_profiles` builds an aliased catalog (`A1: [tag] description`)
  of the **sender's** described `Image` rows (latest ingestion run only) into the draft prompt; the
  `ImagePlaceholderDraft` schema gains `asset_alias` (nullable → stock fallback). Aliases, not raw DB ids,
  so the model can't emit an arbitrary integer that happens to exist. Hero-prefers-logo lives as a prompt
  instruction. Receiver assets are never offered — the receiver's logo must not land in the sender's article.
- Only described assets are selectable, by construction: undescribed images never get `Image` rows
  (ingestion agent skips decorative ones; 15-call vision cap). Accepted, as is the re-ingestion vision cost
  of re-describing a re-uploaded PDF (cache is scoped per `pdf_digest_id`) — both fine at current scale.
- `validate` also checks aliases against the catalog; invalid aliases and missing sections/slots are now
  repairable, not just word counts.
- Repairs are appended turns in the SAME draft conversation: one turn per repair iteration batching all
  current violations, model re-emits the full `ArticleSchema` ("fix only the listed problems; copy every
  other field verbatim"). The stable conversation prefix (system + profiles + draft) makes every repair round
  an OpenAI prompt-cache hit. The per-field `REPAIR_FIELD_*` prompts and stitching code are gone.
- `select_assets` is renamed `finalize_article`: truncation of still-over-limit fields plus alias→`Image.id`
  resolution and stock-query fallback (query = alt text). No matching logic remains.
- `web_sources` are stripped from both profiles in the generate prompt (kept in DB/API). Any fact worth
  writing must already live in summary/offerings; if not, that's a profiling gap to fix upstream.

## Consequences

Alt text is now written with knowledge of the real inventory, so asset usage no longer depends on accidental
word overlap. One prompt carries slightly more tokens (catalog ≤15 lines); repair rounds are cheaper per
token via prompt caching but re-emit the whole article. A model that drifts unlisted fields during repair is
caught by the unchanged validate loop. Companies with no described assets degrade to stock queries for every
slot, stated explicitly in the prompt as "(none)".
