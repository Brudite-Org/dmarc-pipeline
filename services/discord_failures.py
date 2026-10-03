"""Discord embeds that spell out *why* each DMARC failure happened.

Pure formatting — no network, config or database access — so it is cheap to test.
``discord_notifier`` sends the summary embed first, then these.

Discord limits that shape this module:
  * 6000 characters of text in total across all embeds of one message
  * 1024 characters per field value, 256 per title / field name
  * 10 embeds per message
"""

from __future__ import annotations

from typing import Optional

from analysis.diagnosis import STATUS_FAIL, STATUS_ISSUE, diagnose, record_status

COLOR_RED = 0xE74C3C
COLOR_ORANGE = 0xE67E22

MAX_FAILURES_SHOWN = 8
MAX_PARTIAL_LINES = 6
MESSAGE_CHAR_BUDGET = 5500   # headroom under Discord's 6000 per-message cap
MAX_EMBEDS_PER_MESSAGE = 10

FIELD_VALUE_LIMIT = 1024
FIELD_NAME_LIMIT = 256
TITLE_LIMIT = 256


# ── Text helpers ──────────────────────────────────────────────────────────────


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _code(value: Optional[object]) -> str:
    """Inline-code a value from report XML; backticks are removed so it can't break out."""
    if value is None or value == "":
        return "–"
    return "`" + str(value).replace("`", "'") + "`"


def _when(ts: Optional[object]) -> str:
    """ISO timestamp -> ``2026-10-01 00:00 UTC``."""
    if not ts:
        return "unknown"
    text = str(ts).replace("T", " ")
    return f"{text[:16]} UTC"


def _count(value: Optional[object]) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _result_label(result: Optional[str]) -> str:
    return result or "no result"


def embed_size(embed: dict) -> int:
    """Characters Discord counts toward the 6000 limit for this embed."""
    size = len(embed.get("title", "")) + len(embed.get("description", ""))
    size += len(embed.get("footer", {}).get("text", ""))
    for f in embed.get("fields", []):
        size += len(f["name"]) + len(f["value"])
    return size


# ── Embed builders ────────────────────────────────────────────────────────────


def _failure_embed(rec: dict, report: dict) -> dict:
    dx = diagnose(rec, report)
    ip = rec.get("source_ip") or "unknown IP"
    recipient = rec.get("envelope_to")
    count = _count(rec.get("count"))

    fields = [
        {"name": "✉️ Messages", "value": f"**{count:,}**", "inline": True},
        {"name": "📨 Sent to", "value": _code(recipient), "inline": True},
        {"name": "🛡️ Action taken", "value": _code(rec.get("disposition")), "inline": True},
        {
            "name": f"🔑 DKIM — {_result_label(rec.get('dkim_result'))}",
            "value": _clip(dx.dkim.text, FIELD_VALUE_LIMIT),
            "inline": False,
        },
        {
            "name": f"📮 SPF — {_result_label(rec.get('spf_result'))}",
            "value": _clip(dx.spf.text, FIELD_VALUE_LIMIT),
            "inline": False,
        },
        {"name": "⚖️ Impact", "value": _clip(dx.effect, FIELD_VALUE_LIMIT), "inline": False},
    ]

    if dx.actions:
        bullets = "\n".join(f"• {a}" for a in dx.actions)
        fields.append({"name": "🛠️ What to do", "value": _clip(bullets, FIELD_VALUE_LIMIT), "inline": False})

    fields.append({
        "name": "🔎 Finding the exact email",
        "value": _clip(
            "Reports don't include the email itself (no subject or recipient address). "
            f"Search the sending system's logs for {_code(recipient)} "
            f"between {_when(report.get('date_begin'))} and {_when(report.get('date_end'))}.",
            FIELD_VALUE_LIMIT,
        ),
        "inline": False,
    })

    return {
        "title": _clip(f"🚨 DMARC FAIL — {ip}", TITLE_LIMIT),
        "color": COLOR_RED,
        "fields": fields,
    }


def _partial_embed(partials: list[dict], report: dict) -> dict:
    lines = []
    for rec in partials[:MAX_PARTIAL_LINES]:
        dx = diagnose(rec, report)
        broken = "SPF" if dx.dkim.ok else "DKIM"
        result = rec.get("spf_result") if broken == "SPF" else rec.get("dkim_result")
        reason = (dx.spf if broken == "SPF" else dx.dkim).text
        lines.append(
            f"• {_code(rec.get('source_ip'))} → {_code(rec.get('envelope_to'))}: "
            f"**{broken} {_result_label(result)}** — {reason}"
        )
    extra = len(partials) - MAX_PARTIAL_LINES
    if extra > 0:
        lines.append(f"…and {extra} more.")

    return {
        "title": f"⚠️ Partial issues — DMARC still passed ({len(partials)})",
        "description": _clip("\n".join(lines), 4000),
        "color": COLOR_ORANGE,
        "fields": [],
    }


def build_failure_embeds(report: dict, records: list[dict]) -> list[dict]:
    """One embed per failing sender, plus one for partial (one-mechanism) issues.

    Returns an empty list when every record passed both SPF and DKIM alignment.
    """
    failures = [r for r in records if record_status(r) == STATUS_FAIL]
    partials = [r for r in records if record_status(r) == STATUS_ISSUE]

    failures.sort(key=lambda r: _count(r.get("count")), reverse=True)
    shown = failures[:MAX_FAILURES_SHOWN]
    embeds = [_failure_embed(r, report) for r in shown]

    hidden = len(failures) - len(shown)
    if hidden > 0 and embeds:
        embeds[-1]["footer"] = {"text": f"+{hidden} more failing source(s) not shown"}

    if partials:
        embeds.append(_partial_embed(partials, report))
    return embeds


def batch_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Split embeds into groups that each fit Discord's per-message limits."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for embed in embeds:
        size = embed_size(embed)
        if current and (used + size > MESSAGE_CHAR_BUDGET or len(current) >= MAX_EMBEDS_PER_MESSAGE):
            batches.append(current)
            current, used = [], 0
        current.append(embed)
        used += size
    if current:
        batches.append(current)
    return batches
