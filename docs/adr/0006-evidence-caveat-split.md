# 6. Split researched facts into evidence and caveats; bound the revise loop

Date: 2026-08-23

## Status

Accepted

## Context

The first end-to-end brochure (Acme → Sage) was unreadable. A representative sentence:

> Acme brings a bridge between platform capability and production delivery: combining AI-native engineering,
> Acme-reported governance experience, an Acme-reported cross-cloud production methodology, and reported
> experience delivering AI systems operationally.

Three qualifiers in one 60-word paragraph, no numbers, no dates, no named customers. The research behind that
run was excellent — Sage's 300+ third-party apps queued on the AI Gateway (May 2026), the Doyen AI acquisition
cutting migration from weeks to days, Acme's 1M litres saved annually at Accolade Wines — and **none of it
reached the page**. The pipeline was destroying its own research.

Four independent causes, all confirmed against Langfuse traces:

1. **The writers were fed negations.** `GENERATION_RESEARCH_SYSTEM_PROMPT` told the researcher that "an honestly
   unanswered question is better than a padded answer", and `plan_research`'s STORM perspectives include a
   proof-point verifier whose questions produce negative answers by construction. Those landed in
   `CompressedResearchSchema.facts` indistinguishable from positive findings, and `outline` then assigned them to
   sections. `body_value` received four facts, three of them of the form "X was not found" / "does not establish"
   / "is methodological evidence rather than independent validation" — under a
   `SECTION_WRITER_SYSTEM_PROMPT` instruction to *write only from those facts*. The hedging was the raw material,
   not an accident.
2. **The revise loop compounded it.** `revise_node` appended each round's edits to one growing conversation and
   fed the whole thing back, so round 4 was still being instructed to apply rounds 1-3 verbatim. Since most edits
   were "qualify this claim", the qualifiers accumulated monotonically. `should_revise` gated on
   `if critique.required_edits`, which never terminates — a critic reviewing marketing copy against a fact list
   always finds one more thing worth softening — so the loop ran to its cap every time. Raising
   `MAX_REVISE_ATTEMPTS` from 1 to 4 (commit 89ffa4e) turned a latent bug into a 4x hedge amplifier.
3. **Nothing rewarded persuasion.** The rubric was fact_grounding, personalization, tone_match, structure. Three
   of four reward caution; none rewards specificity or readability. The loop's fixed point was a legal memo.
4. **Everything that comments on the article was written before it.** `draft_sections` ran the headline first,
   with facts explicitly withheld ("headline/subheadline draw on the article's overall angle, not a specific fact
   list"), so it could only paraphrase its own angle — hence `Acme: A Proposed Sage Partner Role for Governed
   Invoice-Processing AI`, which is a filename. The pull quote and CTA were told to "draw on the drafted
   sections' claims" but were never passed the drafted sections.

This matches the published failure mode: repeated self-critique degrades output, and a common failure is that
"the critic rewrites a correct answer with extra hedging, dropping specificity without changing the underlying
fact". It is also an instruction-conflict problem, which GPT-5-class models handle *worse* than older ones — they
spend reasoning reconciling contradictions rather than picking a side, and hedging is the safe reconciliation of
"be persuasive" against "only state supported claims" against a fact list made of negations.

## Decision

**Evidence and caveats are different kinds of thing, and only one is writing material.** `ResearchFact` gains
`kind: "evidence" | "caveat"`, defaulting to `evidence` so an unlabelled fact fails useful rather than toxic.
`_evidence_block()` feeds the outline, the section writers, and polish; `_caveats_block()` goes to the outline as
judgement context ("never assign these to a section") and to the critic as an accuracy check ("a reason to CUT an
unsupported claim, never to qualify a supported one"). A caveat never appears in a writer's prompt.

**The revise loop terminates on quality, not on the critic running out of opinions.** Each revise branches from
`polish_base_messages` — the original polish turn — so edits never accumulate across rounds. `CritiqueEdit` gains
`severity`, and only `blocking` edits drive a rewrite; anything whose fix is "add a qualifier to a supported
claim" is defined as advisory. `should_revise` stops when `min_score() >= REVISE_SCORE_FLOOR` (0.8), when no
blocking edits remain, or at `MAX_REVISE_ATTEMPTS` (back to 2). The score floor, not the cap, is meant to be what
stops it.

**The rubric gains `specificity`** — density of concrete anchors, and whether available researched specifics went
unused — so the scoring no longer points in one direction only.

**Anything that comments on the article is written after it.** `draft_sections` writes the body first, then hands
the finished body text to a dedicated headline writer and quote/CTA writer (`FINISHED_BODY_USER_PROMPT`). The CTA
prompt states that the reader works at the Receiver company, which it previously never knew.

**A deterministic specificity gate in `validate`.** Every section must contain a concrete anchor — a numeral, or a
capitalised token that is not merely sentence-initial. Filler words and hedge words are detected and reported to
the repair turn. No model call: relying on a model to police its own specificity is what failed the first time.

**Writers get a brief, not an analyst memo.** `_sender_brief`/`_receiver_brief` assemble offerings,
differentiators, and proof points, replacing `ResearchRun.summary` — which carries the company's own risks and
source discrepancies ("the deck reports 100+ experts while the announcement cited 140+") and taught the
copywriter to distrust its own client. Writers also now receive `sender_style.tone_signals`; that field was
loaded into state and never read, so the sender's brochure was being written in the receiver's voice.

## Consequences

Re-running the identical Acme → Sage request: the Bolt Energie evidence (80% of tickets routed correctly, ~3x
faster replies, up to four days per week of manual sorting saved) now appears where the previous run cited no
numbers at all; the copy carries no hedges; and the headline states a claim instead of labelling a document.

Costs and risks:

- **The evidence/caveat boundary is a model judgement.** A researcher that mislabels a negative finding as
  evidence puts it back in front of a writer. The default is the permissive direction on purpose — an unlabelled
  fact is usable — which trades a small hedging risk against silently starving the writers.
- **Caveats are now invisible to the reader.** They are recorded and shown to the critic, but nothing in the
  rendered PDF discloses that a claim is company-reported rather than independently audited. That is the correct
  call for marketing collateral and the wrong one for anything making regulated claims.
- **The anchor gate is crude.** It counts numerals and non-sentence-initial capitals, so a section can satisfy it
  with a company name and no real evidence. It catches the wholly-abstract paragraph, which was the actual
  failure, and nothing finer.
- **`min_score()` uses the critic's own self-reported numbers**, so a miscalibrated critic can end the loop early.
  Bounded by the fact that the loop's failure mode was running too long, not too short.
