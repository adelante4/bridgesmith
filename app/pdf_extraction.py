"""Deterministic PyMuPDF extraction — no LLM. See spec.md §3.1/§8.

Walks each page's block-level dict in reading order (blocks are already y-sorted
by PyMuPDF within a page), builds an annotated transcript with inline
[[IMAGE:img_id]] markers, and saves embedded image bytes to disk.
"""

import logging
import os
from dataclasses import dataclass, field

import pymupdf as fitz

logger = logging.getLogger(__name__)

# Defensive cap independent of the ingestion agent's 15-describe_image LLM-cost cap —
# protects disk/extraction time against a pathological PDF with hundreds of images.
MAX_IMAGES_EXTRACTED = 60


@dataclass
class ImageMeta:
    file_path: str
    page_number: int


@dataclass
class ExtractionResult:
    annotated_transcript: str
    image_map: dict[str, ImageMeta] = field(default_factory=dict)
    page_count: int = 0
    tables_json: str = "[]"


def extract_pdf_structure(pdf_bytes: bytes, company_id: str, assets_dir: str) -> ExtractionResult:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out_dir = os.path.join(assets_dir, company_id)
    os.makedirs(out_dir, exist_ok=True)

    transcript_parts: list[str] = []
    image_map: dict[str, ImageMeta] = {}
    image_count = 0

    for page_index in range(len(doc)):
        page = doc[page_index]
        page_dict = page.get_text("dict")
        image_index_on_page = 0

        for block in page_dict.get("blocks", []):
            block_type = block.get("type")

            if block_type == 0:
                # Text block: concatenate all line/span text in order.
                lines = block.get("lines", [])
                block_text = "".join(
                    span.get("text", "") for line in lines for span in line.get("spans", [])
                )
                if block_text.strip():
                    transcript_parts.append(block_text.strip())

            elif block_type == 1:
                if image_count >= MAX_IMAGES_EXTRACTED:
                    logger.warning(
                        "Extraction image cap (%d) reached for company_id=%s, skipping remaining images",
                        MAX_IMAGES_EXTRACTED,
                        company_id,
                    )
                    continue

                image_bytes = block.get("image")
                ext = block.get("ext", "png")
                if not image_bytes:
                    continue

                image_id = f"img_{page_index:02d}_{image_index_on_page:02d}"
                image_index_on_page += 1
                image_count += 1

                file_path = os.path.join(out_dir, f"{image_id}.{ext}")
                with open(file_path, "wb") as f:
                    f.write(image_bytes)

                image_map[image_id] = ImageMeta(file_path=file_path, page_number=page_index + 1)
                transcript_parts.append(f"[[IMAGE:{image_id}]]")

    doc_page_count = len(doc)
    doc.close()

    # Naive table extraction is a known limitation (spec §8) — not implemented for v1;
    # find_tables() support varies by PyMuPDF version, so we leave this as an empty list
    # rather than pretending complex tables are captured.
    tables_json = "[]"

    return ExtractionResult(
        annotated_transcript="\n".join(transcript_parts),
        image_map=image_map,
        page_count=doc_page_count,
        tables_json=tables_json,
    )
