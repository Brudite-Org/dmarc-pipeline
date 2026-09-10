"""Microsoft Outlook OAuth2 service — handles authorization for Graph API.

Flow:
1. User clicks "Connect Outlook" → redirect to Microsoft consent screen
2. User authorizes → Microsoft redirects back to /outlook/callback
3. We exchange code for tokens, fetch email, store in DB

Uses MSAL (Microsoft Authentication Library) for robust token management.

Environment variables:
    MS_CLIENT_ID       — Azure AD app registration client ID
    MS_CLIENT_SECRET   — Azure AD client secret
    MS_REDIRECT_URI    — OAuth callback URL (default: http://localhost:8000/outlook/callback)
    MS_TENANT_ID       — Azure AD tenant ID (default: "common")
"""

from __future__ import annotations

import logging
import os
import secrets
from urllib.parse import urlencode

import msal

from config import settings

logger = logging.getLogger("dmarc.outlook_auth")

# ── Configuration ─────────────────────────────────────────────────────────────

CLIENT_ID = settings.ms_client_id
CLIENT_SECRET = settings.ms_client_secret
REDIRECT_URI = settings.ms_redirect_uri
TENANT_ID = settings.ms_tenant_id

# Microsoft Graph API scopes — READ ONLY
# Note: MSAL does NOT accept openid/profile/offline_access as user-provided scopes.
# These are automatically included by MSAL when using the authorization code flow
# with a confidential client. Only Graph API scopes should be passed in the scopes parameter.
SCOPES = ["https://graph.microsoft.com/Mail.Read"]

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


# ── MSAL client ────────────────────────────────────────────────────────────────


def _get_msal_app() -> msal.ConfidentialClientApplication:
    """Create or return the MSAL confidential client application."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError(
            "MS_CLIENT_ID and MS_CLIENT_SECRET must be set. "
            "Get them from Azure Portal → Azure AD → App registrations."
        )

    return msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
    )


# ── OAuth flow ─────────────────────────────────────────────────────────────────


def get_authorization_url(state: str | None = None) -> tuple[str, str]:
    """Build the Microsoft OAuth authorization URL.

    Returns:
        Tuple of (authorization_url, state) — state must be stored and validated in callback.
    """
    app = _get_msal_app()

    auth_state = state or secrets.token_urlsafe(32)

    logger.info(
        "Building auth URL: client_id=%s, authority=%s, redirect_uri=%s, scopes=%s",
        CLIENT_ID, AUTHORITY, REDIRECT_URI, SCOPES,
    )

    auth_url = app.get_authorization_request_url(
        scopes=SCOPES,
        state=auth_state,
        redirect_uri=REDIRECT_URI,
    )

    logger.info("Auth URL: %s", auth_url)
    return auth_url, auth_state


async def exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens.

    Returns:
        Dict with access_token, refresh_token, expires_in, etc.
    """
    app = _get_msal_app()

    result = app.acquire_token_by_authorization_code(
        code=code,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    if "error" in result:
        raise RuntimeError(
            f"Token exchange failed: {result.get('error')} — {result.get('error_description', '')}"
        )

    if "access_token" not in result:
        raise RuntimeError(f"Token exchange returned no access_token: {result}")

    logger.info("Token exchange successful for Outlook account")
    return result


async def get_valid_access_token(token_json: dict) -> str:
    """Get a valid access token, refreshing if necessary.

    Uses MSAL's token cache logic — handles refresh tokens automatically.

    Args:
        token_json: Dict containing access_token, refresh_token, expires_at, etc.

    Returns:
        Valid access token string.
    """
    app = _get_msal_app()

    # Try to acquire token silently using cached refresh token
    refresh_token = token_json.get("refresh_token")

    if refresh_token:
        result = app.acquire_token_by_refresh_token(
            refresh_token=refresh_token,
            scopes=SCOPES,
        )

        if "access_token" in result:
            # Update token_json with new token (caller should persist)
            token_json["access_token"] = result["access_token"]
            if "refresh_token" in result:
                token_json["refresh_token"] = result["refresh_token"]
            # MSAL returns expires_in as seconds from now
            if "expires_in" in result:
                from datetime import datetime, timedelta, timezone
                token_json["expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=result["expires_in"])
                ).isoformat()
            return result["access_token"]

    # Fallback: check if existing token is still valid
    from datetime import datetime, timezone
    expires_at = token_json.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            if datetime.now(timezone.utc) < expiry:
                return token_json["access_token"]
        except (ValueError, TypeError):
            pass

    raise RuntimeError(
        "Unable to get valid access token. Re-authentication required."
    )


def get_outlook_client_config() -> dict | None:
    """Get OAuth client config from environment.

    Returns None if Outlook is not configured.
    """
    if not CLIENT_ID or not CLIENT_SECRET:
        return None

    return {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "authority": AUTHORITY,
        "redirect_uri": REDIRECT_URI,
    }
