"""The feed walk must read the whole period: out-of-order entries, empty pages, shards and resume."""

from datetime import timedelta

from conftest import NOW
from fake_prozorro import make_tender

from prozorro_mcp.client import ProzorroClient, ProzorroError, offset_time
from prozorro_mcp.db import Database
from prozorro_mcp.settings import Settings
from prozorro_mcp.sync import Syncer, make_shards, parse_since

RELEVANT = {"Закупівля комутаторів для ЦОД", "Серверне обладнання", "Антивірусний захист"}


def settings(**kw):
    return Settings(api_url="http://fake/api/2.5", max_retries=0, feed_retry_delay=0, **kw)


async def sync(fake, f, db, since="today", s=None, **kw):
    async with ProzorroClient(s or settings(), transport=fake.transport()) as client:
        return await Syncer(client, db, f).sync(parse_since(since, NOW) if since else None, **kw)


def titles(db):
    return {r["title"] for r in db.search(limit=None)[0]}


def test_offset_time():
    assert offset_time("1696888800.0.2.c9e589b703dacf9b5ed4833357465084").timestamp() == 1696888800.0
    assert offset_time("1696888800.25").timestamp() == 1696888800.25
    assert offset_time("12") is None and offset_time(None) is None


async def test_old_date_modified_in_the_middle_does_not_stop_the_walk(fake, tender_filter, tenders):
    """A re-indexed tender: public_modified today (feed position), dateModified two years ago. Earlier versions
    stopped the whole sync on it and reported success."""
    stale = make_tender("Переіндексований", [("32420000-3", "Комутатор")], 900_000, created=NOW - timedelta(days=800))
    stale["public_modified"] = (NOW.replace(hour=9, minute=33)).timestamp()
    fake.tenders[stale["id"]] = stale
    db = Database(":memory:")
    stats = await sync(fake, tender_filter, db)
    assert titles(db) == RELEVANT
    assert stats["complete"] is True and "warning" not in stats
    assert stats["old_date_modified"] == 1
    assert stats["reached_modified"] >= parse_since("today", NOW).isoformat()[:10]


async def test_empty_page_in_the_middle_is_retried(fake, tender_filter):
    fake.empty_feed_calls = {2, 3}  # the second page comes back empty twice, then normally
    db = Database(":memory:")
    stats = await sync(fake, tender_filter, db)
    assert titles(db) == RELEVANT
    assert stats["complete"] is True and stats["feed_empty_retries"] == 2


async def test_persistent_empty_pages_make_an_incomplete_run(fake, tender_filter):
    fake.empty_feed_calls = set(range(2, 50))  # the feed "ends" after the first page
    db = Database(":memory:")
    stats = await sync(fake, tender_filter, db)
    assert stats["complete"] is False and "НЕПОВНА" in stats["warning"]
    assert stats["reached_modified"] > parse_since("today", NOW).isoformat()
    # the checkpoint stays and since=last ignores the incomplete run
    assert db.get_meta("sync_checkpoint") and db.last_finished_run(tender_filter.name) is None

    fake.empty_feed_calls = set()
    stats = await sync(fake, tender_filter, db, since=None, resume=True)
    assert stats["complete"] is True and titles(db) == RELEVANT
    assert db.get_meta("sync_checkpoint") is None


async def test_max_pages_is_reported_as_incomplete(fake, tender_filter):
    db = Database(":memory:")
    stats = await sync(fake, tender_filter, db, max_pages=1)
    assert stats["complete"] is False and "--max-pages" in stats["warning"]


def test_make_shards():
    since = NOW - timedelta(days=4)
    shards = make_shards(since, 4, now=NOW)
    assert shards[0].end is None and shards[-1].start == since.isoformat()
    assert [s.start for s in shards[:-1]] == [s.end for s in shards[1:]]
    assert offset_time(shards[1].offset) is not None


def spread_tenders(n_days=6):
    """Relevant tenders created on each of the last n days (so that shards have work)."""
    out = []
    for d in range(n_days):
        created = NOW - timedelta(days=d, hours=1)
        out.append(make_tender(f"Сервери {d}", [("48820000-2", "Сервер HPE ProLiant")], 1_500_000, created=created))
    # older than the period: the walk ends on it
    out.append(make_tender("Старий", [("32420000-3", "Комутатор")], 900_000, created=NOW - timedelta(days=30)))
    return out


async def test_shards_read_the_whole_period(tender_filter):
    from fake_prozorro import FakeProzorro

    fake = FakeProzorro(spread_tenders(), page_size=1)
    db = Database(":memory:")
    async with ProzorroClient(settings(), transport=fake.transport()) as client:
        stats = await Syncer(client, db, tender_filter).sync(NOW - timedelta(days=7), shards=3)
    assert stats["complete"] is True and stats["relevant_found"] == 6
    assert len(stats["shard_details"]) == 3 and all(s["complete"] for s in stats["shard_details"])


async def test_resume_continues_all_shards(tender_filter):
    from fake_prozorro import FakeProzorro

    fake = FakeProzorro(spread_tenders(), page_size=1)
    db = Database(":memory:")
    fake.fail_feed_after = 4
    try:
        async with ProzorroClient(settings(), transport=fake.transport()) as client:
            await Syncer(client, db, tender_filter).sync(NOW - timedelta(days=7), shards=3)
        raise AssertionError("expected a failure")
    except ProzorroError:
        pass
    import json

    cp = json.loads(db.get_meta("sync_checkpoint"))
    assert len(cp["shards"]) == 3 and any(not s["done"] for s in cp["shards"])
    fake.fail_feed_after = None
    async with ProzorroClient(settings(), transport=fake.transport()) as client:
        stats = await Syncer(client, db, tender_filter).sync(resume=True)
    assert stats["complete"] is True
    assert len(db.search(limit=None)[0]) == 6 and db.get_meta("sync_checkpoint") is None


async def test_exclusions_are_skipped_and_hidden(fake, tender_filter, tenders):
    db = Database(":memory:")
    t = next(t for t in tenders if t["title"] == "Антивірусний захист")
    db.add_exclusion(t["tenderID"], "не наш профіль")
    stats = await sync(fake, tender_filter, db)
    assert stats["skip_excluded"] == 1
    assert titles(db) == RELEVANT - {"Антивірусний захист"}
    assert db.exclusions()[0]["reason"] == "не наш профіль"
    # added after the sync: stays in the database, hidden from selections
    other = next(t for t in tenders if t["title"] == "Серверне обладнання")
    db.add_exclusion(other["tenderID"], "дубль")
    assert titles(db) == {"Закупівля комутаторів для ЦОД"}
    assert db.remove_exclusion(other["tenderID"]) and "Серверне обладнання" in titles(db)


async def test_targeted_sync(fake, tender_filter, tenders):
    db = Database(":memory:")
    ids = [t["id"] for t in tenders if t["title"] in ("Антивірусний захист", "Закупівля картриджів")]
    async with ProzorroClient(settings(), transport=fake.transport()) as client:
        out = await Syncer(client, db, tender_filter).sync_ids(ids)
    by_title = {r["tenderID"]: r for r in out["tenders"]}
    assert len(by_title) == 2 and sum(r["relevant"] for r in out["tenders"]) == 1
    assert titles(db) == {"Антивірусний захист"}
    assert db.last_finished_run(tender_filter.name) is None  # a targeted run is not a feed sync


async def test_shards_refuse_when_api_ignores_time_offsets(tender_filter):
    import pytest
    from fake_prozorro import FakeProzorro

    from prozorro_mcp.sync import SyncError

    fake = FakeProzorro(spread_tenders(), page_size=1)
    feed = fake.feed
    fake.feed = lambda params: feed({k: v for k, v in params.items() if not (k == "offset" and "." not in v)})
    db = Database(":memory:")
    with pytest.raises(SyncError, match="без --shards"):
        async with ProzorroClient(settings(), transport=fake.transport()) as client:
            await Syncer(client, db, tender_filter).sync(NOW - timedelta(days=7), shards=3)
