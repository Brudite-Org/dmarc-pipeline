"""OAuth routes — handle Gmail account connection flow.

Endpoints:
    GET  /oauth/start        → Redirect to Google consent
    GET  /oauth/callback     → Handle Google's redirect
    GET  /oauth/accounts     → List connected accounts
    POST /oauth/accounts/:id/sync    → Sync emails now
    DELETE /oauth/accounts/:id       → Disconnect account
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from models.accounts import (
    add_account,
    deactivate_account,
    get_account,
    list_accounts,
    update_sync_time,
    update_token,
)
from services.oauth import (
    CLIENT_ID,
    CLIENT_SECRET,
    exchange_code,
    get_authorization_url,
    get_email_from_token,
)

logger = logging.getLogger("dmarc.oauth_routes")

router = APIRouter(prefix="/oauth", tags=["oauth"])


# ── Check if OAuth is configured ──────────────────────────────────────────────


def _check_configured():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET."
        )


# ── Start OAuth flow ──────────────────────────────────────────────────────────


@router.get("/start")
async def oauth_start():
    """Redirect user to Google OAuth consent screen."""
    _check_configured()
    url = get_authorization_url()
    return RedirectResponse(url=url)


# ── OAuth callback ────────────────────────────────────────────────────────────


@router.get("/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Handle Google's redirect after user authorizes."""
    _check_configured()

    if error:
        logger.warning("OAuth error: %s", error)
        return RedirectResponse(url=f"/?oauth_error={error}")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        # Exchange code for tokens
        tokens = await exchange_code(code)

        # Fetch user's email
        email = await get_email_from_token(tokens["access_token"])

        # Calculate expiry
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600))
        ).isoformat()

        # Store in database
        account = add_account(
            email=email,
            credentials={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
            token={
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token"),
                "expires_at": expires_at,
                "token_type": tokens.get("token_type", "Bearer"),
                "scope": tokens.get("scope", ""),
            },
        )

        logger.info("Gmail account connected: %s", email)

        # Get account ID for backfill
        account_id = account.get("id") if account else None

        # Trigger backfill in background (scan past 10 days)
        if account_id:
            import asyncio
            asyncio.create_task(_trigger_backfill(account_id))

        return RedirectResponse(url="/?oauth_success=true")

    except Exception as exc:
        logger.error("OAuth callback failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"OAuth failed: {exc}")


# ── List accounts ─────────────────────────────────────────────────────────────


@router.get("/accounts")
async def list_oauth_accounts():
    """List all connected Gmail accounts."""
    accounts = list_accounts()
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
async def sync_account(account_id: int, backfill: bool = False):
    """Sync DMARC emails for a specific account.

    Args:
        backfill: If true, scan past 10 days of emails
    """
    from services.gmail_sync import sync_account_emails

    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        count = await sync_account_emails(account, backfill=backfill)
        # sync_account_emails() may have refreshed the access token in
        # memory (account["token_json"] is mutated by reference) — persist
        # it so the next sync doesn't have to refresh again.
        update_token(account_id, account.get("token_json", {}))
        update_sync_time(account_id, datetime.now(timezone.utc).isoformat())
        return {"status": "ok", "reports_synced": count, "backfill": backfill}
    except Exception as exc:
        logger.error("Sync failed for account %s: %s", account_id, exc)
        raise HTTPException(status_code=500, detail=f"Sync failed: {exc}")


# ── Backfill (background task) ─────────────────────────────────────────────────


async def _trigger_backfill(account_id: int) -> None:
    """Trigger backfill for a new account (scan past 10 days)."""
    from services.gmail_sync import sync_account_emails

    account = get_account(account_id)
    if not account:
        return

    try:
        logger.info("[%s] Starting backfill (past 10 days)...", account.get("email"))
        count = await sync_account_emails(account, backfill=True)
        update_token(account_id, account.get("token_json", {}))
        update_sync_time(account_id, datetime.now(timezone.utc).isoformat())
        logger.info("[%s] Backfill complete: %d report(s)", account.get("email"), count)
    except Exception as exc:
        logger.error("[%s] Backfill failed: %s", account.get("email"), exc)


# ── Disconnect account ────────────────────────────────────────────────────────


@router.delete("/accounts/{account_id}")
async def delete_oauth_account(account_id: int):
    """Disconnect a Gmail account."""
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    deactivate_account(account_id)
    return {"status": "ok", "message": "Account disconnected"}
