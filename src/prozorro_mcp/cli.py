"""Command line: `prozorro-mcp` (MCP server over stdio) and sync/coverage/search/summary/export/docs/offers/
calibrate/filters/exclude/export-db."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .client import NotFound, ProzorroClient, ProzorroError
from .db import Database
from .filter import TenderFilter
from .profiles import FilterRegistry, ensure_matches
from .selection import TenderQuery, select_rows, select_tenders
from .settings import KYIV_TZ, Settings
from .sync import Syncer, SyncError, parse_since, resolve_since


def _registry(s: Settings) -> FilterRegistry:
    return FilterRegistry(s.filters_dir, s.user_filters_dir, s.default_filter, s.filter_config)


def _filter(s: Settings, db: Database, name: str | None) -> TenderFilter:
    f = _registry(s).resolve(db, name)
    ensure_matches(db, f)
    return f


def _dump(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _query(args: argparse.Namespace, profile: str, query: str | None = None) -> TenderQuery:
    return TenderQuery(
        query=query if query is not None else getattr(args, "query", None),
        topic=args.topic,
        min_value=args.min_value,
        created_from=args.created_from,
        created_to=args.created_to,
        stage=args.stage,
        status=args.status,
        awarded_from=args.awarded_from,
        awarded_to=args.awarded_to,
        period_from=args.period_from,
        period_to=args.period_to,
        period_mode=args.period_mode,
        profile=profile,
    )


def _period_note(args: argparse.Namespace) -> str | None:
    parts = []
    if args.period_from or args.period_to:
        mode = {"created": "оголошені", "awarded": "з рішенням про переможця", "either": "оголошені або з рішенням"}
        parts.append(f"{mode[args.period_mode]} з {args.period_from or '…'} до {args.period_to or '…'}")
    if args.created_from or args.created_to:
        parts.append(f"створені з {args.created_from or '…'} до {args.created_to or '…'}")
    if args.awarded_from or args.awarded_to:
        parts.append(f"рішення з {args.awarded_from or '…'} до {args.awarded_to or '…'}")
    if args.stage != "all":
        parts.append({"active": "тривають", "complete": "завершені"}[args.stage])
    return ("Вибірка: " + "; ".join(parts)) if parts else None


# commands -------------------------------------------------------------------------------------------


async def _sync(args: argparse.Namespace, s: Settings) -> None:
    from .site import SiteClient, resolve_refs

    db = Database(s.db_path)
    f = _filter(s, db, args.filter)
    async with ProzorroClient(s) as client:
        syncer = Syncer(client, db, f, s.concurrency, progress=_log)
        if args.tender_ids:
            refs = _read_list(args.tender_ids)
            async with SiteClient(s) as site:
                ids, missing = await resolve_refs(refs, db, client, site)
            stats = await syncer.sync_ids(ids)
            stats["not_resolved"] = missing
        elif args.resume:
            stats = await syncer.sync(resume=True, max_pages=args.max_pages)
        else:
            stats = await syncer.sync(
                resolve_since(args.since, db, f.name),
                only_new=not args.all_modified,
                until=parse_since(args.until) if args.until else None,
                max_pages=args.max_pages,
                shards=args.shards,
            )
    ensure_matches(db, f)
    _dump(stats)
    if stats.get("complete") is False:
        _log(stats.get("warning") or "Синхронізація неповна")
        sys.exit(3)


async def _coverage(args: argparse.Namespace, s: Settings) -> None:
    from .coverage import check_coverage
    from .site import SiteClient

    db = Database(s.db_path)
    f = _filter(s, db, args.filter)
    cpvs = [c.strip() for c in (args.cpv or "").split(",") if c.strip()]
    async with ProzorroClient(s) as client, SiteClient(s) as site:
        report = await check_coverage(
            db,
            site,
            f,
            date_from=args.date_from,
            date_to=args.date_to,
            cpvs=cpvs or None,
            min_value=args.min_value,
            fetch=args.fetch_missing,
            client=client,
            concurrency=s.concurrency,
            progress=_log,
        )
    ensure_matches(db, f)
    _dump(report)


def _exclude(args: argparse.Namespace, s: Settings) -> None:
    from .site import normalize_tender_id

    db = Database(s.db_path)
    if args.action == "list":
        _dump(db.exclusions())
        return
    out = []
    for ref in args.tenders:
        tid = normalize_tender_id(ref)
        if not tid:
            raise ValueError(f"{ref!r}: потрібен номер UA-…")
        if args.action == "add":
            out.append(db.add_exclusion(tid, args.reason))
        else:
            out.append({"tenderID": tid, "removed": db.remove_exclusion(tid)})
    _dump(out)


def _export_db(args: argparse.Namespace, s: Settings) -> None:
    _dump(Database(s.db_path).export_copy(Path(args.path).expanduser(), relevant_only=args.relevant_only))


def _search(args: argparse.Namespace, s: Settings) -> None:
    db = Database(s.db_path)
    f = _filter(s, db, args.filter)
    rows, total = select_rows(db, _query(args, f.name, args.query), sort=args.sort, limit=args.limit)
    _dump({"filter": f.name, "total": total, "results": rows})


def _summary(args: argparse.Namespace, s: Settings) -> None:
    from .analytics import summarize

    db = Database(s.db_path)
    f = _filter(s, db, args.filter)
    _dump(summarize(select_tenders(db, _query(args, f.name)), f, top=args.top))


def _export(args: argparse.Namespace, s: Settings) -> None:
    from .export import export_tenders

    db = Database(s.db_path)
    f = _filter(s, db, args.filter)
    tenders = select_tenders(db, _query(args, f.name))
    path = (
        Path(args.output).expanduser()
        if args.output
        else s.output_dir / "Експорт" / f"prozorro_{datetime.now(KYIV_TZ):%Y-%m-%d_%H%M}.xlsx"
    )
    offers = db.offers(tenders=[t["id"] for t in tenders])
    rows = export_tenders(
        tenders,
        path,
        f,
        include_summary=not args.no_summary,
        period_note=_period_note(args),
        offers=offers,
        exclusions=db.exclusions(),
    )
    _dump({"path": str(path), "filter": f.name, "rows": rows})


async def _docs(args: argparse.Namespace, s: Settings) -> None:
    from .batch import download_batch, prune_winner_docs
    from .documents import prune_folders
    from .offers import load_rules
    from .site import SiteClient

    db = Database(s.db_path)
    root = s.output_dir / "Документи"
    f = _filter(s, db, args.filter)
    if args.prune or args.prune_winner_docs:
        if args.prune_winner_docs:
            result = prune_winner_docs(root, load_rules(s), confirm=args.yes)
        else:
            keep = {t.get("tenderID") for t in select_tenders(db, TenderQuery(profile=f.name))}
            result = prune_folders(root, keep, confirm=args.yes)
        if not args.yes:
            _log("Нічого не видалено. Щоб видалити, повторіть команду з --yes.")
        _dump({"filter": f.name, **result})
        return

    refs = _refs(args)
    if not refs:
        q = _query(args, f.name)
        q.limit = args.max_tenders
        refs = [t["id"] for t in select_tenders(db, q)]
    async with ProzorroClient(s) as client, SiteClient(s) as site:
        out = await download_batch(
            client,
            s,
            db,
            refs,
            with_winners=args.with_winners,
            winner_docs=args.winner_docs,
            tender_concurrency=args.tender_concurrency,
            extract=not args.no_text,
            ocr=args.ocr,
            include_signatures=args.signatures,
            include_bids=args.bids,
            remaining=args.remaining,
            tender_filter=f,
            site=site,
            progress=_log,
        )
    _dump(out)


async def _calibrate(args: argparse.Namespace, s: Settings) -> None:
    from .calibrate import build_report, write_report

    db = Database(s.db_path)
    f = _filter(s, db, args.filter)
    async with ProzorroClient(s) as client:
        report = await build_report(
            db,
            None if args.offline else client,
            f,
            created_from=args.created_from,
            created_to=args.created_to,
            sample=args.sample,
            concurrency=s.concurrency,
            progress=_log,
            cpv_prefixes=[p for p in (args.cpv or "").split(",") if p.strip()],
            min_value=args.min_value,
        )
    path = (
        Path(args.output).expanduser()
        if args.output
        else s.output_dir / "Експорт" / f"calibration_{f.name}_{datetime.now(KYIV_TZ):%Y-%m-%d_%H%M}.xlsx"
    )
    rows = write_report(report, path)
    _dump(
        {
            "path": str(path),
            "filter": f.name,
            "passed_tenders": report["passed_tenders"],
            "rejected_total": report["rejected_total"],
            "rejected_with_digest": report["rejected_with_digest"],
            "rejected_targeted": report["rejected_targeted"],
            "rejected_without_digest": report["rejected_without_digest"],
            "rejected_checked": report["rejected_checked"],
            "candidates": len(report["candidates"]),
            "rows": rows,
        }
    )


def _read_list(path: str) -> list[str]:
    lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    return [ln.split("#", 1)[0].strip() for ln in lines if ln.split("#", 1)[0].strip()]


def _refs(args: argparse.Namespace) -> list[str]:
    refs = list(args.tenders)
    if args.ids_file:
        refs += _read_list(args.ids_file)
    return list(dict.fromkeys(refs))


async def _offers(args: argparse.Namespace, s: Settings) -> None:
    from .offers import fetch_tender, load_rules, normalize_rows, prepare_offer

    db = Database(s.db_path)
    if args.action == "text":
        from .batch import winner_folder
        from .offers import text_for_folder

        root = s.output_dir / "Документи"
        rules = load_rules(s)
        out, errors = [], []
        refs = _refs(args)
        for i, ref in enumerate(refs, start=1):
            try:
                r = text_for_folder(winner_folder(root, ref, db), ocr=args.ocr, rules=rules)
            except ValueError as e:
                errors.append({"tender": ref, "error": str(e)})
                _log(f"[{i}/{len(refs)}] {ref}: помилка {e}")
                continue
            _log(f"[{i}/{len(refs)}] {r['tenderID']}: документів {r['documents']}, OCR {len(r['ocr'])}")
            out.append(r)
        _dump({"tenders": out, "errors": errors})
        return
    if args.action == "fetch":
        from .batch import fetch_skipped

        async with ProzorroClient(s) as client:
            _dump(await fetch_skipped(client, s, db, args.tender, args.file))
        return
    if args.action == "list":
        rows = db.offers(tender=args.tender, vendor=args.vendor, supplier=args.supplier, query=args.query)
        if args.output:
            from openpyxl import Workbook

            from .export import write_offers

            wb = Workbook()
            n = write_offers(wb.active, rows)
            wb.active.title = "Що виграло"
            path = Path(args.output).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            wb.save(path)
            _dump({"path": str(path), "rows": n})
        else:
            _dump(rows)
        return
    if args.action == "save":
        payload = json.loads(Path(args.file).expanduser().read_text(encoding="utf-8"))
        out = []
        for entry in payload if isinstance(payload, list) else [payload]:
            tender = db.get_tender(entry["tender"])
            if not tender:
                raise ValueError(f"Тендер {entry['tender']!r} не знайдено в базі: спершу `offers prepare`")
            rows = normalize_rows(tender, entry.get("rows") or [])
            out.append(
                {
                    "tenderID": tender.get("tenderID"),
                    "saved_rows": db.save_offers(tender["id"], tender.get("tenderID"), rows),
                }
            )
        _dump(out)
        return

    # prepare
    f = _filter(s, db, args.filter)
    refs = _refs(args)
    if not refs:
        q = _query(args, f.name)
        q.limit = args.max_tenders
        refs = [t["id"] for t in select_tenders(db, q)]
    out, errors = [], []
    async with ProzorroClient(s) as client:
        for i, ref in enumerate(refs, start=1):
            try:
                tender = await fetch_tender(client, db, ref)
                db.save_tender(tender, f.evaluate(tender))
                r = await prepare_offer(
                    client,
                    s,
                    db,
                    tender,
                    max_chars=0,
                    winner_docs=args.winner_docs,
                    extract=not args.no_text,
                    ocr=args.ocr,
                    rules=load_rules(s) if args.winner_docs == "minimal" else None,
                )
            except (ValueError, NotFound, ProzorroError) as e:
                errors.append({"tender": ref, "error": str(e)})
                _log(f"[{i}/{len(refs)}] {ref}: помилка {e}")
                continue
            docs = r["documents"]
            _log(f"[{i}/{len(refs)}] {r['tenderID']}: переможців {len(r['winners'])}, документів {len(docs)}")
            out.append(
                {
                    "tenderID": r["tenderID"],
                    "folder": r["folder"],
                    "winners": [w["supplier"] for w in r["winners"]],
                    "documents": len(docs),
                    "skipped_documents": len(r["skipped_documents"]),
                    "without_text": [d["file"] for d in docs if not d.get("chars") and not d.get("ocr_chars")]
                    if r["text_extracted"]
                    else None,
                    "vendor_mentions": r["vendor_mentions"],
                    "saved_rows": len(r["saved_rows"]),
                }
            )
    _dump({"tenders": out, "errors": errors})


def _filters(args: argparse.Namespace, s: Settings) -> None:
    db = Database(s.db_path)
    reg = _registry(s)
    if args.use:
        ensure_matches(db, reg.set_active(db, args.use))
    keys = ("name", "active", "source", "description", "min_value")
    _dump([{k: v for k, v in f.items() if k in keys} for f in reg.list(db)])


# argument parsing -----------------------------------------------------------------------------------

DATE_HELP = "today | yesterday | 7d | 2026-07-01 | ISO-час"


def _selection_args(p: argparse.ArgumentParser, with_query: bool = True) -> None:
    g = p.add_argument_group("вибірка тендерів з бази")
    if with_query:
        g.add_argument("--query", help="повнотекстовий пошук по назві, замовнику та позиціях")
    g.add_argument("--topic", help="тема (група) фільтра, напр. endpoint, siem_soc")
    g.add_argument("--stage", choices=["active", "complete", "all"], default="all", help="тривають / завершені")
    g.add_argument("--status", action="append", help="точний статус Prozorro; можна кілька разів")
    g.add_argument("--min-value", type=float, help="мінімальна вартість релевантних лотів, грн")
    g.add_argument("--created-from", help=f"оголошені з ({DATE_HELP})")
    g.add_argument("--created-to", help="оголошені до (не включно)")
    g.add_argument("--awarded-from", help="рішення про переможця / договір з")
    g.add_argument("--awarded-to", help="рішення про переможця / договір до (не включно)")
    g.add_argument("--period-from", help="початок періоду для --period-mode")
    g.add_argument("--period-to", help="кінець періоду (не включно)")
    g.add_argument(
        "--period-mode",
        choices=["created", "awarded", "either"],
        default="created",
        help="до чого застосувати період: дата оголошення, дата рішення або будь-яка з них (АБО)",
    )


def _winner_doc_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--winner-docs",
        choices=["minimal", "all"],
        default="minimal",
        help="minimal: лише авторизаційні листи, специфікації, цінові пропозиції (config/winner-docs.yaml); all: усе",
    )
    p.add_argument("--no-text", action="store_true", help="не витягати текст зараз (пізніше: offers text)")
    p.add_argument("--ocr", action="store_true", help="розпізнати скани переможця (tesseract, перші 2 сторінки)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prozorro-mcp")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="запустити MCP-сервер (stdio); команда за замовчуванням")

    p = sub.add_parser("sync", help="синхронізувати тендери за період")
    p.add_argument("--since", default="today", help=f"початок: {DATE_HELP} | last (від попередньої синхронізації)")
    p.add_argument("--until", help="лише тендери, створені до цієї дати (не включно)")
    p.add_argument("--all-modified", action="store_true", help="також старші тендери, змінені за період")
    p.add_argument("--resume", action="store_true", help="продовжити перервану синхронізацію з місця зупинки")
    p.add_argument("--max-pages", type=int, default=None, help="обмеження сторінок стрічки (для тестів)")
    p.add_argument(
        "--shards",
        type=int,
        default=1,
        help="читати стрічку паралельно N вікнами за часом зміни (для довгих періодів: 3–6)",
    )
    p.add_argument(
        "--tender-ids",
        metavar="FILE",
        help="адресно завантажити й перевірити тендери зі списку (UA-… або id, по одному в рядку), без обходу стрічки",
    )
    p_sync = p

    p_cov = sub.add_parser("coverage", help="звірити базу з пошуком prozorro.gov.ua: які тендери пропущено і чому")
    p_cov.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD (період подання пропозицій)")
    p_cov.add_argument("--to", dest="date_to", required=True, help="YYYY-MM-DD включно")
    p_cov.add_argument("--cpv", help="коди CPV через кому (типово — основні коди фільтра)")
    p_cov.add_argument("--min-value", type=float, help="мінімальна очікувана вартість (типово — поріг фільтра)")
    p_cov.add_argument(
        "--fetch-missing", action="store_true", help="дозавантажити пропущені тендери адресно і перевірити фільтром"
    )

    p_exc = sub.add_parser("exclude", help="ручні виключення тендерів (зберігаються між синхронізаціями)")
    exc = p_exc.add_subparsers(dest="action", required=True)
    p_exc_add = exc.add_parser("add", help="виключити тендер(и)")
    p_exc_add.add_argument("tenders", nargs="+", help="UA-… або посилання")
    p_exc_add.add_argument("--reason", help="причина (потрапляє в Excel на аркуш «Виключені»)")
    p_exc_rm = exc.add_parser("remove", help="повернути тендер(и)")
    p_exc_rm.add_argument("tenders", nargs="+")
    exc.add_parser("list", help="список виключень")

    p_edb = sub.add_parser("export-db", help="компактна копія бази для перенесення між середовищами")
    p_edb.add_argument("path", help="шлях до нового файлу .db")
    p_edb.add_argument(
        "--relevant-only", action="store_true", help="без кешу рішень щодо нерелевантних тендерів (значно менше)"
    )

    p_search = sub.add_parser("search", help="пошук у локальній базі")
    p_search.add_argument("query", nargs="?")
    _selection_args(p_search, with_query=False)
    p_search.add_argument(
        "--sort", default="date_desc", choices=["date_desc", "date_asc", "value_desc", "value_asc", "deadline_asc"]
    )
    p_search.add_argument("--limit", type=int, default=20)

    p_summary = sub.add_parser("summary", help="зведена аналітика: суми, розбивки, топ переможців і замовників")
    _selection_args(p_summary)
    p_summary.add_argument("--top", type=int, default=10, help="скільки рядків у топах")

    p_export = sub.add_parser("export", help="вивантажити тендери з бази в Excel")
    _selection_args(p_export)
    p_export.add_argument("-o", "--output", help="шлях до .xlsx (за замовчуванням у теці експорту)")
    p_export.add_argument("--no-summary", action="store_true", help="без аркуша «Аналітика»")

    p_docs = sub.add_parser("docs", help="завантажити тендерну документацію")
    p_docs.add_argument("tenders", nargs="*", help="id, UA-… або посилання (кілька); без них діє вибірка")
    p_docs.add_argument("--ids-file", help="файл зі списком тендерів, по одному в рядку")
    _selection_args(p_docs)
    p_docs.add_argument("--bids", action="store_true", help="також документи пропозицій учасників")
    p_docs.add_argument("--signatures", action="store_true", help="також підписи .p7s")
    p_docs.add_argument("--max-tenders", type=int, default=20, help="максимум тендерів з вибірки")
    p_docs.add_argument(
        "--prune", action="store_true", help="показати теки тендерів, що не проходять фільтр (з --yes — видалити)"
    )
    p_docs.add_argument("--yes", action="store_true", help="підтвердити видалення для --prune / --prune-winner-docs")
    p_docs.add_argument(
        "--with-winners",
        action="store_true",
        help="одним проходом також документи переможців і договорів (+ _winner.json), як offers prepare",
    )
    _winner_doc_args(p_docs)
    p_docs.add_argument("--tender-concurrency", type=int, default=4, help="скільки тендерів обробляти паралельно")
    p_docs.add_argument(
        "--remaining", action="store_true", help="пропустити тендери, повністю завантажені раніше (без запитів до API)"
    )
    p_docs.add_argument(
        "--prune-winner-docs",
        action="store_true",
        help="показати (з --yes — видалити) файли переможців, що не потрібні за config/winner-docs.yaml",
    )

    p_cal = sub.add_parser("calibrate", help="звіт для калібрування фільтра (Excel для позначок)")
    p_cal.add_argument("--created-from", help=f"тендери, оголошені з ({DATE_HELP})")
    p_cal.add_argument("--created-to", help="оголошені до (не включно)")
    p_cal.add_argument(
        "--sample",
        type=int,
        default=300,
        help="скільки відкинутих тендерів БЕЗ збережених даних дозібрати за запуск (повторні запуски продовжують); "
        "ризикові тендери зі збереженими даними перевіряються завжди",
    )
    p_cal.add_argument("--cpv", help="також перевірити відкинуті з кодами CPV, префікси через кому: 48,7226,3242")
    p_cal.add_argument("--min-value", type=float, help="мінімальна вартість відкинутих (типово — поріг фільтра)")
    p_cal.add_argument("--offline", action="store_true", help="без звернень до Prozorro (лише аркуш «Пройшли»)")
    p_cal.add_argument("-o", "--output", help="шлях до .xlsx")

    p_off = sub.add_parser("offers", help="що саме виграло: документи переможців, збереження й перегляд")
    off = p_off.add_subparsers(dest="action", required=True)
    p_prep = off.add_parser(
        "prepare", help="завантажити документи переможців і договорів, витягти текст (_winner.json, _text/)"
    )
    p_prep.add_argument("tenders", nargs="*", help="id, UA-… або посилання; без них діє вибірка (типово завершені)")
    p_prep.add_argument("--ids-file", help="файл зі списком тендерів, по одному в рядку")
    _selection_args(p_prep)
    p_prep.set_defaults(stage="complete")
    p_prep.add_argument("--max-tenders", type=int, default=20, help="максимум тендерів з вибірки")
    p_prep.add_argument("--filter", help="профіль фільтра лише для цієї команди")
    _winner_doc_args(p_prep)
    p_text = off.add_parser("text", help="витягти текст (і OCR сканів) з уже завантажених документів переможця")
    p_text.add_argument("tenders", nargs="*", help="UA-… або id")
    p_text.add_argument("--ids-file", help="файл зі списком тендерів")
    p_text.add_argument("--ocr", action="store_true", help="розпізнати скани (tesseract, перші 2 сторінки)")
    p_fetch = off.add_parser("fetch", help="докачати документ переможця, пропущений режимом minimal")
    p_fetch.add_argument("tender", help="UA-… або id")
    p_fetch.add_argument("--file", action="append", required=True, help="назва документа (можна кілька разів)")
    p_save = off.add_parser("save", help='зберегти рядки з JSON: {"tender": "UA-…", "rows": [...]} або список')
    p_save.add_argument("file")
    p_olist = off.add_parser("list", help="збережені рядки «що виграло»")
    p_olist.add_argument("--vendor")
    p_olist.add_argument("--supplier")
    p_olist.add_argument("--query")
    p_olist.add_argument("--tender")
    p_olist.add_argument("-o", "--output", help="зберегти в Excel (.xlsx)")

    p_filters = sub.add_parser("filters", help="профілі фільтрів; з назвою — зробити активним")
    p_filters.add_argument("use", nargs="?", help="назва профілю, який зробити активним")

    for p in (p_sync, p_search, p_summary, p_export, p_docs, p_cal, p_cov):
        p.add_argument("--filter", help="профіль фільтра лише для цієї команди (за замовчуванням активний)")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    try:
        if args.cmd == "sync":
            asyncio.run(_sync(args, s))
        elif args.cmd == "search":
            _search(args, s)
        elif args.cmd == "summary":
            _summary(args, s)
        elif args.cmd == "export":
            _export(args, s)
        elif args.cmd == "docs":
            asyncio.run(_docs(args, s))
        elif args.cmd == "calibrate":
            asyncio.run(_calibrate(args, s))
        elif args.cmd == "offers":
            asyncio.run(_offers(args, s))
        elif args.cmd == "filters":
            _filters(args, s)
        elif args.cmd == "coverage":
            asyncio.run(_coverage(args, s))
        elif args.cmd == "exclude":
            _exclude(args, s)
        elif args.cmd == "export-db":
            _export_db(args, s)
        else:
            from .server import run

            run()
    except (SyncError, ValueError) as e:
        _log(f"Помилка: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
