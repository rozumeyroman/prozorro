from conftest import NOW

from prozorro_mcp.client import ProzorroClient
from prozorro_mcp.db import Database
from prozorro_mcp.settings import Settings
from prozorro_mcp.summary import tender_summary
from prozorro_mcp.sync import Syncer, parse_since


async def run_sync(fake, tender_filter, db, **kw):
    settings = Settings(api_url="http://fake/api/2.5", max_retries=0)
    async with ProzorroClient(settings, transport=fake.transport()) as client:
        return await Syncer(client, db, tender_filter).sync(parse_since("today", NOW), **kw)


async def test_sync_today(fake, tender_filter):
    db = Database(":memory:")
    stats = await run_sync(fake, tender_filter, db)
    titles = {r["title"] for r in db.search(limit=100)[0]}
    assert titles == {"Закупівля комутаторів для ЦОД", "Серверне обладнання", "Антивірусний захист"}
    assert stats["relevant_found"] == 3
    assert stats["skip_draft"] == 1
    assert stats["skip_old"] == 1
    assert stats["rejected_prefilter"] == 3  # reporting, belowThreshold/general, lots sum
    assert stats["rejected_value"] == 2
    assert stats["rejected_topic"] == 1
    # 6 full fetches, the 3 prefiltered tenders are never requested
    assert stats["fetched_new"] == 6
    assert sum(1 for p in fake.requests if p.startswith("/api/2.5/tenders/")) == 6


async def test_second_sync_uses_decision_cache(fake, tender_filter):
    db = Database(":memory:")
    await run_sync(fake, tender_filter, db)
    fake.requests.clear()
    stats = await run_sync(fake, tender_filter, db)
    assert stats.get("fetched_new", 0) == 0
    assert stats["skip_unchanged"] == 3
    assert not [p for p in fake.requests if p.startswith("/api/2.5/tenders/")]


async def test_refresh_changed_relevant_tender(fake, tender_filter, tenders):
    db = Database(":memory:")
    await run_sync(fake, tender_filter, db)
    t = next(t for t in tenders if t["title"] == "Антивірусний захист")
    t["dateModified"] = NOW.replace(hour=14).isoformat()
    t["status"] = "active.qualification"
    stats = await run_sync(fake, tender_filter, db)
    assert stats["fetched_refresh"] == 1
    assert db.get_tender(t["tenderID"])["status"] == "active.qualification"


async def test_search_and_summary(fake, tender_filter, tenders):
    db = Database(":memory:")
    await run_sync(fake, tender_filter, db)
    rows, total = db.search(query="комутатори")
    assert total == 1 and rows[0]["topics"] == "network"
    rows, _ = db.search(topic="cybersecurity")
    assert rows[0]["title"] == "Антивірусний захист"
    rows, _ = db.search(sort="value_desc")
    assert rows[0]["relevant_value"] == 2_500_000

    switches = next(t for t in tenders if t["title"] == "Закупівля комутаторів для ЦОД")
    s = tender_summary(db.get_tender(switches["tenderID"]))
    assert s["awards"][0]["winner"] and s["awards"][0]["supplier"]["edrpou"] == "12345678"
    assert s["bids"][0]["unitPrices"][0]["unitPrice"].startswith("98 000.00 UAH")
    assert s["url"].startswith("https://prozorro.gov.ua/tender/UA-")
