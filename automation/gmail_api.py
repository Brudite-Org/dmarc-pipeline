"""Gmail API watcher — OAuth2-based, supports multiple accounts.

Setup for each account:
1. Go to https://console.cloud.google.com
2. Create project → Enable Gmail API → Create OAuth credentials (Desktop app)
3. Download credentials.json
4. Run once: python -m automation.gmail_api --auth-only
   → Opens browser, saves token to ~/.dmarc-pipeline/tokens/{email}.json

Usage:
    python -m automation.gmail_api --auth-only          # First-time auth
    python -m automation.gmail_api --auth-only --email a@b.c  # Auth specific account
    python -m automation.gmail_api                      # Run once
    python -m automation.gmail_api --loop               # Poll continuously
    python -m automation.gmail_api --accounts           # List configured accounts

Environment:
    GMAIL_CREDENTIALS_DIR   — Directory with credentials.json files (default: ~/.dmarc-pipeline/credentials)
    GMAIL_TOKEN_DIR         — Directory for OAuth tokens (default: ~/.dmarc-pipeline/tokens)
    GMAIL_POLL_INTERVAL     — Seconds between polls (default: 300)
    GMAIL_QUERY             — Gmail search query (default: subject:DMARC has:attachment newer_than:1d)
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path

from config import settings

logger = logging.getLogger("dmarc.gmail_api")

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_BASE_DIR = Path.home() / ".dmarc-pipeline"
CREDENTIALS_DIR = Path(os.environ.get("GMAIL_CREDENTIALS_DIR", DEFAULT_BASE_DIR / "credentials"))
TOKEN_DIR = Path(os.environ.get("GMAIL_TOKEN_DIR", DEFAULT_BASE_DIR / "tokens"))
POLL_INTERVAL = int(os.environ.get("GMAIL_POLL_INTERVAL", "300"))
GMAIL_QUERY = os.environ.get("GMAIL_QUERY", "subject:DMARC has:attachment newer_than:1d")
MARK_READ = os.environ.get("EMAIL_MARK_READ", "true").lower() == "true"

# ── Google API imports ────────────────────────────────────────────────────────

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


# ── Account management ────────────────────────────────────────────────────────


def list_credentials() -> list[Path]:
    """List all credential files."""
    if not CREDENTIALS_DIR.exists():
        return []
    return list(CREDENTIALS_DIR.glob("*.json"))


def list_accounts() -> list[str]:
    """List all authenticated accounts (have valid tokens)."""
    if not TOKEN_DIR.exists():
        return []
    accounts = []
    for token_file in TOKEN_DIR.glob("*.json"):
        try:
            data = json.loads(token_file.read_text())
            # Extract email from token if available
            email = data.get("client_id", token_file.stem)[:50]
            accounts.append(f"{token_file.stem}")
        except Exception:
            accounts.append(f"{token_file.stem} (invalid)")
    return accounts


def get_token_path(email: str) -> Path:
    """Get token file path for an email."""
    safe_email = email.replace("@", "_at_").replace(".", "_")
    return TOKEN_DIR / f"{safe_email}.json"


def get_credentials_for_account(credentials_path: Path, token_path: Path) -> Credentials | None:
    """Load or refresh credentials for an account."""
    if not token_path.exists():
        return None

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds.valid:
        return creds

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            return creds
        except Exception as exc:
            logger.warning("Token refresh failed for %s: %s", token_path.stem, exc)
            return None

    return None


def authenticate_account(credentials_path: Path, email: str | None = None) -> Path:
    """Run OAuth flow for a new account. Returns token path."""
    if not GMAIL_AVAILABLE:
        raise RuntimeError(
            "Gmail API libraries not installed. Run:\n"
            "pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    # Use provided email or derive from filename
    account_email = email or credentials_path.stem
    token_path = get_token_path(account_email)

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path.write_text(creds.to_json())
    logger.info("Authenticated and saved token: %s", token_path)
    print(f"✓ Authenticated: {account_email}")
    print(f"  Token saved to: {token_path}")

    return token_path


# ── Gmail operations ──────────────────────────────────────────────────────────


def get_gmail_service(creds: Credentials):
    """Build Gmail API service from credentials."""
    return build("gmail", "v1", credentials=creds)


def fetch_dmarc_emails(service, account_name: str = "unknown") -> list[tuple[str, bytes]]:
    """Search for DMARC emails and extract attachments."""
    results = service.users().messages().list(userId="me", q=GMAIL_QUERY).execute()
    messages = results.get("messages", [])

    if not messages:
        return []

    logger.info("[%s] Found %d new DMARC email(s)", account_name, len(messages))

    attachments: list[tuple[str, bytes]] = []

    for msg_info in messages:
        msg = service.users().messages().get(userId="me", id=msg_info["id"], format="full").execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        subject = headers.get("Subject", "Unknown")
        sender = headers.get("From", "Unknown")
        logger.info("[%s] Processing: %s (from %s)", account_name, subject, sender)

        if "parts" in msg["payload"]:
            for part in msg["payload"]["parts"]:
                if part.get("filename") and part.get("body", {}).get("attachmentId"):
                    filename = part["filename"]
                    if filename.lower().endswith((".zip", ".xml", ".gz")):
                        att_id = part["body"]["attachmentId"]
                        att = service.users().messages().attachments().get(
                            userId="me", messageId=msg_info["id"], id=att_id
                        ).execute()
                        data = base64.urlsafe_b64decode(att["data"])
                        attachments.append((filename, data))
                        logger.info("  → %s (%d bytes)", filename, len(data))

        if MARK_READ:
            service.users().messages().modify(
                userId="me", id=msg_info["id"], body={"removeLabelIds": ["UNREAD"]}
            ).execute()

    return attachments


def process_attachments(attachments: list[tuple[str, bytes]]) -> int:
    """Save attachments to reports directory."""
    if not attachments:
        return 0

    reports_dir = settings.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for filename, data in attachments:
        timestamp = int(time.time())
        safe_name = f"{timestamp}_{filename}"
        filepath = reports_dir / safe_name
        filepath.write_bytes(data)
        logger.info("Saved: %s", filepath)
        count += 1

    return count


# ── Main operations ───────────────────────────────────────────────────────────


def get_all_authenticated_services() -> list[tuple[str, object]]:
    """Get Gmail services for all authenticated accounts."""
    if not GMAIL_AVAILABLE:
        raise RuntimeError("Gmail API libraries not installed")

    services = []
    credentials_files = list_credentials()

    if not credentials_files:
        logger.warning("No credential files found in %s", CREDENTIALS_DIR)
        logger.info("Place your credentials.json files there, then run with --auth-only")
        return []

    for cred_file in credentials_files:
        account_name = cred_file.stem
        token_path = get_token_path(account_name)
        creds = get_credentials_for_account(cred_file, token_path)

        if creds:
            service = get_gmail_service(creds)
            services.append((account_name, service))
        else:
            logger.warning("No valid token for %s. Run: python -m automation.gmail_api --auth-only --email %s", account_name, account_name)

    return services


def run_once() -> int:
    """Run a single check across all accounts."""
    services = get_all_authenticated_services()
    if not services:
        return 0

    total = 0
    for account_name, service in services:
        try:
            attachments = fetch_dmarc_emails(service, account_name)
            count = process_attachments(attachments)
            total += count
        except Exception as exc:
            logger.error("[%s] Failed: %s", account_name, exc)

    return total


def run_loop() -> None:
    """Run continuous polling across all accounts."""
    logger.info("Starting Gmail API watcher (poll interval: %ds)", POLL_INTERVAL)
    logger.info("Monitoring accounts: %s", ", ".join(name for name, _ in get_all_authenticated_services()))

    while True:
        try:
            count = run_once()
            if count > 0:
                logger.info("Processed %d new report(s)", count)
        except Exception as exc:
            logger.error("Poll failed: %s", exc)

        time.sleep(POLL_INTERVAL)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DMARC Gmail API Watcher (multi-account)")
    parser.add_argument("--auth-only", action="store_true", help="Run OAuth flow for new account")
    parser.add_argument("--email", type=str, help="Email address for auth")
    parser.add_argument("--credentials", type=str, help="Path to specific credentials.json")
    parser.add_argument("--loop", action="store_true", help="Run in continuous polling mode")
    parser.add_argument("--accounts", action="store_true", help="List configured accounts")
    args = parser.parse_args()

    from logging_config import setup_logging
    setup_logging()

    if args.accounts:
        accounts = list_accounts()
        if accounts:
            print("Configured accounts:")
            for acc in accounts:
                print(f"  • {acc}")
        else:
            print("No accounts configured.")
            print(f"Place credentials.json in: {CREDENTIALS_DIR}")
        return

    if args.auth_only:
        if args.credentials:
            cred_path = Path(args.credentials)
        elif CREDENTIALS_DIR.exists() and list(CREDENTIALS_DIR.glob("*.json")):
            cred_path = list(CREDENTIALS_DIR.glob("*.json"))[0]
        else:
            print("Error: No credentials found.")
            print(f"Place credentials.json in: {CREDENTIALS_DIR}")
            print("Or specify: --credentials /path/to/credentials.json")
            sys.exit(1)

        authenticate_account(cred_path, args.email)
        return

    if args.loop:
        run_loop()
    else:
        count = run_once()
        print(f"Processed {count} new report(s)")


if __name__ == "__main__":
    main()
