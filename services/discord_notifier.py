"""Discord notifier — posts DMARC report summaries to a Discord channel.

Uses Discord incoming webhooks for simple, bot-free integration.
Rich embeds with color-coded pass/fail status.

Environment variables:
    DISCORD_WEBHOOK_URL  — Discord incoming webhook URL
    DISCORD_ENABLED      — Enable/disable notifications (default: false)

Usage:
    from services.discord_notifier import post_discord_summary
    post_discord_summary(report_id=42)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("dmarc.discord")

# ── Configuration ─────────────────────────────────────────────────────────────

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "") or settings.discord_webhook_url
ENABLED = (
    os.environ.get("DISCORD_ENABLED", "").lower() in ("true", "1", "yes")
    or settings.discord_enabled
)


# ── Color constants ────────────────────────────────────────────────────────────

COLOR_GREEN = 0x2ECC71    # Pass rate > 95%
COLOR_YELLOW = 0xF1C40F   # Pass rate 80-95%
COLOR_RED = 0xE74C3C      # Pass rate <= 80%
COLOR_BLUE = 0x3498DB     # Info/no data


# ── Public API ─────────────────────────────────────────────────────────────────


def is_configured() -> bool:
    """Check if Discord notifications are configured and enabled."""
    return ENABLED and bool(WEBHOOK_URL)


def post_discord_summary(report_id: int) -> bool:
    """Post a DMARC report summary to Discord.

    Args:
        report_id: The Supabase ID of the report to summarize.

    Returns:
        True if message was sent successfully, False otherwise.
    """
    if not is_configured():
        logger.debug("Discord not configured, skipping notification")
        return False

    try:
        from models import select, select_single

        # Fetch report
        report = select_single("dmarc_reports", report_id)
        if not report:
            logger.warning("Report %d not found for Discord notification", report_id)
            return False

        # Fetch records
        records = select("dmarc_records", filters={"report_id": report_id})

        # Build embed
        embed = _build_embed(report, records)

        # Send to Discord
        return _send_webhook(embed)

    except Exception as exc:
        logger.error("Failed to post Discord summary for report %d: %s", report_id, exc)
        return False


def post_test_message() -> bool:
    """Send a test message to verify webhook configuration."""
    if not is_configured():
        logger.warning("Discord not configured")
        return False

    payload = {
        "content": "🔔 DMARC Pipeline test message — webhook is working!",
        "embeds": [{
            "title": "Test Notification",
            "description": "If you see this, your Discord webhook is configured correctly.",
            "color": COLOR_BLUE,
            "timestamp": datetime.utcnow().isoformat(),
        }]
    }

    return _send_webhook(payload)


# ── Embed builder ──────────────────────────────────────────────────────────────


def _build_embed(report: dict, records: list[dict]) -> dict:
    """Build a Discord embed from report and records data."""
    # Calculate pass/fail
    total_records = len(records)
    pass_count = sum(1 for r in records if r.get("dkim_aligned") or r.get("spf_aligned"))
    fail_count = total_records - pass_count
    pass_rate = (pass_count / total_records * 100) if total_records > 0 else 0

    # Determine color based on pass rate
    if pass_rate > 95:
        color = COLOR_GREEN
        status_emoji = "🟢"
    elif pass_rate > 80:
        color = COLOR_YELLOW
        status_emoji = "🟡"
    else:
        color = COLOR_RED
        status_emoji = "🔴"

    # Format date range
    date_begin = report.get("date_begin", "Unknown")
    date_end = report.get("date_end", "Unknown")
    if date_begin and len(str(date_begin)) > 10:
        date_begin = str(date_begin)[:10]
    if date_end and len(str(date_end)) > 10:
        date_end = str(date_end)[:10]

    # Get reporter info
    org_name = report.get("org_name", "Unknown")
    domain = report.get("domain", "Unknown")
    report_id_str = report.get("report_id", "N/A")

    # Build fields
    fields = [
        {
            "name": "📅 Date Range",
            "value": f"{date_begin} → {date_end}",
            "inline": True,
        },
        {
            "name": "🌐 Domain",
            "value": domain,
            "inline": True,
        },
        {
            "name": "📧 Reporter",
            "value": org_name,
            "inline": True,
        },
        {
            "name": "✅ Pass",
            "value": f"**{pass_count:,}** ({pass_rate:.1f}%)",
            "inline": True,
        },
        {
            "name": "❌ Fail",
            "value": f"**{fail_count:,}** ({100 - pass_rate:.1f}%)",
            "inline": True,
        },
        {
            "name": "📊 Total Records",
            "value": f"{total_records:,}",
            "inline": True,
        },
    ]

    # Add top failing sources (up to 3)
    failing_records = [
        r for r in records
        if not r.get("dkim_aligned") and not r.get("spf_aligned")
    ]
    if failing_records:
        # Group by source IP
        from collections import defaultdict
        ip_fails: dict[str, dict] = defaultdict(lambda: {"count": 0, "domains": set()})
        for r in failing_records:
            ip = r.get("source_ip", "unknown")
            ip_fails[ip]["count"] += r.get("count", 1)
            if r.get("header_from"):
                ip_fails[ip]["domains"].add(r["header_from"])

        # Sort by count
        top_ips = sorted(ip_fails.items(), key=lambda x: x[1]["count"], reverse=True)[:3]

        fail_details = []
        for ip, data in top_ips:
            domains = ", ".join(list(data["domains"])[:2])
            fail_details.append(f"• `{ip}` — {data['count']} fails — {domains}")

        fields.append({
            "name": "🔝 Top Failing Sources",
            "value": "\n".join(fail_details) or "None",
            "inline": False,
        })

    # Add health score
    health_score = int(pass_rate)
    if health_score >= 90:
        health_label = "Excellent"
    elif health_score >= 70:
        health_label = "Good"
    elif health_score >= 50:
        health_label = "Fair"
    elif health_score >= 25:
        health_label = "Poor"
    else:
        health_label = "Critical"

    fields.append({
        "name": "🏥 Health Score",
        "value": f"{health_score}/100 — {health_label}",
        "inline": True,
    })

    # Build embed
    embed = {
        "title": f"📊 DMARC Report — {domain}",
        "description": f"Report ID: `{report_id_str}`",
        "color": color,
        "fields": fields,
        "footer": {
            "text": f"DMARC Pipeline • {status_emoji} {pass_rate:.1f}% pass rate"
        },
        "timestamp": datetime.utcnow().isoformat(),
    }

    return embed


# ── Webhook sender ─────────────────────────────────────────────────────────────


def _send_webhook(embed: dict | dict) -> bool:
    """Send a message to Discord webhook.

    Args:
        embed: Either an embed dict or a full payload dict with 'embeds' key.

    Returns:
        True if successful.
    """
    if "embeds" in embed:
        payload = embed
    else:
        payload = {"embeds": [embed]}

    try:
        response = httpx.post(
            WEBHOOK_URL,
            json=payload,
            timeout=30.0,
            headers={"Content-Type": "application/json"},
        )

        if response.status_code in (200, 204):
            logger.info("Discord notification sent successfully")
            return True
        else:
            logger.error(
                "Discord webhook failed: %d — %s",
                response.status_code,
                response.text,
            )
            return False

    except httpx.RequestError as exc:
        logger.error("Discord webhook request failed: %s", exc)
        return False


# ── CLI ─────────────────────────────────────────────────────────────────────────


def main():
    """Entry point for CLI usage."""
    import argparse

    parser = argparse.ArgumentParser(description="DMARC Discord Notifier")
    parser.add_argument(
        "--report",
        type=int,
        default=None,
        help="Post summary for specific report ID",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send a test message",
    )
    args = parser.parse_args()

    from logging_config import setup_logging
    setup_logging()

    if args.test:
        success = post_test_message()
        print("Test message sent!" if success else "Failed to send test message")
    elif args.report:
        success = post_discord_summary(args.report)
        print(f"Summary posted for report {args.report}" if success else "Failed to post summary")
    else:
        print("Use --report <id> or --test")


if __name__ == "__main__":
    main()
