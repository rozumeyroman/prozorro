"""Async client for the public Prozorro CDB API (read-only).

See api-specs/cdb-public-api.openapi.yaml for the endpoints used here.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from .settings import Settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class ProzorroError(RuntimeError):
    pass


class NotFound(ProzorroError):
    pass


class ProzorroClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.api_url,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent, **settings.extra_headers},
            transport=transport,
            follow_redirects=False,
        )
        self.requests_made = 0

    async def __aenter__(self) -> ProzorroClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = 1.0
        for attempt in range(self.settings.max_retries + 1):
            try:
                self.requests_made += 1
                resp = await self._http.get(url, params=params)
            except httpx.TransportError as e:
                if attempt == self.settings.max_retries:
                    raise ProzorroError(f"GET {url}: {e!r}") from e
                log.warning("GET %s failed (%r), retry in %.1fs", url, e, delay)
            else:
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 404:
                    raise NotFound(f"GET {url}: 404 Not Found")
                if resp.status_code not in RETRY_STATUSES or attempt == self.settings.max_retries:
                    raise ProzorroError(f"GET {url}: HTTP {resp.status_code} {resp.text[:300]}")
                log.warning("GET %s -> %s, retry in %.1fs", url, resp.status_code, delay)
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60)
        raise AssertionError("unreachable")

    async def download(self, url: str, dest: Path) -> int:
        """Stream a document to `dest` (via a temporary file), following redirects. Returns the size in bytes."""
        tmp = dest.with_name(dest.name + ".part")
        delay = 1.0
        for attempt in range(self.settings.max_retries + 1):
            try:
                self.requests_made += 1
                async with self._http.stream("GET", url, follow_redirects=True) as resp:
                    if resp.status_code == 200:
                        size = 0
                        with open(tmp, "wb") as f:
                            async for chunk in resp.aiter_bytes():
                                f.write(chunk)
                                size += len(chunk)
                        tmp.replace(dest)
                        return size
                    if resp.status_code == 404:
                        raise NotFound(f"GET {url}: 404 Not Found")
                    if resp.status_code not in RETRY_STATUSES or attempt == self.settings.max_retries:
                        raise ProzorroError(f"GET {url}: HTTP {resp.status_code}")
                    log.warning("GET %s -> %s, retry in %.1fs", url, resp.status_code, delay)
            except httpx.TransportError as e:
                if attempt == self.settings.max_retries:
                    raise ProzorroError(f"GET {url}: {e!r}") from e
                log.warning("GET %s failed (%r), retry in %.1fs", url, e, delay)
            finally:
                tmp.unlink(missing_ok=True)
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, 60)
        raise AssertionError("unreachable")

    async def get_tender(self, tender_id: str) -> dict[str, Any]:
        return (await self._get(f"/tenders/{tender_id}"))["data"]

    async def get_contract(self, contract_id: str) -> dict[str, Any]:
        return (await self._get(f"/contracts/{contract_id}"))["data"]

    async def iter_feed(
        self,
        resource: str = "tenders",
        *,
        opt_fields: list[str] | None = None,
        descending: bool = True,
        offset: str | None = None,
        limit: int = 1000,
        max_pages: int | None = None,
    ) -> AsyncIterator[tuple[list[dict[str, Any]], str | None]]:
        """Yield (page items, offset of the next page). Descending order walks from the newest modification back.

        The next-page offset can be stored and passed back as `offset` to resume the walk later.
        """
        params: dict[str, Any] = {"limit": limit}
        if descending:
            params["descending"] = 1
        if opt_fields:
            params["opt_fields"] = ",".join(opt_fields)
        if offset:
            params["offset"] = offset
        pages = 0
        while True:
            page = await self._get(f"/{resource}", params=params)
            data = page.get("data") or []
            if not data:
                return
            next_offset = (page.get("next_page") or {}).get("offset") or None
            yield data, next_offset
            pages += 1
            if not next_offset or (max_pages and pages >= max_pages):
                return
            params["offset"] = next_offset
