"""MCP server exposing Prozorro tender tools to Claude."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from functools import cache
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from .client import NotFound, ProzorroClient, ProzorroError
from .db import Database
from .documents import DocumentDownloader, safe_name
from .export import export_tenders
from .filter import TenderFilter
from .profiles import FilterError, FilterRegistry, ensure_matches
from .selection import TenderQuery, select_tenders
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
Фільтри: активний профіль (list_filters, use_filter) діє в усіх інструментах, доки користувач не попросить змінити.
Параметр `filter` в окремому виклику застосовує інший профіль лише до цього виклику. Щоб змінити умови відбору
(поріг, коди, ключові слова), використовуйте save_filter.
4. export_excel: вивантаження в Excel (основний формат, яким користується користувач).
5. download_documents: тендерна документація в теки «Замовник - Предмет - UA-ID».
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
def registry() -> FilterRegistry:
    s = settings()
    return FilterRegistry(s.filters_dir, s.user_filters_dir, s.default_filter, s.filter_config)


def tender_filter(name: str | None = None) -> TenderFilter:
    """The named filter profile for one call, or the active profile."""
    return registry().resolve(db(), name)


def profile_filter(name: str | None = None) -> TenderFilter:
    """Like tender_filter, and makes sure stored tenders are evaluated under this profile."""
    f = tender_filter(name)
    ensure_matches(db(), f)
    return f


FilterParam = Annotated[
    str | None,
    Field(description="Профіль фільтра лише для цього виклику (див. list_filters). Без нього діє активний фільтр"),
]
TopicParam = Annotated[
    str | None,
    Field(
        description="Тема (група) фільтра, напр. endpoint, network_security, siem_soc, identity, appsec_testing, "
        "data_email; повний список тем: list_filters"
    ),
]


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
    f = profile_filter()
    return {
        "api_url": s.api_url,
        "db_path": str(s.db_path),
        "output_dir": str(s.output_dir),
        "active_filter": f.summary(),
        "counts": db().counts(f.name),
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
    filter: FilterParam = None,
) -> dict[str, Any]:
    """Завантажити з Prozorro тендери за період і зберегти релевантні у локальну базу.

    Проходить фід змін від найновіших до `since`, відкидає нецікаві тендери за типом процедури та сумою лотів,
    решту завантажує повністю і перевіряє тему (CPV, ключові слова) та очікувану вартість.
    Повертає статистику відсіювання по шарах і кількість знайдених тендерів.
    """
    since_dt = parse_since(since)
    f = tender_filter(filter)

    def report(msg: str) -> None:
        # Syncer reports synchronously; send the MCP log message in the background.
        asyncio.ensure_future(ctx.info(msg))

    async with client() as c:
        syncer = Syncer(c, db(), f, settings().concurrency, progress=report)
        stats = await syncer.sync(since_dt, only_new=only_new)
    ensure_matches(db(), f)
    stats["since_kyiv"] = since_dt.astimezone(KYIV_TZ).isoformat(timespec="minutes")
    stats["relevant_in_db_created_since"] = db().search(created_from=since_dt.isoformat(), limit=0, profile=f.name)[1]
    return stats


@mcp.tool()
async def search_tenders(
    query: Annotated[str | None, Field(description="Повнотекстовий пошук по назві, замовнику та позиціях")] = None,
    topic: TopicParam = None,
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
    filter: FilterParam = None,
) -> dict[str, Any]:
    """Пошук релевантних тендерів у локальній базі (спершу виконайте sync_tenders за потрібний період).

    Показує лише тендери, що проходять активний фільтр (або вказаний у `filter`).
    Повертає сторінку результатів і total (усього збігів), щоб можна було гортати через offset.
    """
    f = profile_filter(filter)
    rows, total = db().search(
        profile=f.name,
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
        r["matched_items"] = db().matched_items(r["id"], f.name)
    return {"filter": f.name, "total": total, "offset": offset, "count": len(rows), "results": rows}


@mcp.tool()
async def get_tender(
    tender: Annotated[
        str, Field(description="Внутрішній id (32 hex), UA-…-ідентифікатор або посилання prozorro.gov.ua")
    ],
    refresh: Annotated[bool, Field(description="Перезавантажити з API замість локальної копії")] = False,
    include_documents: bool = True,
    filter: FilterParam = None,
) -> dict[str, Any]:
    """Картка тендера: замовник, вартість, лоти, позиції (CPV), пропозиції з цінами за одиницю, переможці,
    договори та список документів."""
    data = await load_tender(tender, refresh=refresh)
    summary = tender_summary(data, include_documents=include_documents)
    f = tender_filter(filter)
    decision = f.evaluate(data)
    summary["filter"] = {
        "name": f.name,
        "relevant": decision.relevant,
        "reason": decision.reason,
        "topics": decision.topics,
    }
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
    filter: FilterParam = None,
) -> dict[str, Any]:
    """Пояснити, чому тендер пройшов або не пройшов фільтр: збіги позицій за CPV та ключовими словами і розрахунок
    вартості. Корисно, щоб вирішити, що змінити через save_filter."""
    data = await load_tender(tender, refresh=True)
    f = tender_filter(filter)
    d = f.evaluate(data)
    return {
        "filter": f.name,
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


StageParam = Annotated[
    Literal["active", "complete", "all"],
    Field(
        description="active: тендери, що тривають (будь-який active.*); complete: завершені (договір підписано); all"
    ),
]
DateParam = Annotated[str | None, Field(description="Формат since: 'today', 'yesterday', '7d', '2026-10-01'")]


@mcp.tool()
async def export_excel(
    query: Annotated[str | None, Field(description="Повнотекстовий пошук по назві, замовнику та позиціях")] = None,
    topic: TopicParam = None,
    stage: StageParam = "all",
    status: Annotated[list[str] | None, Field(description="Точні статуси (замість stage)")] = None,
    min_value: Annotated[float | None, Field(description="Мінімальна вартість релевантних лотів, грн")] = None,
    created_from: DateParam = None,
    created_to: DateParam = None,
    awarded_from: Annotated[
        str | None, Field(description="Лише тендери, де переможця визначено або договір підписано з цієї дати")
    ] = None,
    awarded_to: DateParam = None,
    file_name: Annotated[
        str | None, Field(description="Назва файлу без шляху; за замовчуванням з датою й часом")
    ] = None,
    filter: FilterParam = None,
) -> dict[str, Any]:
    """Вивантажити тендери з локальної бази в Excel (.xlsx).

    Аркуші: «Тендери» (з посиланнями на prozorro.gov.ua), «Позиції», «Переможці» (рішення, суми, знижка, договір),
    «Пропозиції» (усі учасники та їхні суми), «Ціни за одиницю». Файл створюється в теці експорту користувача;
    поверніть користувачу шлях до файлу. Дані беруться з локальної бази: спершу виконайте sync_tenders.
    До файлу потрапляють лише тендери, що проходять активний фільтр (або вказаний у `filter`).
    """
    f = profile_filter(filter)
    q = TenderQuery(
        profile=f.name,
        query=query,
        topic=topic,
        stage=stage,
        status=status,
        min_value=min_value,
        created_from=created_from,
        created_to=created_to,
        awarded_from=awarded_from,
        awarded_to=awarded_to,
    )
    tenders = select_tenders(db(), q)
    name = safe_name(file_name, 100) if file_name else f"prozorro_{datetime.now(KYIV_TZ):%Y-%m-%d_%H%M}"
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    path = settings().output_dir / "Експорт" / name
    counts = export_tenders(tenders, path, f)
    return {"path": str(path), "filter": f.name, "rows": counts}


@mcp.tool()
async def download_documents(
    ctx: Context,
    tender: Annotated[
        str | None, Field(description="Один тендер: id, UA-… або посилання. Якщо не задано, діють фільтри нижче")
    ] = None,
    stage: StageParam = "all",
    topic: TopicParam = None,
    query: Annotated[str | None, Field(description="Повнотекстовий пошук по назві, замовнику та позиціях")] = None,
    created_from: DateParam = None,
    awarded_from: Annotated[str | None, Field(description="Переможця визначено/договір підписано з цієї дати")] = None,
    include_bid_documents: Annotated[
        bool, Field(description="Також документи пропозицій учасників (технічні та цінові пропозиції), якщо публічні")
    ] = False,
    include_signatures: Annotated[bool, Field(description="Також файли підписів .p7s")] = False,
    max_tenders: Annotated[int, Field(ge=1, le=200)] = 20,
    filter: FilterParam = None,
) -> dict[str, Any]:
    """Завантажити тендерну документацію в теки на диску користувача.

    Тека кожного тендера: «<Замовник> - <Предмет закупівлі> - <UA-ID>» у теці «Документи». Перед завантаженням
    тендер оновлюється з API Prozorro. Повторний запуск докачує лише нові або змінені документи.
    Можна передати один тендер або вибрати тендери з локальної бази фільтрами (stage=active — ті, що тривають;
    stage=complete — завершені).
    """
    if tender:
        refs = [tender]
    else:
        q = TenderQuery(
            profile=profile_filter(filter).name,
            query=query,
            topic=topic,
            stage=stage,
            created_from=created_from,
            awarded_from=awarded_from,
            limit=max_tenders,
        )
        refs = [t["id"] for t in select_tenders(db(), q)]
    root = settings().output_dir / "Документи"
    results, errors = [], []
    async with client() as c:
        downloader = DocumentDownloader(c, root, settings().doc_hosts, settings().concurrency)
        for i, ref in enumerate(refs, start=1):
            try:
                data = await _fresh_tender(c, ref)
            except (ValueError, ProzorroError) as e:
                errors.append({"tender": ref, "error": str(e)})
                continue
            await ctx.info(f"{i}/{len(refs)}: {data.get('tenderID')}")
            r = await downloader.download_tender(data, include_signatures, include_bid_documents)
            results.append(r.__dict__)
    return {
        "root": str(root),
        "tenders": len(results),
        "files_downloaded": sum(len(r["downloaded"]) for r in results),
        "files_skipped": sum(r["skipped"] for r in results),
        "files_failed": sum(len(r["failed"]) for r in results),
        "megabytes": round(sum(r["bytes"] for r in results) / 1_048_576, 1),
        "details": results,
        "errors": errors,
    }


async def _fresh_tender(c: ProzorroClient, ref: str) -> dict[str, Any]:
    """Latest tender data from the API (stored copies of finished tenders may predate the award)."""
    hex_id, ua_id = resolve_ref(ref)
    if not hex_id:
        stored = db().get_tender(ua_id) if ua_id else None
        if not stored:
            raise ValueError(f"Тендер {ref!r} не знайдено в локальній базі; передайте внутрішній id (32 hex)")
        hex_id = stored["id"]
    try:
        data = await c.get_tender(hex_id)
    except NotFound as e:
        raise ValueError(f"Тендер {hex_id} не знайдено в Prozorro") from e
    stored = db().get_tender(hex_id)
    if stored:
        db().save_tender(data, tender_filter().evaluate(data))
    return data


@mcp.tool()
async def list_filters() -> list[dict[str, Any]]:
    """Профілі фільтрів: назва, опис, чи активний, поріг вартості, групи CPV і тем ключових слів.

    Активний фільтр діє в усіх інструментах, доки його не змінять через use_filter. Вбудовані профілі:
    cybersecurity (кібербезпека), it-infrastructure (кібербезпека + мережі + сервери).
    """
    return registry().list(db())


@mcp.tool()
async def use_filter(
    name: Annotated[str, Field(description="Назва профілю зі списку list_filters")],
) -> dict[str, Any]:
    """Зробити профіль фільтра активним. Вибір зберігається між сеансами, доки користувач не попросить змінити.

    Уже збережені тендери переоцінюються за новим фільтром локально, без звернень до Prozorro. Тендери, яких
    у базі ще немає (бо попередній фільтр їх відкинув), з'являться після sync_tenders.
    """
    try:
        f = registry().set_active(db(), name)
    except FilterError as e:
        raise ValueError(str(e)) from e
    relevant = ensure_matches(db(), f)
    return {"active_filter": f.summary(), "relevant_stored_tenders": relevant}


@mcp.tool()
async def save_filter(
    name: Annotated[str, Field(description="Назва нового/власного профілю: латиниця, цифри, '-', '_'")],
    base: Annotated[
        str | None, Field(description="Профіль-основа (обов'язково для нового). Для зміни власного можна не вказувати")
    ] = None,
    description: str | None = None,
    min_value: Annotated[float | None, Field(description="Новий поріг очікуваної вартості, грн")] = None,
    value_scope: Annotated[
        Literal["relevant_lots", "tender"] | None,
        Field(description="Рахувати вартість лотів з релевантними позиціями або всього тендера"),
    ] = None,
    add_cpv: Annotated[
        list[str] | None, Field(description="Основні коди CPV (самі по собі достатні), формат 12345678-9")
    ] = None,
    cpv_group: Annotated[str | None, Field(description="Тема для add_cpv (за замовчуванням custom)")] = None,
    add_cpv_weak: Annotated[list[str] | None, Field(description="Загальні коди (лише з ключовими словами)")] = None,
    exclude_cpv: Annotated[
        list[str] | None, Field(description="Коди, які ніколи не підходять (разом з підкодами)")
    ] = None,
    remove_cpv: Annotated[list[str] | None, Field(description="Прибрати коди з усіх списків")] = None,
    add_keywords: Annotated[
        list[str] | None, Field(description="Ключові слова/регулярні вирази (без урахування регістру)")
    ] = None,
    keyword_group: Annotated[str | None, Field(description="Тема для add_keywords (за замовчуванням custom)")] = None,
    remove_keywords: list[str] | None = None,
    add_exclude_keywords: Annotated[
        list[str] | None, Field(description="Слова-виключення: позиція з загальним кодом з таким словом не підходить")
    ] = None,
    remove_exclude_keywords: list[str] | None = None,
    add_exclude_items: Annotated[
        list[str] | None,
        Field(
            description="Початок опису позиції, з яким вона не підходить за жодного коду "
            "(напр. 'кабел', 'модул\\w* пам'): для кабелів і комплектуючих під загальними кодами"
        ),
    ] = None,
    remove_exclude_items: list[str] | None = None,
    add_procurement_method_types: list[str] | None = None,
    remove_procurement_method_types: list[str] | None = None,
    activate: Annotated[bool, Field(description="Одразу зробити активним")] = True,
) -> dict[str, Any]:
    """Створити або змінити власний профіль фільтра на основі наявного і (за замовчуванням) зробити його активним.

    Використовуйте, коли користувач у запиті просить змінити умови відбору: поріг вартості, коди CPV, ключові
    слова, виключення. Вбудовані профілі не змінюються: зміни зберігаються як новий профіль у теці «Фільтри»
    користувача. Перед змінами покажіть користувачу, що саме буде змінено.
    """
    changes = {k: v for k, v in locals().items() if k not in ("name", "base", "activate") and v is not None}
    try:
        f, path = registry().save(name, base, changes)
        if activate:
            registry().set_active(db(), name)
    except (FilterError, re.error) as e:
        raise ValueError(str(e)) from e
    relevant = ensure_matches(db(), f)
    return {"saved": str(path), "active": activate, "filter": f.summary(), "relevant_stored_tenders": relevant}


@mcp.tool()
async def delete_filter(name: str) -> dict[str, Any]:
    """Видалити власний профіль фільтра (вбудовані видалити не можна). Якщо він був активним, активним стає
    профіль за замовчуванням."""
    try:
        registry().delete(db(), name)
    except FilterError as e:
        raise ValueError(str(e)) from e
    return {"deleted": name, "active_filter": registry().active_name(db())}


def run() -> None:
    mcp.run()
