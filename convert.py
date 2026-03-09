"""
CASO Comply -- Document-to-PDF Conversion Module

Wraps LibreOffice CLI (headless mode) to convert Word, Excel, and
PowerPoint documents to PDF so they can be processed by the existing
PDF analysis and remediation pipeline.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("caso-comply-api.convert")

# File extensions that LibreOffice can convert to PDF
SUPPORTED_EXTENSIONS = {".docx", ".xlsx", ".doc", ".xls", ".pptx", ".ppt"}

CONVERSION_TIMEOUT = 60  # seconds


def is_convertible(filename: str) -> bool:
    """Return True if the file has an extension we can convert to PDF."""
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


def convert_to_pdf(input_path: Path, output_dir: Path) -> Path:
    """
    Convert a document to PDF using LibreOffice headless mode.

    Args:
        input_path: Path to the source document (.docx, .xlsx, etc.)
        output_dir: Directory where the converted PDF will be written.

    Returns:
        Path to the generated PDF file.

    Raises:
        RuntimeError: If conversion fails (timeout, non-zero exit, missing output).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "libreoffice",
        "--headless",
        "--norestore",
        "--convert-to", "pdf",
        "--outdir", str(output_dir),
        str(input_path),
    ]

    logger.info("Converting %s to PDF: %s", input_path.name, " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CONVERSION_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"LibreOffice conversion timed out after {CONVERSION_TIMEOUT}s "
            f"for {input_path.name}"
        ) from exc

    if result.returncode != 0:
        logger.error(
            "LibreOffice exited with code %d\nstdout: %s\nstderr: %s",
            result.returncode,
            result.stdout,
            result.stderr,
        )
        raise RuntimeError(
            f"LibreOffice conversion failed (exit code {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    # LibreOffice names the output file with the same stem but .pdf extension
    expected_output = output_dir / f"{input_path.stem}.pdf"

    if not expected_output.exists():
        raise RuntimeError(
            f"Conversion produced no output file. Expected: {expected_output}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    logger.info(
        "Converted %s -> %s (%d bytes)",
        input_path.name,
        expected_output.name,
        expected_output.stat().st_size,
    )
    return expected_output
