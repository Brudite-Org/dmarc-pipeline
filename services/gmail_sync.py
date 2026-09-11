"""Gmail sync — fetches DMARC reports from connected accounts.

Detection philosophy: Content is ground truth.
- If XML parses as valid DMARC aggregate report → it IS a DMARC report
- Sender/subject/filename are confidence indicators, NOT gates
- Zero false positives: invalid XML never processed
- Zero false negatives: any valid DMARC report accepted regardless of sender
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from pathlib import Path

from config import settings
from models.processed_emails import is_processed, mark_processed
from services.dmarc_detector import detect_dmarc_report
from services.oauth import get_valid_access_token

logger = logging.getLogger("dmarc.gmail_sync")

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
# Broad query: fetch ALL emails with attachments, then verify content with
# dmarc_detector.py (Layer 4 ground truth). This avoids missing reports that
# have generic subjects but valid DMARC XML attachments.
QUERY = os.environ.get("GMAIL_QUERY", "has:attachment newer_than:30d")
# Backfill: how many days to scan when connecting a new account
BACKFILL_DAYS = int(os.environ.get("GMAIL_BACKFILL_DAYS", "30"))


async def sync_account_emails(account: dict, backfill: bool = False) -> int:
    """Sync DMARC emails for a single account.

    Args:
        account: Account dict from database
        backfill: If True, scan historical emails (past BACKFILL_DAYS)
    """
    import httpx

    # Build query based on mode
    if backfill:
        query = f"has:attachment newer_than:{BACKFILL_DAYS}d"
        mode_label = "backfill"
    else:
        query = QUERY
        mode_label = "sync"

    # Get valid token (refresh if needed)
    token_json = account.get("token_json", {})
    access_token = await get_valid_access_token(token_json)

    headers = {"Authorization": f"Bearer {access_token}"}
    email = account.get("email", "unknown")

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Search for emails (broad query — we verify with content)
        search_url = f"{GMAIL_API_BASE}/messages"
        params = {"q": query, "maxResults": 50}
        response = await client.get(search_url, headers=headers, params=params)

        if response.status_code != 200:
            logger.error("[%s] Search failed: %s", email, response.text)
            return 0

        messages = response.json().get("messages", [])
        if not messages:
            logger.info("[%s] No emails to check (%s)", email, mode_label)
            return 0

        logger.info("[%s] %s: checking %d email(s)", email, mode_label, len(messages))

        saved = 0
        skipped = 0

        account_id = account.get("id")

        for msg_info in messages:
            msg_id = msg_info["id"]

            # Skip already processed messages
            if is_processed(account_id, msg_id):
                continue

            # Mark as processed (even if not a DMARC report — we checked it)
            mark_processed(account_id, msg_id)

            # Get full message
            msg_url = f"{GMAIL_API_BASE}/messages/{msg_id}"
            msg_resp = await client.get(msg_url, headers=headers, params={"format": "full"})

            if msg_resp.status_code != 200:
                continue

            msg = msg_resp.json()

            # Extract headers
            headers_map = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            subject = headers_map.get("Subject", "")
            sender = headers_map.get("From", "")

            # Find attachments
            attachments = []
            _find_attachments(msg.get("payload", {}), attachments)

            if not attachments:
                continue

            # Process each attachment
            for att_info in attachments:
                filename = att_info["filename"]
                att_id = att_info["attachment_id"]

                # Download attachment
                att_url = f"{GMAIL_API_BASE}/messages/{msg_id}/attachments/{att_id}"
                att_resp = await client.get(att_url, headers=headers)

                if att_resp.status_code != 200:
                    logger.warning("[%s] Failed to download %s", email, filename)
                    continue

                # Decode base64url
                att_data = att_resp.json().get("data", "")
                file_bytes = base64.urlsafe_b64decode(att_data)

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

        logger.info("[%s] Sync complete: %d saved, %d skipped", email, saved, skipped)

    return saved


def _find_attachments(payload: dict, results: list[dict]) -> None:
    """Recursively find attachments in message payload."""
    if payload.get("filename") and payload.get("body", {}).get("attachmentId"):
        results.append({
            "filename": payload["filename"],
            "attachment_id": payload["body"]["attachmentId"],
        })

    for part in payload.get("parts", []):
        _find_attachments(part, results)


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
