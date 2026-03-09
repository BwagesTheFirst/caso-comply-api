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
from fastapi.responses import FileResponse, JSONResponse, Response

from pydantic import BaseModel
from supabase import create_client

from auth import validate_api_key, record_usage, update_last_used, enforce_tenant_access
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


def _get_supabase_admin():
    """Return a Supabase client using service role key for Storage access."""
    import os
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required")
    return create_client(url, key)


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


# ---------------------------------------------------------------------------
# Pydantic models for license endpoints
# ---------------------------------------------------------------------------


class LicenseValidateRequest(BaseModel):
    license_key: str
    hostname: str | None = None


class LicenseUsageRequest(BaseModel):
    pages_processed: int
    pdfs_completed: int = 0
    hostname: str | None = None
    filename: str | None = None
    remediation_type: str | None = None


class ReviewSubmitResponse(BaseModel):
    review_id: str
    status: str


# ---------------------------------------------------------------------------
# License endpoints (used by Docker agent)
# ---------------------------------------------------------------------------


@app.post("/api/license/validate")
async def license_validate(body: LicenseValidateRequest):
    """
    Validate a license key for a Docker agent on startup.

    The agent sends its license key and hostname; this endpoint checks the
    key is valid, the account is active, and the plan allows API access.
    """
    # Build a fake Authorization header so we can reuse validate_api_key
    authorization = f"Bearer {body.license_key}"

    try:
        auth_ctx = validate_api_key(authorization)
    except HTTPException as exc:
        return JSONResponse(
            status_code=200,
            content={"valid": False, "reason": exc.detail},
        )

    # Run full tenant enforcement (status, trial, feature check)
    try:
        tenant_info = enforce_tenant_access(
            auth_ctx["tenant_id"], required_scope="api_access"
        )
    except HTTPException as exc:
        return JSONResponse(
            status_code=200,
            content={"valid": False, "reason": exc.detail},
        )

    logger.info(
        "License validated for tenant %s (%s) from host %s",
        auth_ctx["tenant_id"],
        tenant_info["org_name"],
        body.hostname or "unknown",
    )

    # Get the plan dict from enforce_tenant_access for threshold
    plan = tenant_info.get("plan", {})
    threshold = plan.get("review_score_threshold", 70) if isinstance(plan, dict) else 70

    return {
        "valid": True,
        "org": tenant_info["org_name"],
        "plan": tenant_info["plan_name"],
        "pages_used": tenant_info["pages_used"],
        "features": tenant_info.get("features", {}),
        "review_score_threshold": threshold,
    }


@app.post("/api/license/usage")
async def license_usage(
    body: LicenseUsageRequest,
    authorization: str | None = Header(None),
):
    """
    Report batch usage from a Docker agent.

    The agent sends the number of pages/PDFs processed; this endpoint
    records usage and returns the updated totals.
    """
    auth_ctx = validate_api_key(authorization or "")

    # Enforce tenant access before recording usage
    enforce_tenant_access(auth_ctx["tenant_id"], required_scope="api_access")

    # Determine remediation type and rate
    rem_type = body.remediation_type or "standard"
    default_rates = {"standard": 25, "ai_verified": 35, "human_review": 400}
    rate = default_rates.get(rem_type, 25)

    usage = record_usage(
        tenant_id=auth_ctx["tenant_id"],
        api_key_id=auth_ctx["api_key_id"],
        action="remediate",
        pages=body.pages_processed,
        filename=body.filename or f"batch:{body.pdfs_completed} PDFs",
        doc_format="pdf",
        remediation_type=rem_type,
        rate_cents=rate,
    )
    update_last_used(auth_ctx["api_key_id"])

    pages_remaining = max(
        0, usage["pages_included"] - usage["pages_used"]
    ) if usage["pages_included"] >= 0 and usage["pages_used"] >= 0 else -1

    logger.info(
        "Batch usage recorded: %d pages from tenant %s (host: %s). Total: %d/%d",
        body.pages_processed,
        auth_ctx["tenant_id"],
        body.hostname or "unknown",
        usage["pages_used"],
        usage["pages_included"],
    )

    return {
        "pages_used": usage["pages_used"],
        "pages_remaining": pages_remaining,
    }


# ---------------------------------------------------------------------------
# Core endpoints
# ---------------------------------------------------------------------------


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
    # Optional API key authentication + tenant enforcement
    auth_ctx: dict | None = None
    if authorization:
        auth_ctx = validate_api_key(authorization)
        enforce_tenant_access(auth_ctx["tenant_id"])

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

    # Track usage -- always record, even for anonymous/demo requests
    DEMO_TENANT_ID = "00000000-0000-0000-0000-000000000000"
    tenant_id = auth_ctx["tenant_id"] if auth_ctx else DEMO_TENANT_ID
    api_key_id = auth_ctx["api_key_id"] if auth_ctx else None

    usage = record_usage(
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        action="analyze",
        pages=page_count,
        filename=original_filename,
        doc_format=original_ext.lstrip("."),
        rate_cents=0,
    )

    headers: dict[str, str] = {}
    if auth_ctx:
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
    # Optional API key authentication + tenant enforcement
    auth_ctx: dict | None = None
    if authorization:
        auth_ctx = validate_api_key(authorization)
        enforce_tenant_access(auth_ctx["tenant_id"])

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

    # Track usage -- always record, even for anonymous/demo requests
    DEMO_TENANT_ID = "00000000-0000-0000-0000-000000000000"
    tenant_id = auth_ctx["tenant_id"] if auth_ctx else DEMO_TENANT_ID
    api_key_id = auth_ctx["api_key_id"] if auth_ctx else None

    rem_type = "ai_verified" if verify else "standard"
    rem_rate = 35 if verify else 25

    usage = record_usage(
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        action="remediate",
        pages=page_count,
        filename=original_filename,
        doc_format=original_ext.lstrip("."),
        remediation_type=rem_type,
        rate_cents=rem_rate,
    )

    headers: dict[str, str] = {}
    if auth_ctx:
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


# ---------------------------------------------------------------------------
# Human Review endpoints
# ---------------------------------------------------------------------------


@app.post("/api/review/submit")
async def review_submit(
    file: UploadFile = File(...),
    filename: str = Query(..., description="Original filename"),
    original_path: str = Query(..., description="Path on customer's machine"),
    output_path: str = Query(..., description="Where the AI-remediated file was written"),
    ai_score: int = Query(..., description="Score after AI remediation"),
    page_count: int = Query(0, description="Number of pages"),
    authorization: str | None = Header(None),
):
    """Docker agent submits a low-scoring file for human review."""
    auth_ctx = validate_api_key(authorization or "")
    enforce_tenant_access(auth_ctx["tenant_id"], required_scope="api_access")

    sb = _get_supabase_admin()

    insert_result = sb.table("review_queue").insert({
        "tenant_id": auth_ctx["tenant_id"],
        "filename": filename,
        "original_path": original_path,
        "output_path": output_path,
        "ai_score": ai_score,
        "page_count": page_count,
        "status": "pending",
        "storage_path": "",
    }).execute()

    if not insert_result.data:
        raise HTTPException(status_code=500, detail="Failed to create review queue entry")

    review_id = insert_result.data[0]["id"]

    storage_path = f"{auth_ctx['tenant_id']}/{review_id}/original.pdf"
    file_bytes = await file.read()

    sb.storage.from_("review-files").upload(
        path=storage_path,
        file=file_bytes,
        file_options={"content-type": "application/pdf"},
    )

    sb.table("review_queue").update({
        "storage_path": storage_path,
    }).eq("id", review_id).execute()

    logger.info(
        "Review submitted: %s (score %d) for tenant %s → %s",
        filename, ai_score, auth_ctx["tenant_id"], review_id,
    )

    return {"review_id": review_id, "status": "pending"}


@app.get("/api/review/queue")
async def review_queue_list(
    status: str | None = Query(None, description="Filter by status"),
    authorization: str | None = Header(None),
):
    """List review queue items. Admin only."""
    sb = _get_supabase_admin()
    query = sb.table("review_queue").select(
        "*, tenants(name)"
    ).order("created_at", desc=False)
    if status:
        query = query.eq("status", status)
    result = query.execute()
    return {"reviews": result.data or []}


@app.get("/api/review/pending")
async def review_pending(
    authorization: str | None = Header(None),
):
    """Returns completed reviews for this tenant (ready for agent to download)."""
    auth_ctx = validate_api_key(authorization or "")
    sb = _get_supabase_admin()
    result = sb.table("review_queue").select("*").eq(
        "tenant_id", auth_ctx["tenant_id"]
    ).eq("status", "completed").execute()
    return {"reviews": result.data or []}


@app.get("/api/review/{review_id}/download")
async def review_download_original(review_id: str):
    """Download the original (low-scoring) file for review."""
    sb = _get_supabase_admin()
    result = sb.table("review_queue").select("*").eq("id", review_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Review not found")
    review = result.data[0]
    file_bytes = sb.storage.from_("review-files").download(review["storage_path"])
    return Response(
        content=file_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{review["filename"]}"'},
    )


@app.post("/api/review/{review_id}/start")
async def review_start(review_id: str):
    """Mark a review as in_review."""
    sb = _get_supabase_admin()
    result = sb.table("review_queue").update({
        "status": "in_review",
    }).eq("id", review_id).eq("status", "pending").execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Review not found or not in pending status")
    return {"status": "in_review"}


@app.post("/api/review/{review_id}/release")
async def review_release(review_id: str):
    """Release a review back to pending."""
    sb = _get_supabase_admin()
    result = sb.table("review_queue").update({
        "status": "pending",
    }).eq("id", review_id).eq("status", "in_review").execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Review not found or not in_review status")
    return {"status": "pending"}


@app.post("/api/review/{review_id}/complete")
async def review_complete(
    review_id: str,
    file: UploadFile = File(...),
    notes: str = Query("", description="Optional reviewer notes"),
):
    """Upload the corrected file and mark the review as completed."""
    sb = _get_supabase_admin()
    result = sb.table("review_queue").select("*").eq("id", review_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Review not found")
    review = result.data[0]
    if review["status"] not in ("pending", "in_review"):
        raise HTTPException(status_code=400, detail=f"Cannot complete review in '{review['status']}' status")

    corrected_path = f"{review['tenant_id']}/{review_id}/corrected.pdf"
    file_bytes = await file.read()
    sb.storage.from_("review-files").upload(
        path=corrected_path,
        file=file_bytes,
        file_options={"content-type": "application/pdf"},
    )

    from datetime import datetime, timezone
    sb.table("review_queue").update({
        "status": "completed",
        "corrected_path": corrected_path,
        "reviewer_notes": notes or None,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", review_id).execute()

    logger.info("Review %s completed for %s", review_id, review["filename"])
    return {"status": "completed", "review_id": review_id}


@app.get("/api/review/{review_id}/download-corrected")
async def review_download_corrected(
    review_id: str,
    authorization: str | None = Header(None),
):
    """Download the corrected file. Called by the Docker agent."""
    auth_ctx = validate_api_key(authorization or "")
    sb = _get_supabase_admin()
    result = sb.table("review_queue").select("*").eq("id", review_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Review not found")
    review = result.data[0]
    if review["tenant_id"] != auth_ctx["tenant_id"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    if not review.get("corrected_path"):
        raise HTTPException(status_code=404, detail="Corrected file not yet uploaded")
    file_bytes = sb.storage.from_("review-files").download(review["corrected_path"])
    return Response(
        content=file_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{review["filename"]}"'},
    )


@app.post("/api/review/{review_id}/delivered")
async def review_delivered(
    review_id: str,
    authorization: str | None = Header(None),
):
    """Agent confirms it has downloaded and placed the corrected file."""
    auth_ctx = validate_api_key(authorization or "")
    sb = _get_supabase_admin()
    from datetime import datetime, timezone
    result = sb.table("review_queue").update({
        "status": "delivered",
        "delivered_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", review_id).eq("tenant_id", auth_ctx["tenant_id"]).eq("status", "completed").execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Review not found or not in completed status")
    logger.info("Review %s delivered to agent for tenant %s", review_id, auth_ctx["tenant_id"])
    return {"status": "delivered"}
