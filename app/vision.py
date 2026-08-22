"""describe_image vision subagent — a separate, narrowly-scoped multimodal call.

Pure function: given an image + hint, get a description. No shared history with
the ingestion agent, no caching/persistence logic here (that lives in the tool
wrapper in graphs/ingestion_agent.py).

Uses langchain-core's standard (provider-agnostic) image content block, so this
works against whichever model VISION_MODEL_ENV resolves to.
"""

import base64
import io
import logging
import mimetypes

from langchain_core.runnables import RunnableConfig
from PIL import Image as PILImage

from app.llm import VISION_MODEL_ENV, get_agent_model
from app.observability import new_trace_config
from app.prompts import VISION_SUBAGENT_PROMPT
from app.schemas import ImageDescription

logger = logging.getLogger(__name__)

MAX_LONGEST_EDGE = 1568  # Anthropic's guidance for bounding image token cost.


def _downsize(image_bytes: bytes, media_type: str) -> tuple[bytes, str]:
    img = PILImage.open(io.BytesIO(image_bytes))
    longest_edge = max(img.width, img.height)
    if longest_edge <= MAX_LONGEST_EDGE:
        return image_bytes, media_type

    scale = MAX_LONGEST_EDGE / longest_edge
    new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    img = img.convert("RGB").resize(new_size)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def describe_image_subagent(
    image_path: str, context_hint: str, config: RunnableConfig | None = None
) -> ImageDescription:
    """`config` should be the caller's RunnableConfig, threaded down so this call
    nests under the caller's Langfuse trace. Falls back to a fresh (root) trace
    only for standalone/direct calls outside the ingestion agent."""
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    media_type = mimetypes.guess_type(image_path)[0] or "image/png"
    image_bytes, media_type = _downsize(image_bytes, media_type)
    b64_data = base64.b64encode(image_bytes).decode("utf-8")

    # Fresh model instance per call — no shared conversation history with the caller.
    model = get_agent_model(VISION_MODEL_ENV, temperature=0).with_structured_output(ImageDescription)

    # Standard content blocks (langchain-core v1) — translated by each chat model
    # integration to its own wire format, so this works across providers.
    message = {
        "role": "user",
        "content": [
            {
                "type": "image",
                "source_type": "base64",
                "data": b64_data,
                "mime_type": media_type,
            },
            {"type": "text", "text": VISION_SUBAGENT_PROMPT.format(context_hint=context_hint)},
        ],
    }

    result = model.invoke([message], config=config or new_trace_config())
    return result


def format_description_for_tool_result(description: ImageDescription) -> str:
    parts = [f"type: {description.image_type}"]
    if description.visible_text:
        parts.append(f"visible_text: {description.visible_text}")
    parts.append(f"summary: {description.summary}")
    return " | ".join(parts)
