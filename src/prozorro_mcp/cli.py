"""Command line: `prozorro-mcp` (MCP server over stdio), `prozorro-mcp sync`, `prozorro-mcp search`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .client import ProzorroClient
from .db import Database
from .filter import TenderFilter
from .settings import Settings
from .sync import Syncer, parse_since


def _dump(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


async def _sync(args: argparse.Namespace, s: Settings) -> None:
    db = Database(s.db_path)
    async with ProzorroClient(s) as client:
        syncer = Syncer(
            client,
            db,
            TenderFilter.from_file(s.filter_config),
            s.concurrency,
            progress=lambda m: print(m, file=sys.stderr),
        )
        stats = await syncer.sync(parse_since(args.since), only_new=not args.all_modified, max_pages=args.max_pages)
    _dump(stats)


def _export(args: argparse.Namespace, s: Settings) -> None:
    from datetime import datetime
    from pathlib import Path

    from .export import export_tenders
    from .selection import TenderQuery, select_tenders
    from .settings import KYIV_TZ

    q = TenderQuery(stage=args.stage, created_from=args.created_from, awarded_from=args.awarded_from, topic=args.topic)
    tenders = select_tenders(Database(s.db_path), q)
    path = (
        Path(args.output)
        if args.output
        else s.output_dir / "Експорт" / f"prozorro_{datetime.now(KYIV_TZ):%Y-%m-%d_%H%M}.xlsx"
    )
    _dump({"path": str(path), "rows": export_tenders(tenders, path, TenderFilter.from_file(s.filter_config))})


async def _docs(args: argparse.Namespace, s: Settings) -> None:
    from .documents import DocumentDownloader
    from .selection import TenderQuery, select_tenders

    db = Database(s.db_path)
    if args.tender:
        refs = [args.tender]
    else:
        q = TenderQuery(
            stage=args.stage, created_from=args.created_from, awarded_from=args.awarded_from, limit=args.max_tenders
        )
        refs = [t["id"] for t in select_tenders(db, q)]
    out = []
    async with ProzorroClient(s) as client:
        downloader = DocumentDownloader(client, s.output_dir / "Документи", s.doc_hosts, s.concurrency)
        for ref in refs:
            stored = db.get_tender(ref)
            tender = await client.get_tender(stored["id"] if stored else ref)
            r = await downloader.download_tender(tender, include_bids=args.bids)
            print(
                f"{r.tender_id}: +{len(r.downloaded)} / пропущено {r.skipped} / помилок {len(r.failed)}",
                file=sys.stderr,
            )
            out.append(r.__dict__)
    _dump(out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prozorro-mcp")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="запустити MCP-сервер (stdio); команда за замовчуванням")
    p_sync = sub.add_parser("sync", help="синхронізувати тендери за період")
    p_sync.add_argument("--since", default="today", help="today | yesterday | 24h | 3d | 2026-10-06 | ISO-час")
    p_sync.add_argument("--all-modified", action="store_true", help="також старші тендери, змінені за період")
    p_sync.add_argument("--max-pages", type=int, default=None, help="обмеження сторінок фіду (для тестів)")
    p_export = sub.add_parser("export", help="вивантажити тендери з бази в Excel")
    p_export.add_argument("--stage", choices=["active", "complete", "all"], default="all")
    p_export.add_argument("--created-from")
    p_export.add_argument("--awarded-from")
    p_export.add_argument("--topic")
    p_export.add_argument("-o", "--output", help="шлях до .xlsx (за замовчуванням у теці експорту)")
    p_docs = sub.add_parser("docs", help="завантажити тендерну документацію")
    p_docs.add_argument("tender", nargs="?", help="id, UA-… або посилання; без нього діють фільтри")
    p_docs.add_argument("--stage", choices=["active", "complete", "all"], default="all")
    p_docs.add_argument("--created-from")
    p_docs.add_argument("--awarded-from")
    p_docs.add_argument("--bids", action="store_true", help="також документи пропозицій учасників")
    p_docs.add_argument("--max-tenders", type=int, default=20)
    p_search = sub.add_parser("search", help="пошук у локальній базі")
    p_search.add_argument("query", nargs="?")
    p_search.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    if args.cmd == "sync":
        asyncio.run(_sync(args, s))
    elif args.cmd == "export":
        _export(args, s)
    elif args.cmd == "docs":
        asyncio.run(_docs(args, s))
    elif args.cmd == "search":
        rows, total = Database(s.db_path).search(query=args.query, limit=args.limit)
        _dump({"total": total, "results": rows})
    else:
        from .server import run

        run()


if __name__ == "__main__":
    main()
