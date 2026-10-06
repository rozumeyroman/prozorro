"""MCP server exposing Prozorro tender tools to Claude."""

from __future__ import annotations

import asyncio
import re
from functools import cache
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from .client import NotFound, ProzorroClient
from .db import Database
from .filter import TenderFilter
from .settings import KYIV_TZ, Settings
from .summary import documents_list, tender_summary, tender_url
from .sync import Syncer, parse_since

INSTRUCTIONS = """\
Інструменти для аналізу публічних закупівель Prozorro (Україна) за темами: кібербезпека, мережеве та серверне
обладнання. Локальна база містить лише тендери, що пройшли фільтр (тема + очікувана вартість від порогу з конфігу).

Типовий порядок роботи:
1. sync_tenders: підтягнути нові тендери з Prozorro (за замовчуванням створені сьогодні).
2. search_tenders: шукати у локальній базі (текст, тема, вартість, дати, статус).
3. get_tender: повна картка тендера: позиції, учасники та їхні ціни, переможці, договори, документи.
Посилання на тендер для людини: поле url (prozorro.gov.ua/tender/UA-...).
"""

mcp = MCPServer("prozorro", instructions=INSTRUCTIONS)

HEX_ID = re.compile(r"^[0-9a-f]{32}$")
UA_ID = re.compile(r"UA-\d{4}-\d{2}-\d{2}-\d{6}-[a-z]", re.I)


@cache
def settings() -> Settings:
    return Settings.from_env()


@cache
def db() -> Database:
    return Database(settings().db_path)


@cache
def tender_filter() -> TenderFilter:
    return TenderFilter.from_file(settings().filter_config)


def client() -> ProzorroClient:
    return ProzorroClient(settings())


def resolve_ref(ref: str) -> tuple[str | None, str | None]:
    """Return (hex id, UA id) from a hex id, a UA-... id or a prozorro.gov.ua URL."""
    ref = ref.strip()
    if HEX_ID.match(ref):
        return ref, None
    m = UA_ID.search(ref)
    if m:
        ua = m.group(0)
        return None, ua[:-1].upper() + ua[-1].lower()
    return None, None


async def load_tender(ref: str, refresh: bool = False) -> dict[str, Any]:
    hex_id, ua_id = resolve_ref(ref)
    if not hex_id and not ua_id:
        raise ValueError(f"Не розпізнано ідентифікатор тендера: {ref!r}. Потрібен id (32 hex) або UA-…")
    stored = db().get_tender(hex_id or ua_id)  # type: ignore[arg-type]
    if stored and not refresh:
        return stored
    if not hex_id:
        if stored:
            hex_id = stored["id"]
        else:
            raise ValueError(
                f"Тендера {ua_id} немає в локальній базі, а API Prozorro шукає лише за внутрішнім id (32 hex). "
                "Спробуйте sync_tenders за потрібний період або передайте внутрішній id."
            )
    async with client() as c:
        try:
            return await c.get_tender(hex_id)
        except NotFound as e:
            raise ValueError(f"Тендер {hex_id} не знайдено в Prozorro") from e


@mcp.tool()
async def status() -> dict[str, Any]:
    """Стан сервера: шлях до бази, кількість тендерів, параметри фільтра, остання синхронізація."""
    s = settings()
    return {
        "api_url": s.api_url,
        "db_path": str(s.db_path),
        "filter_config": str(s.filter_config),
        "filter": tender_filter().summary(),
        "counts": db().counts(),
        "last_sync": db().last_run(),
    }


@mcp.tool()
async def sync_tenders(
    ctx: Context,
    since: Annotated[
        str, Field(description="Початок періоду: 'today', 'yesterday', '24h', '3d', дата '2026-10-06' або ISO-час")
    ] = "today",
    only_new: Annotated[
        bool,
        Field(description="True: лише тендери, створені після since. False: також старші тендери, змінені після since"),
    ] = True,
) -> dict[str, Any]:
    """Завантажити з Prozorro тендери за період і зберегти релевантні у локальну базу.

    Проходить фід змін від найновіших до `since`, відкидає нецікаві тендери за типом процедури та сумою лотів,
    решту завантажує повністю і перевіряє тему (CPV, ключові слова) та очікувану вартість.
    Повертає статистику відсіювання по шарах і кількість знайдених тендерів.
    """
    since_dt = parse_since(since)

    def report(msg: str) -> None:
        # Syncer reports synchronously; send the MCP log message in the background.
        asyncio.ensure_future(ctx.info(msg))

    async with client() as c:
        syncer = Syncer(c, db(), tender_filter(), settings().concurrency, progress=report)
        stats = await syncer.sync(since_dt, only_new=only_new)
    stats["since_kyiv"] = since_dt.astimezone(KYIV_TZ).isoformat(timespec="minutes")
    stats["relevant_in_db_created_since"] = db().search(created_from=since_dt.isoformat(), limit=0)[1]
    return stats


@mcp.tool()
async def search_tenders(
    query: Annotated[str | None, Field(description="Повнотекстовий пошук по назві, замовнику та позиціях")] = None,
    topic: Annotated[
        Literal["network", "servers_storage", "cybersecurity", "keyword"] | None,
        Field(description="Тема: network, servers_storage, cybersecurity або keyword (знайдено за ключовим словом)"),
    ] = None,
    min_value: Annotated[float | None, Field(description="Мінімальна вартість релевантних лотів, грн")] = None,
    created_from: Annotated[
        str | None, Field(description="Створені з (since-формат: 'today', '7d', '2026-10-01')")
    ] = None,
    created_to: Annotated[str | None, Field(description="Створені до (не включно), той самий формат")] = None,
    status: Annotated[
        list[str] | None, Field(description="Статуси, напр. ['active.tendering'] для тих, що приймають пропозиції")
    ] = None,
    sort: Literal["date_desc", "date_asc", "value_desc", "value_asc", "deadline_asc"] = "date_desc",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict[str, Any]:
    """Пошук релевантних тендерів у локальній базі (спершу виконайте sync_tenders за потрібний період).

    Повертає сторінку результатів і total (усього збігів), щоб можна було гортати через offset.
    """
    rows, total = db().search(
        query=query,
        topic=topic,
        min_value=min_value,
        created_from=parse_since(created_from).isoformat() if created_from else None,
        created_to=parse_since(created_to).isoformat() if created_to else None,
        statuses=status,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    for r in rows:
        r["url"] = tender_url({"tenderID": r["tender_id"]})
        r["matched_items"] = db().matched_items(r["id"])
    return {"total": total, "offset": offset, "count": len(rows), "results": rows}


@mcp.tool()
async def get_tender(
    tender: Annotated[
        str, Field(description="Внутрішній id (32 hex), UA-…-ідентифікатор або посилання prozorro.gov.ua")
    ],
    refresh: Annotated[bool, Field(description="Перезавантажити з API замість локальної копії")] = False,
    include_documents: bool = True,
) -> dict[str, Any]:
    """Картка тендера: замовник, вартість, лоти, позиції (CPV), пропозиції з цінами за одиницю, переможці,
    договори та список документів."""
    data = await load_tender(tender, refresh=refresh)
    summary = tender_summary(data, include_documents=include_documents)
    decision = tender_filter().evaluate(data)
    summary["filter"] = {"relevant": decision.relevant, "reason": decision.reason, "topics": decision.topics}
    return summary


@mcp.tool()
async def list_documents(
    tender: Annotated[str, Field(description="Внутрішній id, UA-… або посилання prozorro.gov.ua")],
) -> list[dict[str, Any]]:
    """Документи тендера (остання версія кожного): тип, назва, формат і посилання на файл."""
    return documents_list(await load_tender(tender))


@mcp.tool()
async def explain_filter(
    tender: Annotated[str, Field(description="Внутрішній id, UA-… або посилання prozorro.gov.ua")],
) -> dict[str, Any]:
    """Пояснити, чому тендер пройшов або не пройшов фільтр: збіги позицій за CPV та ключовими словами і розрахунок
    вартості. Корисно для налаштування config/tender-filter.yaml."""
    data = await load_tender(tender, refresh=True)
    d = tender_filter().evaluate(data)
    return {
        "tenderID": data.get("tenderID"),
        "relevant": d.relevant,
        "stage": d.stage,
        "reason": d.reason,
        "relevant_value": d.relevant_value,
        "currency": d.currency,
        "matches": [m.__dict__ for m in d.matches],
        "items": [
            {"description": i.get("description"), "cpv": (i.get("classification") or {}).get("id")}
            for i in data.get("items") or []
        ],
    }


def run() -> None:
    mcp.run()
