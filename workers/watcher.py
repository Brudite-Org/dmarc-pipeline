"""Folder watcher — ingests DMARC reports as they land in the drop folder.

Uses :mod:`watchdog` to detect new files and dispatches them to
:func:`workers.processor.process_file`. Also backfills any files that were
already present when the watcher started.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from workers.processor import process_existing_files, process_file

logger = logging.getLogger("dmarc.watcher")

SUPPORTED_SUFFIXES = (".zip", ".xml", ".xml.gz")


class _Handler(FileSystemEventHandler):
    """Debounced handler that queues new DMARC files for ingestion."""

    def __init__(self, queue: asyncio.Queue[str], loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._queue = queue
        self._loop = loop
        self._pending: dict[str, float] = {}  # path → last_event_ts

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = str(event.src_path)
        if not any(path.lower().endswith(s) for s in SUPPORTED_SUFFIXES):
            return
        self._pending[path] = time.monotonic()
        # Debounce: wait briefly so large files finish writing before we process.
        self._loop.call_later(0.5, self._maybe_enqueue, path)

    def _maybe_enqueue(self, path: str) -> None:
        """Enqueue only if no newer event arrived within the debounce window."""
        last = self._pending.get(path, 0)
        if time.monotonic() - last < 0.5:
            return  # newer event arrived, skip this one
        self._pending.pop(path, None)
        logger.info("Watcher enqueue: %s", path)
        self._queue.put_nowait(path)


class WatchFolder:
    """Async-friendly wrapper around a watchdog observer."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._observer: Observer | None = None
        self._consumers: list[asyncio.Task] = []

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self, num_workers: int = 2) -> None:
        """Start the watcher + consumer pool and backfill existing files."""
        loop = asyncio.get_running_loop()
        handler = _Handler(self._queue, loop)
        self._observer = Observer()
        self._observer.schedule(handler, str(self.directory), recursive=False)
        self._observer.start()
        logger.info("Watching folder: %s", self.directory)

        # Backfill anything already in the folder.
        await process_existing_files(self.directory)

        # Consumer pool.
        self._consumers = [
            asyncio.create_task(self._consume(i)) for i in range(num_workers)
        ]

    async def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        for task in self._consumers:
            task.cancel()
        await asyncio.gather(*self._consumers, return_exceptions=True)
        self._consumers = []

    async def run_forever(self) -> None:
        """Convenience: start, then block until cancelled."""
        await self.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await self.stop()

    # ── Consumer ───────────────────────────────────────────────────────────

    async def _consume(self, worker_id: int) -> None:
        logger.info("Consumer %d started", worker_id)
        while True:
            path = await self._queue.get()
            try:
                process_file(Path(path))
            except Exception as exc:  # noqa: BLE001 - never let a task die
                logger.exception("Consumer %d failed on %s: %s", worker_id, path, exc)
            finally:
                self._queue.task_done()
