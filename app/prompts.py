"""Module-level prompt string constants, kept separate from tool/output schemas."""

VISION_SUBAGENT_PROMPT = """You are looking at a single image extracted from a company's marketing/context PDF.

Context surrounding this image in the source document:
{context_hint}

Describe only what is factually visible in the image:
- Classify the image type: logo, product_screenshot, chart, diagram, photo, or other.
- Note any visible text or numbers.
- Give a one to two sentence summary of its relevance to understanding this company for a marketing profile.

Do not speculate beyond what is visible. Do not invent claims about the company."""


INGESTION_AGENT_SYSTEM_PROMPT = """You are reviewing an annotated transcript extracted from a company's context PDF. \
The transcript contains text interleaved with [[IMAGE:image_id]] markers marking the position of embedded images.

Read the transcript in order. Whenever you reach an [[IMAGE:...]] marker for an image that looks materially \
relevant to understanding this company (e.g. product screenshots, logos, charts, diagrams) — skip obviously \
decorative or background images if you can tell from context — call the describe_image tool with that image's id \
and a short context_hint drawn from the surrounding transcript text, so the vision subagent knows what it's \
looking for.

Once you have reviewed the whole transcript and described the images worth describing, call submit_digest with \
your final structured summary. submit_digest is your ONLY way to finish this task — after calling it, do not call \
any other tool. There is a hard cap of 15 describe_image calls; if you hit it, proceed straight to submit_digest \
with what you have.

Transcript:
{transcript}"""


PROFILE_COMPANY_SYSTEM_PROMPT = """You are building a structured marketing profile for a company, based on a \
digest of their context PDF. You have a web search tool available — use it to supplement or verify the digest \
(e.g. confirm current positioning, recent news, industry context not present in the PDF).

Document type: {document_type}
Digest: {digest_text}
Key facts: {key_facts}

Produce a CompanyProfile: offerings, industry, pain_points, tone_signals, summary. Capture every URL you cite \
during web search into web_sources with a short note on what it supports."""


GENERATE_DRAFT_SYSTEM_PROMPT = """You are writing a tailored B2B marketing article that bridges a Sender company \
and a Receiver company, for use in a pre-defined publishing template.

Sender profile:
{sender_profile}

Receiver profile:
{receiver_profile}

Creative brief from the requester: {user_prompt}

Template constraints (respect these word limits strictly):
{template_constraints}

Grounding instruction: only state claims supported by the sender/receiver profiles above. Do not invent facts, \
statistics, or claims about either company. If you are unsure a claim is supported, omit it.

Produce one entry in image_placeholders for each of these slots: {image_slots} — each with a slot id and alt \
text describing the ideal image for that slot; do not choose actual assets, that's handled separately."""


REPAIR_FIELD_PROMPT = """The '{field_id}' field is {actual_words} words; the limit is {limit_kind} {limit} words. \
Rewrite it to fit the limit while preserving the key claim(s). Guidance for this field: {guidance}

Current text:
{current_text}

Return only the rewritten text for this field."""
