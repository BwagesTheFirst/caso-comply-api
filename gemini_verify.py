"""
CASO Comply -- Gemini AI Verification Layer

After the initial font-size-based auto-classification, this module sends
page images and the proposed tag assignments to Gemini 2.5 Flash for
semantic verification.  Gemini checks heading hierarchy, reading order,
artifact detection, and generates alt text for images.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

MODEL_ID = "gemini-2.5-flash"

# ── Structured output schema sent to Gemini ──────────────────────────────

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "corrected_tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original_mcid": {"type": "integer"},
                    "page": {"type": "integer"},
                    "type": {"type": "string"},
                    "text": {"type": "string"},
                    "bbox": {"type": "array", "items": {"type": "number"}},
                    "is_artifact": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "original_mcid",
                    "page",
                    "type",
                    "text",
                    "bbox",
                    "is_artifact",
                    "reason",
                ],
            },
        },
        "alt_texts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "description": {"type": "string"},
                },
                "required": ["page", "description"],
            },
        },
        "issues_found": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reading_order_correct": {"type": "boolean"},
        "heading_hierarchy_valid": {"type": "boolean"},
    },
    "required": [
        "corrected_tags",
        "alt_texts",
        "issues_found",
        "reading_order_correct",
        "heading_hierarchy_valid",
    ],
}

# ── Prompt ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert PDF accessibility auditor.  You will receive:

1. Rendered images of every page in a PDF document.
2. A JSON array of proposed tag assignments produced by a font-size heuristic.

Your job is to verify and correct the tag assignments so that a screen reader
will present the document logically to a blind user.

**Rules you MUST enforce:**

- Heading hierarchy: H1 → H2 → H3 in order.  No skipping levels (e.g. H1
  directly followed by H3 with no H2 in between).  A document should have
  exactly one H1 (the main title).
- Body text that is merely large font should remain P (paragraph), not a heading.
  Headings are structural titles/section labels, not long body paragraphs.
- Headers, footers, page numbers, and other repeated boilerplate that appear
  on every page should be marked as Artifact (set is_artifact=true, type="Artifact").
  Screen readers skip artifacts.
- Multi-column layouts: reading order should proceed column-by-column
  (finish left column before right column), NOT line-by-line across columns.
  Reorder tags if needed.
- For any images visible on the pages, generate concise, descriptive alt text
  in the alt_texts array.  Use web search if helpful for context.
- Preserve the original_mcid and page from the input so corrections can be
  mapped back.

Return ONLY the JSON object matching the provided schema.
"""


def _render_pages_as_images(pdf_path: str, dpi: int = 150) -> list[str]:
    """Render every page of *pdf_path* as a PNG and return base64 strings.

    Pages are rendered one at a time to keep peak memory low.
    """
    doc = fitz.open(pdf_path)
    images: list[str] = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=dpi)
        img_bytes = pix.tobytes("png")
        images.append(base64.standard_b64encode(img_bytes).decode("ascii"))
        del pix  # free memory immediately
    doc.close()
    return images


def _build_gemini_contents(
    page_images: list[str],
    tag_assignments: list[dict],
) -> list[dict]:
    """Build the ``contents`` list for a Gemini generateContent request."""
    parts: list[dict] = []

    # Add each page image
    for idx, b64 in enumerate(page_images):
        parts.append({"text": f"--- Page {idx} ---"})
        parts.append({
            "inline_data": {
                "mime_type": "image/png",
                "data": b64,
            }
        })

    # Add the current tag assignments as JSON
    parts.append({
        "text": (
            "Below are the current tag assignments produced by a font-size "
            "heuristic.  Please verify and correct them.\n\n"
            + json.dumps(tag_assignments, indent=2)
        )
    })

    return [{"role": "user", "parts": parts}]


# ── Public API ───────────────────────────────────────────────────────────


async def verify_and_correct(
    pdf_path: str,
    tag_assignments: list[dict],
    content: dict,
) -> dict:
    """Send page images + tags to Gemini 2.5 Flash and return corrections.

    Returns
    -------
    dict
        corrected_tags : list[dict]   – same shape as *tag_assignments* but corrected
        alt_texts       : dict        – {page_num: [{image_index, alt_text}]}
        issues_found    : list[str]   – human-readable issue descriptions
        verification_score : float    – 0-1 confidence
    """
    # ── Graceful fallback wrapper ────────────────────────────────────
    try:
        return await _call_gemini(pdf_path, tag_assignments, content)
    except Exception:
        logger.exception("Gemini verification failed -- returning original tags unchanged")
        return {
            "corrected_tags": tag_assignments,
            "alt_texts": {},
            "issues_found": ["Gemini verification unavailable -- used original tags"],
            "verification_score": 0.0,
        }


async def _call_gemini(
    pdf_path: str,
    tag_assignments: list[dict],
    content: dict,
) -> dict:
    """Internal: actually call the Gemini API."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    # ── Render pages ─────────────────────────────────────────────────
    logger.info("Rendering PDF pages as images for Gemini verification")
    page_images = await asyncio.to_thread(_render_pages_as_images, pdf_path)
    logger.info("Rendered %d page images", len(page_images))

    # ── Build request ────────────────────────────────────────────────
    contents = _build_gemini_contents(page_images, tag_assignments)

    # Free the base64 list now that it is embedded in *contents*
    del page_images

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
    )

    logger.info("Sending request to Gemini %s", MODEL_ID)
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=MODEL_ID,
        contents=contents,
        config=config,
    )

    # ── Parse response ───────────────────────────────────────────────
    raw_text = response.text
    result = json.loads(raw_text)

    # Normalise corrected_tags back to the format remediation.py expects
    corrected_tags = []
    for item in result.get("corrected_tags", []):
        corrected_tags.append({
            "type": item.get("type", "P"),
            "page": item.get("page", 0),
            "mcid": item.get("original_mcid", 0),
            "text": item.get("text", ""),
            "bbox": item.get("bbox", [0, 0, 0, 0]),
            "font_size": _find_font_size(tag_assignments, item.get("original_mcid"), item.get("page")),
            "is_artifact": item.get("is_artifact", False),
            "reason": item.get("reason", ""),
        })

    # Build alt_texts as {page_num: [{image_index, alt_text}]}
    alt_texts: dict[int, list[dict]] = {}
    for entry in result.get("alt_texts", []):
        page = entry.get("page", 0)
        alt_texts.setdefault(page, []).append({
            "image_index": len(alt_texts.get(page, [])),
            "alt_text": entry.get("description", ""),
        })

    issues = result.get("issues_found", [])

    # Compute a simple verification score
    hierarchy_ok = result.get("heading_hierarchy_valid", False)
    order_ok = result.get("reading_order_correct", False)
    n_changes = sum(1 for c in corrected_tags if c.get("reason") and "unchanged" not in c.get("reason", "").lower())
    change_ratio = n_changes / max(len(corrected_tags), 1)
    score = (
        (0.4 if hierarchy_ok else 0.0)
        + (0.4 if order_ok else 0.0)
        + (0.2 * (1.0 - change_ratio))
    )

    logger.info(
        "Gemini verification complete: %d issues, score=%.2f, %d tags corrected",
        len(issues), score, n_changes,
    )

    return {
        "corrected_tags": corrected_tags,
        "alt_texts": alt_texts,
        "issues_found": issues,
        "verification_score": round(score, 3),
    }


def _find_font_size(
    original_tags: list[dict],
    mcid: int | None,
    page: int | None,
) -> float:
    """Look up the font_size from the original tag list by mcid+page."""
    for t in original_tags:
        if t.get("mcid") == mcid and t.get("page") == page:
            return t.get("font_size", 0)
    return 0
