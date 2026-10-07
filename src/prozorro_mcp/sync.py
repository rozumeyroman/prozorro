"""Feed synchronisation: walk the tenders feed, filter, fetch and store relevant tenders."""

from __future__ import annotations

import asyncio
import json
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
# "since=last" starts this long before the previous run of the same filter, to cover feed watermark delays.
LAST_RUN_OVERLAP = timedelta(minutes=10)


class SyncError(ValueError):
    pass


def resolve_since(value: str, db: Database, filter_name: str) -> datetime:
    """Like parse_since, plus 'last': the start of the previous finished sync of this filter (minus an overlap)."""
    if value.strip().lower() in ("last", "останн"):
        run = db.last_finished_run(filter_name)
        if not run:
            raise SyncError(f"Ще не було завершених синхронізацій з фільтром {filter_name}; вкажіть період явно")
        return datetime.fromisoformat(run["started_at"]).astimezone(KYIV_TZ) - LAST_RUN_OVERLAP
    return parse_since(value)


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

    async def sync(
        self,
        since: datetime | None = None,
        only_new: bool = True,
        max_pages: int | None = None,
        until: datetime | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        """Process every feed entry modified since `since`, page by page.

        only_new=True evaluates only tenders created since `since`; already stored relevant tenders are
        refreshed regardless of their creation date. `until` skips tenders created at or after it (the feed is
        ordered by last modification, so the walk itself still starts from the newest change).

        After every page the results are committed and a checkpoint is stored; resume=True continues an
        interrupted run of the same filter from that checkpoint.
        """
        started = time.monotonic()
        stats: Counter[str] = Counter()
        offset: str | None = None
        if resume:
            cp = self.load_checkpoint()
            if not cp:
                raise SyncError("Немає перерваної синхронізації, яку можна продовжити")
            if cp["filter_key"] != self.filter.key:
                raise SyncError(
                    f"Перервану синхронізацію запущено з фільтром {cp['filter_key'].split(':')[0]} іншої версії; "
                    "продовжити з поточним фільтром не можна, запустіть синхронізацію заново"
                )
            since = datetime.fromisoformat(cp["since"])
            until = datetime.fromisoformat(cp["until"]) if cp.get("until") else None
            only_new, offset = cp["only_new"], cp["offset"]
            stats.update(cp.get("stats", {}))
            run_id = cp["run_id"]
            if not offset:  # interrupted right after the last page: nothing left to read
                self.clear_checkpoint()
                out = {
                    "since": cp["since"],
                    "until": cp.get("until"),
                    "only_new": only_new,
                    "filter": self.filter.name,
                    "seconds": 0.0,
                    **dict(sorted(stats.items())),
                }
                self.db.finish_run(run_id, out)
                return out
            self.progress(f"продовження з позиції {offset} ({stats['feed_items']} змін уже оброблено)")
        if since is None:
            raise SyncError("Не вказано початок періоду")
        params = {
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
            "only_new": only_new,
            "filter": self.filter.name,
        }
        if not resume:
            run_id = self.db.start_run(params)
        requests_before = self.client.requests_made

        pages = self.client.iter_feed(
            "tenders", opt_fields=self.filter.feed_fields(), offset=offset, max_pages=max_pages
        )
        async for page, next_offset in pages:
            stats["feed_pages"] += 1
            stop = False
            to_fetch: list[tuple[dict[str, Any], str]] = []
            for item in page:
                modified = parse_dt(item.get("dateModified"))
                if modified and modified < since:
                    stop = True
                    break
                stats["feed_items"] += 1
                action = self._triage(item, since, until, only_new, stats)
                if action:
                    to_fetch.append((item, action))
            if to_fetch:
                await self._fetch_all(to_fetch, stats)
            self.db.commit()
            self.save_checkpoint(run_id, params, next_offset, stats)
            self.progress(
                f"сторінка {stats['feed_pages']}: змін {stats['feed_items']}, "
                f"завантажено повністю {self._fetched(stats)}, релевантних {stats['relevant_found']}"
            )
            if stop or not next_offset:
                break

        self.clear_checkpoint()
        stats["api_requests"] += self.client.requests_made - requests_before
        out = {**params, "seconds": round(time.monotonic() - started, 1), **dict(sorted(stats.items()))}
        self.db.finish_run(run_id, out)
        return out

    @staticmethod
    def _fetched(stats: Counter[str]) -> int:
        return sum(v for k, v in stats.items() if k.startswith("fetched_"))

    async def _fetch_all(self, to_fetch: list[tuple[dict[str, Any], str]], stats: Counter[str]) -> None:
        total, done = len(to_fetch), 0

        async def one(item: dict[str, Any], why: str) -> None:
            nonlocal done
            if await self._fetch_and_decide(item, why, stats):
                stats["relevant_found"] += 1
            done += 1
            if total >= 50 and done % 50 == 0:
                self.progress(f"  завантажено {done}/{total} тендерів сторінки")

        await asyncio.gather(*(one(item, why) for item, why in to_fetch))

    # checkpoint ---------------------------------------------------------------------------------

    CHECKPOINT_KEY = "sync_checkpoint"

    def save_checkpoint(self, run_id: int, params: dict[str, Any], offset: str | None, stats: Counter[str]) -> None:
        cp = {**params, "filter_key": self.filter.key, "run_id": run_id, "offset": offset, "stats": dict(stats)}
        self.db.set_meta(self.CHECKPOINT_KEY, json.dumps(cp, ensure_ascii=False))

    def load_checkpoint(self) -> dict[str, Any] | None:
        raw = self.db.get_meta(self.CHECKPOINT_KEY)
        return json.loads(raw) if raw else None

    def clear_checkpoint(self) -> None:
        self.db.conn.execute("DELETE FROM meta WHERE key = ?", (self.CHECKPOINT_KEY,))
        self.db.commit()

    def _triage(
        self, item: dict[str, Any], since: datetime, until: datetime | None, only_new: bool, stats: Counter[str]
    ) -> str | None:
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
        if until and created and created >= until:
            stats["skip_after_until"] += 1
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
        """Fetch a tender, decide and store it. Returns True for a newly found relevant tender."""
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
