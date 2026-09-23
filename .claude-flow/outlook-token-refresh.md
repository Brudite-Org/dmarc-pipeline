---
name: outlook-token-refresh
description: Outlook OAuth callback omits expires_at, forcing token refresh on every sync
metadata:
  type: project
---

Fact (observed 2026-09-23, not yet fixed): `api/outlook_routes.py` OAuth callback persists the token dict WITHOUT `expires_at`, while `services/outlook_auth.py::get_valid_access_token` only reuses the access token when `expires_at` is >2min in the future — otherwise it burns the refresh token on every call and the caller must persist the rotated dict. If rotation persistence ever fails, the refresh token is invalidated and sync raises "Re-authentication required" (scheduler logs a warning, then the sync still raises).

**Why:** second-leading suspect for Render zero-scan after the pagination fix; also a token-rotation fragility (each refresh typically rotates the refresh token).

**How to apply:** if zero-scan persists after deploying pagination fix, check Render logs for "Token refresh failed" / "Re-authentication required" and Supabase `outlook_accounts.token_json` for missing `expires_at`; fix = store `expires_at = now + expires_in` in callback (Gmail route already does this) and/or make sync skip gracefully instead of raising.
