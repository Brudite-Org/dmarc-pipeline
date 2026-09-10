"""Background scheduler — auto-syncs all connected accounts on a timer.

Runs as a background process alongside the web server.
Polls all connected Gmail and Outlook accounts every N minutes.

Usage:
    python -m services.scheduler              # Poll every 5 min (default)
    python -m services.scheduler --interval 60  # Poll every 60 seconds
    python -m services.scheduler --once        # Run once and exit
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta

from models.accounts import (
    list_accounts,
    list_outlook_accounts,
    update_sync_time,
    update_token,
    update_outlook_token,
    update_outlook_sync_time,
)
from models.processed_emails import get_processed_count
from services.gmail_sync import sync_account_emails as sync_gmail_account
from services.outlook_sync import sync_account_emails as sync_outlook_account
from services.oauth import refresh_token as refresh_gmail_token
from services.outlook_auth import get_valid_access_token

logger = logging.getLogger("dmarc.scheduler")

POLL_INTERVAL = int(os.environ.get("AUTO_SYNC_INTERVAL", "300"))  # 5 minutes default
BATCH_SIZE = 5  # Max accounts to sync per poll cycle


async def sync_all_accounts() -> dict:
    """Sync all active Gmail and Outlook accounts. Returns summary."""
    gmail_accounts = list_accounts(active_only=True)
    outlook_accounts = list_outlook_accounts(active_only=True)
    all_accounts = (
        [("gmail", a) for a in gmail_accounts]
        + [("outlook", a) for a in outlook_accounts]
    )

    if not all_accounts:
        return {"status": "no_accounts", "synced": 0}

    results = {
        "status": "ok",
        "accounts_checked": len(all_accounts),
        "total_reports": 0,
        "errors": [],
    }

    for account_type, account in all_accounts[:BATCH_SIZE]:
        account_id = account.get("id")
        email = account.get("email", "unknown")

        try:
            # Refresh token if needed
            token_json = account.get("token_json", {})

            if account_type == "gmail":
                if token_json.get("refresh_token"):
                    expires_at = token_json.get("expires_at")
                    if expires_at:
                        expiry = datetime.fromisoformat(expires_at)
                        if datetime.now(timezone.utc) >= expiry:
                            new_token = await refresh_gmail_token(
                                token_json["refresh_token"]
                            )
                            token_json["access_token"] = new_token["access_token"]
                            token_json["expires_at"] = (
                                datetime.now(timezone.utc)
                                + timedelta(seconds=new_token["expires_in"])
                            ).isoformat()
                            update_token(account_id, token_json)

                # Check if this is a new account (never synced)
                is_new_account = not account.get("last_sync")

                # Sync emails (backfill for new accounts)
                count = await sync_gmail_account(account, backfill=is_new_account)

                update_sync_time(account_id, datetime.now(timezone.utc).isoformat())

            else:  # outlook
                if token_json:
                    # Get valid access token (auto-refreshes if needed)
                    try:
                        await get_valid_access_token(token_json)
                        update_outlook_token(account_id, token_json)
                    except Exception as token_exc:
                        logger.warning(
                            "[%s] Token refresh failed: %s", email, token_exc
                        )

                # Check if this is a new account (never synced)
                is_new_account = not account.get("last_sync")

                # Sync emails (backfill for new accounts)
                count = await sync_outlook_account(account, backfill=is_new_account)

                update_outlook_sync_time(
                    account_id, datetime.now(timezone.utc).isoformat()
                )

            results["total_reports"] += count

            if is_new_account and count > 0:
                logger.info(
                    "[%s] Backfill complete: %d historical report(s)", email, count
                )

            logger.info("[%s] Auto-synced %d report(s)", email, count)
        except Exception as exc:
            logger.error("[%s] Sync failed: %s", email, exc)
            results["errors"].append({"email": email, "error": str(exc)})

    return results


async def run_loop(interval: int = 300) -> None:
    """Run continuous polling loop.

    Args:
        interval: Seconds between poll cycles (default: 300)
    """
    logger.info(
        "Auto-sync scheduler started (interval: %ds, max accounts: %d)",
        interval,
        BATCH_SIZE,
    )

    while True:
        try:
            start = time.time()
            results = await sync_all_accounts()
            elapsed = time.time() - start

            if results["status"] == "no_accounts":
                logger.debug("No accounts to sync")
            elif results["total_reports"] > 0:
                logger.info(
                    "Auto-sync complete: %d report(s) from %d account(s) in %.1fs",
                    results["total_reports"],
                    results["accounts_checked"],
                    elapsed,
                )
            else:
                logger.debug(
                    "Auto-sync: no new reports (%d accounts checked in %.1fs)",
                    results["accounts_checked"],
                    elapsed,
                )

        except Exception as exc:
            logger.error("Auto-sync cycle failed: %s", exc)

        await asyncio.sleep(interval)


def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="DMARC Auto-Sync Scheduler")
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit",
    )
    args = parser.parse_args()

    from logging_config import setup_logging

    setup_logging()

    # Update poll interval if specified
    interval = args.interval

    if args.once:
        result = asyncio.run(sync_all_accounts())
        print(
            f"Synced {result['total_reports']} report(s) "
            f"from {result['accounts_checked']} account(s)"
        )
    else:
        logger.info(
            "Starting auto-sync scheduler (interval: %ds, Ctrl+C to stop)", interval
        )
        try:
            asyncio.run(run_loop(interval))
        except KeyboardInterrupt:
            logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
