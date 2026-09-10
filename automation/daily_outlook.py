"""Daily Outlook sync — scheduled task to fetch DMARC reports from Outlook.

Designed to be run as a cron job (recommended: daily at 6 AM).
Also works as a standalone script or via run.sh.

Usage:
    python -m automation.daily_outlook          # Sync all accounts once
    python -m automation.daily_outlook --email dmarc@skillbrew.com  # Specific account
    python -m automation.daily_outlook --backfill  # Scan past 10 days
    python -m automation.daily_outlook --notify    # Post Discord summary after

Cron entry (crontab -e):
    0 6 * * * cd /path/to/dmarc_pipeline && python -m automation.daily_outlook --notify
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from models.accounts import (
    list_outlook_accounts,
    get_outlook_account_by_email,
    update_outlook_sync_time,
)

logger = logging.getLogger("dmarc.daily_outlook")


async def sync_all_accounts(backfill: bool = False, notify: bool = False) -> dict:
    """Sync all active Outlook accounts.

    Args:
        backfill: If True, scan past 10 days of emails
        notify: If True, post Discord summary after sync

    Returns:
        Summary dict with results
    """
    from services.outlook_sync import sync_account_emails

    accounts = list_outlook_accounts(active_only=True)

    if not accounts:
        logger.info("No Outlook accounts configured. Visit /outlook/start to connect one.")
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
            logger.info("[%s] Syncing Outlook account...", email)
            count = await sync_account_emails(account, backfill=backfill)
            results["total_reports"] += count
            update_outlook_sync_time(account_id, datetime.now(timezone.utc).isoformat())
            logger.info("[%s] Synced %d report(s)", email, count)
        except Exception as exc:
            logger.error("[%s] Sync failed: %s", email, exc)
            results["errors"].append({"email": email, "error": str(exc)})

    # Post Discord notification if requested
    if notify and results["total_reports"] > 0:
        try:
            from services.discord_notifier import post_discord_summary
            post_discord_summary(results)
            logger.info("Discord notification sent")
        except Exception as exc:
            logger.warning("Discord notification failed: %s", exc)

    return results


async def sync_single_account(
    email: str, backfill: bool = False, notify: bool = False
) -> dict:
    """Sync a specific Outlook account by email address.

    Args:
        email: Email address of the account to sync
        backfill: If True, scan past 10 days of emails
        notify: If True, post Discord summary after sync

    Returns:
        Summary dict with results
    """
    from services.outlook_sync import sync_account_emails

    account = get_outlook_account_by_email(email)
    if not account:
        logger.error("Outlook account not found: %s", email)
        return {"status": "not_found", "email": email}

    results = {
        "status": "ok",
        "accounts_checked": 1,
        "total_reports": 0,
        "errors": [],
    }

    try:
        account_id = account.get("id")
        logger.info("[%s] Syncing Outlook account...", email)
        count = await sync_account_emails(account, backfill=backfill)
        results["total_reports"] += count
        update_outlook_sync_time(account_id, datetime.now(timezone.utc).isoformat())
        logger.info("[%s] Synced %d report(s)", email, count)
    except Exception as exc:
        logger.error("[%s] Sync failed: %s", email, exc)
        results["errors"].append({"email": email, "error": str(exc)})

    # Post Discord notification if requested
    if notify and results["total_reports"] > 0:
        try:
            from services.discord_notifier import post_discord_summary
            post_discord_summary(results)
            logger.info("Discord notification sent")
        except Exception as exc:
            logger.warning("Discord notification failed: %s", exc)

    return results


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Daily Outlook DMARC sync",
        epilog="Example: python -m automation.daily_outlook --notify",
    )
    parser.add_argument(
        "--email",
        type=str,
        default=None,
        help="Sync a specific account by email (default: all accounts)",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Scan past 10 days of emails (useful for initial setup)",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="Post Discord summary after sync",
    )
    args = parser.parse_args()

    from logging_config import setup_logging
    setup_logging()

    if args.email:
        result = asyncio.run(
            sync_single_account(args.email, backfill=args.backfill, notify=args.notify)
        )
    else:
        result = asyncio.run(sync_all_accounts(backfill=args.backfill, notify=args.notify))

    # Print summary
    if result["status"] == "no_accounts":
        print("No Outlook accounts configured. Visit /outlook/start to connect one.")
    elif result["status"] == "not_found":
        print(f"Account not found: {result.get('email')}")
    else:
        print(
            f"Synced {result['total_reports']} report(s) "
            f"from {result['accounts_checked']} account(s)"
        )
        if result["errors"]:
            print(f"Errors: {len(result['errors'])}")
            for err in result["errors"]:
                print(f"  - {err['email']}: {err['error']}")


if __name__ == "__main__":
    main()
