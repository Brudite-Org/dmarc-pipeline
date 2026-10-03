"""Explain *why* a DMARC aggregate-report record failed.

Aggregate reports (RFC 7489) carry no per-message data, so the diagnosis is per
source-IP row: which mechanism failed, for which domain, and the usual causes.
It is derived purely from what is already stored with each record, so no schema
change is needed.

The wording mirrors ``static/file_detail.js`` (diagnoseDkim / diagnoseSpf); keep
the two in step when editing either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

STATUS_FAIL = "fail"    # neither DKIM nor SPF aligned -> DMARC fails
STATUS_ISSUE = "issue"  # exactly one failed -> DMARC passes, but something is mis-set
STATUS_PASS = "pass"


@dataclass
class MechanismDiagnosis:
    ok: bool
    text: str
    actions: list[str] = field(default_factory=list)


@dataclass
class Diagnosis:
    status: str
    dkim: MechanismDiagnosis
    spf: MechanismDiagnosis
    effect: str

    @property
    def actions(self) -> list[str]:
        return [*self.dkim.actions, *self.spf.actions]


def record_status(rec: dict) -> str:
    dkim = bool(rec.get("dkim_aligned"))
    spf = bool(rec.get("spf_aligned"))
    if not dkim and not spf:
        return STATUS_FAIL
    if not dkim or not spf:
        return STATUS_ISSUE
    return STATUS_PASS


def load_auth(rec: dict, key: str) -> list[dict]:
    """Return ``dkim_auth`` / ``spf_auth`` as a list, whether stored as JSON text or a list."""
    value = rec.get(key)
    if value is None:
        value = rec.get(f"{key}_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _result(entry: dict) -> str:
    return (entry.get("result") or "").lower()


def diagnose_dkim(rec: dict, report: dict) -> MechanismDiagnosis:
    auth = load_auth(rec, "dkim_auth")
    mode = "strict" if report.get("adkim") == "s" else "relaxed"
    from_domain = rec.get("header_from") or report.get("domain") or "the From domain"

    if rec.get("dkim_aligned"):
        return MechanismDiagnosis(True, "DKIM passed and is aligned with the From domain.")
    if not auth:
        return MechanismDiagnosis(
            False,
            "No DKIM signature was found on the message.",
            [f"Enable DKIM signing for {from_domain} on the sending service and publish its public key in DNS."],
        )

    passed = [a for a in auth if _result(a) == "pass"]
    if passed:
        domains = ", ".join(dict.fromkeys(a["domain"] for a in passed if a.get("domain"))) or "another domain"
        return MechanismDiagnosis(
            False,
            f"DKIM verified for {domains}, but that domain is not aligned with {from_domain} "
            f"({mode} alignment, adkim={report.get('adkim') or 'r'}). "
            "Only signatures by the From domain count for DMARC.",
            [f"Configure the sender to sign with d={from_domain} (custom/branded DKIM) instead of its own domain."],
        )

    detail = "; ".join(
        f"{a.get('domain') or 'unknown domain'}"
        f"{', selector ' + a['selector'] if a.get('selector') else ''}"
        f" → {a.get('result') or 'no result'}"
        for a in auth
    )
    return MechanismDiagnosis(
        False,
        f"DKIM signature could not be verified ({detail}).",
        [
            "Check the DKIM public key for that selector exists in DNS and matches the key the sender signs with.",
            "If the IP is not one of our services, the message may carry a copied/forged signature or have been "
            "altered in transit (forwarder, mailing list, footer added).",
        ],
    )


_SPF_MEANING = {
    "fail": lambda ip, dom: f"{ip} is not authorised by the SPF record of {dom} (hard fail).",
    "softfail": lambda ip, dom: f"{ip} is not authorised by the SPF record of {dom} (soft fail, ~all).",
    "neutral": lambda ip, dom: f"The SPF record of {dom} makes no statement about {ip} (neutral, ?all).",
    "none": lambda ip, dom: f"{dom} publishes no SPF record.",
    "permerror": lambda ip, dom: f"The SPF record of {dom} is invalid (syntax error or more than 10 DNS lookups).",
    "temperror": lambda ip, dom: f"A temporary DNS error stopped the SPF check for {dom}; it may not repeat.",
}


def diagnose_spf(rec: dict, report: dict) -> MechanismDiagnosis:
    auth = load_auth(rec, "spf_auth")
    mode = "strict" if report.get("aspf") == "s" else "relaxed"
    from_domain = rec.get("header_from") or report.get("domain") or "the From domain"
    ip = rec.get("source_ip") or "The sending IP"

    if rec.get("spf_aligned"):
        return MechanismDiagnosis(True, "SPF passed and is aligned with the From domain.")
    if not auth:
        return MechanismDiagnosis(False, "No SPF result was reported.")

    passed = [a for a in auth if _result(a) == "pass"]
    if passed:
        domains = ", ".join(dict.fromkeys(a["domain"] for a in passed if a.get("domain"))) or "another domain"
        return MechanismDiagnosis(
            False,
            f"SPF passed for {domains} (the envelope/bounce domain), but it is not aligned with "
            f"{from_domain} ({mode} alignment, aspf={report.get('aspf') or 'r'}).",
            [f"Make the sender use a return-path / MAIL FROM on {from_domain} (custom bounce domain)."],
        )

    lines = []
    for a in auth:
        domain = a.get("domain") or rec.get("envelope_from") or from_domain
        meaning = _SPF_MEANING.get(_result(a))
        base = meaning(ip, domain) if meaning else f'SPF result "{a.get("result") or "none"}" for {domain}.'
        lines.append(f"{base} [scope: {a['scope']}]" if a.get("scope") else base)

    actions = []
    if any(_result(a) in ("fail", "softfail", "neutral") for a in auth):
        actions.append(
            f"If {ip} is a service we use, add it (include:/ip4:/ip6:) to the SPF record of the domain above. "
            "If we don't recognise it, treat it as spoofing."
        )
    if any(_result(a) == "permerror" for a in auth):
        actions.append("Flatten the SPF record or remove unused includes to get under the 10-lookup limit.")
    return MechanismDiagnosis(False, " ".join(lines), actions)


def diagnose(rec: dict, report: dict[str, Any]) -> Diagnosis:
    status = record_status(rec)
    dkim = diagnose_dkim(rec, report)
    spf = diagnose_spf(rec, report)
    policy = report.get("p") or "none"

    if status == STATUS_FAIL:
        disposition = rec.get("disposition")
        if disposition and disposition != "none":
            effect = f'DMARC failed. The receiver applied "{disposition}" (published policy p={policy}).'
        else:
            effect = f'DMARC failed. The receiver still delivered it (disposition "none"; published policy p={policy}).'
    elif status == STATUS_ISSUE:
        broken = "SPF" if dkim.ok else "DKIM"
        effect = (
            f"DMARC passed because the other mechanism is aligned, but {broken} is mis-set "
            "and could become a delivery risk if that other mechanism breaks."
        )
    else:
        effect = "DMARC passed."
    return Diagnosis(status, dkim, spf, effect)
