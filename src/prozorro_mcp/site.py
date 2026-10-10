"""The public site prozorro.gov.ua: its (unofficial) search API and finding a tender's internal id by UA-… id.

POST {site}/api/search/tenders (application/x-www-form-urlencoded):
    cpv[]=48760000-3&cpv[]=…&date[tender][start]=2025-01-01&date[tender][end]=2025-05-31&page=1
    text=UA-2025-…   finds a tender by its number
- date[tender] filters by the bid submission period (tenderPeriod), not by dateCreated;
- pages start at 1, 20 results each;
- results have tenderID, title, value, status, procuringEntity, tenderPeriod, enquiryPeriod (no internal id);
- value filters do not work: filter by value here.

The CDB API has no lookup by UA-… id (`/tenders?tenderID=` is ignored). The internal id is taken from the local
database (every tender the sync has ever looked at is there), otherwise from the request the site itself makes when
a tender page opens:
    GET {site}/api/tenders/{UA-ID}/summary -> 200 flat JSON {"id": "<32 hex>", "tenderID": …, "dateModified": …}
                                           -> 404 {"message": ""} for an unknown tender
The id is then checked against GET /tenders/{id} of the public API (tenderID must match). The tender page HTML
(a Vue shell without data) and the search results (no id) do not contain it.

The site allows 60 requests a minute (x-ratelimit-limit / x-ratelimit-remaining): requests to it are spaced by
settings.site_min_interval, a 429 waits for Retry-After, and a nearly spent limit makes a pause.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from .client import NotFound, ProzorroClient, ProzorroError
from .db import Database
from .settings import Settings

log = logging.getLogger(__name__)

PAGE_SIZE = 20
HEX_ID = re.compile(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])")
UA_ID = re.compile(r"UA-\d{4}-\d{2}-\d{2}-\d{6}-[a-z]", re.I)


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return max(0.0, float(resp.headers.get("retry-after", "")))
    except ValueError:
        return default


def normalize_tender_id(ref: str) -> str | None:
    m = UA_ID.search(ref or "")
    return m.group(0)[:-1].upper() + m.group(0)[-1].lower() if m else None


def _results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "tenders", "hits"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
            if isinstance(v, dict):  # {"data": {"items": [...]}}
                inner = _results(v)
                if inner:
                    return inner
    return []


def _total(payload: Any) -> int | None:
    if isinstance(payload, dict):
        for key in ("total", "count", "totalCount"):
            v = payload.get(key)
            if isinstance(v, int):
                return v
        for v in payload.values():
            if isinstance(v, dict):
                t = _total(v)
                if t is not None:
                    return t
    return None


def result_value(r: dict[str, Any]) -> float | None:
    v = r.get("value")
    if isinstance(v, dict):
        v = v.get("amount")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def result_status(r: dict[str, Any]) -> str:
    s = r.get("status")
    if isinstance(s, dict):
        s = s.get("id") or s.get("code") or s.get("title") or s.get("name")
    return str(s or "")


def is_cancelled(r: dict[str, Any]) -> bool:
    s = result_status(r).lower()
    return "cancel" in s or "скасов" in s or "відмін" in s


class SiteClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._lock = asyncio.Lock()  # one request to the site at a time, spaced by site_min_interval
        self._last = 0.0
        self.requests_made = 0
        self._http = httpx.AsyncClient(
            base_url=settings.site_url,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json, text/html"},
            transport=transport,
            follow_redirects=True,
        )

    async def __aenter__(self) -> SiteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        delay = 1.0
        attempt = waited_429 = 0
        while True:
            async with self._lock:
                pause = self.settings.site_min_interval - (time.monotonic() - self._last)
                if pause > 0:
                    await asyncio.sleep(pause)
                try:
                    self.requests_made += 1
                    resp = await self._http.request(method, url, **kw)
                    error = None
                except httpx.TransportError as e:
                    resp, error = None, e
                finally:
                    self._last = time.monotonic()
                if resp is not None:
                    await self._respect_limit(resp)
            if resp is not None and resp.status_code == 429:
                waited_429 += 1
                if waited_429 > max(self.settings.max_retries, 3):
                    raise ProzorroError(f"{method} {url}: HTTP 429 (ліміт сайту) після {waited_429 - 1} очікувань")
                wait = _retry_after(resp, self.settings.site_retry_after)
                log.warning("%s %s -> 429, wait %.0fs", method, url, wait)
                await asyncio.sleep(wait)
                continue
            if resp is not None:
                if resp.status_code < 400 or resp.status_code == 404:
                    return resp
                if resp.status_code not in (500, 502, 503, 504) or attempt >= self.settings.max_retries:
                    raise ProzorroError(f"{method} {url}: HTTP {resp.status_code}")
            elif attempt >= self.settings.max_retries:
                raise ProzorroError(f"{method} {url}: {error!r}") from error
            attempt += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _respect_limit(self, resp: httpx.Response) -> None:
        """Nearly spent per-minute limit: wait (inside the lock, so other requests wait too)."""
        try:
            remaining = int(resp.headers.get("x-ratelimit-remaining", ""))
        except ValueError:
            return
        if remaining <= 2:
            log.warning("prozorro.gov.ua: %d requests left this minute, pausing", remaining)
            await asyncio.sleep(self.settings.site_low_limit_pause)

    async def tender_summary(self, tender_id: str) -> dict[str, Any] | None:
        """GET /api/tenders/{UA-ID}/summary: the site's own short card (with the internal id); None if unknown."""
        resp = await self._request("GET", f"/api/tenders/{tender_id}/summary")
        if resp.status_code == 404:
            return None
        try:
            data = resp.json()
        except ValueError as e:
            raise ProzorroError(f"summary {tender_id}: не JSON: {resp.text[:200]}") from e
        return data if isinstance(data, dict) else None

    async def search_page(self, form: list[tuple[str, str]], page: int) -> Any:
        data: dict[str, list[str]] = {}
        for k, v in [*form, ("page", str(page))]:
            data.setdefault(k, []).append(v)
        resp = await self._request("POST", "/api/search/tenders", data=data)
        try:
            return resp.json()
        except ValueError as e:
            raise ProzorroError(f"пошук prozorro.gov.ua повернув не JSON: {resp.text[:200]}") from e

    async def search(
        self,
        *,
        cpvs: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        text: str | None = None,
        max_pages: int = 500,
        progress: Callable[[str], Any] | None = None,
    ) -> list[dict[str, Any]]:
        """All results of a site search (deduplicated by tenderID). Dates: YYYY-MM-DD, by tender period."""
        form: list[tuple[str, str]] = [("cpv[]", c) for c in cpvs or []]
        if date_from:
            form.append(("date[tender][start]", date_from))
        if date_to:
            form.append(("date[tender][end]", date_to))
        if text:
            form.append(("text", text))
        seen: dict[str, dict[str, Any]] = {}
        total = None
        for page in range(1, max_pages + 1):
            payload = await self.search_page(form, page)
            rows = _results(payload)
            total = total if total is not None else _total(payload)
            for r in rows:
                tid = r.get("tenderID")
                if tid and tid not in seen:
                    seen[tid] = r
            if progress and page % 10 == 0:
                progress(
                    f"пошук prozorro.gov.ua: сторінка {page}, знайдено {len(seen)}" + (f" з {total}" if total else "")
                )
            if len(rows) < PAGE_SIZE or (total is not None and len(seen) >= total):
                break
        return list(seen.values())


def summary_id(summary: dict[str, Any] | None, tender_id: str) -> str | None:
    """Internal id from a site summary, only if it is about this tender and looks like an id."""
    if not summary or summary.get("tenderID") != tender_id:
        return None
    hex_id = str(summary.get("id") or "")
    return hex_id if HEX_ID.fullmatch(hex_id) else None


async def resolve_internal_id(
    tender_id: str, db: Database, client: ProzorroClient, site: SiteClient | None
) -> str | None:
    """Internal id for a UA-… id: local database first, then the site summary, checked against the public API."""
    known = db.internal_id(tender_id)
    if known:
        return known
    if site is None:
        return None
    try:
        candidate = summary_id(await site.tender_summary(tender_id), tender_id)
    except ProzorroError as e:
        log.warning("prozorro.gov.ua summary %s: %s", tender_id, e)
        return None
    if not candidate:
        return None
    try:
        t = await client.get_tender(candidate)
    except NotFound:
        return None
    return candidate if t.get("tenderID") == tender_id else None


async def resolve_refs(
    refs: list[str], db: Database, client: ProzorroClient, site: SiteClient | None
) -> tuple[list[str], list[dict[str, str]]]:
    """Internal ids for a mixed list of internal ids, UA-… ids and links; plus the ones that were not found."""
    ids, missing = [], []
    for ref in refs:
        ref = ref.strip()
        if HEX_ID.fullmatch(ref):
            ids.append(ref)
            continue
        tid = normalize_tender_id(ref)
        hex_id = await resolve_internal_id(tid, db, client, site) if tid else None
        if hex_id:
            ids.append(hex_id)
        else:
            missing.append({"tender": ref, "error": "не вдалося знайти внутрішній id"})
    return list(dict.fromkeys(ids)), missing
