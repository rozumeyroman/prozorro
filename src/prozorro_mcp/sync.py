"""Feed synchronisation: walk the tenders feed, filter, fetch and store relevant tenders."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .client import NotFound, ProzorroClient
from .db import Database
from .filter import DRAFT_STATUSES, OPEN_STATUSES, Decision, TenderFilter
from .settings import KYIV_TZ

log = logging.getLogger(__name__)

RECHECK_OPEN_AFTER = timedelta(days=1)


def parse_since(value: str, now: datetime | None = None) -> datetime:
    """'today', 'yesterday', '24h', '3d', '2026-10-06' or a full ISO timestamp -> aware datetime (Kyiv time)."""
    now = now or datetime.now(KYIV_TZ)
    v = value.strip().lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if v in ("today", "сьогодні"):
        return midnight
    if v in ("yesterday", "вчора"):
        return midnight - timedelta(days=1)
    m = re.fullmatch(r"(\d+)\s*([hd])", v)
    if m:
        n = int(m.group(1))
        return now - (timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n))
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=KYIV_TZ)


def parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Syncer:
    def __init__(
        self,
        client: ProzorroClient,
        db: Database,
        tender_filter: TenderFilter,
        concurrency: int = 4,
        progress: Callable[[str], Any] | None = None,
    ):
        self.client = client
        self.db = db
        self.filter = tender_filter
        self.sem = asyncio.Semaphore(concurrency)
        self.progress = progress or (lambda msg: None)

    async def sync(self, since: datetime, only_new: bool = True, max_pages: int | None = None) -> dict[str, Any]:
        """Process every feed entry modified since `since`.

        only_new=True evaluates only tenders created since `since`; already stored relevant tenders are
        refreshed regardless of their creation date.
        """
        started = time.monotonic()
        params = {"since": since.isoformat(), "only_new": only_new, "filter": self.filter.name}
        run_id = self.db.start_run(params)
        stats: Counter[str] = Counter()
        requests_before = self.client.requests_made
        to_fetch: list[tuple[dict[str, Any], str]] = []  # (feed item, why)

        async for page in self.client.iter_feed("tenders", opt_fields=self.filter.feed_fields(), max_pages=max_pages):
            stats["feed_pages"] += 1
            stop = False
            for item in page:
                modified = parse_dt(item.get("dateModified"))
                if modified and modified < since:
                    stop = True
                    break
                stats["feed_items"] += 1
                action = self._triage(item, since, only_new, stats)
                if action:
                    to_fetch.append((item, action))
            self.progress(f"фід: {stats['feed_items']} змін, до завантаження {len(to_fetch)}")
            self.db.commit()
            if stop:
                break

        results = await asyncio.gather(*(self._fetch_and_decide(item, why, stats) for item, why in to_fetch))
        self.db.commit()
        stats["relevant_found"] = sum(1 for r in results if r)
        stats["api_requests"] = self.client.requests_made - requests_before
        out = {**params, "seconds": round(time.monotonic() - started, 1), **dict(sorted(stats.items()))}
        self.db.finish_run(run_id, out)
        return out

    def _triage(self, item: dict[str, Any], since: datetime, only_new: bool, stats: Counter[str]) -> str | None:
        """Decide what to do with a feed entry without network calls. Returns a fetch reason or None."""
        if item.get("status") in DRAFT_STATUSES:
            stats["skip_draft"] += 1
            return None
        known = self.db.get_decision(item["id"], self.filter.key)
        if known is not None:
            if known["relevant"]:
                if known["date_modified"] != item.get("dateModified"):
                    return "refresh"
                stats["skip_unchanged"] += 1
                return None
            if known["stage"] == "prefilter":
                # Prefilter is free: just run it again on fresh feed data.
                return self._prefilter_or_fetch(item, stats, "recheck")
            checked = parse_dt(known["checked_at"])
            if (
                item.get("status") in OPEN_STATUSES
                and known["date_modified"] != item.get("dateModified")
                and checked
                and datetime.now(checked.tzinfo) - checked > RECHECK_OPEN_AFTER
            ):
                return "recheck"
            stats["skip_known_irrelevant"] += 1
            return None
        created = parse_dt(item.get("dateCreated"))
        if only_new and created and created < since:
            stats["skip_old"] += 1
            return None
        stats["new_candidates"] += 1
        return self._prefilter_or_fetch(item, stats, "new")

    def _prefilter_or_fetch(self, item: dict[str, Any], stats: Counter[str], why: str) -> str | None:
        rejected = self.filter.prefilter(item)
        if rejected:
            stats["rejected_prefilter"] += 1
            self.db.save_decision(item, rejected, self.filter.key)
            return None
        return why

    async def _fetch_and_decide(self, item: dict[str, Any], why: str, stats: Counter[str]) -> bool:
        async with self.sem:
            try:
                tender = await self.client.get_tender(item["id"])
            except NotFound:
                stats["not_found"] += 1
                return False
        stats[f"fetched_{why}"] += 1
        decision = self.filter.evaluate(tender)
        if why == "refresh" and not decision.relevant:
            # Once relevant, keep tracking the tender (e.g. it got cancelled or a lot was dropped).
            decision = Decision(
                True,
                f"раніше релевантний; зараз: {decision.reason}",
                "ok",
                decision.matches,
                decision.relevant_value,
                decision.currency,
            )
        feed_view = {**item, "dateModified": tender.get("dateModified"), "status": tender.get("status")}
        self.db.save_decision(feed_view, decision, self.filter.key)
        if decision.relevant:
            self.db.save_tender(tender, decision)
            self.db.save_match(tender["id"], self.filter.name, self.filter.key, decision)
            return why != "refresh"
        stats[f"rejected_{decision.stage}"] += 1
        return False

    async def evaluate_one(self, tender_ref: str) -> tuple[dict[str, Any], Decision]:
        tender = await self.client.get_tender(tender_ref)
        return tender, self.filter.evaluate(tender)
