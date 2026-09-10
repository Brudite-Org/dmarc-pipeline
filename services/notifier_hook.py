"""Notifier hook — triggers notifications after DMARC report ingestion.

Called by workers/processor.py after a report is successfully ingested.ye
Supports Discord (and future: Slack, Teams, PagerDuty).

Environment variables:
    DISCORD_ENABLED      — Enable Discord notifications
    DISCORD_WEBHOOK_URL  — Discord incoming webhook URL
"""

from __future__ import annotations

import logging

logger = logging.getLogger("dmarc.notifier_hook")


def on_report_ingested(report_id: int, report_data: dict | None = None) -> None:
    """Called after a DMARC report is successfully ingested.

    Triggers all configured notification channels.

    Args:
        report_id: The Supabase ID of the ingested report.
        report_data: Optional report data dict (to avoid re-fetching).
    """
    # Discord
    try:
        from services.discord_notifier import post_discord_summary, is_configured

        if is_configured():
            logger.info("Sending Discord notification for report %d", report_id)
            success = post_discord_summary(report_id)
            if success:
                logger.info("Discord notification sent for report %d", report_id)
            else:
                logger.warning("Discord notification failed for report %d", report_id)
    except ImportError:
        logger.debug("Discord notifier not available")
    except Exception as exc:
        logger.error("Discord notification error for report %d: %s", report_id, exc)

    # Future: Slack
    # try:
    #     from services.slack_notifier import post_slack_summary
    #     post_slack_summary(report_id)
    # except ImportError:
    #     pass

    # Future: Microsoft Teams
    # try:
    #     from services.teams_notifier import post_teams_summary
    #     post_teams_summary(report_id)
    # except ImportError:
    #     pass
