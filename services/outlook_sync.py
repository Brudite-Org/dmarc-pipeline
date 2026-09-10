"""Outlook sync — fetches DMARC reports from connected Microsoft 365 accounts.

Uses Microsoft Graph API to search for emails with DMARC report attachments.
Detection philosophy matches Gmail sync: Content is ground truth.
- If XML parses as valid DMARC aggregate report → it IS a DMARC report
- Sender/subject/filename are confidence indicators, NOT gates

Usage:
    python -m services.outlook_sync              # Sync all accounts once
    python -m services.outlook_sync --account 5   # Sync specific account
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

from config import settings
from models.processed_emails import is_processed, mark_processed
from services.dmarc_detector import detect_dmarc_report
from services.outlook_auth import get_valid_access_token

logger = logging.getLogger("dmarc.outlook_sync")

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Search query — broad filter to catch all DMARC reports
# Graph API uses different syntax than Gmail
SEARCH_QUERY = os.environ.get(
    "OUTLOOK_QUERY",
    "hasAttachments eq true"
)

# Backfill: how many days to scan when connecting a new account
BACKFILL_DAYS = int(os.environ.get("OUTLOOK_BACKFILL_DAYS", "30"))


async def sync_account_emails(account: dict, backfill: bool = False) -> int:
    """Sync DMARC emails for a single Outlook account.

    Args:
        account: Account dict from database
        backfill: If True, scan historical emails (past BACKFILL_DAYS)
    """
    import httpx

    # Get valid token (refresh if needed)
    token_json = account.get("token_json", {})
    access_token = await get_valid_access_token(token_json)

    # Persist refreshed token back to account dict (caller should save)
    account["token_json"] = token_json

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    email = account.get("email", "unknown")
    account_id = account.get("id")

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Build search URL with filter
        # Graph API: $filter for hasAttachments, $search for content
        messages_url = f"{GRAPH_API_BASE}/me/messages"

        # Build filter — look for emails with attachments
        # We can't search subject as flexibly as Gmail, so we filter broadly
        # and rely on content detection
        params = {
            "$filter": "hasAttachments eq true",
            "$top": 50,
            "$select": "id,subject,from,receivedDateTime,hasAttachments",
            "$orderby": "receivedDateTime desc",
        }

        # For backfill, add date filter
        # Note: Exchange rejects $filter with receivedDateTime + $orderby together
        if backfill:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).isoformat()
            params["$filter"] = f"hasAttachments eq true and receivedDateTime ge {cutoff}"
            params.pop("$orderby", None)  # Exchange: filter+orderby on date = InefficientFilter
            mode_label = "backfill"
        else:
            mode_label = "sync"

        response = await client.get(messages_url, headers=headers, params=params)

        if response.status_code != 200:
            logger.error("[%s] Search failed: %s", email, response.text)
            return 0

        messages = response.json().get("value", [])
        if not messages:
            logger.info("[%s] No emails to check (%s)", email, mode_label)
            return 0

        logger.info("[%s] %s: checking %d email(s)", email, mode_label, len(messages))

        saved = 0
        skipped = 0

        for msg_info in messages:
            msg_id = msg_info["id"]

            # Skip already processed messages
            if is_processed(account_id, msg_id):
                continue

            # Mark as processed (even if not a DMARC report — we checked it)
            mark_processed(account_id, msg_id)

            # Extract metadata
            subject = msg_info.get("subject", "")
            sender_info = msg_info.get("from", {}).get("emailAddress", {})
            sender = sender_info.get("address", "unknown")

            # Get attachments for this message
            attachments_url = f"{GRAPH_API_BASE}/me/messages/{msg_id}/attachments"
            att_response = await client.get(attachments_url, headers=headers)

            if att_response.status_code != 200:
                logger.warning("[%s] Failed to get attachments for %s", email, msg_id)
                continue

            attachments = att_response.json().get("value", [])

            # Filter to file attachments only (not inline images)
            file_attachments = [
                att for att in attachments
                if att.get("@odata.type") == "#microsoft.graph.fileAttachment"
            ]

            if not file_attachments:
                continue

            # Process each attachment
            for att in file_attachments:
                filename = att.get("name", "")
                content_bytes = att.get("contentBytes", "")

                if not content_bytes:
                    continue

                # Decode base64 content
                import base64
                try:
                    file_bytes = base64.b64decode(content_bytes)
                except Exception:
                    logger.warning("[%s] Failed to decode attachment: %s", email, filename)
                    continue

                # Write to temp file for content verification
                suffix = Path(filename).suffix if Path(filename).suffix else ".bin"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(file_bytes)
                    tmp_path = Path(tmp.name)

                # ── CONTENT-FIRST DETECTION ──────────────────────────────
                result = detect_dmarc_report(
                    sender=sender,
                    subject=subject,
                    filename=filename,
                    file_path=tmp_path,
                )

                if result.is_dmarc_report:
                    # Valid DMARC report — save it
                    await _save_attachment(filename, file_bytes)
                    saved += 1
                    logger.info(
                        "[%s] ✓ DMARC report: %s (confidence: %s, metadata: %d/3)",
                        email,
                        filename,
                        result.confidence,
                        result.metadata_score,
                    )
                else:
                    skipped += 1
                    logger.debug(
                        "[%s] Skipped: %s — %s",
                        email,
                        filename,
                        result.reason,
                    )

                # Cleanup temp file
                tmp_path.unlink(missing_ok=True)

        logger.info("[%s] Sync complete: %d saved, %d skipped", email, saved, skipped)

    return saved


async def _save_attachment(filename: str, data: bytes) -> None:
    """Save attachment to reports directory."""
    reports_dir = settings.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time())
    safe_name = f"{timestamp}_{filename}"
    filepath = reports_dir / safe_name
    filepath.write_bytes(data)
    logger.info("Saved: %s", filepath)


async def sync_all_accounts(backfill: bool = False) -> dict:
    """Sync all active Outlook accounts.

    Returns:
        Summary dict with status, accounts_checked, total_reports, errors.
    """
    from models.accounts import list_outlook_accounts

    accounts = list_outlook_accounts(active_only=True)

    if not accounts:
        return {"status": "no_accounts", "synced": 0}

    results = {
        "status": "ok",
        "accounts_checked": len(accounts),
        "total_reports": 0,
        "errors": [],
    }

    for account in accounts:
        account_id = account.get("id")
        email = account.get("email", "unknown")

        try:
            count = await sync_account_emails(account, backfill=backfill)
            results["total_reports"] += count

            # Update last sync time and persist refreshed token
            from models.accounts import update_outlook_token, update_outlook_sync_time
            from datetime import datetime, timezone

            update_outlook_token(account_id, account.get("token_json", {}))
            update_outlook_sync_time(account_id, datetime.now(timezone.utc).isoformat())

            logger.info("[%s] Auto-synced %d report(s)", email, count)
        except Exception as exc:
            logger.error("[%s] Sync failed: %s", email, exc)
            results["errors"].append({"email": email, "error": str(exc)})

    return results


# ── CLI ─────────────────────────────────────────────────────────────────────────


def main():
    """Entry point for CLI usage."""
    import argparse

    parser = argparse.ArgumentParser(description="DMARC Outlook Sync")
    parser.add_argument(
        "--account",
        type=int,
        default=None,
        help="Sync specific account by ID",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Scan historical emails (past BACKFILL_DAYS)",
    )
    args = parser.parse_args()

    from logging_config import setup_logging
    setup_logging()

    if args.account:
        from models.accounts import get_outlook_account
        account = get_outlook_account(args.account)
        if not account:
            print(f"Account {args.account} not found")
            return
        result = asyncio.run(sync_account_emails(account, backfill=args.backfill))
        print(f"Synced {result} report(s)")
    else:
        result = asyncio.run(sync_all_accounts(backfill=args.backfill))
        print(f"Synced {result['total_reports']} report(s) from {result['accounts_checked']} account(s)")


if __name__ == "__main__":
    main()
