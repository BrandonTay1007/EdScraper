"""Ed Discussion API client. GET only: this class deliberately has no write methods.

Raw JSON responses are archived under `paths.raw_dir()` so `sync.reparse()` can rebuild the store
offline:  user.json, <cid>/list/<offset>.json, <cid>/threads/<tid>.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from edscrape import paths
from edscrape.auth import AuthError, load_token

BASE_URL = "https://edstem.org/api"
RETRY_STATUSES = frozenset({429}) | frozenset(range(500, 600))
MAX_BACKOFF_S = 30.0


class ApiError(Exception):
    """Non-retryable HTTP failure, or a retryable one that exhausted its retries."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class EdClient:
    """Async context manager. Backoff before retry n (0-based) is
    min(30, backoff_base * 2**n) + jitter*backoff_base (default base 0.5s: 0.5, 1, 2, 4, 8 s),
    or the Retry-After header when larger (capped at 30 s). `sleep` is injectable for tests.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        concurrency: int = 4,
        rate_per_sec: float = 8,
        base_url: str = BASE_URL,
        archive: bool = True,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token if token is not None else load_token()
        self._base_url = base_url
        self._archive = archive
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._next_slot = 0.0
        self._rate_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max(1, min(concurrency, 8)))
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> EdClient:
        if not self._token:
            raise AuthError("no token; run login")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"x-token": self._token},
            timeout=httpx.Timeout(30.0),
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- internals ---------------------------------------------------------------------------

    async def _throttle(self) -> None:
        if not self._interval:
            return
        async with self._rate_lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
            wait = slot - now
        if wait > 0:
            await self._sleep(wait)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        delay = min(MAX_BACKOFF_S, self._backoff_base * 2**attempt)
        delay += self._jitter() * self._backoff_base
        if retry_after:
            try:
                delay = max(delay, min(MAX_BACKOFF_S, float(retry_after)))
            except ValueError:
                pass
        return delay

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        if self._http is None:
            raise RuntimeError("EdClient must be used as an async context manager")
        for attempt in range(self._max_retries + 1):
            await self._throttle()
            retry_after: str | None = None
            try:
                async with self._sem:
                    resp = await self._http.get(path, params=params)
            except httpx.TransportError as e:
                if attempt == self._max_retries:
                    raise ApiError(None, f"GET {path}: {type(e).__name__}") from e
            else:
                if resp.status_code == 401:
                    raise AuthError(f"GET {path}: 401")
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in RETRY_STATUSES or attempt == self._max_retries:
                    raise ApiError(resp.status_code, f"GET {path}: HTTP {resp.status_code}")
                retry_after = resp.headers.get("retry-after")
            await self._sleep(self._backoff(attempt, retry_after))
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _json(resp: httpx.Response, path: str) -> dict[str, Any]:
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise ApiError(resp.status_code, f"GET {path}: invalid JSON") from e

    def _write_raw(self, target: Path, resp: httpx.Response) -> None:
        if self._archive:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(resp.content)
            os.replace(tmp, target)

    # -- public GET methods ------------------------------------------------------------------

    async def get_user(self) -> dict[str, Any]:
        resp = await self._get("/user")
        data = self._json(resp, "/user")
        self._write_raw(paths.raw_dir() / "user.json", resp)
        return data

    async def list_threads(
        self, course_id: int, limit: int = 100, offset: int = 0, sort: str = "new"
    ) -> dict[str, Any]:
        path = f"/courses/{course_id}/threads"
        resp = await self._get(path, {"limit": limit, "offset": offset, "sort": sort})
        data = self._json(resp, path)
        self._write_raw(paths.raw_dir() / str(course_id) / "list" / f"{offset}.json", resp)
        return data

    async def iter_pages(
        self, course_id: int, limit: int = 100, sort: str = "new"
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Yield `(offset, page)` until a short page (fewer than `limit` raw items)."""
        offset = 0
        while True:
            page = await self.list_threads(course_id, limit=limit, offset=offset, sort=sort)
            yield offset, page
            if len(page.get("threads", [])) < limit:
                return
            offset += limit

    async def iter_threads(
        self, course_id: int, limit: int = 100, sort: str = "new"
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield list items across pages, deduplicated by thread id (pages shift while paging)."""
        seen: set[int] = set()
        async for _offset, page in self.iter_pages(course_id, limit=limit, sort=sort):
            for item in page.get("threads", []):
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                yield item

    async def get_thread(self, thread_id: int, course_id: int | None = None) -> dict[str, Any]:
        """`GET /threads/{id}` (never sends `view`). Archived under the thread's course id,
        taken from the response (`thread.course_id`) unless `course_id` is given."""
        path = f"/threads/{thread_id}"
        resp = await self._get(path)
        data = self._json(resp, path)
        cid = course_id if course_id is not None else data.get("thread", {}).get("course_id")
        if cid is not None:
            self._write_raw(paths.raw_dir() / str(cid) / "threads" / f"{thread_id}.json", resp)
        return data
