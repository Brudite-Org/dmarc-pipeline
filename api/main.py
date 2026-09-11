"""FastAPI application — DMARC report API + dashboard.

Production entry point: ``gunicorn wsgi:app -k uvicorn.workers.UvicornWorker``
Local dev: ``python -m cli serve`` or ``./run.sh``

Data layer: Supabase REST API (PostgREST) with service role key.
Tables: dmarc_reports, dmarc_records
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from analysis.engine import build_analysis_from_dicts
from config import settings
from models import (
    count,
    delete,
    get_client,
    insert,
    select,
    select_single,
    update,
)
from models.schemas import RecordRow, ReportMetadata, StatsSummary, UploadResponse
from parsers.dmarc_xml import parse_dmarc_xml
from services.dmarc_detector import (
    REPORT_CONTAINER_SUFFIXES,
    has_report_container_suffix,
)
from workers.processor import process_file

logger = logging.getLogger("dmarc.api")

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
REPORT_DROP_DIR = settings.reports_dir
QUARANTINE_DIR = settings.quarantine_dir

STATIC_DIR.mkdir(exist_ok=True)
TEMPLATES_DIR.mkdir(exist_ok=True)
REPORT_DROP_DIR.mkdir(exist_ok=True)
QUARANTINE_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── App lifecycle ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    from logging_config import setup_logging

    setup_logging()
    get_client()
    yield


app = FastAPI(
    title="DMARC Report Pipeline",
    description="Ingest, store, and browse DMARC aggregate reports.",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── OAuth routes ───────────────────────────────────────────────────────────────

from api.oauth_routes import router as oauth_router
from api.outlook_routes import router as outlook_router
app.include_router(oauth_router)
app.include_router(outlook_router)


# ── Middleware ─────────────────────────────────────────────────────────────────


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "%s %s → %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
    return response


# ── Health check ──────────────────────────────────────────────────────────────


@app.get("/health", include_in_schema=False)
@app.head("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok", "version": "1.0.0"}


# ── Dashboard ─────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ── Upload endpoint ───────────────────────────────────────────────────────────

MAX_BYTES = settings.max_upload_size_mb * 1024 * 1024


@app.post("/api/upload", status_code=201)
def upload_report(file: UploadFile = File(...)):
    """Upload a DMARC ``.zip`` / ``.xml`` / ``.xml.gz`` report."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    if not has_report_container_suffix(file.filename):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {', '.join(sorted(REPORT_CONTAINER_SUFFIXES))}",
        )

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    if len(data) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {settings.max_upload_size_mb} MB limit",
        )

    REPORT_DROP_DIR.mkdir(exist_ok=True)
    target = REPORT_DROP_DIR / file.filename
    target.write_bytes(data)

    result = process_file(target)
    if result is None:
        return UploadResponse(status="error", filename=file.filename).model_dump()

    if result.is_duplicate:
        return UploadResponse(
            status="duplicate",
            filename=file.filename,
            extracted_files=result.extracted_files,
            skipped_duplicates=result.skipped,
        ).model_dump()

    # Build per-file analysis
    per_file_analysis = []
    for report_dict in result.reports:
        analysis = build_analysis_from_dicts([report_dict], result.records_by_report)
        per_file_analysis.append({
            "report_id": report_dict["id"],
            "xml_filename": report_dict["xml_filename"],
            "org_name": report_dict["org_name"],
            "domain": report_dict["domain"],
            "date_begin": report_dict["date_begin"],
            "date_end": report_dict["date_end"],
            "analysis": analysis,
        })

    collective = build_analysis_from_dicts(result.reports, result.records_by_report)

    return UploadResponse(
        status="ingested",
        filename=file.filename,
        extracted_files=result.extracted_files,
        failed_files=result.failed,
        skipped_duplicates=result.skipped,
        per_file_analysis=per_file_analysis,
        collective_analysis=collective,
    ).model_dump()


# ── Reports endpoints ─────────────────────────────────────────────────────────


@app.get("/api/reports", response_model=list[ReportMetadata])
def list_reports(
    domain: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    filters = {}
    if domain:
        filters["domain"] = domain

    # Fetch reports
    rows = select(
        "dmarc_reports",
        filters=filters if filters else None,
        order="date_end.desc",
        limit=limit,
    )

    if not rows:
        return []

    # Fetch ALL records for these reports in ONE query
    report_ids = [row["id"] for row in rows]
    all_records = _fetch_records_for_reports(report_ids)

    # Build result
    result = []
    for row in rows:
        records = all_records.get(row["id"], [])
        pass_fail = _compute_pass_fail(records)

        result.append(
            ReportMetadata(
                id=row["id"],
                xml_filename=row["xml_filename"],
                org_name=row.get("org_name"),
                report_id=row.get("report_id"),
                date_begin=row.get("date_begin"),
                date_end=row.get("date_end"),
                domain=row.get("domain"),
                adkim=row.get("adkim"),
                aspf=row.get("aspf"),
                p=row.get("p"),
                sp=row.get("sp"),
                pct=row.get("pct"),
                record_count=len(records),
                pass_count=pass_fail["pass"],
                fail_count=pass_fail["fail"],
                created_at=row.get("created_at"),
            )
        )
    return result


def _fetch_records_for_reports(report_ids: list[int]) -> dict[int, list[dict]]:
    """Fetch records for multiple reports, grouped by report_id."""
    if not report_ids:
        return {}

    # Group by report_id
    grouped: dict[int, list[dict]] = {}

    # Fetch records in batches to avoid timeout
    # Use a reasonable limit per request
    all_records = select("dmarc_records", limit=1000)

    for rec in all_records:
        rid = rec.get("report_id")
        if rid in report_ids:
            if rid not in grouped:
                grouped[rid] = []
            grouped[rid].append(rec)

    return grouped


def _compute_pass_fail(records: list[dict]) -> dict:
    pass_count = 0
    fail_count = 0
    for rec in records:
        dkim = rec.get("dkim_aligned") or False
        spf = rec.get("spf_aligned") or False
        if dkim or spf:
            pass_count += 1
        else:
            fail_count += 1
    return {"pass": pass_count, "fail": fail_count}


@app.get("/api/reports/{report_id}", response_model=ReportMetadata)
def get_report(report_id: int):
    row = select_single("dmarc_reports", report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")

    records = select("dmarc_records", filters={"report_id": report_id})
    pass_fail = _compute_pass_fail(records)

    return ReportMetadata(
        id=row["id"],
        xml_filename=row["xml_filename"],
        org_name=row.get("org_name"),
        report_id=row.get("report_id"),
        date_begin=row.get("date_begin"),
        date_end=row.get("date_end"),
        domain=row.get("domain"),
        adkim=row.get("adkim"),
        aspf=row.get("aspf"),
        p=row.get("p"),
        sp=row.get("sp"),
        pct=row.get("pct"),
        record_count=len(records),
        pass_count=pass_fail["pass"],
        fail_count=pass_fail["fail"],
        created_at=row.get("created_at"),
    )


@app.get("/api/reports/{report_id}/detail")
def get_report_detail(report_id: int):
    row = select_single("dmarc_reports", report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Report not found")

    records = select("dmarc_records", filters={"report_id": report_id})

    for rec in records:
        if rec.get("dkim_auth_json"):
            if isinstance(rec["dkim_auth_json"], str):
                rec["dkim_auth"] = json.loads(rec["dkim_auth_json"])
            else:
                rec["dkim_auth"] = rec["dkim_auth_json"]
        else:
            rec["dkim_auth"] = []
        if rec.get("spf_auth_json"):
            if isinstance(rec["spf_auth_json"], str):
                rec["spf_auth"] = json.loads(rec["spf_auth_json"])
            else:
                rec["spf_auth"] = rec["spf_auth_json"]
        else:
            rec["spf_auth"] = []

    analysis = build_analysis_from_dicts([row], {row["id"]: records})

    return {
        "report": row,
        "records": records,
        "analysis": analysis,
    }


# ── Records endpoints ─────────────────────────────────────────────────────────


@app.get("/api/reports/{report_id}/records", response_model=list[RecordRow])
def list_records(
    report_id: int,
    source_ip: str | None = None,
    header_from: str | None = None,
    result: str | None = Query(None, description="Filter: pass or fail"),
    limit: int = Query(200, ge=1, le=2000),
):
    filters = {"report_id": report_id}
    if source_ip:
        filters["source_ip"] = source_ip
    if header_from:
        filters["header_from"] = header_from

    rows = select("dmarc_records", filters=filters, limit=limit)

    if result == "pass":
        rows = [r for r in rows if r.get("dkim_aligned") or r.get("spf_aligned")]
    elif result == "fail":
        rows = [
            r for r in rows if not r.get("dkim_aligned") and not r.get("spf_aligned")
        ]

    return [RecordRow(**r) for r in rows]


# ── Statistics ────────────────────────────────────────────────────────────────


@app.get("/api/stats", response_model=StatsSummary)
def stats(domain: str | None = None):
    filters = {}
    if domain:
        filters["domain"] = domain

    reports = select("dmarc_reports", filters=filters if filters else None)
    records = select("dmarc_records", limit=1000)

    records_by_report = defaultdict(list)
    for rec in records:
        records_by_report[rec["report_id"]].append(rec)

    analysis = build_analysis_from_dicts(reports, records_by_report)

    overall = analysis.get("overall", {})
    alignment = analysis.get("alignment", {})

    return StatsSummary(
        total_reports=overall.get("total_reports", 0),
        total_records=overall.get("total_records", 0),
        total_messages=overall.get("total_messages", 0),
        pass_count=alignment.get("either_pass", 0),
        fail_count=alignment.get("both_fail", 0),
        dkim_pass=alignment.get("dkim_pass", 0),
        dkim_fail=alignment.get("dkim_fail", 0),
        spf_pass=alignment.get("spf_pass", 0),
        spf_fail=alignment.get("spf_fail", 0),
        top_source_ips=analysis.get("top_ips", []),
        top_header_froms=analysis.get("top_header_froms", []),
        per_domain=analysis.get("per_domain", []),
    )


# ── Analysis endpoint ─────────────────────────────────────────────────────────


@app.get("/api/analysis")
def analysis():
    reports = select("dmarc_reports")
    records = select("dmarc_records")

    records_by_report = defaultdict(list)
    for rec in records:
        records_by_report[rec["report_id"]].append(rec)

    return build_analysis_from_dicts(reports, records_by_report)


# ── Pages ─────────────────────────────────────────────────────────────────────


@app.get("/analysis", response_class=HTMLResponse, include_in_schema=False)
async def analysis_page(request: Request):
    return templates.TemplateResponse("analysis.html", {"request": request})


@app.get("/file/{report_id}", response_class=HTMLResponse, include_in_schema=False)
async def file_detail_page(report_id: int, request: Request):
    return templates.TemplateResponse("file_detail.html", {"request": request})
