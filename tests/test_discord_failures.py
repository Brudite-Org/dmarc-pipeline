"""Tests for failure diagnosis and the Discord failure embeds.

Aggregate reports can't say which email failed, but the Discord alert must say
which sender failed and why, without sending anyone to the website.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from analysis.diagnosis import diagnose, load_auth, record_status  # noqa: E402
from services.discord_failures import (  # noqa: E402
    FIELD_VALUE_LIMIT,
    MAX_EMBEDS_PER_MESSAGE,
    MAX_FAILURES_SHOWN,
    MESSAGE_CHAR_BUDGET,
    batch_embeds,
    build_failure_embeds,
    embed_size,
)

REPORT = {
    "id": 1,
    "report_id": "0123456789abcdef0123456789abcdef",
    "domain": "example.com",
    "org_name": "Fake Reporter Inc",
    "date_begin": "2026-10-01T00:00:00+00:00",
    "date_end": "2026-10-02T00:00:00+00:00",
    "adkim": "r",
    "aspf": "r",
    "p": "quarantine",
}


def passing(**over):
    rec = {
        "source_ip": "2001:db8::1", "count": 1, "header_from": "example.com",
        "envelope_from": "example.com", "envelope_to": "other.example.org", "disposition": "none",
        "dkim_aligned": True, "spf_aligned": True, "dkim_result": "pass", "spf_result": "pass",
        "dkim_auth_json": json.dumps([{"domain": "example.com", "result": "pass", "selector": "selector1"}]),
        "spf_auth_json": json.dumps([{"domain": "example.com", "result": "pass", "scope": "mfrom"}]),
    }
    rec.update(over)
    return rec


def failing(**over):
    """A fabricated failing record (RFC 5737 documentation IP, example domains)."""
    rec = passing(
        source_ip="203.0.113.50", envelope_to="recipient.example.net", disposition="quarantine",
        dkim_aligned=False, spf_aligned=False, dkim_result="fail", spf_result="fail",
        dkim_auth_json=json.dumps([{"domain": "example.com", "result": "fail", "selector": "selector1"}]),
        spf_auth_json=json.dumps([{"domain": "example.com", "result": "fail", "scope": "mfrom"}]),
    )
    rec.update(over)
    return rec


def field_map(embed):
    return {f["name"]: f["value"] for f in embed["fields"]}


# ── Diagnosis ─────────────────────────────────────────────────────────────────


def test_record_status():
    assert record_status(passing()) == "pass"
    assert record_status(failing()) == "fail"
    assert record_status(passing(spf_aligned=False)) == "issue"


def test_load_auth_accepts_json_text_list_and_garbage():
    assert load_auth({"dkim_auth_json": '[{"domain": "a.com"}]'}, "dkim_auth") == [{"domain": "a.com"}]
    assert load_auth({"dkim_auth": [{"domain": "b.com"}]}, "dkim_auth") == [{"domain": "b.com"}]
    assert load_auth({"dkim_auth_json": "not json"}, "dkim_auth") == []
    assert load_auth({}, "spf_auth") == []


def test_failure_is_explained():
    dx = diagnose(failing(), REPORT)
    assert dx.status == "fail"
    assert "selector1" in dx.dkim.text and "could not be verified" in dx.dkim.text
    assert "203.0.113.50 is not authorised by the SPF record of example.com" in dx.spf.text
    assert 'applied "quarantine"' in dx.effect


def test_unaligned_third_party_pass_is_called_out():
    rec = failing(
        dkim_auth_json=json.dumps([{"domain": "thirdparty.example", "result": "pass", "selector": "k1"}]),
        spf_auth_json=json.dumps([{"domain": "bounce.thirdparty.example", "result": "pass", "scope": "mfrom"}]),
    )
    dx = diagnose(rec, REPORT)
    assert "DKIM verified for thirdparty.example" in dx.dkim.text and "not aligned" in dx.dkim.text
    assert "SPF passed for bounce.thirdparty.example" in dx.spf.text and "not aligned" in dx.spf.text


def test_missing_signature_and_permerror():
    dx = diagnose(failing(dkim_auth_json="[]", spf_auth_json=json.dumps(
        [{"domain": "example.com", "result": "permerror", "scope": "mfrom"}])), REPORT)
    assert "No DKIM signature" in dx.dkim.text
    assert "10 DNS lookups" in dx.spf.text
    assert any("10-lookup" in a for a in dx.actions)


def test_delivered_despite_policy_when_disposition_none():
    dx = diagnose(failing(disposition="none"), REPORT)
    assert "still delivered" in dx.effect


# ── Failure embeds ────────────────────────────────────────────────────────────


def test_all_passing_report_produces_no_failure_embeds():
    assert build_failure_embeds(REPORT, [passing(), passing()]) == []


def test_failure_embed_contains_every_detail():
    embeds = build_failure_embeds(REPORT, [passing(), failing()])
    assert len(embeds) == 1
    e = embeds[0]
    f = field_map(e)
    assert "203.0.113.50" in e["title"] and "DMARC FAIL" in e["title"]
    assert f["📨 Sent to"] == "`recipient.example.net`"
    assert f["✉️ Messages"] == "**1**"
    assert f["🛡️ Action taken"] == "`quarantine`"
    assert "selector1" in f["🔑 DKIM — fail"]
    assert "not authorised" in f["📮 SPF — fail"]
    assert 'applied "quarantine"' in f["⚖️ Impact"]
    assert "add it (include:/ip4:/ip6:)" in f["🛠️ What to do"]
    finding = f["🔎 Finding the exact email"]
    assert "`recipient.example.net`" in finding and "2026-10-01 00:00 UTC" in finding and "2026-10-02 00:00 UTC" in finding


def test_partial_issue_gets_its_own_embed_and_no_failure_embed():
    embeds = build_failure_embeds(REPORT, [passing(spf_aligned=False, spf_result="softfail",
        spf_auth_json=json.dumps([{"domain": "example.com", "result": "softfail", "scope": "mfrom"}]))])
    assert len(embeds) == 1
    assert "Partial issues" in embeds[0]["title"]
    assert "SPF softfail" in embeds[0]["description"]


def test_failures_sorted_by_volume_and_capped_with_footer():
    recs = [failing(source_ip=f"198.51.100.{i}", count=i + 1) for i in range(MAX_FAILURES_SHOWN + 4)]
    embeds = build_failure_embeds(REPORT, recs)
    assert len(embeds) == MAX_FAILURES_SHOWN
    assert "198.51.100.11" in embeds[0]["title"]  # highest count first
    assert embeds[-1]["footer"]["text"] == "+4 more failing source(s) not shown"


def test_batches_respect_discord_limits():
    long_domain = "x" * 400 + ".com"
    recs = [failing(source_ip=f"198.51.100.{i}", count=i + 1, envelope_to=long_domain,
                    dkim_auth_json=json.dumps([{"domain": long_domain, "result": "fail", "selector": "s" * 200}]),
                    spf_auth_json=json.dumps([{"domain": long_domain, "result": "fail", "scope": "mfrom"}]))
            for i in range(20)]
    embeds = build_failure_embeds(REPORT, recs)
    for e in embeds:
        for fld in e["fields"]:
            assert len(fld["value"]) <= FIELD_VALUE_LIMIT and len(fld["name"]) <= 256
        assert len(e["title"]) <= 256
    batches = batch_embeds(embeds)
    assert sum(len(b) for b in batches) == len(embeds)
    for b in batches:
        assert len(b) <= MAX_EMBEDS_PER_MESSAGE
        assert sum(embed_size(e) for e in b) <= MESSAGE_CHAR_BUDGET or len(b) == 1


def test_xml_values_cannot_break_out_of_code_spans():
    embeds = build_failure_embeds(REPORT, [failing(envelope_to="evil`**@everyone**`.com")])
    assert field_map(embeds[0])["📨 Sent to"] == "`evil'**@everyone**'.com`"


# ── End to end through the notifier (HTTP stubbed) ────────────────────────────


@pytest.fixture
def notifier(monkeypatch):
    cfg = types.ModuleType("config")
    cfg.settings = types.SimpleNamespace(discord_webhook_url="https://discord.test/hook", discord_enabled=True)
    monkeypatch.setitem(sys.modules, "config", cfg)
    monkeypatch.delitem(sys.modules, "services.discord_notifier", raising=False)
    mod = importlib.import_module("services.discord_notifier")
    yield mod
    sys.modules.pop("services.discord_notifier", None)


def test_summary_and_failure_details_share_one_message_without_pings(notifier, monkeypatch):
    records = [passing(), failing()]
    models = types.ModuleType("models")
    models.select_single = lambda table, rid: dict(REPORT)
    models.select = lambda table, filters=None: records
    monkeypatch.setitem(sys.modules, "models", models)

    sent = []

    class Resp:
        status_code = 204
        text = ""

    monkeypatch.setattr(notifier.httpx, "post", lambda url, json=None, **kw: sent.append(json) or Resp())

    assert notifier.post_discord_summary(1) is True
    assert len(sent) == 1  # summary and failure details share one message
    assert sent[0]["embeds"][0]["title"].startswith("📊 DMARC Report")
    assert "DMARC FAIL — 203.0.113.50" in sent[0]["embeds"][1]["title"]
    assert all(p["allowed_mentions"] == {"parse": []} for p in sent)


def test_clean_report_sends_only_the_summary(notifier, monkeypatch):
    models = types.ModuleType("models")
    models.select_single = lambda table, rid: dict(REPORT)
    models.select = lambda table, filters=None: [passing()]
    monkeypatch.setitem(sys.modules, "models", models)
    sent = []

    class Resp:
        status_code = 204
        text = ""

    monkeypatch.setattr(notifier.httpx, "post", lambda url, json=None, **kw: sent.append(json) or Resp())
    assert notifier.post_discord_summary(1) is True
    assert len(sent) == 1


def test_overflow_message_failure_makes_the_call_report_failure(notifier, monkeypatch):
    models = types.ModuleType("models")
    models.select_single = lambda table, rid: dict(REPORT)
    models.select = lambda table, filters=None: [failing(source_ip=f"198.51.100.{i}") for i in range(8)]
    monkeypatch.setitem(sys.modules, "models", models)
    codes = iter([204, 500, 500])

    class Resp:
        def __init__(self):
            self.status_code = next(codes)
            self.text = "boom"

    monkeypatch.setattr(notifier.httpx, "post", lambda url, json=None, **kw: Resp())
    assert notifier.post_discord_summary(1) is False


def test_malformed_records_never_crash_the_builder():
    hostile = [
        {},
        {"count": None, "dkim_aligned": None},
        {"dkim_aligned": False, "spf_aligned": False, "count": "3",
         "dkim_auth_json": "{bad", "spf_auth_json": None},
        {"dkim_aligned": False, "spf_aligned": False, "count": "abc", "source_ip": None,
         "disposition": None, "dkim_auth_json": [{"result": None}],
         "spf_auth_json": [{"domain": None, "result": "FAIL"}]},
    ]
    for rec in hostile:
        build_failure_embeds({}, [rec])
    embeds = build_failure_embeds({}, hostile[2:])
    assert field_map(embeds[0])["✉️ Messages"] == "**3**"
