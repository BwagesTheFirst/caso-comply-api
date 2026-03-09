"""
CASO Comply -- PDF Accessibility Remediation API

FastAPI service consumed by the Next.js frontend.
Provides PDF analysis, automated remediation, and file download.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from auth import validate_api_key, record_usage, update_last_used
from convert import is_convertible, convert_to_pdf
from remediation import analyze_pdf, remediate_pdf_async

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

ACCEPTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".doc", ".xls", ".pptx", ".ppt"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("caso-comply-api")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CASO Comply API",
    description="PDF accessibility analysis and remediation service",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3099",
        "https://caso-comply.vercel.app",
    ],
    allow_origin_regex=r"https://.*\.render\.com|https://caso-comply.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_upload(upload: UploadFile) -> tuple[str, Path]:
    """Persist an uploaded file and return (file_id, path)."""
    if not upload.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    ext = Path(upload.filename).suffix.lower()
    if ext not in ACCEPTED_EXTENSIONS:
        accepted = ", ".join(sorted(ACCEPTED_EXTENSIONS))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Accepted: {accepted}",
        )

    file_id = uuid.uuid4().hex[:12]
    safe_name = f"{file_id}_{Path(upload.filename).name}"
    dest = UPLOAD_DIR / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)

    logger.info("Saved upload %s (%d bytes)", dest.name, dest.stat().st_size)
    return file_id, dest


def _ensure_pdf(file_id: str, file_path: Path, original_filename: str) -> tuple[Path, bool]:
    """
    If the file is not a PDF, convert it to PDF using LibreOffice.

    Returns:
        (pdf_path, was_converted) -- the path to the PDF and whether
        conversion was performed.
    """
    if file_path.suffix.lower() == ".pdf":
        return file_path, False

    if not is_convertible(original_filename):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot convert {original_filename} to PDF",
        )

    logger.info("Converting %s to PDF before processing", original_filename)
    try:
        pdf_path = convert_to_pdf(file_path, UPLOAD_DIR)
    except RuntimeError as exc:
        logger.exception("Conversion failed for %s", original_filename)
        raise HTTPException(
            status_code=500,
            detail=f"Document conversion failed: {exc}",
        ) from exc

    # Rename to include the file_id so downstream code can find it
    final_path = UPLOAD_DIR / f"{file_id}_{pdf_path.name}"
    pdf_path.rename(final_path)
    logger.info("Converted PDF saved as %s", final_path.name)
    return final_path, True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health check endpoint for Render and uptime monitors."""
    return {"status": "ok"}


@app.get("/")
async def root():
    return {
        "service": "CASO Comply API",
        "version": "0.1.0",
        "endpoints": [
            "GET  /health",
            "POST /api/analyze",
            "POST /api/remediate",
            "POST /api/verify/{file_id}",
            "GET  /api/download/{file_id}",
        ],
    }


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    authorization: str | None = Header(None),
):
    """
    Upload a PDF and receive an accessibility analysis.

    Returns score, grade, structural checks, content summary, and issues.
    Optionally authenticate with a Bearer token for usage tracking.
    """
    # Optional API key authentication
    auth_ctx: dict | None = None
    if authorization:
        auth_ctx = validate_api_key(authorization)

    file_id, path = _save_upload(file)
    original_filename = file.filename or ""
    pdf_path, was_converted = _ensure_pdf(file_id, path, original_filename)

    try:
        result = analyze_pdf(str(pdf_path))
    except Exception as exc:
        logger.exception("Analysis failed for %s", pdf_path.name)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc

    original_ext = Path(original_filename).suffix.lower() if original_filename else ".pdf"
    page_count = result["content"].get("pages_analyzed", 0)

    body = {
        "file_id": file_id,
        "filename": file.filename,
        "original_format": original_ext.lstrip("."),
        "was_converted": was_converted,
        "score": result["score"],
        "structure": result["structure"],
        "content": {
            "total_text_blocks": result["content"]["total_text_blocks"],
            "total_images": result["content"]["total_images"],
            "pages_analyzed": result["content"]["pages_analyzed"],
        },
        "tables": result["tables"],
    }

    # Track usage & build response headers when authenticated
    headers: dict[str, str] = {}
    if auth_ctx:
        usage = record_usage(
            tenant_id=auth_ctx["tenant_id"],
            api_key_id=auth_ctx["api_key_id"],
            action="analyze",
            pages=page_count,
            filename=original_filename,
            doc_format=original_ext.lstrip("."),
        )
        update_last_used(auth_ctx["api_key_id"])
        headers["X-CASO-Pages-Used"] = str(usage["pages_used"])
        headers["X-CASO-Pages-Remaining"] = str(
            max(0, usage["pages_included"] - usage["pages_used"])
            if usage["pages_included"] >= 0 and usage["pages_used"] >= 0
            else -1
        )

    return JSONResponse(content=body, headers=headers)


@app.post("/api/remediate")
async def remediate(
    file: UploadFile = File(...),
    verify: bool = Query(True, description="Run Gemini AI verification on tag assignments"),
    authorization: str | None = Header(None),
):
    """
    Upload a PDF, remediate it, and receive before/after comparison
    plus a download URL for the remediated file.

    Set ?verify=false to skip the Gemini AI verification step.
    Optionally authenticate with a Bearer token for usage tracking.
    """
    # Optional API key authentication
    auth_ctx: dict | None = None
    if authorization:
        auth_ctx = validate_api_key(authorization)

    file_id, path = _save_upload(file)
    original_filename = file.filename or ""
    pdf_path, was_converted = _ensure_pdf(file_id, path, original_filename)

    output_name = f"{file_id}_remediated.pdf"
    output_path = OUTPUT_DIR / output_name

    try:
        result = await remediate_pdf_async(str(pdf_path), str(output_path), verify=verify)
    except Exception as exc:
        logger.exception("Remediation failed for %s", pdf_path.name)
        raise HTTPException(status_code=500, detail=f"Remediation failed: {exc}") from exc

    original_ext = Path(original_filename).suffix.lower() if original_filename else ".pdf"

    # Determine page count from the analysis result
    page_count = 0
    if "before" in result and "structure" in result["before"]:
        # Try to get page count from structure or content
        page_count = result["before"].get("content", {}).get("pages_analyzed", 0)
    if page_count == 0 and "after" in result and "structure" in result["after"]:
        page_count = result["after"].get("content", {}).get("pages_analyzed", 0)
    # Fallback: count from blocks_tagged
    if page_count == 0:
        page_count = max(
            (b.get("page", 0) for b in result.get("tag_assignments", []) if isinstance(b, dict)),
            default=1,
        )

    body = {
        "file_id": file_id,
        "filename": file.filename,
        "original_format": original_ext.lstrip("."),
        "was_converted": was_converted,
        "download_url": f"/api/download/{file_id}",
        "blocks_tagged": result["blocks_tagged"],
        "tag_summary": result.get("tag_summary", {}),
        "before": {
            "score": result["before"]["score"],
            "structure": result["before"]["structure"],
        },
        "after": {
            "score": result["after"]["score"],
            "structure": result["after"]["structure"],
        },
        "tag_assignments": result["tag_assignments"],
        "page_dimensions": result.get("page_dimensions", []),
    }

    # Include Gemini verification details when available
    if "verification" in result:
        body["verification"] = result["verification"]

    # Track usage & build response headers when authenticated
    headers: dict[str, str] = {}
    if auth_ctx:
        usage = record_usage(
            tenant_id=auth_ctx["tenant_id"],
            api_key_id=auth_ctx["api_key_id"],
            action="remediate",
            pages=page_count,
            filename=original_filename,
            doc_format=original_ext.lstrip("."),
        )
        update_last_used(auth_ctx["api_key_id"])
        headers["X-CASO-Pages-Used"] = str(usage["pages_used"])
        headers["X-CASO-Pages-Remaining"] = str(
            max(0, usage["pages_included"] - usage["pages_used"])
            if usage["pages_included"] >= 0 and usage["pages_used"] >= 0
            else -1
        )

    return JSONResponse(content=body, headers=headers)


@app.post("/api/verify/{file_id}")
async def verify(file_id: str):
    """
    Run Gemini AI verification on a previously remediated PDF.

    This is a premium feature -- the frontend calls it separately
    after the initial (unverified) remediation is complete.
    """
    if not file_id.isalnum() or len(file_id) > 24:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    # Find the original uploaded file
    upload_matches = list(UPLOAD_DIR.glob(f"{file_id}_*"))
    if not upload_matches:
        raise HTTPException(status_code=404, detail="Original upload not found")

    upload_path = upload_matches[0]

    # Find the remediated file
    output_path = OUTPUT_DIR / f"{file_id}_remediated.pdf"
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Remediated file not found -- run remediation first")

    try:
        result = await remediate_pdf_async(str(upload_path), str(output_path), verify=True)
    except Exception as exc:
        logger.exception("Verification failed for %s", upload_path.name)
        raise HTTPException(status_code=500, detail=f"Verification failed: {exc}") from exc

    response = {
        "file_id": file_id,
        "blocks_tagged": result["blocks_tagged"],
        "tag_summary": result.get("tag_summary", {}),
        "tag_assignments": result["tag_assignments"],
        "page_dimensions": result.get("page_dimensions", []),
        "after": {
            "score": result["after"]["score"],
            "structure": result["after"]["structure"],
        },
    }

    if "verification" in result:
        response["verification"] = result["verification"]

    return response


@app.get("/api/download/{file_id}")
async def download(file_id: str):
    """Download a previously remediated PDF by file ID."""
    # Validate file_id format (hex string)
    if not file_id.isalnum() or len(file_id) > 24:
        raise HTTPException(status_code=400, detail="Invalid file ID")

    pattern = f"{file_id}_remediated.pdf"
    matches = list(OUTPUT_DIR.glob(pattern))

    if not matches:
        raise HTTPException(status_code=404, detail="Remediated file not found")

    target = matches[0]
    return FileResponse(
        path=str(target),
        media_type="application/pdf",
        filename=target.name,
    )
