"""Regression tests for Gmail sync pagination (nextPageToken).

Same bug class as Outlook: sync_account_emails() only ever looked at the
first Gmail page (maxResults=50, no nextPageToken follow). On a busy
mailbox the 50 newest messages are rarely DMARC reports, so syncs scanned
"many emails" yet found nothing — and marked them processed so they were
never revisited.

These tests stub the DB/token/detector layers and drive the Gmail HTTP
surface with a fake httpx.AsyncClient.
"""

from __future__ import annotations

import base64
import sys
import types
from pathlib import Path

import httpx
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


# ── Stubs for heavy/side-effecting layers ─────────────────────────────────────

def _ensure_stub(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


SEEN: set = set()

_config_stub = _ensure_stub("config")


class _Settings:
    reports_dir = Path("/tmp/dmarc-test-reports-gmail")


_config_stub.settings = _Settings()

_processed_stub = _ensure_stub("models.processed_emails")
_processed_stub.is_processed = lambda account_id, msg_id: msg_id in SEEN
_processed_stub.mark_processed = lambda account_id, msg_id: SEEN.add(msg_id)

_detector_stub = _ensure_stub("services.dmarc_detector")


class _Detection:
    def __init__(self, hit: bool):
        self.is_dmarc_report = hit
        self.confidence = "high" if hit else "none"
        self.metadata_score = 3 if hit else 0
        self.reason = "stub"


_detector_stub.detect_dmarc_report = (
    lambda sender="", subject="", filename="", file_path=None, file_bytes=None: _Detection(True)
)
_detector_stub.has_report_container_suffix = lambda name: name.endswith(".zip")

_oauth_stub = _ensure_stub("services.oauth")


async def _fake_token(token_json: dict) -> str:
    return "fake-token"


_oauth_stub.get_valid_access_token = _fake_token

_workers_stub = _ensure_stub("workers")
_processor_stub = _ensure_stub("workers.processor")


class _IngestResult:
    reports = [{"id": 1}]
    is_duplicate = False


_processor_stub.process_file = lambda path: _IngestResult()

from services import gmail_sync  # noqa: E402


# ── Fake httpx surface ────────────────────────────────────────────────────────

LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def _list_entry(msg_id: str) -> dict:
    return {"id": msg_id, "threadId": f"t-{msg_id}"}


def _full_message(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Report domain: x"},
                {"name": "From", "value": "noreply@example.com"},
            ],
            "parts": [{
                "filename": "example.com!x!20260920!20260921.zip",
                "body": {"attachmentId": f"att-{msg_id}"},
            }],
        },
    }


def _att_body() -> dict:
    return {"data": base64.urlsafe_b64encode(b"fake-zip-bytes").decode()}


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    """Replays queued list-pages; synthesizes message/attachment bodies."""

    queue: list = []
    calls: list = []  # (url, params) tuples

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        FakeClient.calls.append((url, dict(params or {})))
        if "/attachments/" in url:
            return FakeResponse(_att_body())
        if url == LIST_URL:
            payload = FakeClient.queue.pop(0)
            return FakeResponse(payload)
        # Full message fetch: .../messages/{id}
        msg_id = url.rsplit("/", 1)[-1]
        return FakeResponse(_full_message(msg_id))


@pytest.fixture
def account(tmp_path):
    SEEN.clear()
    FakeClient.queue = []
    FakeClient.calls = []
    _config_stub.settings.reports_dir = tmp_path
    gmail_sync.MAX_PAGES = 10
    return {"id": 9, "email": "dmarc@gmail.com", "token_json": {}}


@pytest.fixture
def patch_http(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def _pages(*bodies):
    FakeClient.queue = list(bodies)


def _list_calls():
    return [(u, p) for (u, p) in FakeClient.calls if u == LIST_URL]


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_follows_next_page_token(account, patch_http):
    """A report sitting on page 2 must be found, not silently missed."""
    _pages(
        {"messages": [_list_entry("m-new-1")], "nextPageToken": "tok-abc"},
        {"messages": [_list_entry("m-new-2")]},
    )
    saved = await gmail_sync.sync_account_emails(account, backfill=False)
    assert saved == 2
    lists = _list_calls()
    assert len(lists) == 2
    assert lists[1][1].get("pageToken") == "tok-abc"


@pytest.mark.asyncio
async def test_skips_processed_without_extra_calls(account, patch_http):
    """Steady-state sync where page 1 is fully processed stops at 1 call,
    even when Gmail offers a nextPageToken (older pages are all already
    seen)."""
    SEEN.update({"m-old-1", "m-old-2"})
    _pages(
        {"messages": [_list_entry("m-old-1"), _list_entry("m-old-2")],
         "nextPageToken": "tok-abc"},
    )
    saved = await gmail_sync.sync_account_emails(account, backfill=False)
    assert saved == 0
    assert len(_list_calls()) == 1


@pytest.mark.asyncio
async def test_backfill_keeps_walking_past_processed_pages(account, patch_http):
    """Backfill must not early-stop: processed page 1 + fresh page 2."""
    SEEN.update({"m-old-1"})
    _pages(
        {"messages": [_list_entry("m-old-1")], "nextPageToken": "tok-abc"},
        {"messages": [_list_entry("m-new-9")]},
    )
    saved = await gmail_sync.sync_account_emails(account, backfill=True)
    assert saved == 1


@pytest.mark.asyncio
async def test_max_pages_caps_runaway_pagination(account, patch_http, monkeypatch):
    """A mailbox with endless nextPageTokens is bounded by MAX_PAGES."""
    monkeypatch.setattr(gmail_sync, "MAX_PAGES", 2)
    _pages(
        {"messages": [_list_entry("m-a")], "nextPageToken": "tok-2"},
        {"messages": [_list_entry("m-b")], "nextPageToken": "tok-3"},
        {"messages": [_list_entry("m-c")]},  # never reached
    )
    saved = await gmail_sync.sync_account_emails(account, backfill=True)
    assert saved == 2
    assert len(_list_calls()) == 2


@pytest.mark.asyncio
async def test_single_page_without_token_unchanged(account, patch_http):
    """Common case: one page, no nextPageToken — exactly 1 list call."""
    _pages({"messages": [_list_entry("m-only")]})
    saved = await gmail_sync.sync_account_emails(account, backfill=False)
    assert saved == 1
    assert len(_list_calls()) == 1


@pytest.mark.asyncio
async def test_search_failure_returns_zero(account, patch_http, monkeypatch):
    """A non-200 on the first page returns 0 without raising."""

    class FailClient(FakeClient):
        async def get(self, url, headers=None, params=None):
            FakeClient.calls.append((url, dict(params or {})))
            return FakeResponse({"error": "boom"}, status=500)

    monkeypatch.setattr(httpx, "AsyncClient", FailClient)
    saved = await gmail_sync.sync_account_emails(account, backfill=False)
    assert saved == 0


# ── Retry-on-failure (messages must not be marked processed on fetch errors) ──

def _client_failing(msg_status=200, att_status=200):
    class _C(FakeClient):
        async def get(self, url, headers=None, params=None):
            FakeClient.calls.append((url, dict(params or {})))
            if "/attachments/" in url:
                return FakeResponse(_att_body(), status=att_status)
            if url == LIST_URL:
                return FakeResponse(FakeClient.queue.pop(0))
            msg_id = url.rsplit("/", 1)[-1]
            return FakeResponse(_full_message(msg_id), status=msg_status)
    return _C


@pytest.mark.asyncio
async def test_message_fetch_failure_is_retried(account, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client_failing(msg_status=500))
    _pages({"messages": [_list_entry("m-1")]})
    saved = await gmail_sync.sync_account_emails(account, backfill=False)
    assert saved == 0
    assert "m-1" not in SEEN


@pytest.mark.asyncio
async def test_attachment_download_failure_is_retried(account, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _client_failing(att_status=500))
    _pages({"messages": [_list_entry("m-2")]})
    saved = await gmail_sync.sync_account_emails(account, backfill=False)
    assert saved == 0
    assert "m-2" not in SEEN


@pytest.mark.asyncio
async def test_successful_message_is_marked(account, patch_http):
    _pages({"messages": [_list_entry("m-3")]})
    saved = await gmail_sync.sync_account_emails(account, backfill=False)
    assert saved == 1
    assert "m-3" in SEEN


@pytest.mark.asyncio
async def test_bad_base64_does_not_crash_sync(account, monkeypatch):
    class _C(FakeClient):
        async def get(self, url, headers=None, params=None):
            if "/attachments/" in url:
                return FakeResponse({"data": "!!!not-base64!!!"})
            return await FakeClient.get(self, url, headers, params)
    monkeypatch.setattr(httpx, "AsyncClient", _C)
    _pages({"messages": [_list_entry("m-4")]})
    saved = await gmail_sync.sync_account_emails(account, backfill=False)
    assert saved == 0
