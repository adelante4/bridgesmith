# 4. Perspective-guided research questions, single researcher

Date: 2026-08-23

## Status

Accepted (amends ADR 0003's research stage; outline/draft/polish/critique stages unchanged)

## Context

ADR 0003's research stage (`research_brief -> research_sender || research_receiver`) asked two per-company
agents to research a flat dimension list per company — which largely re-derived what the ingestion-time
`CompanyProfile` already holds (offerings, industry, pain points). Redundant work, and not what made STORM
effective: STORM's core mechanism is *perspective-guided question asking* — the questions come from simulated
readers with distinct concerns, which is what surfaces the non-obvious facts a generic "research company X"
prompt never asks for.

## Decision

Adopt STORM's core idea, cut its machinery (no simulated multi-turn conversations, no per-perspective agents):

- `plan_research` (replaces `research_brief`): one LLM call derives 2-3 perspectives of people who will judge
  *this* article for *this* pairing and brief — e.g. the budget decision-maker at the receiver, a technical
  evaluator, a sender proof-point verifier — each with 2-4 concrete, searchable questions that the profiles do
  NOT already answer (`ResearchPlanSchema`).
- `research` (replaces `research_sender` + `research_receiver`): one `create_deep_agent` answers all the
  questions (web_search + fact-finder sub-agent, ~6-search in-prompt budget), same `CompressedResearchSchema`
  fact+URL output. The per-company split was artificial — perspective questions are pairing-scoped, not
  company-scoped — and halving the agents halves the harness overhead.
- Downstream (`outline`, `polish`, `critique`) consumes one merged fact block instead of two per-company blocks.

## Consequences

Research now targets the gap between what the profiles say and what a skeptical reader needs — instead of
re-profiling companies already profiled at ingestion. One fewer deep agent per generation run; the parallel
fan-out/join disappears from the graph (`load_profiles -> plan_research -> research -> outline`, linear).
Question quality now rests on `plan_research`'s perspective choice — a weak plan starves the researcher, where
the old flat dimension list degraded to generic-but-safe research.
