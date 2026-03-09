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
    api_key_id: str | None,
    action: str,
    pages: int,
    filename: str | None = None,
    doc_format: str | None = None,
) -> dict:
    """
    Insert a usage record and return a summary for the current billing period.

    Args:
        tenant_id:  UUID of the tenant / organization.
        api_key_id: UUID of the API key used (None for demo/anonymous).
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
        "action": action,
        "pages_consumed": pages,
        "document_filename": filename,
        "document_format": doc_format,
        "billing_period_start": billing_period_start.date().isoformat(),
    }
    if api_key_id is not None:
        record["api_key_id"] = api_key_id

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
            .select("pages_consumed")
            .eq("tenant_id", tenant_id)
            .gte("billing_period_start", billing_period_start.date().isoformat())
            .execute()
        )
        pages_used = sum(row.get("pages_consumed", 0) for row in (agg.data or []))
    except Exception:
        logger.exception("Failed to aggregate usage for tenant %s", tenant_id)
        pages_used = -1

    # Look up the tenant's plan to get pages_included
    pages_included = -1
    try:
        plan = (
            sb.table("tenants")
            .select("subscription_plans(pages_included)")
            .eq("id", tenant_id)
            .limit(1)
            .execute()
        )
        if plan.data:
            sp = plan.data[0].get("subscription_plans")
            if isinstance(sp, dict):
                pages_included = sp.get("pages_included", -1)
            elif isinstance(sp, list) and sp:
                pages_included = sp[0].get("pages_included", -1)
    except Exception:
        logger.exception("Failed to look up plan for tenant %s", tenant_id)

    return {"pages_used": pages_used, "pages_included": pages_included}


# ---------------------------------------------------------------------------
# Tenant enforcement
# ---------------------------------------------------------------------------


def enforce_tenant_access(tenant_id: str, required_scope: str | None = None) -> dict:
    """
    Verify a tenant is allowed to make requests.

    Checks (in order):
    1. Tenant exists and status is not suspended/cancelled.
    2. If trial, trial_ends_at has not passed.
    3. If required_scope provided, the tenant's plan features include it.
    4. Usage is within plan limits (unless the plan allows overage).

    Args:
        tenant_id:      UUID of the tenant.
        required_scope: Optional feature flag to check, e.g. "api_access".

    Returns:
        dict with tenant info including org_name, plan_name, status,
        pages_included, pages_used, pages_remaining, and features.

    Raises:
        HTTPException(403) for access denied (suspended, cancelled, trial
            expired, missing feature).
        HTTPException(429) for plan usage limit exceeded with no overage.
    """
    sb = _get_supabase()

    # ---- 1. Look up tenant + joined plan info ----
    try:
        result = (
            sb.table("tenants")
            .select(
                "id, org_name, status, trial_ends_at, plan_id, "
                "subscription_plans(id, name, pages_included, features, overage_rate_cents)"
            )
            .eq("id", tenant_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception("Failed to look up tenant %s", tenant_id)
        raise HTTPException(
            status_code=500,
            detail="Unable to verify account status. Please try again later.",
        ) from exc

    if not result.data:
        raise HTTPException(status_code=403, detail="Account not found.")

    tenant = result.data[0]
    plan = tenant.get("subscription_plans") or {}

    # ---- 2. Check account status ----
    status = (tenant.get("status") or "").lower()

    if status == "suspended":
        raise HTTPException(
            status_code=403,
            detail=(
                "Account suspended. Please contact support@casocomply.com "
                "to resolve any outstanding issues and restore access."
            ),
        )

    if status == "cancelled":
        raise HTTPException(
            status_code=403,
            detail=(
                "Account cancelled. If you'd like to reactivate your subscription, "
                "please contact support@casocomply.com."
            ),
        )

    # ---- 3. Check trial expiration ----
    if status == "trial":
        trial_ends_at = tenant.get("trial_ends_at")
        if trial_ends_at:
            try:
                trial_dt = datetime.fromisoformat(
                    trial_ends_at.replace("Z", "+00:00")
                )
                if trial_dt < datetime.now(timezone.utc):
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Your free trial has expired. "
                            "Please upgrade to a paid plan at https://app.casocomply.com/billing "
                            "to continue using the API."
                        ),
                    )
            except (ValueError, TypeError):
                logger.warning(
                    "Could not parse trial_ends_at '%s' for tenant %s",
                    trial_ends_at,
                    tenant_id,
                )

    # ---- 4. Check required feature / scope ----
    features = plan.get("features") or {}
    if required_scope and not features.get(required_scope):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your current plan ({plan.get('name', 'Unknown')}) does not include "
                f"the '{required_scope}' feature. Please upgrade your plan at "
                "https://app.casocomply.com/billing."
            ),
        )

    # ---- 5. Check usage limits ----
    pages_included = plan.get("pages_included", 0)
    overage_rate_cents = plan.get("overage_rate_cents", 0)

    now = datetime.now(timezone.utc)
    billing_period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    pages_used = 0
    try:
        agg = (
            sb.table("usage_records")
            .select("pages_consumed")
            .eq("tenant_id", tenant_id)
            .gte("billing_period_start", billing_period_start.date().isoformat())
            .execute()
        )
        pages_used = sum(row.get("pages_consumed", 0) for row in (agg.data or []))
    except Exception:
        logger.exception("Failed to aggregate usage for tenant %s", tenant_id)

    pages_remaining = max(0, pages_included - pages_used)

    if pages_used >= pages_included and pages_included > 0:
        if not overage_rate_cents:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Plan limit exceeded. You have used {pages_used:,} of "
                    f"{pages_included:,} pages included in your "
                    f"{plan.get('name', '')} plan this billing period. "
                    "Upgrade your plan or wait until the next billing cycle. "
                    "Visit https://app.casocomply.com/billing to manage your subscription."
                ),
            )
        else:
            logger.warning(
                "Tenant %s exceeded included pages (%d/%d) -- overage billing applies at %d cents/page",
                tenant_id,
                pages_used,
                pages_included,
                overage_rate_cents,
            )

    return {
        "tenant_id": tenant_id,
        "org_name": tenant.get("org_name", ""),
        "status": status,
        "plan_name": plan.get("name", "Unknown"),
        "plan_id": plan.get("id"),
        "pages_included": pages_included,
        "pages_used": pages_used,
        "pages_remaining": pages_remaining,
        "features": features,
    }
