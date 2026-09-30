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
from services.dmarc_detector import detect_dmarc_report, has_report_container_suffix
from services.outlook_auth import get_valid_access_token

logger = logging.getLogger("dmarc.outlook_sync")

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Backfill: how many days to scan when connecting a new account
BACKFILL_DAYS = int(os.environ.get("OUTLOOK_BACKFILL_DAYS", "30"))

# Pagination: Graph returns at most $top messages per page. Follow
# @odata.nextLink up to this many pages so reports beyond the first
# page are not silently missed. (Each page is $top=50 messages, so the
# default cap covers 500 messages per sync; already-seen messages cost
# zero extra API calls thanks to the processed_emails check below.)
MAX_PAGES = int(os.environ.get("OUTLOOK_SYNC_MAX_PAGES", "10"))


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
        messages_url = f"{GRAPH_API_BASE}/me/messages"

        # Content-first detection: fetch ALL emails with attachments, then verify
        # attachment content using dmarc_detector (Layer 4 ground truth check).
        # We deliberately omit $search (KQL) because DMARC terms in subject/body
        # are unreliable — reports can arrive with generic subjects. $filter handles
        # the structural constraints only.
        if backfill:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).isoformat()
            params = {
                "$filter": f"receivedDateTime ge {cutoff} and hasAttachments eq true",
                "$orderby": "receivedDateTime desc",
                "$top": 50,
                "$select": "id,subject,from,receivedDateTime,hasAttachments",
            }
            mode_label = "backfill"
        else:
            # Graph does NOT return newest-first by default with $filter, and
            # $orderby properties must also appear in $filter (first), else
            # it errors with InefficientFilter. The dummy lower bound
            # satisfies that so the early-stop below is safe.
            params = {
                "$filter": "receivedDateTime ge 1900-01-01T00:00:00Z and hasAttachments eq true",
                "$orderby": "receivedDateTime desc",
                "$top": 50,
                "$select": "id,subject,from,receivedDateTime,hasAttachments",
            }
            mode_label = "sync"

        response = await client.get(messages_url, headers=headers, params=params)

        if response.status_code != 200:
            logger.error("[%s] Search failed: %s", email, response.text)
            return 0

        # ── PAGINATED FETCH ──────────────────────────────────────────
        # Graph caps each response at $top messages and exposes the rest
        # via @odata.nextLink. Mailbox results come back newest-first,
        # so stop early once a full page of already-processed messages
        # is seen: older pages only contain older messages, all
        # previously checked. Steady-state sync stays at 1 request
        # while backfill on a busy mailbox walks as far back as needed.
        messages: list = []
        page = 0
        page_resp = response
        while True:
            page += 1
            if page_resp.status_code != 200:
                logger.error("[%s] Page %d failed: %s", email, page, page_resp.text)
                break
            body = page_resp.json()
            page_messages = body.get("value", [])
            if not page_messages:
                break
            fresh = [m for m in page_messages if not is_processed(account_id, m["id"])]
            messages.extend(fresh)
            next_link = body.get("@odata.nextLink")
            if not next_link or page >= MAX_PAGES:
                break
            if not fresh and not backfill:
                break
            page_resp = await client.get(next_link, headers=headers)

        if not messages:
            logger.info("[%s] No new emails to check (%s)", email, mode_label)
            return 0

        logger.info(
            "[%s] %s: checking %d new email(s) across %d page(s)",
            email, mode_label, len(messages), page,
        )

        saved = 0
        skipped = 0

        for msg_info in messages:
            msg_id = msg_info["id"]

            # Skip already processed messages
            if is_processed(account_id, msg_id):
                continue

            # Extract metadata
            subject = msg_info.get("subject", "")
            sender_info = msg_info.get("from", {}).get("emailAddress", {})
            sender = sender_info.get("address", "unknown")

            # Get attachments for this message
            attachments_url = f"{GRAPH_API_BASE}/me/messages/{msg_id}/attachments"
            att_response = await client.get(attachments_url, headers=headers)

            if att_response.status_code != 200:
                # Not marked processed — retry on next sync
                logger.warning("[%s] Failed to get attachments for %s", email, msg_id)
                continue

            attachments = att_response.json().get("value", [])

            # Filter to file attachments only (not inline images), and to
            # filenames that could possibly be a DMARC report container
            # (.zip/.xml/.xml.gz/.gz) — skip pdf/png/docx/etc. before
            # spending CPU decoding and content-verifying them.
            file_attachments = [
                att for att in attachments
                if att.get("@odata.type") == "#microsoft.graph.fileAttachment"
                and has_report_container_suffix(att.get("name", ""))
            ]

            if not file_attachments:
                mark_processed(account_id, msg_id)
                continue

            # Set when an attachment download fails so the message is
            # retried next sync instead of being silently dropped
            retry = False

            # Process each attachment
            for att in file_attachments:
                filename = att.get("name", "")
                content_bytes = att.get("contentBytes", "")

                import base64
                if content_bytes:
                    try:
                        file_bytes = base64.b64decode(content_bytes)
                    except Exception:
                        logger.warning("[%s] Failed to decode attachment: %s", email, filename)
                        continue
                else:
                    # Graph omits contentBytes from the list response for
                    # large attachments — download the raw bytes directly.
                    raw_url = f"{attachments_url}/{att['id']}/$value"
                    raw_resp = await client.get(raw_url, headers=headers)
                    if raw_resp.status_code != 200 or not raw_resp.content:
                        logger.warning("[%s] Failed to download %s", email, filename)
                        retry = True
                        continue
                    file_bytes = raw_resp.content

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
                    saved_path = await _save_attachment(filename, file_bytes)
                    saved += 1
                    logger.info(
                        "[%s] ✓ DMARC report: %s (confidence: %s, metadata: %d/3)",
                        email,
                        filename,
                        result.confidence,
                        result.metadata_score,
                    )

                    # Ingest into dmarc_reports/dmarc_records tables
                    try:
                        from workers.processor import process_file as _process_file
                        ingest_result = _process_file(saved_path)
                        if ingest_result and ingest_result.reports:
                            logger.info(
                                "[%s] Ingested %d report(s) into DB",
                                email,
                                len(ingest_result.reports),
                            )
                        elif ingest_result and ingest_result.is_duplicate:
                            logger.info("[%s] Report already in DB (duplicate)", email)
                    except Exception as ingest_exc:
                        logger.error("[%s] DB ingest failed: %s", email, ingest_exc)
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

            # Mark as processed (even if not a DMARC report — we checked it)
            if not retry:
                mark_processed(account_id, msg_id)

        logger.info("[%s] Sync complete: %d saved, %d skipped", email, saved, skipped)

    return saved


async def _save_attachment(filename: str, data: bytes) -> Path:
    """Save attachment to reports directory."""
    reports_dir = settings.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time())
    safe_name = f"{timestamp}_{filename}"
    filepath = reports_dir / safe_name
    filepath.write_bytes(data)
    logger.info("Saved: %s", filepath)
    return filepath


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
