"""Single-writer persistence service for parse payloads."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from cxxtract.cache import repository as repo
from cxxtract.config import Settings
from cxxtract.models import ParsePayload

logger = logging.getLogger(__name__)


class SingleWriterService:
    """Serialize DB writes to avoid SQLite write-write contention."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._queue: asyncio.Queue[ParsePayload] = asyncio.Queue(maxsize=settings.writer_queue_size)
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._oldest_enqueue_ts: float = 0.0

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def lag_ms(self) -> float:
        if self._oldest_enqueue_ts <= 0:
            return 0.0
        return round((time.monotonic() - self._oldest_enqueue_ts) * 1000.0, 1)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="cxxtract-single-writer")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        await self.flush()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def enqueue(self, payload: ParsePayload) -> None:
        if not self._running:
            raise RuntimeError("SingleWriterService is not running")
        if self._queue.empty():
            self._oldest_enqueue_ts = time.monotonic()
        await self._queue.put(payload)

    async def flush(self) -> None:
        await self._queue.join()
        self._oldest_enqueue_ts = 0.0

    async def _persist_one(self, payload: ParsePayload) -> None:
        attempts = 0
        max_attempts = max(1, self._settings.writer_retry_attempts)
        last_exc: Optional[Exception] = None
        while attempts < max_attempts:
            try:
                await repo.upsert_parse_payload(payload)
                await repo.update_context_overlay_stats(
                    payload.context_id,
                    file_delta=1,
                    row_delta=(
                        len(payload.output.symbols)
                        + len(payload.output.references)
                        + len(payload.output.call_edges)
                        + len(payload.resolved_include_deps)
                    ),
                    max_overlay_files=self._settings.max_overlay_files,
                    max_overlay_rows=self._settings.max_overlay_rows,
                )
                return
            except Exception as exc:
                last_exc = exc
                attempts += 1
                if attempts >= max_attempts:
                    break
                await asyncio.sleep(self._settings.writer_retry_delay_ms / 1000.0)
        assert last_exc is not None
        raise last_exc

    async def _run(self) -> None:
        batch_size = max(1, self._settings.writer_batch_size)
        while True:
            if not self._running and self._queue.empty():
                return
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            batch = [item]
            while len(batch) < batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            try:
                for payload in batch:
                    await self._persist_one(payload)
            except Exception:
                logger.exception("Single writer failed to persist parse payload batch")
            finally:
                for _ in batch:
                    self._queue.task_done()
                if self._queue.empty():
                    self._oldest_enqueue_ts = 0.0

