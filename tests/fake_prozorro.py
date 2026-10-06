"""A fake Prozorro CDB API for tests and offline demos.

Tenders are built from a real tender taken from the openprocurement.api docs (tests/fixtures/tender_complete.json)
and modified to cover every branch of the relevance filter.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx

from prozorro_mcp.settings import KYIV_TZ

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tender_complete.json").read_text(encoding="utf-8"))
API_PREFIX = "/api/2.5"


def _value(amount: float) -> dict[str, Any]:
    return {"amount": amount, "currency": "UAH", "valueAddedTaxIncluded": True}


def make_tender(
    title: str,
    items: list[tuple[str, str]],  # (CPV, description)
    value: float,
    *,
    created: datetime,
    modified: datetime | None = None,
    pmt: str = "aboveThreshold",
    kind: str = "general",
    status: str = "active.tendering",
    lots: list[tuple[str, float, list[int]]] | None = None,  # (title, value, indexes of items in lot)
    with_results: bool = False,
) -> dict[str, Any]:
    t = copy.deepcopy(FIXTURE)
    tid = uuid4().hex
    seq = int(tid[:6], 16) % 1_000_000
    t.update(
        id=tid,
        tenderID=f"UA-{created:%Y-%m-%d}-{seq:06d}-a",
        title=title,
        status=status,
        procurementMethodType=pmt,
        value=_value(value),
        dateCreated=created.isoformat(),
        date=created.isoformat(),
        dateModified=(modified or created).isoformat(),
        tenderPeriod={"startDate": created.isoformat(), "endDate": (created + timedelta(days=10)).isoformat()},
    )
    t["procuringEntity"] = {**t["procuringEntity"], "kind": kind}
    template_item = t["items"][0]
    t["items"] = []
    for cpv, desc in items:
        it = copy.deepcopy(template_item)
        it.update(id=uuid4().hex, description=desc, quantity=10, unit={"code": "H87", "name": "штука"})
        it["classification"] = {"scheme": "ДК021", "id": cpv, "description": desc}
        it.pop("relatedLot", None)
        t["items"].append(it)
    if lots:
        t["lots"] = []
        for title_, amount, idx in lots:
            lot_id = uuid4().hex
            t["lots"].append({"id": lot_id, "title": title_, "status": "active", "value": _value(amount)})
            for i in idx:
                t["items"][i]["relatedLot"] = lot_id
    else:
        t.pop("lots", None)
    for key in ("bids", "awards", "contracts", "qualifications", "auctionPeriod", "awardPeriod"):
        t.pop(key, None)
    if with_results:
        supplier = {
            "name": 'ТОВ "Мережеві Рішення"',
            "identifier": {"scheme": "UA-EDR", "id": "12345678", "legalName": 'ТОВ "Мережеві Рішення"'},
            "address": {"region": "м. Київ", "countryName": "Україна"},
        }
        other = {
            "name": 'ТОВ "Конкурент"',
            "identifier": {"scheme": "UA-EDR", "id": "87654321", "legalName": 'ТОВ "Конкурент"'},
            "address": {"region": "Львівська область", "countryName": "Україна"},
        }
        unit_items = [
            {
                "id": i["id"],
                "description": i["description"],
                "quantity": 10,
                "unit": {"code": "H87", "name": "штука", "value": _value(price)},
            }
            for i, price in zip(t["items"], [98_000.0] * len(t["items"]), strict=True)
        ]
        bid1, bid2 = uuid4().hex, uuid4().hex
        t["bids"] = [
            {
                "id": bid1,
                "status": "active",
                "tenderers": [supplier],
                "value": _value(value * 0.82),
                "items": unit_items,
            },
            {"id": bid2, "status": "active", "tenderers": [other], "value": _value(value * 0.9)},
        ]
        t["awards"] = [
            {
                "id": uuid4().hex,
                "status": "active",
                "bid_id": bid1,
                "suppliers": [supplier],
                "value": _value(value * 0.82),
                "date": created.isoformat(),
            }
        ]
        t["contracts"] = [
            {"id": uuid4().hex, "status": "active", "value": _value(value * 0.82), "dateSigned": created.isoformat()}
        ]
        t["status"] = "complete"
    return t


def demo_tenders(now: datetime | None = None) -> list[dict[str, Any]]:
    """A day of feed activity covering all filter branches. Comments say what the filter should do."""
    now = now or datetime.now(KYIV_TZ)
    today = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if today > now:
        today = now - timedelta(hours=1)
    yesterday = today - timedelta(days=1)

    def at(minutes: int) -> datetime:
        return min(today + timedelta(minutes=minutes), now - timedelta(seconds=10))

    return [
        # relevant: strong CPV (network), no lots
        make_tender(
            "Закупівля комутаторів для ЦОД",
            [("32420000-3", "Комутатор Cisco Catalyst 9300-48P")],
            1_200_000,
            created=at(5),
            with_results=True,
        ),
        # relevant: weak CPV 30230000 + keyword "сервер", one lot
        make_tender(
            "Серверне обладнання",
            [("30230000-0", "Сервер Dell PowerEdge R760"), ("30230000-0", "Монітор 27 дюймів")],
            2_500_000,
            created=at(10),
            lots=[("Сервери", 2_500_000, [0, 1])],
        ),
        # relevant: belowThreshold from a "special" entity, cybersecurity
        make_tender(
            "Антивірусний захист",
            [("48760000-3", "Ліцензії ESET PROTECT Complete, 300 робочих місць")],
            800_000,
            created=at(15),
            pmt="belowThreshold",
            kind="special",
        ),
        # rejected (value): strong CPV but 400k
        make_tender("Міжмережевий екран", [("48730000-4", "Ліцензія FortiGate UTM на 1 рік")], 400_000, created=at(20)),
        # rejected (value, relevant_lots scope): 5M furniture lot + 300k switch lot
        make_tender(
            "Меблі та мережеве обладнання",
            [("39100000-3", "Столи офісні"), ("32420000-3", "Комутатор MikroTik CRS326")],
            5_300_000,
            created=at(25),
            lots=[("Меблі", 5_000_000, [0]), ("Мережа", 300_000, [1])],
        ),
        # rejected (topic): weak CPV without keywords
        make_tender("Закупівля картриджів", [("30200000-1", "Картридж HP 59A для принтера")], 600_000, created=at(30)),
        # rejected (prefilter): reporting is not in the list
        make_tender(
            "Прямий договір: маршрутизатори",
            [("32413100-2", "Маршрутизатор Juniper MX204")],
            2_000_000,
            created=at(35),
            pmt="reporting",
        ),
        # rejected (prefilter): belowThreshold, general entity
        make_tender(
            "Спрощена закупівля: мережеве обладнання",
            [("32420000-3", "Комутатор TP-Link")],
            150_000,
            created=at(40),
            pmt="belowThreshold",
            kind="general",
        ),
        # rejected (prefilter, no fetch): lots sum < 500k
        make_tender(
            "Кабельна продукція",
            [("32420000-3", "Патч-корди"), ("32420000-3", "Кабель UTP")],
            150_000,
            created=at(45),
            lots=[("Патч-корди", 100_000, [0]), ("Кабель", 50_000, [1])],
        ),
        # skipped: draft
        make_tender("Чернетка: СХД", [("48820000-2", "СХД NetApp")], 3_000_000, created=at(50), status="draft"),
        # skipped with only_new: created yesterday, modified today
        make_tender(
            "Вчорашній тендер: сервери",
            [("48820000-2", "Сервер HPE ProLiant DL380")],
            1_500_000,
            created=yesterday,
            modified=at(55),
        ),
        # older than `since`: ends the feed walk
        make_tender("Старий тендер", [("32420000-3", "Комутатор")], 900_000, created=yesterday - timedelta(hours=2)),
    ]


class FakeProzorro:
    """In-memory API: descending feed (newest modification first) with opaque offsets and GET /tenders/{id}."""

    def __init__(self, tenders: list[dict[str, Any]], page_size: int = 4):
        self.tenders = {t["id"]: t for t in tenders}
        self.page_size = page_size
        self.requests: list[str] = []

    def feed(self, params: dict[str, str]) -> dict[str, Any]:
        ordered = sorted(self.tenders.values(), key=lambda t: t["dateModified"], reverse=bool(params.get("descending")))
        start = int(params.get("offset") or 0)
        limit = min(int(params.get("limit") or 100), self.page_size)
        fields = set((params.get("opt_fields") or "").split(",")) - {""}
        page = ordered[start : start + limit]
        data = [{"id": t["id"], "dateModified": t["dateModified"], **{f: t[f] for f in fields if f in t}} for t in page]
        out: dict[str, Any] = {"data": data, "next_page": {"offset": str(start + len(page))}}
        return out

    def handle(self, path: str, query: str) -> tuple[int, dict[str, Any]]:
        self.requests.append(path)
        params = {k: v[0] for k, v in parse_qs(query).items()}
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX) :]
        if path == "/tenders":
            return 200, self.feed(params)
        if path.startswith("/tenders/"):
            t = self.tenders.get(path.split("/")[2])
            if t:
                return 200, {"data": t}
        return 404, {"status": "error", "errors": [{"location": "url", "name": "id", "description": "Not Found"}]}

    # httpx transport for unit tests
    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            status, body = self.handle(request.url.path, request.url.query.decode())
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handler)

    # real HTTP server for end-to-end tests / demos
    def serve(self) -> tuple[ThreadingHTTPServer, str]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                status, body = fake.handle(u.path, u.query)
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}{API_PREFIX}"
