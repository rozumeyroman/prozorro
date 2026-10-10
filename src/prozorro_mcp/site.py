"""The public site prozorro.gov.ua: its (unofficial) search API and finding a tender's internal id by UA-… id.

POST {site}/api/search/tenders (application/x-www-form-urlencoded):
    cpv[]=48760000-3&cpv[]=…&date[tender][start]=2025-01-01&date[tender][end]=2025-05-31&page=1
    text=UA-2025-…   finds a tender by its number
- date[tender] filters by the bid submission period (tenderPeriod), not by dateCreated;
- pages start at 1, 20 results each;
- results have tenderID, title, value, status, procuringEntity, tenderPeriod, enquiryPeriod (no internal id);
- value filters do not work: filter by value here.

The CDB API has no lookup by UA-… id. The internal id is taken from the local database (every tender the sync has
ever looked at is there), otherwise from the tender page prozorro.gov.ua/tender/UA-…: every 32-hex string on it
is a candidate, checked against GET /tenders/{id} (tenderID must match), so a wrong guess is never used.
"""

from __future__ import annotations

import asyncio
import logging
import re
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
        for attempt in range(self.settings.max_retries + 1):
            try:
                resp = await self._http.request(method, url, **kw)
            except httpx.TransportError as e:
                if attempt == self.settings.max_retries:
                    raise ProzorroError(f"{method} {url}: {e!r}") from e
            else:
                if resp.status_code < 400 or resp.status_code == 404:
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504) or attempt == self.settings.max_retries:
                    raise ProzorroError(f"{method} {url}: HTTP {resp.status_code}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        raise AssertionError("unreachable")

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

    async def page_candidates(self, tender_id: str) -> list[str]:
        """32-hex ids found on the tender page and in the search result for it, most frequent first."""
        found: dict[str, int] = {}
        try:
            resp = await self._request("GET", f"/tender/{tender_id}")
            if resp.status_code == 200:
                for m in HEX_ID.finditer(resp.text):
                    found[m.group(1)] = found.get(m.group(1), 0) + 1
        except ProzorroError as e:
            log.warning("tender page %s: %s", tender_id, e)
        try:
            for r in _results(await self.search_page([("text", tender_id)], 1)):
                if r.get("tenderID") == tender_id:
                    for m in HEX_ID.finditer(str(r)):
                        found[m.group(1)] = found.get(m.group(1), 0) + 5
        except ProzorroError as e:
            log.warning("site search %s: %s", tender_id, e)
        return sorted(found, key=lambda k: -found[k])


async def resolve_internal_id(
    tender_id: str, db: Database, client: ProzorroClient, site: SiteClient | None, max_candidates: int = 6
) -> str | None:
    """Internal id for a UA-… id: local database first, then candidates from the site checked against the API."""
    known = db.internal_id(tender_id)
    if known:
        return known
    if site is None:
        return None
    for candidate in (await site.page_candidates(tender_id))[:max_candidates]:
        try:
            t = await client.get_tender(candidate)
        except NotFound:
            continue
        if t.get("tenderID") == tender_id:
            return candidate
    return None


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
