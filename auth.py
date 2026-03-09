"""
CASO Comply -- API Key Authentication & Usage Tracking

Validates API keys against Supabase and records per-request usage
for metered billing.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

from fastapi import HTTPException
from supabase import Client, create_client

logger = logging.getLogger("caso-comply-api.auth")

# ---------------------------------------------------------------------------
# Supabase client (lazy singleton)
# ---------------------------------------------------------------------------

_supabase: Client | None = None


def _get_supabase() -> Client:
    """Return a cached Supabase client, initializing on first call."""
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
                "for API key authentication"
            )
        _supabase = create_client(url, key)
    return _supabase


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------


def validate_api_key(authorization: str) -> dict:
    """
    Validate an API key from the Authorization header.

    Args:
        authorization: The full header value, e.g. "Bearer caso_ak_abc123..."

    Returns:
        dict with tenant_id, api_key_id, and scopes.

    Raises:
        HTTPException(401) if the key is missing, malformed, inactive,
        expired, or not found.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header. Expected: Bearer caso_ak_...",
        )

    raw_key = authorization[len("Bearer ") :]
    if not raw_key.startswith("caso_ak_"):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key format. Keys must start with 'caso_ak_'.",
        )

    # Hash the key to look it up
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    sb = _get_supabase()
    result = (
        sb.table("api_keys")
        .select("id, tenant_id, scopes, is_active, expires_at")
        .eq("key_hash", key_hash)
        .limit(1)
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    row = result.data[0]

    if not row.get("is_active"):
        raise HTTPException(status_code=401, detail="API key is deactivated.")

    expires_at = row.get("expires_at")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if exp_dt < datetime.now(timezone.utc):
                raise HTTPException(status_code=401, detail="API key has expired.")
        except (ValueError, TypeError):
            logger.warning("Could not parse expires_at '%s' for key %s", expires_at, row["id"])

    logger.info("Authenticated API key %s for tenant %s", row["id"], row["tenant_id"])

    return {
        "tenant_id": row["tenant_id"],
        "api_key_id": row["id"],
        "scopes": row.get("scopes") or [],
    }


def update_last_used(api_key_id: str) -> None:
    """Stamp last_used_at on the API key row."""
    try:
        sb = _get_supabase()
        sb.table("api_keys").update(
            {"last_used_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", api_key_id).execute()
    except Exception:
        logger.exception("Failed to update last_used_at for key %s", api_key_id)


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------


def record_usage(
    tenant_id: str,
    api_key_id: str,
    action: str,
    pages: int,
    filename: str | None = None,
    doc_format: str | None = None,
) -> dict:
    """
    Insert a usage record and return a summary for the current billing period.

    Args:
        tenant_id:  UUID of the tenant / organization.
        api_key_id: UUID of the API key used.
        action:     "analyze" or "remediate".
        pages:      Number of pages processed.
        filename:   Original filename (optional).
        doc_format: File format, e.g. "pdf", "docx" (optional).

    Returns:
        dict with pages_used (this period) and pages_included (from plan).
    """
    sb = _get_supabase()
    now = datetime.now(timezone.utc)
    billing_period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Insert the usage record
    record = {
        "tenant_id": tenant_id,
        "api_key_id": api_key_id,
        "action": action,
        "pages": pages,
        "filename": filename,
        "doc_format": doc_format,
        "billing_period_start": billing_period_start.isoformat(),
        "created_at": now.isoformat(),
    }

    try:
        sb.table("usage_records").insert(record).execute()
    except Exception:
        logger.exception("Failed to insert usage record for tenant %s", tenant_id)
        # Non-fatal -- don't block the request over a billing record failure
        return {"pages_used": -1, "pages_included": -1}

    # Aggregate usage for the current billing period
    try:
        agg = (
            sb.table("usage_records")
            .select("pages")
            .eq("tenant_id", tenant_id)
            .gte("billing_period_start", billing_period_start.isoformat())
            .execute()
        )
        pages_used = sum(row.get("pages", 0) for row in (agg.data or []))
    except Exception:
        logger.exception("Failed to aggregate usage for tenant %s", tenant_id)
        pages_used = -1

    # Look up the tenant's plan to get pages_included
    pages_included = -1
    try:
        plan = (
            sb.table("tenants")
            .select("pages_included")
            .eq("id", tenant_id)
            .limit(1)
            .execute()
        )
        if plan.data:
            pages_included = plan.data[0].get("pages_included", -1)
    except Exception:
        logger.exception("Failed to look up plan for tenant %s", tenant_id)

    return {"pages_used": pages_used, "pages_included": pages_included}
