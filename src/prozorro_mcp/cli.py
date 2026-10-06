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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prozorro-mcp")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="запустити MCP-сервер (stdio); команда за замовчуванням")
    p_sync = sub.add_parser("sync", help="синхронізувати тендери за період")
    p_sync.add_argument("--since", default="today", help="today | yesterday | 24h | 3d | 2026-10-06 | ISO-час")
    p_sync.add_argument("--all-modified", action="store_true", help="також старші тендери, змінені за період")
    p_sync.add_argument("--max-pages", type=int, default=None, help="обмеження сторінок фіду (для тестів)")
    p_search = sub.add_parser("search", help="пошук у локальній базі")
    p_search.add_argument("query", nargs="?")
    p_search.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    s = Settings.from_env()
    if args.cmd == "sync":
        asyncio.run(_sync(args, s))
    elif args.cmd == "search":
        rows, total = Database(s.db_path).search(query=args.query, limit=args.limit)
        _dump({"total": total, "results": rows})
    else:
        from .server import run

        run()


if __name__ == "__main__":
    main()
