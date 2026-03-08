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

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from remediation import analyze_pdf, remediate_pdf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

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
    """Persist an uploaded PDF and return (file_id, path)."""
    if not upload.filename or not upload.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    file_id = uuid.uuid4().hex[:12]
    safe_name = f"{file_id}_{Path(upload.filename).name}"
    dest = UPLOAD_DIR / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)

    logger.info("Saved upload %s (%d bytes)", dest.name, dest.stat().st_size)
    return file_id, dest


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
            "GET  /api/download/{file_id}",
        ],
    }


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    """
    Upload a PDF and receive an accessibility analysis.

    Returns score, grade, structural checks, content summary, and issues.
    """
    file_id, path = _save_upload(file)

    try:
        result = analyze_pdf(str(path))
    except Exception as exc:
        logger.exception("Analysis failed for %s", path.name)
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc

    return {
        "file_id": file_id,
        "filename": file.filename,
        "score": result["score"],
        "structure": result["structure"],
        "content": {
            "total_text_blocks": result["content"]["total_text_blocks"],
            "total_images": result["content"]["total_images"],
            "pages_analyzed": result["content"]["pages_analyzed"],
        },
        "tables": result["tables"],
    }


@app.post("/api/remediate")
async def remediate(file: UploadFile = File(...)):
    """
    Upload a PDF, remediate it, and receive before/after comparison
    plus a download URL for the remediated file.
    """
    file_id, path = _save_upload(file)

    output_name = f"{file_id}_remediated.pdf"
    output_path = OUTPUT_DIR / output_name

    try:
        result = remediate_pdf(str(path), str(output_path))
    except Exception as exc:
        logger.exception("Remediation failed for %s", path.name)
        raise HTTPException(status_code=500, detail=f"Remediation failed: {exc}") from exc

    return {
        "file_id": file_id,
        "filename": file.filename,
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
    }


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
