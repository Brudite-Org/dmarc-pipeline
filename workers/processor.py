"""File ingestion worker — unzips DMARC reports and persists to Supabase.

Flow: .zip/.xml/.xml.gz → extract XML → parse → insert into Supabase tables
(dmarc_reports, dmarc_records).

Idempotency is enforced on ``report_id`` — re-running on the same report
is a safe no-op rather than a duplicate.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import aiofiles

from config import settings
from models import insert, select
from parsers.dmarc_xml import DmarcReport as ParsedReport, parse_dmarc_xml
from services.dmarc_detector import verify_xml_content

logger = logging.getLogger("dmarc.processor")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _pick_dkim_domain(dkim_auth: list, reported_domain: str | None) -> str | None:
    """Choose the DKIM domain that best represents this record.

    Google (and others) may report multiple DKIM signatures. We prefer the one
    matching the reported domain; fall back to the first entry otherwise.
    """
    if not dkim_auth:
        return None
    if reported_domain:
        for dkim in dkim_auth:
            if dkim.domain == reported_domain:
                return dkim.domain
    return dkim_auth[0].domain


# ── Public API ────────────────────────────────────────────────────────────────


@dataclass
class IngestResult:
    """Detailed result of ingesting one upload (zip / xml / xml.gz)."""

    archive_name: str
    extracted_files: list[str] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)
    records_by_report: dict[int, list[dict]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def is_duplicate(self) -> bool:
        return not self.reports and not self.failed and bool(self.skipped)


def process_file(path: Path) -> IngestResult | None:
    """Ingest a single DMARC report file into Supabase."""
    path = Path(path)
    if not path.exists():
        logger.warning("File not found: %s", path)
        return None

    logger.info("Processing: %s", path.name)
    result = IngestResult(archive_name=path.name)

    # Collect all (xml_bytes, xml_name) pairs from this file
    try:
        xml_entries = _extract_all_xml(path)
    except Exception as exc:
        logger.error("Failed to extract XML from %s: %s", path.name, exc)
        _quarantine(path)
        result.failed.append(path.name)
        return result

    for xml_bytes, xml_name in xml_entries:
        result.extracted_files.append(xml_name)

        # ── CONTENT-FIRST GATE (Layer 4 — the only hard gate) ────────────
        # parse_dmarc_xml() is intentionally lenient (it never raises for
        # well-formed XML that simply isn't a DMARC report), so every entry
        # must independently prove it's a genuine DMARC aggregate report
        # before we persist anything or fire a Discord notification.
        if not verify_xml_content(xml_bytes):
            logger.warning("Not a valid DMARC aggregate report, skipping: %s", xml_name)
            result.failed.append(xml_name)
            continue

        try:
            parsed: ParsedReport = parse_dmarc_xml(xml_name, xml_bytes)
        except ValueError as exc:
            logger.error("Failed to parse %s: %s", xml_name, exc)
            result.failed.append(xml_name)
            continue

        report_id = _persist(parsed, archive_name=path.name, xml_name=xml_name)
        if report_id is not None:
            inserted = select("dmarc_reports", filters={"id": report_id})
            if inserted:
                result.reports.append(inserted[0])
                records = select("dmarc_records", filters={"report_id": report_id})
                result.records_by_report[report_id] = records
        else:
            result.skipped.append(parsed.metadata.report_id or xml_name)

    # No entry in this archive was a genuine DMARC report — quarantine it so
    # the watcher/backfill don't keep re-scanning a file that will never pass.
    if not result.reports and not result.skipped and result.failed:
        _quarantine(path)

    return result


async def process_existing_files(directory: Path) -> int:
    """Process every supported file in a directory (batch/initial load)."""
    directory = Path(directory)
    count = 0
    for ext in ("*.zip", "*.xml", "*.xml.gz"):
        for path in sorted(directory.glob(ext)):
            result = process_file(path)
            if result is not None and result.reports:
                count += len(result.reports)
    return count


# ── Internals ─────────────────────────────────────────────────────────────────


def _extract_all_xml(path: Path) -> list[tuple[bytes, str]]:
    """Return XML content from any supported input (zip/xml/gz)."""
    suffix = path.suffix.lower()

    if suffix == ".zip":
        return _sync_extract_all_xml(path)

    if suffix == ".gz":
        import gzip
        data = gzip.open(path, "rb").read()
        return [(data, path.name)]

    # Plain XML
    return [(path.read_bytes(), path.name)]


def _sync_extract_all_xml(path: Path) -> list[tuple[bytes, str]]:
    """Extract all XML members from a zip archive."""
    with zipfile.ZipFile(path, "r") as zf:
        xml_members = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not xml_members:
            raise ValueError(f"No XML files found in zip: {path.name}")
        return [(zf.read(m), Path(m).name) for m in xml_members]


def _persist(
    parsed: ParsedReport, archive_name: str, xml_name: str
) -> int | None:
    """Persist a parsed report to Supabase. Returns the new report ID."""
    policy = parsed.metadata.policy
    report_id = parsed.metadata.report_id

    # Idempotency guard — skip if we've already ingested this report_id
    if report_id:
        existing = select("dmarc_reports", filters={"report_id": report_id}, limit=1)
        if existing:
            logger.info("Skipping duplicate report_id=%s", report_id)
            return None

    # Resolve domain
    domain = policy.domain
    if not domain and parsed.records:
        domain = parsed.records[0].header_from

    # Insert report
    report_data = {
        "xml_filename": xml_name,
        "archive_filename": archive_name,
        "org_name": parsed.metadata.org_name,
        "org_email": parsed.metadata.org_email,
        "report_id": report_id,
        "date_begin": parsed.metadata.date_begin.isoformat() if parsed.metadata.date_begin else None,
        "date_end": parsed.metadata.date_end.isoformat() if parsed.metadata.date_end else None,
        "domain": domain,
        "adkim": policy.adkim,
        "aspf": policy.aspf,
        "p": policy.p,
        "sp": policy.sp,
        "pct": policy.pct,
    }

    inserted = insert("dmarc_reports", report_data)
    if not inserted:
        logger.error("Failed to insert report")
        return None

    new_id = inserted[0]["id"]

    # Bulk-insert records for this report
    records_data = []
    for r in parsed.records:
        records_data.append({
            "report_id": new_id,
            "source_ip": r.source_ip,
            "count": r.count or 0,
            "header_from": r.header_from,
            "envelope_from": r.envelope_from,
            "envelope_to": r.envelope_to,
            "disposition": r.disposition,
            "dkim_aligned": r.dkim_aligned,
            "spf_aligned": r.spf_aligned,
            "dkim_result": r.dkim_result,
            "spf_result": r.spf_result,
            "dkim_domain": _pick_dkim_domain(r.dkim_auth, domain),
            "spf_domain": r.spf_auth[0].domain if r.spf_auth else None,
            "dkim_auth_json": _json.dumps([
                {"domain": d.domain, "result": d.result, "selector": d.selector}
                for d in r.dkim_auth
            ]),
            "spf_auth_json": _json.dumps([
                {"domain": s.domain, "result": s.result, "scope": s.scope}
                for s in r.spf_auth
            ]),
        })

    if records_data:
        insert("dmarc_records", records_data)

    logger.info(
        "Stored report_id=%s with %d records (Supabase ID: %s)",
        report_id,
        len(records_data),
        new_id,
    )

    # Trigger post-ingestion hook (Discord notification, etc.)
    try:
        from services.notifier_hook import on_report_ingested

        on_report_ingested(
            new_id,
            {
                "report_id": report_id,
                "domain": domain,
                "org_name": parsed.metadata.org_name,
                "date_begin": report_data.get("date_begin"),
                "date_end": report_data.get("date_end"),
                "record_count": len(records_data),
            },
        )
    except Exception as hook_exc:
        # Never fail ingestion because notification failed
        logger.warning("Post-ingestion hook failed: %s", hook_exc)

    return new_id


def _quarantine(path: Path) -> None:
    """Move an unprocessable file aside so it doesn't block future runs."""
    quarantine_dir = settings.quarantine_dir
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    target = quarantine_dir / path.name
    path.rename(target)
    logger.warning("Quarantined unprocessable file → %s", target)
