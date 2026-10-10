"""prozorro.gov.ua integration on REAL responses captured on 10.10.2026 (tests/fixtures/site, do not edit).

The fake server alone once let a broken id lookup pass: the real tender page has no id in it. These tests parse the
real payloads: the site summary (where the id actually is), its 404, the empty Vue page and the search results.
"""

import json

import httpx
import pytest
from fake_prozorro import SITE_FIXTURES, FakeProzorro

from prozorro_mcp.client import ProzorroClient
from prozorro_mcp.coverage import check_coverage
from prozorro_mcp.db import Database
from prozorro_mcp.settings import Settings
from prozorro_mcp.site import HEX_ID, SiteClient, _results, resolve_internal_id, summary_id

FORTISIEM = ("UA-2025-01-03-000122-a", "8d122ba0e00e4b8b9a081913bc71f37c")
OBLENERGO = ("UA-2025-01-08-005675-a", "d900575e21944283a22bd868b858c496")


def fixture(name: str) -> bytes:
    return (SITE_FIXTURES / name).read_bytes()


def settings(**kw):
    kw = {"site_min_interval": 0, "site_retry_after": 0, **kw}
    return Settings(
        api_url="https://public-api.prozorro.gov.ua/api/2.5", site_url="https://prozorro.gov.ua", max_retries=1, **kw
    )


def real_transport(calls: list[str] | None = None) -> httpx.MockTransport:
    """Serves the captured files for the URLs they were captured from."""
    json_headers = {"content-type": "application/json", "x-ratelimit-limit": "60", "x-ratelimit-remaining": "56"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            calls.append(f"{request.method} {request.url.host}{path}")
        if request.url.host == "public-api.prozorro.gov.ua":
            hex_id = path.rsplit("/", 1)[-1]
            f = SITE_FIXTURES / f"public_api_tender_{hex_id}.json"
            if f.exists():
                return httpx.Response(200, content=f.read_bytes(), headers=json_headers)
            return httpx.Response(404, json={"status": "error"})
        if path.startswith("/api/tenders/") and path.endswith("/summary"):
            ref = path.split("/")[3]
            for name in (f"site_summary_{ref}.json", f"site_summary_by_internal_id_{ref}.json"):
                if (SITE_FIXTURES / name).exists():
                    return httpx.Response(200, content=fixture(name), headers=json_headers)
            return httpx.Response(
                404, content=fixture("site_summary_404_UA-2099-01-01-000001-a.json"), headers=json_headers
            )
        if path.startswith("/tender/"):
            return httpx.Response(200, content=fixture("site_tender_page_UA-2025-01-03-000122-a.html"))
        if path == "/api/search/tenders":
            name = (
                "site_search_text_UA-2025-01-03-000122-a.json"
                if b"text=" in request.content
                else ("site_search_cpv48760000-3_2025-01_p1.json")
            )
            return httpx.Response(200, content=fixture(name), headers=json_headers)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("tender_id, hex_id", [FORTISIEM, OBLENERGO])
async def test_summary_gives_the_internal_id(tender_id, hex_id):
    async with SiteClient(settings(), transport=real_transport()) as site:
        summary = await site.tender_summary(tender_id)
    assert summary_id(summary, tender_id) == hex_id
    assert summary["dateModified"] and summary["status"] == "complete"


async def test_summary_404_is_none():
    async with SiteClient(settings(), transport=real_transport()) as site:
        assert await site.tender_summary("UA-2099-01-01-000001-a") is None
    assert json.loads(fixture("site_summary_404_UA-2099-01-01-000001-a.json")) == {"message": ""}


def test_summary_of_another_tender_is_not_used():
    summary = json.loads(fixture("site_summary_UA-2025-01-03-000122-a.json"))
    assert summary_id(summary, OBLENERGO[0]) is None
    # reverse lookup by internal id has no id, only tenderID
    assert summary_id(json.loads(fixture(f"site_summary_by_internal_id_{FORTISIEM[1]}.json")), FORTISIEM[0]) is None


def test_tender_page_and_search_have_no_id():
    assert HEX_ID.findall(fixture("site_tender_page_UA-2025-01-03-000122-a.html").decode()) == []
    text = json.loads(fixture("site_search_text_UA-2025-01-03-000122-a.json"))
    rows = _results(text)
    assert [r["tenderID"] for r in rows] == [FORTISIEM[0]] and "id" not in rows[0]


def test_search_payload():
    payload = json.loads(fixture("site_search_cpv48760000-3_2025-01_p1.json"))
    rows = _results(payload)
    assert len(rows) == 5 and payload["total"] == 5
    for r in rows:
        assert {"tenderID", "value", "tenderPeriod", "status", "procuringEntity"} <= set(r)
        assert "id" not in r


async def test_search_parses_real_payload():
    async with SiteClient(settings(), transport=real_transport()) as site:
        rows = await site.search(cpvs=["48760000-3"], date_from="2025-01-01", date_to="2025-01-31")
    assert len(rows) == 5


@pytest.mark.parametrize("tender_id, hex_id", [FORTISIEM, OBLENERGO])
async def test_resolve_through_summary_and_public_api(tender_id, hex_id):
    calls: list[str] = []
    s = settings()
    async with (
        ProzorroClient(s, transport=real_transport(calls)) as client,
        SiteClient(s, transport=real_transport(calls)) as site,
    ):
        assert await resolve_internal_id(tender_id, Database(":memory:"), client, site) == hex_id
    # summary, then the check against the public API; no page HTML, no text search
    assert calls == [
        f"GET prozorro.gov.ua/api/tenders/{tender_id}/summary",
        f"GET public-api.prozorro.gov.ua/api/2.5/tenders/{hex_id}",
    ]


async def test_resolve_unknown_and_site_down():
    s = settings()
    async with (
        ProzorroClient(s, transport=real_transport()) as client,
        SiteClient(s, transport=real_transport()) as site,
    ):
        assert await resolve_internal_id("UA-2099-01-01-000001-a", Database(":memory:"), client, site) is None

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    async with SiteClient(s, transport=httpx.MockTransport(down)) as site:
        async with ProzorroClient(s, transport=real_transport()) as client:
            assert await resolve_internal_id(FORTISIEM[0], Database(":memory:"), client, site) is None


async def test_site_429_waits_and_retries():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) <= 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "Too Many Attempts."})
        return httpx.Response(200, content=fixture("site_summary_UA-2025-01-03-000122-a.json"))

    async with SiteClient(settings(), transport=httpx.MockTransport(handler)) as site:
        summary = await site.tender_summary(FORTISIEM[0])
    assert summary_id(summary, FORTISIEM[0]) == FORTISIEM[1] and len(attempts) == 3


async def test_low_rate_limit_pauses(monkeypatch):
    import prozorro_mcp.site as site_mod

    sleeps: list[float] = []
    real_sleep = site_mod.asyncio.sleep

    async def fake_sleep(sec):
        sleeps.append(sec)
        await real_sleep(0)

    monkeypatch.setattr(site_mod.asyncio, "sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=fixture("site_summary_UA-2025-01-03-000122-a.json"),
            headers={"x-ratelimit-limit": "60", "x-ratelimit-remaining": "1"},
        )

    async with SiteClient(settings(site_low_limit_pause=30), transport=httpx.MockTransport(handler)) as site:
        await site.tender_summary(FORTISIEM[0])
    assert 30 in sleeps


async def test_requests_to_the_site_are_spaced(monkeypatch):
    import prozorro_mcp.site as site_mod

    sleeps: list[float] = []
    real_sleep = site_mod.asyncio.sleep

    async def fake_sleep(sec):
        sleeps.append(sec)
        await real_sleep(0)

    monkeypatch.setattr(site_mod.asyncio, "sleep", fake_sleep)
    async with SiteClient(settings(site_min_interval=1.0), transport=real_transport()) as site:
        for _ in range(3):
            await site.tender_summary(FORTISIEM[0])
    assert len([s for s in sleeps if 0 < s <= 1.0]) == 2  # the 2nd and 3rd requests waited


def real_fake() -> FakeProzorro:
    """The fake API with the two real tenders and their real site summaries."""
    tenders = [json.loads(fixture(f"public_api_tender_{h}.json"))["data"] for _, h in (FORTISIEM, OBLENERGO)]
    fake = FakeProzorro(tenders)
    for tid, _ in (FORTISIEM, OBLENERGO):
        fake.summaries[tid] = json.loads(fixture(f"site_summary_{tid}.json"))
    return fake


async def test_coverage_fetch_missing_with_real_tenders(cyber_filter):
    """`coverage --from 2025-01-01 --to 2025-01-31 --fetch-missing` on an empty base: both tenders are found by
    their UA-… id through the site summary, fetched and pass the cybersecurity filter."""
    fake = real_fake()
    s = Settings(api_url="http://fake/api/2.5", site_url="http://fake", max_retries=0, site_min_interval=0)
    db = Database(":memory:")
    async with (
        ProzorroClient(s, transport=fake.transport()) as c,
        SiteClient(s, transport=fake.transport()) as site,
    ):
        kw = {"date_from": "2025-01-01", "date_to": "2025-01-31", "cpvs": ["48730000-4"], "client": c}
        report = await check_coverage(db, site, cyber_filter, fetch=True, **kw)
        assert report["fetched"]["targeted"] == 2 and report["missing"] == 0
        assert report["not_in_db"] == []
        assert any(p == f"/api/tenders/{FORTISIEM[0]}/summary" for p in fake.requests)
        assert not any(p.startswith("/tender/") for p in fake.requests)
        assert db.is_relevant(FORTISIEM[0], cyber_filter.name) and db.is_relevant(OBLENERGO[0], cyber_filter.name)

        again = await check_coverage(db, site, cyber_filter, **kw)
        assert again["missing"] == 0 and again["by_reason"] == {"є в базі": 2}


async def test_site_429_through_the_fake(cyber_filter):
    fake = real_fake()
    fake.site_429 = 2
    s = Settings(api_url="http://fake/api/2.5", site_url="http://fake", max_retries=0, site_min_interval=0)
    async with ProzorroClient(s, transport=fake.transport()) as c, SiteClient(s, transport=fake.transport()) as site:
        assert await resolve_internal_id(FORTISIEM[0], Database(":memory:"), c, site) == FORTISIEM[1]
