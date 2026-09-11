"""DMARC report detector — content-first verification.

PHILOSOPHY:
  Content (Layer 4) is the ONLY ground truth.
  If XML parses as a valid DMARC aggregate report → it IS a DMARC report.
  Sender/subject/filename are confidence indicators, not gates.

This ensures:
  - Zero false positives: invalid XML is never processed
  - Zero false negatives: any valid DMARC report is accepted, regardless of sender

LAYERS:
  Layer 4 (content): HARD GATE — must pass
  Layers 1-3 (metadata): SOFT — used for confidence scoring and logging only
"""

from __future__ import annotations

import gzip
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

logger = logging.getLogger("dmarc.detector")

# ═══════════════════════════════════════════════════════════════════════════
# LAYER 4: CONTENT VALIDATION (GROUND TRUTH — THE ONLY HARD GATE)
# ═══════════════════════════════════════════════════════════════════════════

REQUIRED_XML_ELEMENTS = [
    "report_metadata",
    "policy_published",
    "record",
]


def verify_xml_content(xml_bytes: bytes) -> bool:
    """Parse XML and verify it's a valid DMARC aggregate report.

    This is the GROUND TRUTH check. If this passes, the file is
    definitively a DMARC aggregate report — no other checks needed.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return False

    # Must have <feedback> root element
    if root.tag != "feedback":
        return False

    # Must have all required child elements
    for required in REQUIRED_XML_ELEMENTS:
        if root.find(required) is None:
            return False

    # Must have at least one <record> with auth results
    records = root.findall("record")
    if len(records) == 0:
        return False

    # Verify report_metadata has required fields
    meta = root.find("report_metadata")
    if meta is not None:
        if meta.find("org_name") is None:
            return False
        if meta.find("report_id") is None:
            return False

    # Verify policy_published has domain
    policy = root.find("policy_published")
    if policy is not None:
        if policy.find("domain") is None:
            return False

    return True


def verify_zip_content(zip_bytes: bytes) -> bool:
    """Verify a zip file contains a valid DMARC report XML."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            xml_files = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not xml_files:
                return False
            # At least one XML must be a valid DMARC report
            for xml_name in xml_files:
                xml_data = zf.read(xml_name)
                if verify_xml_content(xml_data):
                    return True
            return False
    except (zipfile.BadZipFile, Exception):
        return False


def verify_gz_content(gz_bytes: bytes) -> bool:
    """Verify a gzip file contains a valid DMARC report XML."""
    try:
        data = gzip.decompress(gz_bytes)
        return verify_xml_content(data)
    except Exception:
        return False


def verify_file_content(filepath: Path) -> bool:
    """Verify a file (xml, gz, or zip) is a valid DMARC report."""
    suffix = filepath.suffix.lower()

    if suffix == ".xml":
        return verify_xml_content(filepath.read_bytes())

    if suffix == ".gz" or str(filepath).endswith(".xml.gz"):
        return verify_gz_content(filepath.read_bytes())

    if suffix == ".zip":
        return verify_zip_content(filepath.read_bytes())

    return False


# ═══════════════════════════════════════════════════════════════════════════
# CHEAP PRE-FILTER — attachment container types (NOT a content gate)
# ═══════════════════════════════════════════════════════════════════════════

# DMARC aggregate reports are only ever delivered in these container formats
# (RFC 7489 §7.2.1). This is purely an efficiency filter to avoid downloading
# and decoding attachments (PDFs, images, docs, etc.) that can never be DMARC
# reports. It is NOT the source of truth — verify_file_content()/
# verify_xml_content() (Layer 4, above) is always the real gate applied
# afterward to anything that passes this filter.
REPORT_CONTAINER_SUFFIXES = (".xml.gz", ".gz", ".zip", ".xml")


def has_report_container_suffix(filename: str) -> bool:
    """Does this filename look like a possible DMARC report container?

    Cheap, name-only check — used to skip obviously-irrelevant attachments
    (e.g. invoice.pdf, screenshot.png) before spending a network round-trip
    or CPU cycles on them. Always followed by real content verification.
    """
    if not filename:
        return False
    lower = filename.lower()
    return any(lower.endswith(suffix) for suffix in REPORT_CONTAINER_SUFFIXES)


# ═══════════════════════════════════════════════════════════════════════════
# LAYERS 1-3: METADATA CHECKS (SOFT — CONFIDENCE SCORING ONLY)
# ═══════════════════════════════════════════════════════════════════════════

# These are NOT gates. They only contribute to a confidence score.
# A file that fails these but passes Layer 4 is STILL a valid DMARC report.

KNOWN_REPORTER_DOMAINS = {
    "google.com", "googlemail.com", "microsoft.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "yahoo.com", "ymail.com",
    "amazonses.com", "mail.ru", "zoho.com", "proofpoint.com",
    "mimecast.com", "barracuda.com", "sendgrid.net", "mailgun.org",
}

DMARC_SUBJECT_PATTERNS = [
    re.compile(r"report\s+domain\s*:.*submitter\s*:", re.IGNORECASE),
    re.compile(r"dmarc\s+report", re.IGNORECASE),
]

DMARC_FILENAME_PATTERN = re.compile(
    r"^[a-zA-Z0-9][\w\-\.]*![a-zA-Z0-9][\w\-\.]*!\d{8,12}!\d{8,12}\.(zip|xml|gz)$",
    re.IGNORECASE,
)


def check_sender(sender: str) -> bool:
    """Soft check: Is sender a known DMARC reporter?"""
    sender_lower = sender.lower()
    email_match = re.search(r"<([^>]+)>", sender_lower)
    if email_match:
        sender_lower = email_match.group(1)
    if "@" in sender_lower:
        domain = sender_lower.split("@")[-1]
    else:
        domain = sender_lower
    return domain in KNOWN_REPORTER_DOMAINS


def check_subject(subject: str) -> bool:
    """Soft check: Does subject match DMARC report patterns?"""
    return any(p.search(subject) for p in DMARC_SUBJECT_PATTERNS)


def check_filename(filename: str) -> bool:
    """Soft check: Does filename match RFC 7489 convention?"""
    return bool(DMARC_FILENAME_PATTERN.match(filename))


# ═══════════════════════════════════════════════════════════════════════════
# COMBINED DETECTION
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class DetectionResult:
    """Result of DMARC report detection."""
    is_dmarc_report: bool
    confidence: str  # "high", "medium", "low"
    content_valid: bool
    metadata_score: int  # 0-3 (how many soft checks passed)
    reason: str


def detect_dmarc_report(
    sender: str = "",
    subject: str = "",
    filename: str = "",
    file_path: Optional[Path] = None,
    file_bytes: Optional[bytes] = None,
) -> DetectionResult:
    """
    Detect if a file is a DMARC aggregate report.

    ARCHITECTURE:
    - Layer 4 (content) is the ONLY hard gate
    - Layers 1-3 (metadata) are soft checks for confidence scoring
    - If content validates → IS a DMARC report (regardless of metadata)
    - If content fails → NOT a DMARC report (regardless of metadata)
    """
    # ── Layer 4: Content validation (HARD GATE) ─────────────────────────
    content_valid = False

    if file_path and file_path.exists():
        content_valid = verify_file_content(file_path)
    elif file_bytes:
        # Determine type from filename or try all
        suffix = Path(filename).suffix.lower() if filename else ""
        if suffix == ".xml":
            content_valid = verify_xml_content(file_bytes)
        elif suffix == ".zip":
            content_valid = verify_zip_content(file_bytes)
        elif suffix == ".gz":
            content_valid = verify_gz_content(file_bytes)
        else:
            # Try all formats
            content_valid = (
                verify_xml_content(file_bytes)
                or verify_zip_content(file_bytes)
                or verify_gz_content(file_bytes)
            )

    # ── Layers 1-3: Metadata checks (SOFT — confidence only) ────────────
    metadata_score = 0
    if sender and check_sender(sender):
        metadata_score += 1
    if subject and check_subject(subject):
        metadata_score += 1
    if filename and check_filename(filename):
        metadata_score += 1

    # ── Decision ────────────────────────────────────────────────────────
    if content_valid:
        is_report = True
        # Confidence based on metadata agreement
        if metadata_score >= 2:
            confidence = "high"
            reason = f"Valid DMARC XML + {metadata_score}/3 metadata checks passed"
        elif metadata_score >= 1:
            confidence = "medium"
            reason = "Valid DMARC XML (metadata partially matches)"
        else:
            confidence = "medium"
            reason = "Valid DMARC XML (unusual sender/subject/filename — but content is definitive)"
    else:
        is_report = False
        confidence = "none"
        if metadata_score >= 2:
            reason = f"Metadata suggests DMARC ({metadata_score}/3) but content is NOT valid DMARC XML"
        else:
            reason = "Not a valid DMARC aggregate report"

    return DetectionResult(
        is_dmarc_report=is_report,
        confidence=confidence,
        content_valid=content_valid,
        metadata_score=metadata_score,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════════════════
# CLI TESTING
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python -m services.dmarc_detector --file /path/to/report.zip")
        print("  python -m services.dmarc_detector --sender 'a@b.com' --subject 'DMARC' --filename 'x.zip'")
        sys.exit(1)

    import argparse

    parser = argparse.ArgumentParser(description="Test DMARC report detection")
    parser.add_argument("--sender", default="")
    parser.add_argument("--subject", default="")
    parser.add_argument("--filename", default="")
    parser.add_argument("--file", default="")
    args = parser.parse_args()

    result = detect_dmarc_report(
        sender=args.sender,
        subject=args.subject,
        filename=args.filename,
        file_path=Path(args.file) if args.file else None,
    )

    print(f"Is DMARC report: {result.is_dmarc_report}")
    print(f"Confidence: {result.confidence}")
    print(f"Content valid: {result.content_valid}")
    print(f"Metadata score: {result.metadata_score}/3")
    print(f"Reason: {result.reason}")
