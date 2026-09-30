"""Regression tests for Outlook sync pagination (@odata.nextLink).

Bug: sync_account_emails() only ever looked at the first Graph page
($top=50, no nextLink follow). On a busy mailbox the 50 newest messages
are rarely DMARC reports, so syncs scanned "many emails" yet found
nothing — and marked them processed so they were never revisited.

These tests stub the DB/token/detector layers and drive the Graph HTTP
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

class _Settings:
    reports_dir = Path("/tmp/dmarc-test-reports")


_config_stub = types.ModuleType("config")
_config_stub.settings = _Settings()
sys.modules["config"] = _config_stub

_models_stub = types.ModuleType("models")
sys.modules["models"] = _models_stub

_processed_stub = types.ModuleType("models.processed_emails")
_processed_stub.is_processed = lambda account_id, msg_id: msg_id in SEEN
_processed_stub.mark_processed = lambda account_id, msg_id: SEEN.add(msg_id)
sys.modules["models.processed_emails"] = _processed_stub

_detector_stub = types.ModuleType("services.dmarc_detector")

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
sys.modules["services.dmarc_detector"] = _detector_stub

_auth_stub = types.ModuleType("services.outlook_auth")

async def _fake_token(token_json: dict) -> str:
    return "fake-token"

_auth_stub.get_valid_access_token = _fake_token
sys.modules["services.outlook_auth"] = _auth_stub

_workers_stub = types.ModuleType("workers")
_processor_stub = types.ModuleType("workers.processor")

class _IngestResult:
    reports = [{"id": 1}]
    is_duplicate = False

_processor_stub.process_file = lambda path: _IngestResult()
sys.modules["workers"] = _workers_stub
sys.modules["workers.processor"] = _processor_stub

SEEN: set = set()

from services import outlook_sync  # noqa: E402


# ── Fake httpx surface ────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


def _msg(msg_id: str) -> dict:
    return {
        "id": msg_id,
        "subject": "Report domain: x",
        "from": {"emailAddress": {"address": "noreply@example.com"}},
        "receivedDateTime": "2026-09-20T00:00:00Z",
        "hasAttachments": True,
    }


def _att_page() -> dict:
    return {
        "value": [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "example.com!x!20260920!20260921.zip",
            "contentBytes": base64.b64encode(b"fake-zip-bytes").decode(),
        }]
    }


class FakeClient:
    """Replays queued responses; records every GET URL."""

    queue: list = []
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        FakeClient.calls.append(url)
        if url.endswith("/attachments"):
            return FakeResponse(_att_page())
        payload = FakeClient.queue.pop(0)
        return FakeResponse(payload)


@pytest.fixture
def account(tmp_path):
    SEEN.clear()
    FakeClient.queue = []
    FakeClient.calls = []
    _config_stub.settings.reports_dir = tmp_path
    outlook_sync.MAX_PAGES = 10
    return {"id": 7, "email": "dmarc@skillbrew.com", "token_json": {}}


@pytest.fixture
def patch_http(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def _pages(*bodies):
    FakeClient.queue = list(bodies)


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_follows_nextlink_to_second_page(account, patch_http):
    """A report sitting on page 2 must be found, not silently missed."""
    _pages(
        {"value": [_msg("m-new-1")],
         "@odata.nextLink": "https://graph/next?$skiptoken=abc"},
        {"value": [_msg("m-new-2")]},
    )
    saved = await outlook_sync.sync_account_emails(account, backfill=False)
    assert saved == 2
    list_calls = [c for c in FakeClient.calls if not c.endswith("/attachments")]
    assert len(list_calls) == 2
    assert list_calls[1].startswith("https://graph/next")


@pytest.mark.asyncio
async def test_skips_processed_without_extra_calls(account, patch_http):
    """Steady-state sync where page 1 is fully processed stops at 1 call,
    even when Graph offers a nextLink (older pages are all already seen)."""
    SEEN.update({"m-old-1", "m-old-2"})
    _pages(
        {"value": [_msg("m-old-1"), _msg("m-old-2")],
         "@odata.nextLink": "https://graph/next?$skiptoken=abc"},
    )
    saved = await outlook_sync.sync_account_emails(account, backfill=False)
    assert saved == 0
    assert len(FakeClient.calls) == 1


@pytest.mark.asyncio
async def test_backfill_keeps_walking_past_processed_pages(account, patch_http):
    """Backfill must not early-stop: processed page 1 + fresh page 2."""
    SEEN.update({"m-old-1"})
    _pages(
        {"value": [_msg("m-old-1")],
         "@odata.nextLink": "https://graph/next?$skiptoken=abc"},
        {"value": [_msg("m-new-9")]},
    )
    saved = await outlook_sync.sync_account_emails(account, backfill=True)
    assert saved == 1


@pytest.mark.asyncio
async def test_max_pages_caps_runaway_pagination(account, patch_http, monkeypatch):
    """A mailbox with endless nextLinks is bounded by MAX_PAGES."""
    monkeypatch.setattr(outlook_sync, "MAX_PAGES", 2)
    _pages(
        {"value": [_msg("m-a")],
         "@odata.nextLink": "https://graph/next?page=2"},
        {"value": [_msg("m-b")],
         "@odata.nextLink": "https://graph/next?page=3"},
        {"value": [_msg("m-c")]},  # never reached
    )
    saved = await outlook_sync.sync_account_emails(account, backfill=True)
    assert saved == 2
    list_calls = [c for c in FakeClient.calls if "attachments" not in c]
    assert len(list_calls) == 2


@pytest.mark.asyncio
async def test_single_page_without_nextlink_unchanged(account, patch_http):
    """Common case: one page, no nextLink — exactly 1 list call."""
    _pages({"value": [_msg("m-only")]})
    saved = await outlook_sync.sync_account_emails(account, backfill=False)
    assert saved == 1
    list_calls = [c for c in FakeClient.calls if "attachments" not in c]
    assert len(list_calls) == 1


@pytest.mark.asyncio
async def test_search_failure_returns_zero(account, patch_http, monkeypatch):
    """A non-200 on the first page returns 0 without raising."""

    class FailClient(FakeClient):
        async def get(self, url, headers=None, params=None):
            FakeClient.calls.append(url)
            return FakeResponse({"error": "boom"}, status=500)

    monkeypatch.setattr(httpx, "AsyncClient", FailClient)
    saved = await outlook_sync.sync_account_emails(account, backfill=False)
    assert saved == 0


# ── Ordering, large attachments, retry-on-failure ─────────────────────────────

def _client_with(att_payload=None, att_status=200, raw_status=200, raw_body=b"fake-zip-bytes"):
    """FakeClient variant with controllable attachment-list / $value behaviour."""

    class _C(FakeClient):
        async def get(self, url, headers=None, params=None):
            FakeClient.calls.append(url)
            _C.params.append(params)
            if url.endswith("/$value"):
                r = FakeResponse({}, status=raw_status)
                r.content = raw_body
                return r
            if url.endswith("/attachments"):
                return FakeResponse(att_payload if att_payload is not None else _att_page(),
                                    status=att_status)
            return FakeResponse(FakeClient.queue.pop(0))

    _C.params = []
    return _C


def _large_att_page() -> dict:
    return {"value": [{
        "@odata.type": "#microsoft.graph.fileAttachment",
        "id": "att-big",
        "name": "google.com!x!20260920!20260921.zip",
        # no contentBytes: Graph omits it for large attachments
    }]}


@pytest.mark.asyncio
@pytest.mark.parametrize("backfill", [False, True])
async def test_list_query_is_ordered_newest_first(account, monkeypatch, backfill):
    """Graph only guarantees newest-first with $orderby, and $orderby's
    property must lead the $filter or Graph rejects it (InefficientFilter)."""
    C = _client_with()
    monkeypatch.setattr(httpx, "AsyncClient", C)
    _pages({"value": []})
    await outlook_sync.sync_account_emails(account, backfill=backfill)
    params = C.params[0]
    assert params["$orderby"] == "receivedDateTime desc"
    assert params["$filter"].startswith("receivedDateTime ge ")
    assert "hasAttachments eq true" in params["$filter"]


@pytest.mark.asyncio
async def test_large_attachment_downloaded_via_value(account, monkeypatch):
    C = _client_with(att_payload=_large_att_page())
    monkeypatch.setattr(httpx, "AsyncClient", C)
    _pages({"value": [_msg("m-big")]})
    saved = await outlook_sync.sync_account_emails(account, backfill=False)
    assert saved == 1
    assert any(c.endswith("/attachments/att-big/$value") for c in FakeClient.calls)
    assert "m-big" in SEEN


@pytest.mark.asyncio
async def test_large_attachment_download_failure_is_retried(account, monkeypatch):
    C = _client_with(att_payload=_large_att_page(), raw_status=500)
    monkeypatch.setattr(httpx, "AsyncClient", C)
    _pages({"value": [_msg("m-big")]})
    saved = await outlook_sync.sync_account_emails(account, backfill=False)
    assert saved == 0
    assert "m-big" not in SEEN  # stays unprocessed → retried next sync


@pytest.mark.asyncio
async def test_attachment_list_failure_is_retried(account, monkeypatch):
    C = _client_with(att_status=503)
    monkeypatch.setattr(httpx, "AsyncClient", C)
    _pages({"value": [_msg("m-x")]})
    await outlook_sync.sync_account_emails(account, backfill=False)
    assert "m-x" not in SEEN


@pytest.mark.asyncio
async def test_message_without_report_attachment_is_marked(account, monkeypatch):
    pdf = {"value": [{"@odata.type": "#microsoft.graph.fileAttachment",
                      "id": "a", "name": "invoice.pdf", "contentBytes": "eA=="}]}
    C = _client_with(att_payload=pdf)
    monkeypatch.setattr(httpx, "AsyncClient", C)
    _pages({"value": [_msg("m-pdf")]})
    saved = await outlook_sync.sync_account_emails(account, backfill=False)
    assert saved == 0
    assert "m-pdf" in SEEN
