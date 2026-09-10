"""Outlook OAuth routes — handle Microsoft account connection flow.

Endpoints:
    GET  /outlook/start           → Redirect to Microsoft consent
    GET  /outlook/callback        → Handle Microsoft's redirect
    GET  /outlook/accounts        → List connected Outlook accounts
    POST /outlook/accounts/:id/sync   → Sync emails now
    DELETE /outlook/accounts/:id      → Disconnect account
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from models.accounts import (
    add_outlook_account,
    deactivate_outlook_account,
    get_outlook_account,
    list_outlook_accounts,
    update_outlook_sync_time,
)
from services.outlook_auth import exchange_code, get_authorization_url

logger = logging.getLogger("dmarc.outlook_routes")

router = APIRouter(prefix="/outlook", tags=["outlook"])


# ── Check if Outlook OAuth is configured ─────────────────────────────────────


def _check_configured():
    from config import settings

    if not settings.ms_client_id:
        raise HTTPException(
            status_code=503,
            detail="Outlook OAuth not configured. Set MS_CLIENT_ID."
        )


# ── Start OAuth flow ──────────────────────────────────────────────────────────


@router.get("/start")
async def outlook_start():
    """Redirect user to Microsoft OAuth consent screen."""
    _check_configured()
    url, _state = get_authorization_url()
    return RedirectResponse(url=url)


# ── OAuth callback ────────────────────────────────────────────────────────────


@router.get("/callback")
async def outlook_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Microsoft's redirect after user authorizes."""
    _check_configured()

    if error:
        logger.warning("Outlook OAuth error: %s", error)
        return RedirectResponse(url=f"/?outlook_error={error}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        # Exchange code for tokens
        tokens = exchange_code(code)

        # Extract email from id_token claims
        email = tokens.get("id_token_claims", {}).get("preferred_username", "")
        if not email:
            # Fallback: try to get from access token
            email = tokens.get("id_token_claims", {}).get("email", "")

        # Store in database
        account = add_outlook_account(
            email=email,
            credentials={},
            token={
                "access_token": tokens.get("access_token"),
                "refresh_token": tokens.get("refresh_token"),
                "id_token": tokens.get("id_token"),
                "expires_in": tokens.get("expires_in"),
                "token_type": tokens.get("token_type", "Bearer"),
                "scope": tokens.get("scope", ""),
            },
        )

        logger.info("Outlook account connected: %s", email)

        # Get account ID for backfill
        account_id = account.get("id") if account else None

        # Trigger backfill in background (scan past 10 days)
        if account_id:
            import asyncio
            asyncio.create_task(_trigger_backfill(account_id))

        return RedirectResponse(url="/?outlook_success=true")

    except Exception as exc:
        logger.error("Outlook OAuth callback failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Outlook OAuth failed: {exc}")


# ── List accounts ─────────────────────────────────────────────────────────────


@router.get("/accounts")
async def list_outlook_accounts_endpoint():
    """List all connected Outlook accounts."""
    accounts = list_outlook_accounts()
    # Don't expose tokens in response
    return [
        {
            "id": acc["id"],
            "email": acc["email"],
            "is_active": acc.get("is_active", True),
            "last_sync": acc.get("last_sync"),
            "created_at": acc.get("created_at"),
        }
        for acc in accounts
    ]


# ── Sync account ───────────────────────────────────────────────────────────────


@router.post("/accounts/{account_id}/sync")
async def sync_outlook_account(account_id: int, backfill: bool = False):
    """Sync DMARC emails for a specific Outlook account.

    Args:
        backfill: If true, scan past 10 days of emails
    """
    from services.outlook_sync import sync_account_emails

    account = get_outlook_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        count = await sync_account_emails(account, backfill=backfill)
        update_outlook_sync_time(account_id, datetime.now(timezone.utc).isoformat())
        return {"status": "ok", "reports_synced": count, "backfill": backfill}
    except Exception as exc:
        logger.error("Outlook sync failed for account %s: %s", account_id, exc)
        raise HTTPException(status_code=500, detail=f"Sync failed: {exc}")


# ── Backfill (background task) ─────────────────────────────────────────────────


async def _trigger_backfill(account_id: int) -> None:
    """Trigger backfill for a new account (scan past 10 days)."""
    from services.outlook_sync import sync_account_emails

    account = get_outlook_account(account_id)
    if not account:
        return

    try:
        logger.info("[%s] Starting Outlook backfill (past 10 days)...", account.get("email"))
        count = await sync_account_emails(account, backfill=True)
        update_outlook_sync_time(account_id, datetime.now(timezone.utc).isoformat())
        logger.info("[%s] Outlook backfill complete: %d report(s)", account.get("email"), count)
    except Exception as exc:
        logger.error("[%s] Outlook backfill failed: %s", account.get("email"), exc)


# ── Disconnect account ────────────────────────────────────────────────────────


@router.delete("/accounts/{account_id}")
async def delete_outlook_account_endpoint(account_id: int):
    """Disconnect an Outlook account."""
    account = get_outlook_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    deactivate_outlook_account(account_id)
    return {"status": "ok", "message": "Account disconnected"}
