"""Feed synchronisation: walk the tenders feed, filter, fetch and store relevant tenders."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .client import NotFound, ProzorroClient, offset_time, time_offset
from .db import Database
from .filter import DRAFT_STATUSES, OPEN_STATUSES, Decision, TenderFilter
from .probe import tender_digest
from .settings import KYIV_TZ

log = logging.getLogger(__name__)

RECHECK_OPEN_AFTER = timedelta(days=1)
# "since=last" starts this long before the previous run of the same filter, to cover feed watermark delays.
LAST_RUN_OVERLAP = timedelta(minutes=10)
# A walk that stopped further than this from the start of its period did not read the whole period.
COMPLETENESS_TOLERANCE = timedelta(days=1)
# Feed fields the walk itself needs (on top of the filter's prefilter fields).
FEED_FIELDS = ["dateModified", "public_modified"]


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


def feed_position(item: dict[str, Any]) -> datetime | None:
    """Where a feed entry sits in the feed. The feed is ordered by public_modified, NOT by dateModified: entries
    with an old dateModified appear among recent ones (re-indexed or migrated tenders), so dateModified cannot be
    used to decide that the walk has gone past the start of the period."""
    pm = item.get("public_modified")
    if pm in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(pm), UTC)
    except (TypeError, ValueError):
        return parse_dt(str(pm))


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(KYIV_TZ).isoformat(timespec="seconds") if dt else None


@dataclass
class Shard:
    """A window [start, end) of the feed (by public_modified), walked from `end` back to `start`."""

    start: str  # ISO
    end: str | None  # ISO; None: from the newest change
    offset: str | None = None  # where to continue
    done: bool = False  # reached `start`
    reached: str | None = None  # oldest feed position read so far (ISO)
    end_reason: str | None = None  # "window" (reached start), "empty", "no_next", "max_pages"
    pages: int = 0

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)

    @property
    def end_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.end) if self.end else None

    def note_position(self, pos: datetime | None) -> None:
        if pos and (not self.reached or pos < datetime.fromisoformat(self.reached)):
            self.reached = _iso(pos)

    def complete(self) -> bool:
        if self.done:
            return True
        if not self.reached:
            return False
        # 1 day for long periods; a fraction of the window for short ones (a daily sync must not pass at noon).
        span = (self.end_dt or datetime.now(UTC)) - self.start_dt
        tolerance = min(COMPLETENESS_TOLERANCE, span / 20)
        return datetime.fromisoformat(self.reached) <= self.start_dt + tolerance


def make_shards(since: datetime, n: int, now: datetime | None = None) -> list[Shard]:
    """Split [since, now] into n equal windows; the first shard is the newest."""
    now = now or datetime.now(KYIV_TZ)
    n = max(1, n)
    step = (now - since) / n
    bounds = [since + step * i for i in range(n)] + [None]
    shards = []
    for i in reversed(range(n)):
        end = bounds[i + 1]
        shards.append(
            Shard(
                start=_iso(bounds[i]) or "",
                end=_iso(end),
                offset=time_offset(end) if end else None,
            )
        )
    return shards


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
        self.excluded: set[str] = set()

    async def sync(
        self,
        since: datetime | None = None,
        only_new: bool = True,
        max_pages: int | None = None,
        until: datetime | None = None,
        resume: bool = False,
        shards: int = 1,
    ) -> dict[str, Any]:
        """Process every feed entry changed since `since`, page by page.

        only_new=True evaluates only tenders created since `since`; already stored relevant tenders are
        refreshed regardless of their creation date. `until` skips tenders created at or after it (the feed is
        ordered by last modification, so the walk itself still starts from the newest change).

        shards>1 splits [since, now] into windows that are read in parallel (each from its end back to its start).

        After every page the results are committed and a checkpoint is stored; resume=True continues an
        interrupted run of the same filter (all its shards) from that checkpoint.

        The result says how far back the feed was actually read (reached_modified) and whether that covers the
        whole period (complete). An incomplete run keeps its checkpoint, so `resume` can finish it.
        """
        started = time.monotonic()
        stats: Counter[str] = Counter()
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
            only_new = cp["only_new"]
            if "shards" in cp:
                shard_list = [Shard(**sh) for sh in cp["shards"]]
            else:  # checkpoint of an older version: one walk, offset None meant "nothing left"
                shard_list = [Shard(start=cp["since"], end=None, offset=cp["offset"], done=not cp["offset"])]
                if shard_list[0].done:
                    shard_list[0].end_reason = "window"
            stats.update(cp.get("stats", {}))
            run_id = cp["run_id"]
            left = [sh for sh in shard_list if not sh.done]
            self.progress(
                f"продовження: шардів {len(left)} з {len(shard_list)} ({stats['feed_items']} змін уже оброблено)"
            )
        else:
            if since is None:
                raise SyncError("Не вказано початок періоду")
            shard_list = make_shards(since, shards)
        params = {
            "since": since.isoformat(),
            "until": until.isoformat() if until else None,
            "only_new": only_new,
            "filter": self.filter.name,
        }
        if len(shard_list) > 1:
            params["shards"] = len(shard_list)
        if not resume:
            run_id = self.db.start_run(params)
        requests_before = self.client.requests_made
        self.excluded = self.db.excluded_ids()

        ctx = _WalkContext(run_id, params, shard_list, since, until, only_new, stats)
        self.save_checkpoint(ctx)
        try:
            # A failing shard cancels the others; the checkpoint keeps every shard's position for --resume.
            async with asyncio.TaskGroup() as tg:
                for i, sh in enumerate(shard_list):
                    if not sh.done:
                        tg.create_task(self._walk(ctx, sh, i, max_pages))
        except BaseExceptionGroup as eg:
            self.db.commit()
            raise eg.exceptions[0] from None

        stats["api_requests"] += self.client.requests_made - requests_before
        complete = all(sh.complete() for sh in shard_list)
        oldest = shard_list[-1]  # the window that starts at `since`
        out: dict[str, Any] = {
            **params,
            "seconds": round(time.monotonic() - started, 1),
            "complete": complete,
            "reached_modified": oldest.reached or (oldest.start if oldest.done else None),
            **dict(sorted(stats.items())),
        }
        if len(shard_list) > 1:
            out["shard_details"] = [
                {
                    "from": sh.start,
                    "to": sh.end,
                    "reached": sh.reached,
                    "complete": sh.complete(),
                    "pages": sh.pages,
                    "end": sh.end_reason,
                }
                for sh in shard_list
            ]
        if complete:
            self.clear_checkpoint()
        else:
            gaps = [sh for sh in shard_list if not sh.complete()]
            reasons = {
                "empty": "API повертало порожні сторінки",
                "no_next": "API не дало наступної сторінки",
                "max_pages": "досягнуто --max-pages",
                "stopped": "обхід перервано",
                None: "обхід не завершено",
            }
            out["warning"] = (
                "Синхронізація НЕПОВНА: стрічку прочитано лише до "
                + "; ".join(
                    f"{sh.reached or sh.end or 'початку'} "
                    f"(вікно від {sh.start}: {reasons.get(sh.end_reason, sh.end_reason)})"
                    for sh in gaps
                )
                + f", а потрібно до {_iso(since)}. Тендери, змінені раніше, не перевірено. "
                "Продовжте: `prozorro-mcp sync --resume` (або sync_tenders з resume=true)."
            )
            self.progress(out["warning"])
        self.db.finish_run(run_id, out)
        return out

    async def _walk(self, ctx: _WalkContext, shard: Shard, index: int, max_pages: int | None) -> None:
        stats = ctx.stats
        prefix = f"[шард {index + 1}/{len(ctx.shards)}] " if len(ctx.shards) > 1 else ""
        state: dict[str, Any] = {}
        fields = list(dict.fromkeys([*self.filter.feed_fields(), *FEED_FIELDS]))
        pages = self.client.iter_feed(
            "tenders", opt_fields=fields, offset=shard.offset, max_pages=max_pages, state=state
        )
        try:
            await self._read_pages(ctx, shard, pages, prefix)
        finally:
            if state.get("empty_retries"):
                stats["feed_empty_retries"] += state["empty_retries"]
        if not shard.done:
            shard.end_reason = state.get("end")
            if shard.end_reason in ("no_next",) and shard.complete():
                shard.done = True
            self.save_checkpoint(ctx)

    async def _read_pages(self, ctx: _WalkContext, shard: Shard, pages: Any, prefix: str) -> None:
        stats = ctx.stats
        start, end = shard.start_dt, shard.end_dt
        stopped = False
        async for page, next_offset in pages:
            stats["feed_pages"] += 1
            shard.pages += 1
            to_fetch: list[tuple[dict[str, Any], str]] = []
            positioned = 0
            for item in page:
                pos = feed_position(item)
                modified = parse_dt(item.get("dateModified"))
                if pos is not None:
                    positioned += 1
                    if pos < start:
                        stopped = True
                        break
                    if end and pos >= end:  # belongs to the newer window (offsets may overlap a little)
                        continue
                    shard.note_position(pos)
                    if modified and modified < ctx.since:
                        stats["old_date_modified"] += 1  # out of dateModified order: earlier versions stopped here
                elif modified and modified < start:
                    # Without public_modified the position is unknown: skip this entry but keep walking.
                    stats["out_of_order"] += 1
                    continue
                else:
                    shard.note_position(modified)
                stats["feed_items"] += 1
                action = self._triage(item, ctx.since, ctx.until, ctx.only_new, stats)
                if action:
                    to_fetch.append((item, action))
            if not stopped and not positioned:
                # No public_modified in the feed: use the position encoded in the next offset, or, failing that,
                # a whole page older than the window start.
                page_pos = offset_time(next_offset)
                if page_pos is not None:
                    if page_pos < start:
                        stopped = True
                    else:
                        shard.note_position(page_pos)
                elif all((parse_dt(i.get("dateModified")) or start) < start for i in page):
                    stopped = True
            if to_fetch:
                await self._fetch_all(to_fetch, stats, prefix)
            self.db.commit()
            shard.offset = next_offset
            if stopped:
                shard.done, shard.end_reason = True, "window"
            self.save_checkpoint(ctx)
            where = f" (до {shard.reached[:10]})" if shard.reached else ""
            self.progress(
                f"{prefix}сторінка {shard.pages}{where}: змін {stats['feed_items']}, "
                f"завантажено повністю {self._fetched(stats)}, релевантних {stats['relevant_found']}"
            )
            if stopped:
                break

    @staticmethod
    def _fetched(stats: Counter[str]) -> int:
        return sum(v for k, v in stats.items() if k.startswith("fetched_"))

    async def _fetch_all(
        self, to_fetch: list[tuple[dict[str, Any], str]], stats: Counter[str], prefix: str = ""
    ) -> None:
        total, done = len(to_fetch), 0

        async def one(item: dict[str, Any], why: str) -> None:
            nonlocal done
            if await self._fetch_and_decide(item, why, stats):
                stats["relevant_found"] += 1
            done += 1
            if total >= 50 and done % 50 == 0:
                self.progress(f"{prefix}  завантажено {done}/{total} тендерів сторінки")

        await asyncio.gather(*(one(item, why) for item, why in to_fetch))

    # targeted sync -------------------------------------------------------------------------------

    async def sync_ids(self, internal_ids: list[str]) -> dict[str, Any]:
        """Fetch and decide the given tenders (internal ids) regardless of the feed: e.g. ones the feed walk missed."""
        started = time.monotonic()
        stats: Counter[str] = Counter()
        results: list[dict[str, Any]] = []
        params = {"filter": self.filter.name, "targeted": len(internal_ids)}
        run_id = self.db.start_run(params)
        self.excluded = self.db.excluded_ids()

        async def one(hex_id: str) -> None:
            item = {"id": hex_id}
            relevant = await self._fetch_and_decide(item, "targeted", stats)
            d = self.db.get_decision(hex_id, self.filter.key)
            results.append(
                {
                    "id": hex_id,
                    "tenderID": d["tender_id"] if d else None,
                    "relevant": bool(d["relevant"]) if d else False,
                    "reason": d["reason"] if d else "не знайдено в Prozorro",
                    "new": relevant,
                }
            )

        await asyncio.gather(*(one(i) for i in internal_ids))
        self.db.commit()
        out = {
            **params,
            "seconds": round(time.monotonic() - started, 1),
            **dict(sorted(stats.items())),
            "tenders": results,
        }
        self.db.finish_run(run_id, {k: v for k, v in out.items() if k != "tenders"})
        return out

    # checkpoint ---------------------------------------------------------------------------------

    CHECKPOINT_KEY = "sync_checkpoint"

    def save_checkpoint(self, ctx: _WalkContext) -> None:
        cp = {
            **ctx.params,
            "filter_key": self.filter.key,
            "run_id": ctx.run_id,
            "shards": [asdict(sh) for sh in ctx.shards],
            "stats": dict(ctx.stats),
        }
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
        if item.get("tenderID") and item["tenderID"] in self.excluded:
            stats["skip_excluded"] += 1
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
        feed_view = {
            **item,
            "tenderID": item.get("tenderID") or tender.get("tenderID"),
            "dateModified": tender.get("dateModified"),
            "status": tender.get("status"),
        }
        self.db.save_decision(feed_view, decision, self.filter.key, tender_digest(tender))
        if decision.relevant:
            self.db.save_tender(tender, decision)
            self.db.save_match(tender["id"], self.filter.name, self.filter.key, decision)
            return why != "refresh"
        stats[f"rejected_{decision.stage}"] += 1
        return False

    async def evaluate_one(self, tender_ref: str) -> tuple[dict[str, Any], Decision]:
        tender = await self.client.get_tender(tender_ref)
        return tender, self.filter.evaluate(tender)


@dataclass
class _WalkContext:
    run_id: int
    params: dict[str, Any]
    shards: list[Shard]
    since: datetime
    until: datetime | None
    only_new: bool
    stats: Counter[str]
