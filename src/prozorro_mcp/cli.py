"""Command line: `prozorro-mcp` (MCP server over stdio) and sync/search/summary/export/docs/calibrate/filters."""

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
    db = Database(s.db_path)
    f = _filter(s, db, args.filter)
    async with ProzorroClient(s) as client:
        syncer = Syncer(client, db, f, s.concurrency, progress=_log)
        if args.resume:
            stats = await syncer.sync(resume=True, max_pages=args.max_pages)
        else:
            stats = await syncer.sync(
                resolve_since(args.since, db, f.name),
                only_new=not args.all_modified,
                until=parse_since(args.until) if args.until else None,
                max_pages=args.max_pages,
            )
    ensure_matches(db, f)
    _dump(stats)


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
    rows = export_tenders(tenders, path, f, include_summary=not args.no_summary, period_note=_period_note(args))
    _dump({"path": str(path), "filter": f.name, "rows": rows})


async def _docs(args: argparse.Namespace, s: Settings) -> None:
    from .documents import DocumentDownloader, prune_folders

    db = Database(s.db_path)
    root = s.output_dir / "Документи"
    f = _filter(s, db, args.filter)
    if args.prune:
        keep = {t.get("tenderID") for t in select_tenders(db, TenderQuery(profile=f.name))}
        result = prune_folders(root, keep, confirm=args.yes)
        if not args.yes:
            _log("Нічого не видалено. Щоб видалити ці теки, повторіть команду з --yes.")
        _dump({"filter": f.name, **result})
        return

    refs = list(args.tenders)
    if args.ids_file:
        lines = Path(args.ids_file).expanduser().read_text(encoding="utf-8").splitlines()
        refs += [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if not refs:
        q = _query(args, f.name)
        q.limit = args.max_tenders
        refs = [t["id"] for t in select_tenders(db, q)]
    out, errors = [], []
    async with ProzorroClient(s) as client:
        downloader = DocumentDownloader(client, root, s.doc_hosts, s.concurrency)
        for i, ref in enumerate(refs, start=1):
            stored = db.get_tender(ref.strip())
            try:
                tender = await client.get_tender(stored["id"] if stored else ref.strip())
                r = await downloader.download_tender(tender, include_signatures=args.signatures, include_bids=args.bids)
            except (NotFound, ProzorroError) as e:
                errors.append({"tender": ref, "error": str(e) if stored else f"не знайдено в базі чи Prozorro: {e}"})
                _log(f"[{i}/{len(refs)}] {ref}: помилка {e}")
                continue
            _log(
                f"[{i}/{len(refs)}] {r.tender_id}: +{len(r.downloaded)} / "
                f"пропущено {r.skipped} / помилок {len(r.failed)}"
            )
            out.append(r.__dict__)
    _dump({"tenders": out, "errors": errors})


async def _calibrate(args: argparse.Namespace, s: Settings) -> None:
    from .calibrate import build_report, write_report

    db = Database(s.db_path)
    f = _filter(s, db, args.filter)
    async with ProzorroClient(s) as client:
        report = await build_report(
            db,
            client if args.sample else None,
            f,
            created_from=args.created_from,
            created_to=args.created_to,
            sample=args.sample,
            concurrency=s.concurrency,
            progress=_log,
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
            "rejected_checked": report["rejected_checked"],
            "rows": rows,
        }
    )


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
    p_sync = p

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
    p_docs.add_argument("--yes", action="store_true", help="підтвердити видалення для --prune")

    p_cal = sub.add_parser("calibrate", help="звіт для калібрування фільтра (Excel для позначок)")
    p_cal.add_argument("--created-from", help=f"тендери, оголошені з ({DATE_HELP})")
    p_cal.add_argument("--created-to", help="оголошені до (не включно)")
    p_cal.add_argument(
        "--sample", type=int, default=300, help="скільки відкинутих тендерів перевірити повторно (0 — без мережі)"
    )
    p_cal.add_argument("-o", "--output", help="шлях до .xlsx")

    p_filters = sub.add_parser("filters", help="профілі фільтрів; з назвою — зробити активним")
    p_filters.add_argument("use", nargs="?", help="назва профілю, який зробити активним")

    for p in (p_sync, p_search, p_summary, p_export, p_docs, p_cal):
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
        elif args.cmd == "filters":
            _filters(args, s)
        else:
            from .server import run

            run()
    except (SyncError, ValueError) as e:
        _log(f"Помилка: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
