from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import HttpConfig

log = logging.getLogger(__name__)

_MAX_BYTES = 5 * 1024 * 1024  # 5 MB


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    return False


class _RateLimiter:
    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps if rps > 0 else 0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class HttpClient:
    def __init__(self, cfg: HttpConfig) -> None:
        self._cfg = cfg
        self._client: Optional[httpx.AsyncClient] = None
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._rate = _RateLimiter(cfg.rate_limit_rps)

    async def __aenter__(self) -> "HttpClient":
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self._cfg.user_agent},
            timeout=self._cfg.timeout_s,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()

    async def get_text(self, url: str, timeout: Optional[float] = None) -> str:
        return await self._get(url, as_bytes=False, timeout=timeout)  # type: ignore

    async def get_bytes(self, url: str, timeout: Optional[float] = None) -> bytes:
        return await self._get(url, as_bytes=True, timeout=timeout)  # type: ignore

    async def head_ok(self, url: str) -> bool:
        try:
            async with self._sem:
                await self._rate.acquire()
                r = await self._client.head(url, timeout=10.0)
                return r.status_code < 400
        except Exception:
            return False

    async def _get(self, url: str, *, as_bytes: bool, timeout: Optional[float]) -> bytes | str:
        cfg = self._cfg

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(cfg.max_retries),
            wait=wait_exponential(multiplier=cfg.backoff_base_s, min=cfg.backoff_base_s, max=30),
            reraise=True,
        )
        async def _do() -> bytes | str:
            async with self._sem:
                await self._rate.acquire()
                t = timeout or cfg.timeout_s
                r = await self._client.get(url, timeout=t)
                r.raise_for_status()
                if as_bytes:
                    if len(r.content) > _MAX_BYTES:
                        log.warning("Response too large (%d bytes) for %s", len(r.content), url)
                        return r.content[:_MAX_BYTES]
                    return r.content
                return r.text

        return await _do()
