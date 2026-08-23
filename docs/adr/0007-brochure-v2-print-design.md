# 7. brochure_v2: a designed print template, a derived palette, and brand-mark ownership

Date: 2026-08-23

## Status

Accepted

## Context

spec.md §2 puts PDF rendering out of scope — "the deliverable is the JSON contract only". `brochure_v1` was
built anyway as a demo artifact, and it showed: a full-bleed cover carrying ~40 words on an otherwise empty A4,
then three untitled paragraphs of 14px body copy, ending a third of the way down page 2. It read as a text file
with a coloured first page.

Specific defects, each with a distinct cause:

- **Everything rendered in monospace.** The template is a `.html` file, so Jinja autoescaped the quotes in the
  CSS font stack to `&#39;`, invalidating the whole `font-family` declaration. WeasyPrint dropped it and fell
  back to its own default, which in the container resolved to Liberation Mono (`fc-match sans-serif`). A
  secondary bug compounded it: the detected font name is a PostScript name (`SpaceGrotesk`), and Google Fonts
  404s on that — it wants `Space+Grotesk`.
- **Orange-to-black across A4.** The cover ran `linear-gradient` from `primary_color` to `accent_color`. Both are
  real detected values (Acme: `#FF4B00` and `#000000`) and neither is wrong; interpolating two arbitrary detected
  hexes across a full page is.
- **Black blobs where images belonged.** Content slots rendered extracted assets at `object-fit: cover`, so a
  512x512 icon was zoom-cropped into a full-width band. Ten of Acme's sixteen assets are orange line icons burned
  onto solid black JPEGs with no alpha.
- **A customer's logo could become the sender's masthead.** Acme's deck contains logos for Bolt and Accolade,
  its own customers. `ImageTag` marks both those and Acme's own mark as `logo`; nothing distinguished them.

## Decision

Add `brochure_v2` as a second template. `brochure_v1` and `b2b_newsletter_v1` are untouched — leaving them
working is what demonstrates that the loader is generic, which turns a new template from scope creep into
evidence for spec.md §6's "adding a template is a config change".

**Registration is data-driven.** `TemplateConfig.html_template` names the stylesheet; `null` means JSON-only.
This replaces `PDF_ENABLED_TEMPLATE_IDS` in the route and the hardcoded `brochure_v1.html` in the renderer.

**Layout: Swiss/editorial, floats not grid.** Two A4 pages, no cover — a masthead opens page 1. Twelve-column
rhythm, body in a 7-column measure (~65 characters), 3-column side channel for section headings and marks.

The columns are floats, and this is load-bearing rather than stylistic. WeasyPrint 69 renders `display: grid`
without error and places columns correctly — but a grid *container* stretches to fill the available page height,
so each block claimed a page to itself and the brochure came out as five near-empty pages. This was only caught
by measuring the rendered output; a capability probe (does it render without raising?) says grid is fine.

**Palette derivation (`app/palette.py`).** Two detected hexes are not a design system, so nothing trusts them as
a pair. Paper is always near-white and ink near-black. `primary` supplies one hue as tints for panels and rules,
plus a contrast-guarded variant darkened until it passes 4.5:1 for small type. `accent` becomes a true second hue
only when it is chromatic and at least `_MIN_HUE_DISTANCE` from primary. An *achromatic* accent is demoted to ink
and hairlines rather than discarded — Acme's black is the ink of their own material — but a *chromatic* accent
that merely sits close to primary must not become ink, or a navy/blue brand would get an entire brochure set in
blue body text.

**Emphasis via a two-marker Markdown subset (`app/richtext.py`).** Section writers mark the strongest measured
result; the page sets it larger and in the brand colour, so evidence carries visual weight without being lifted
into a separate stat block. The converter HTML-escapes everything first and re-introduces exactly two tags, so no
other markup can survive: raw model-authored HTML would be an injection hole, and a full Markdown parser would
admit the link and image syntax this design deliberately carries none of. Word limits and the
anchor/filler/hedge gates all run on `richtext.strip()`ed text, because a word limit is a promise about what a
reader sees.

**Marks are tiled, never cropped.** Each asset renders `object-fit: contain` on a tile filled with its own
sampled modal corner colour. A black-background icon becomes a deliberate chip; a white-background asset gets a
tile that disappears. One rule, no per-company special-casing.

**`Image.is_own_brand`.** The ingestion vision pass now classifies whether a mark belongs to the document's owner
— and is told which company owns the document, which it previously never knew. Identity slots (`hero`,
`masthead`) require it; content slots exclude logos entirely, since a wordmark stretched into a content slot
reads as a mistake regardless of who owns it. Existing rows default to `False` via `db.py`'s `_NEW_COLUMNS`
patch, so they are never used as a mark.

**Section headings are editorial content**, written per article against a `max_heading_words` cap, with a
contract-level `heading_fallback` as a safety net only. They were briefly hardcoded as a Jinja map keyed on
section id, which fits exactly one article shape.

## Consequences

The Acme → Sage pair renders as two pages, 337 + 163 words, in Space Grotesk, with tiled marks in the side
channel and researched numbers emphasised in brand colour.

Costs and risks:

- **The masthead is empty for every pre-existing company.** `is_own_brand` defaults to `False`, so Acme's mark is
  not trusted and the sender name is set typographically instead. Correct (it fails closed) but it means the
  demo artifact shows no logo until that company's deck is re-ingested. The source PDF is not persisted, so
  backfilling requires a re-upload through `POST /context`.
- **Page 2 ends about 45% down.** ~480 words plus furniture does not fill two A4 pages. The documented norm for
  this format is 200-350 words, which suggests a one-page leave-behind would sit better; two pages at 480 was an
  explicit product decision, not an oversight.
- **Palette thresholds are tuned against four brand pairs.** `_MIN_CHROMA` and `_MIN_HUE_DISTANCE` are covered by
  tests for Acme's orange/black, a blue/amber pair, a navy/blue pair, and a pale primary. A sender whose detected
  "colours" come from a photographic or gradient logo has not been tried.
- **Emphasis is a model judgement.** The writer decides what to mark; a section can come back with no marks
  (one of three did on the verification run). The design degrades to plain body text, which is acceptable.
- **`brochure_v2` is still beyond spec.** It should be presented as a demo artifact, not as evidence of having
  the JSON contract remains the deliverable.
