"""Async client for the public Prozorro CDB API (read-only).

See api-specs/cdb-public-api.openapi.yaml for the endpoints used here.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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


def offset_time(offset: str | None) -> datetime | None:
    """Feed position encoded in a feed offset: `{timestamp}.{skip_len}.{skip_hash}` or a plain timestamp.

    The timestamp is the `public_modified` of the last item returned (the feed is ordered by it).
    """
    if not offset:
        return None
    parts = offset.split(".")
    candidates = [".".join(parts[:2]), parts[0]] if len(parts) >= 2 else [parts[0]]
    for c in candidates:
        try:
            ts = float(c)
        except ValueError:
            continue
        if ts > 1e9:  # a real Unix time, not a page number
            return datetime.fromtimestamp(ts, UTC)
    return None


def time_offset(moment: datetime) -> str:
    """Feed offset that starts a descending walk just before `moment` (by public_modified)."""
    return f"{moment.timestamp():.6f}"


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
                    try:
                        return resp.json()
                    except ValueError as e:  # a cut-off body: retry like a transport error
                        if attempt == self.settings.max_retries:
                            raise ProzorroError(f"GET {url}: некоректна відповідь ({e})") from e
                        log.warning("GET %s: broken JSON (%s), retry in %.1fs", url, e, delay)
                        await asyncio.sleep(delay + random.uniform(0, delay / 2))
                        delay = min(delay * 2, 60)
                        continue
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
        state: dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[list[dict[str, Any]], str | None]]:
        """Yield (page items, offset of the next page). Descending order walks from the newest modification back.

        The next-page offset can be stored and passed back as `offset` to resume the walk later.

        An empty page is not taken as the end of the feed right away: the API sometimes returns one in the middle
        of the feed. The same offset is requested again (settings.feed_empty_retries times, with a pause); if the
        API keeps returning an empty page but points to a different next offset, the walk follows it. Why the walk
        ended is written to `state["end"]`: "empty" (empty page after retries), "no_next" (no next offset),
        "max_pages" or "stopped" (the caller stopped reading).
        """
        state = state if state is not None else {}
        state["end"] = "stopped"
        params: dict[str, Any] = {"limit": limit}
        if descending:
            params["descending"] = 1
        if opt_fields:
            params["opt_fields"] = ",".join(opt_fields)
        if offset:
            params["offset"] = offset
        pages = empty_tries = empty_skips = 0
        while True:
            page = await self._get(f"/{resource}", params=params)
            data = page.get("data") or []
            next_offset = (page.get("next_page") or {}).get("offset") or None
            if not data:
                current = params.get("offset")
                if empty_tries < self.settings.feed_empty_retries:
                    empty_tries += 1
                    state["empty_retries"] = state.get("empty_retries", 0) + 1
                    log.warning("feed /%s: empty page at offset %s, retry %d", resource, current, empty_tries)
                    await asyncio.sleep(self.settings.feed_retry_delay * empty_tries)
                    continue
                if next_offset and next_offset != current and empty_skips < 5:
                    empty_skips += 1
                    empty_tries = 0
                    params["offset"] = next_offset
                    continue
                state["end"] = "empty"
                return
            empty_tries = empty_skips = 0
            yield data, next_offset
            pages += 1
            if not next_offset:
                state["end"] = "no_next"
                return
            if max_pages and pages >= max_pages:
                state["end"] = "max_pages"
                return
            params["offset"] = next_offset
