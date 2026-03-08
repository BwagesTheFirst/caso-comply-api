"""
CASO Comply -- PDF Accessibility Remediation Engine

Core engine for analyzing and remediating PDF accessibility issues.
Extracts content structure via PyMuPDF, detects tables via pdfplumber,
auto-classifies text blocks by font size into heading/paragraph tags,
and writes a proper tagged PDF structure using pikepdf.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber
import pikepdf
from pikepdf import Array, Dictionary, Name, String

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Analysis
# ---------------------------------------------------------------------------

def analyze_structure(pdf_path: str) -> dict:
    """Analyze PDF structural accessibility features using pikepdf."""
    report = {
        "file": str(pdf_path),
        "tagged": False,
        "has_lang": False,
        "has_title": False,
        "title": None,
        "language": None,
        "has_struct_tree": False,
        "page_count": 0,
        "has_outlines": False,
        "has_display_doc_title": False,
        "issues": [],
    }

    with pikepdf.open(pdf_path) as pdf:
        report["page_count"] = len(pdf.pages)

        # Tagged / MarkInfo
        mark_info = pdf.Root.get("/MarkInfo")
        if mark_info:
            report["tagged"] = bool(mark_info.get("/Marked", False))
        if not report["tagged"]:
            report["issues"].append("PDF is not tagged -- no structure tree exists")

        # StructTreeRoot
        struct_tree = pdf.Root.get("/StructTreeRoot")
        if struct_tree:
            report["has_struct_tree"] = True
        else:
            report["issues"].append("No StructTreeRoot -- document has no semantic structure")

        # Language
        lang = pdf.Root.get("/Lang")
        if lang:
            report["has_lang"] = True
            report["language"] = str(lang)
        else:
            report["issues"].append("No /Lang -- document language not specified")

        # Title
        if pdf.docinfo:
            title = pdf.docinfo.get("/Title")
            if title:
                report["has_title"] = True
                report["title"] = str(title)
        if not report["has_title"]:
            report["issues"].append("No /Title in document info")

        # DisplayDocTitle
        viewer_prefs = pdf.Root.get("/ViewerPreferences")
        if viewer_prefs and viewer_prefs.get("/DisplayDocTitle"):
            report["has_display_doc_title"] = True
        else:
            report["issues"].append("DisplayDocTitle not set")

        # Bookmarks / Outlines
        if "/Outlines" in pdf.Root:
            report["has_outlines"] = True
        elif report["page_count"] > 20:
            report["issues"].append(
                f"No bookmarks -- recommended for documents over 20 pages "
                f"({report['page_count']} pages)"
            )

    return report


def extract_content(pdf_path: str, max_pages: int = 50) -> dict:
    """Extract text blocks with positions, font sizes, and images using PyMuPDF."""
    content: dict = {
        "pages": [],
        "total_images": 0,
        "total_text_blocks": 0,
    }

    doc = fitz.open(pdf_path)
    page_limit = min(len(doc), max_pages)

    for page_num in range(page_limit):
        page = doc[page_num]
        page_data = {
            "page": page_num,
            "width": page.rect.width,
            "height": page.rect.height,
            "text_blocks": [],
            "images": [],
        }

        blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block["type"] == 0:  # text
                spans_info = _extract_spans(block)
                if spans_info["text"]:
                    page_data["text_blocks"].append(spans_info)
                    content["total_text_blocks"] += 1
            elif block["type"] == 1:  # image
                page_data["images"].append({
                    "bbox": list(block["bbox"]),
                    "width": block.get("width", 0),
                    "height": block.get("height", 0),
                })
                content["total_images"] += 1

        content["pages"].append(page_data)

    doc.close()
    return content


def _extract_spans(block: dict) -> dict:
    """Pull full text, dominant font size, and font name from a text block."""
    text_parts: list[str] = []
    sizes: list[float] = []
    fonts: list[str] = []

    for line in block.get("lines", []):
        line_text = ""
        for span in line.get("spans", []):
            span_text = span.get("text", "")
            line_text += span_text
            if span_text.strip():
                sizes.append(span.get("size", 0))
                fonts.append(span.get("font", ""))
        text_parts.append(line_text)

    text = "\n".join(text_parts).strip()
    dominant_size = max(sizes) if sizes else 0
    dominant_font = max(set(fonts), key=fonts.count) if fonts else ""

    return {
        "text": text,
        "bbox": list(block["bbox"]),
        "font_size": round(dominant_size, 2),
        "font_name": dominant_font,
    }


def detect_tables(pdf_path: str, max_pages: int = 50) -> dict:
    """Detect tables using pdfplumber."""
    result = {"tables_found": 0, "pages_with_tables": [], "pages_analyzed": 0}

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages[:max_pages]):
            result["pages_analyzed"] += 1
            tables = page.find_tables()
            if tables:
                result["tables_found"] += len(tables)
                result["pages_with_tables"].append(i)

    return result


def compute_score(structure: dict, content: dict, tables: dict) -> dict:
    """Compute a weighted accessibility score from structural checks."""
    checks = {
        "tagged": {
            "passed": structure["tagged"],
            "weight": 25,
            "description": "Document is tagged",
        },
        "language": {
            "passed": structure["has_lang"],
            "weight": 10,
            "description": "Document language specified",
        },
        "title": {
            "passed": structure["has_title"],
            "weight": 10,
            "description": "Document title set",
        },
        "struct_tree": {
            "passed": structure["has_struct_tree"],
            "weight": 25,
            "description": "Structure tree present",
        },
        "display_doc_title": {
            "passed": structure["has_display_doc_title"],
            "weight": 5,
            "description": "DisplayDocTitle enabled",
        },
        "bookmarks": {
            "passed": structure["has_outlines"] or structure["page_count"] <= 20,
            "weight": 5,
            "description": "Bookmarks present (or not needed)",
        },
    }

    # Image alt text -- conservative: if tagged we give partial credit
    if content["total_images"] > 0:
        checks["alt_text"] = {
            "passed": structure["tagged"],
            "weight": 10,
            "description": "Images may have alt text (tagged PDF)",
        }
    else:
        checks["alt_text"] = {
            "passed": True,
            "weight": 10,
            "description": "No images -- alt text not needed",
        }

    # Table headers
    if tables["tables_found"] > 0:
        checks["table_headers"] = {
            "passed": structure["tagged"],
            "weight": 10,
            "description": "Tables may have proper headers (tagged PDF)",
        }
    else:
        checks["table_headers"] = {
            "passed": True,
            "weight": 10,
            "description": "No tables -- headers not needed",
        }

    total_possible = sum(c["weight"] for c in checks.values())
    total_earned = sum(c["weight"] for c in checks.values() if c["passed"])
    score = round((total_earned / total_possible) * 100) if total_possible else 0

    return {
        "score": score,
        "total_possible": total_possible,
        "total_earned": total_earned,
        "checks": checks,
        "grade": (
            "A" if score >= 90 else
            "B" if score >= 70 else
            "C" if score >= 50 else
            "F"
        ),
    }


def analyze_pdf(file_path: str) -> dict:
    """
    Full analysis pipeline.  Returns a dict with structure, content,
    tables, score, and issue summary.
    """
    path = str(file_path)
    structure = analyze_structure(path)
    content = extract_content(path)
    tables = detect_tables(path)
    score = compute_score(structure, content, tables)

    return {
        "structure": structure,
        "content": {
            "total_text_blocks": content["total_text_blocks"],
            "total_images": content["total_images"],
            "pages_analyzed": len(content["pages"]),
            "pages": content["pages"],
        },
        "tables": tables,
        "score": score,
    }


# ---------------------------------------------------------------------------
# 2. Auto-classification
# ---------------------------------------------------------------------------

def _classify_blocks(content: dict) -> list[dict]:
    """
    Auto-classify text blocks into H1 / H2 / H3 / P by font size.

    Strategy:
      - Collect all distinct font sizes across the document.
      - Sort descending.  Largest = H1, second = H2, third = H3,
        everything else = P.
      - Only assign heading tags to blocks whose text is short enough
        to be plausible headings (< 200 chars).
    """
    MAX_HEADING_CHARS = 200

    # Gather all non-empty blocks with sizes
    all_blocks: list[dict] = []
    for page_data in content["pages"]:
        for block in page_data["text_blocks"]:
            if block["text"].strip():
                all_blocks.append({
                    **block,
                    "page": page_data["page"],
                })

    if not all_blocks:
        return []

    # Determine distinct sizes
    sizes = sorted({b["font_size"] for b in all_blocks if b["font_size"] > 0}, reverse=True)

    # Build a size -> tag map
    size_tag: dict[float, str] = {}
    heading_levels = ["H1", "H2", "H3"]
    for idx, size in enumerate(sizes):
        if idx < len(heading_levels):
            size_tag[size] = heading_levels[idx]
        else:
            size_tag[size] = "P"

    # If there's only one distinct size, everything is P
    if len(sizes) <= 1:
        size_tag = {s: "P" for s in sizes}

    # Assign tags
    classified: list[dict] = []
    mcid_counters: dict[int, int] = defaultdict(int)

    for block in all_blocks:
        tag = size_tag.get(block["font_size"], "P")

        # Long text cannot be a heading
        if tag != "P" and len(block["text"]) > MAX_HEADING_CHARS:
            tag = "P"

        page = block["page"]
        mcid = mcid_counters[page]
        mcid_counters[page] += 1

        classified.append({
            "type": tag,
            "page": page,
            "mcid": mcid,
            "text": block["text"],
            "bbox": block["bbox"],
            "font_size": block["font_size"],
        })

    return classified


# ---------------------------------------------------------------------------
# 3. Tag writing (pikepdf)
# ---------------------------------------------------------------------------

def _add_metadata(pdf: pikepdf.Pdf, title: str, lang: str = "en-US"):
    """Set document-level accessibility metadata."""
    pdf.Root[Name("/Lang")] = String(lang)

    with pdf.open_metadata() as meta:
        meta["dc:title"] = title
        meta["dc:language"] = [lang]

    pdf.docinfo[Name("/Title")] = String(title)

    pdf.Root[Name("/ViewerPreferences")] = Dictionary({
        "/DisplayDocTitle": pikepdf.Boolean(True),
    })

    pdf.Root[Name("/MarkInfo")] = Dictionary({
        "/Marked": pikepdf.Boolean(True),
    })


def _build_structure_tree(pdf: pikepdf.Pdf, tag_assignments: list[dict]):
    """Build StructTreeRoot with Document -> heading/paragraph elements."""
    struct_elems = []

    for tag in tag_assignments:
        elem = pdf.make_indirect(Dictionary({
            "/Type": Name("/StructElem"),
            "/S": Name(f"/{tag['type']}"),
            "/Pg": pdf.pages[tag["page"]].obj,
            "/K": tag["mcid"],
        }))
        struct_elems.append(elem)

    doc_elem = pdf.make_indirect(Dictionary({
        "/Type": Name("/StructElem"),
        "/S": Name("/Document"),
        "/K": Array(struct_elems),
    }))

    for elem in struct_elems:
        elem[Name("/P")] = doc_elem

    # Parent tree -- maps (page_num, MCID) -> struct element
    pages_mcid_map: dict[int, list[tuple[int, pikepdf.Object]]] = defaultdict(list)
    for i, tag in enumerate(tag_assignments):
        pages_mcid_map[tag["page"]].append((tag["mcid"], struct_elems[i]))

    nums_array = Array()
    for page_num in sorted(pages_mcid_map):
        nums_array.append(page_num)
        page_elems = sorted(pages_mcid_map[page_num], key=lambda x: x[0])
        mcid_array = Array([elem for _, elem in page_elems])
        nums_array.append(pdf.make_indirect(mcid_array))

    parent_tree = pdf.make_indirect(Dictionary({
        "/Type": Name("/ParentTree"),
        "/Nums": nums_array,
    }))

    struct_tree_root = pdf.make_indirect(Dictionary({
        "/Type": Name("/StructTreeRoot"),
        "/K": doc_elem,
        "/ParentTree": parent_tree,
    }))

    doc_elem[Name("/P")] = struct_tree_root
    pdf.Root[Name("/StructTreeRoot")] = struct_tree_root


def _inject_marked_content(pdf: pikepdf.Pdf, page_num: int, page_tags: list[dict]):
    """
    Inject per-block BDC/EMC marked content operators into the page content stream.

    For each tagged block we emit:
        /<TagType> <</MCID n>> BDC
    before the block's text operators and EMC after.

    This implementation parses the raw content stream, identifies text-drawing
    sequences (BT...ET), and wraps each one with the corresponding tag's
    marked-content operators in order.  If there are more BT/ET groups than
    tags, extra groups get a /Span wrapper.  If there are fewer, remaining
    tags still get empty BDC/EMC pairs so the structure tree stays consistent.
    """
    page = pdf.pages[page_num]
    if "/Contents" not in page:
        return

    contents = page["/Contents"]
    if isinstance(contents, pikepdf.Array):
        raw = b""
        for ref in contents:
            raw += ref.read_bytes()
    else:
        raw = contents.read_bytes()

    # Split stream into BT...ET groups and non-text segments
    segments = _split_content_stream(raw)

    # Match text groups to tags in order
    text_group_idx = 0
    new_parts: list[bytes] = []

    for seg_type, seg_bytes in segments:
        if seg_type == "text" and text_group_idx < len(page_tags):
            tag = page_tags[text_group_idx]
            marker_start = f"/{tag['type']} <</MCID {tag['mcid']}>> BDC\n".encode()
            marker_end = b"\nEMC\n"
            new_parts.append(marker_start + seg_bytes + marker_end)
            text_group_idx += 1
        else:
            new_parts.append(seg_bytes)

    # If there are remaining tags with no corresponding text group,
    # append empty marked-content pairs to keep the structure tree valid.
    while text_group_idx < len(page_tags):
        tag = page_tags[text_group_idx]
        empty = f"/{tag['type']} <</MCID {tag['mcid']}>> BDC\nEMC\n".encode()
        new_parts.append(empty)
        text_group_idx += 1

    page[Name("/Contents")] = pdf.make_stream(b"".join(new_parts))
    page[Name("/StructParents")] = page_num


def _split_content_stream(raw: bytes) -> list[tuple[str, bytes]]:
    """
    Split a PDF content stream into alternating (type, bytes) segments.
    type is either 'other' or 'text' (BT...ET block).

    This is a byte-level scan that respects string literals (parentheses)
    so we don't false-match BT/ET inside text strings.
    """
    segments: list[tuple[str, bytes]] = []
    pos = 0
    length = len(raw)
    current_start = 0
    in_text = False

    while pos < length:
        # Skip over string literals to avoid false BT/ET matches
        if raw[pos:pos + 1] == b"(":
            depth = 1
            pos += 1
            while pos < length and depth > 0:
                ch = raw[pos:pos + 1]
                if ch == b"\\":
                    pos += 1  # skip escaped char
                elif ch == b"(":
                    depth += 1
                elif ch == b")":
                    depth -= 1
                pos += 1
            continue

        if not in_text:
            # Look for BT (must be preceded by whitespace/start and followed by whitespace/end)
            if raw[pos:pos + 2] == b"BT" and _is_operator_boundary(raw, pos, 2):
                # Everything before BT is 'other'
                if pos > current_start:
                    segments.append(("other", raw[current_start:pos]))
                current_start = pos
                in_text = True
                pos += 2
                continue
        else:
            # Look for ET
            if raw[pos:pos + 2] == b"ET" and _is_operator_boundary(raw, pos, 2):
                end = pos + 2
                segments.append(("text", raw[current_start:end]))
                current_start = end
                in_text = False
                pos = end
                continue

        pos += 1

    # Remaining bytes
    if current_start < length:
        seg_type = "text" if in_text else "other"
        segments.append((seg_type, raw[current_start:]))

    return segments


def _is_operator_boundary(raw: bytes, pos: int, op_len: int) -> bool:
    """Check that the operator at raw[pos:pos+op_len] is delimited properly."""
    before_ok = (pos == 0) or raw[pos - 1:pos] in (
        b" ", b"\n", b"\r", b"\t", b"\x00",
    )
    end = pos + op_len
    after_ok = (end >= len(raw)) or raw[end:end + 1] in (
        b" ", b"\n", b"\r", b"\t", b"\x00",
    )
    return before_ok and after_ok


# ---------------------------------------------------------------------------
# 4. Public remediation entry point
# ---------------------------------------------------------------------------

def remediate_pdf(file_path: str, output_path: str | None = None) -> dict:
    """
    Full remediation pipeline:
      1. Analyze the input PDF (before state).
      2. Extract content and auto-classify blocks by font size.
      3. Write tags, metadata, and marked content operators.
      4. Analyze the output PDF (after state).
      5. Return before/after comparison and output path.
    """
    file_path = str(file_path)
    if output_path is None:
        p = Path(file_path)
        output_path = str(p.parent / f"{p.stem}_remediated{p.suffix}")

    logger.info("Analyzing input PDF: %s", file_path)
    before_analysis = analyze_pdf(file_path)

    logger.info("Extracting content for classification")
    content = extract_content(file_path)
    tag_assignments = _classify_blocks(content)

    if not tag_assignments:
        logger.warning("No text blocks found -- nothing to remediate")
        return {
            "before": before_analysis,
            "after": before_analysis,
            "tag_assignments": [],
            "output_path": file_path,
            "blocks_tagged": 0,
        }

    # Derive title from first H1
    title = "Untitled Document"
    for tag in tag_assignments:
        if tag["type"] == "H1":
            title = tag["text"][:256]
            break

    logger.info("Writing tags: %d elements across %d pages",
                len(tag_assignments),
                len({t["page"] for t in tag_assignments}))

    pdf = pikepdf.open(file_path)

    _add_metadata(pdf, title)
    _build_structure_tree(pdf, tag_assignments)

    # Inject marked content per page
    pages_tags: dict[int, list[dict]] = defaultdict(list)
    for tag in tag_assignments:
        pages_tags[tag["page"]].append(tag)
    for page_num, ptags in pages_tags.items():
        ptags.sort(key=lambda t: t["mcid"])
        _inject_marked_content(pdf, page_num, ptags)

    pdf.save(output_path)
    pdf.close()

    logger.info("Saved remediated PDF: %s", output_path)

    after_analysis = analyze_pdf(output_path)

    # Build tag summary
    tag_summary: dict[str, int] = defaultdict(int)
    for tag in tag_assignments:
        tag_summary[tag["type"]] += 1

    # Collect page dimensions for frontend coordinate mapping
    page_dimensions = [
        {"page": p["page"], "width": p["width"], "height": p["height"]}
        for p in content["pages"]
    ]

    return {
        "before": before_analysis,
        "after": after_analysis,
        "tag_assignments": [
            {
                "type": t["type"],
                "page": t["page"],
                "mcid": t["mcid"],
                "text": t["text"][:120],
                "font_size": t["font_size"],
                "bbox": t["bbox"],
            }
            for t in tag_assignments
        ],
        "page_dimensions": page_dimensions,
        "tag_summary": dict(tag_summary),
        "output_path": output_path,
        "blocks_tagged": len(tag_assignments),
    }
