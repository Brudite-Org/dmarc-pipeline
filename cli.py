"""Command-line entry point for the DMARC pipeline.

Usage:
    python -m cli watch        # watch the drop folder and ingest on arrival
    python -m cli ingest       # batch-ingest everything in the drop folder once
    python -m cli serve        # run the FastAPI app (same as ``python -m api.main``)
    python -m cli init-db      # print schema setup instructions

Schema is managed via ``supabase_setup.sql`` (run manually in the Supabase
SQL editor) — there is no programmatic init step against Supabase's REST API.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from workers.watcher import WatchFolder

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def cmd_watch(args: argparse.Namespace) -> None:
    setup_logging(args.log_level)
    watcher = WatchFolder(args.directory)
    try:
        await watcher.run_forever()
    except KeyboardInterrupt:
        await watcher.stop()


async def cmd_ingest(args: argparse.Namespace) -> None:
    setup_logging(args.log_level)
    from workers.processor import process_existing_files

    count = await process_existing_files(args.directory)
    print(f"Ingested {count} report(s) from {args.directory}")


async def cmd_init_db(args: argparse.Namespace) -> None:
    setup_logging(args.log_level)
    print(
        "Schema is managed via supabase_setup.sql — run it in the "
        "Supabase SQL editor (Dashboard → SQL Editor → paste & run). "
        "There is no programmatic init step against the REST API."
    )


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    setup_logging(args.log_level)
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="dmarc", description="DMARC report pipeline")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p_watch = sub.add_parser("watch", help="Watch the drop folder for new reports")
    p_watch.add_argument("--directory", default=str(REPORTS_DIR))
    p_watch.set_defaults(func=cmd_watch)

    p_ingest = sub.add_parser("ingest", help="Batch-ingest reports already in the folder")
    p_ingest.add_argument("--directory", default=str(REPORTS_DIR))
    p_ingest.set_defaults(func=cmd_ingest)

    sub.add_parser("init-db", help="Create database tables").set_defaults(func=cmd_init_db)

    p_serve = sub.add_parser("serve", help="Run the FastAPI server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    if asyncio.iscoroutinefunction(args.func):
        asyncio.run(args.func(args))
    else:
        args.func(args)


if __name__ == "__main__":
    main()
