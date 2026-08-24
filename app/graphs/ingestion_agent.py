"""Tool-calling ingestion agent — describe_image + submit_digest tools.

See spec.md §3.1/§7. Built on langgraph.prebuilt.create_react_agent. The two
tools are built per-run via a factory closure so per-request state (DB
session, company_id, image_map, call counters) never leaks across concurrent
requests.
"""

import logging
import threading

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from sqlmodel import Session, select

from app.llm import INGESTION_AGENT_MODEL_ENV, get_agent_model
from app.models import Company, Image, ImageTag
from app.observability import new_trace_config
from app.pdf_extraction import ImageMeta
from app.prompts import INGESTION_AGENT_SYSTEM_PROMPT, INGESTION_AGENT_USER_PROMPT
from app.schemas import PdfDigestSchema
from app.vision import describe_image_subagent, format_description_for_tool_result

logger = logging.getLogger(__name__)

MAX_DESCRIBE_IMAGE_CALLS = 50

_IMAGE_TYPE_TO_TAG = {
    "logo": ImageTag.logo,
    "product_screenshot": ImageTag.product_image,
    "chart": ImageTag.chart,
    "diagram": ImageTag.generic,
    "photo": ImageTag.generic,
    "other": ImageTag.generic,
}


class _IngestionRunState:
    """Mutable state shared between the two tools for a single ingestion run."""

    def __init__(self) -> None:
        self.vision_calls_made = 0
        self.cap_hit = False
        self.digest: PdfDigestSchema | None = None
        self.submitted = False


def _build_tools(
    session: Session,
    company_id: str,
    pdf_digest_id: int,
    image_map: dict[str, ImageMeta],
    run_state: _IngestionRunState,
    config: RunnableConfig,
):
    # create_react_agent's ToolNode runs multiple tool calls from one LLM turn
    # concurrently in worker threads. The sqlite3 connection (check_same_thread=
    # False) tolerates cross-thread use but not *concurrent* use, so unlocked
    # access here corrupts the cursor (sqlite3.InterfaceError: bad parameter or
    # other API misuse). Serialize all session access per run.
    session_lock = threading.Lock()

    # The vision subagent has to know whose document this is before it can say
    # whether a mark is the owner's own or a customer's. Resolved once here
    # rather than threaded through the graph as another parameter.
    owner = session.get(Company, company_id)
    company_name = (owner.name if owner else None) or company_id

    @tool
    def describe_image(image_id: str, context_hint: str) -> str:
        """Describe an image from the document. Call for images materially relevant
        to understanding the company (product shots, logos, charts, diagrams);
        skip obviously decorative images. Pass a short context_hint from the
        surrounding transcript text."""
        if run_state.submitted:
            return "[error] submit_digest already called; no further tool calls allowed."

        with session_lock:
            # Idempotency within this run only: image_id is stable per-run, not
            # company-wide, so caching is scoped to this run's pdf_digest_id.
            cached = session.exec(
                select(Image).where(Image.pdf_digest_id == pdf_digest_id, Image.image_id == image_id)
            ).first()
            if cached is not None:
                return f"type: {cached.tag.value} | summary: {cached.description}"

            if run_state.vision_calls_made >= MAX_DESCRIBE_IMAGE_CALLS:
                run_state.cap_hit = True
                return "[cap reached] descriptions unavailable for further images; proceed to submit_digest."

            meta = image_map.get(image_id)
            if meta is None:
                return f"[error] unknown image_id '{image_id}'"

            run_state.vision_calls_made += 1

        # Same config as the outer agent.invoke — nests this vision sub-call as a
        # child span of the ingestion agent's trace instead of a disconnected root.
        description = describe_image_subagent(
            meta.file_path, context_hint, company_name=company_name, config=config
        )

        tag = _IMAGE_TYPE_TO_TAG.get(description.image_type, ImageTag.generic)
        stored_description = description.summary
        if description.visible_text:
            stored_description += f" | visible_text: {description.visible_text}"

        with session_lock:
            # Durability boundary: persist immediately, independent of the outer graph run.
            image_row = Image(
                company_id=company_id,
                pdf_digest_id=pdf_digest_id,
                image_id=image_id,
                file_path=meta.file_path,
                page_number=meta.page_number,
                description=stored_description,
                tag=tag,
                is_own_brand=description.is_own_brand,
            )
            session.add(image_row)
            session.commit()

        return format_description_for_tool_result(description)

    @tool
    def submit_digest(digest: PdfDigestSchema) -> str:
        """Submit the final structured digest for this document. This is the ONLY
        way to end the task — after calling it, call no further tools."""
        run_state.digest = digest
        run_state.submitted = True
        return "Digest submitted."

    return [describe_image, submit_digest]


def run_ingestion_agent(
    transcript: str,
    session: Session,
    company_id: str,
    pdf_digest_id: int,
    image_map: dict[str, ImageMeta],
    config: RunnableConfig | None = None,
) -> tuple[PdfDigestSchema, int, bool]:
    """Runs the tool-calling ingestion agent to completion.

    Builds one RunnableConfig/callback handler here (nested under whatever
    Langfuse span is current — the caller's @observe span — via OTEL context)
    and threads that SAME instance explicitly into agent.invoke and the
    describe_image tool closure. Explicit threading (not just ambient context)
    is required here specifically because create_react_agent's ToolNode runs
    tool calls concurrently in worker threads, and OTEL context vars don't
    propagate across threads on their own.

    Returns (digest, images_reviewed, images_cap_hit).
    """
    config = config or new_trace_config()
    run_state = _IngestionRunState()
    tools = _build_tools(session, company_id, pdf_digest_id, image_map, run_state, config)
    model = get_agent_model(INGESTION_AGENT_MODEL_ENV)

    agent = create_react_agent(model, tools, prompt=INGESTION_AGENT_SYSTEM_PROMPT)
    agent.invoke(
        {"messages": [{"role": "user", "content": INGESTION_AGENT_USER_PROMPT.format(transcript=transcript)}]},
        config=config,
    )

    if not run_state.submitted or run_state.digest is None:
        logger.error("Ingestion agent for company_id=%s did not submit a digest", company_id)
        raise RuntimeError("ingestion agent did not call submit_digest")

    return run_state.digest, run_state.vision_calls_made, run_state.cap_hit
