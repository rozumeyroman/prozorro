"""End-to-end: a real MCP client starts the server over stdio; the server talks HTTP to a fake Prozorro API."""

import json
import sys

from fake_prozorro import FakeProzorro, demo_tenders
from mcp import Client
from mcp.client.stdio import StdioServerParameters


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def test_mcp_stdio_end_to_end(tmp_path):
    fake = FakeProzorro(demo_tenders())
    server, api_url = fake.serve()
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "prozorro_mcp.cli", "serve"],
            env={
                "PROZORRO_API_URL": api_url,
                "PROZORRO_DB": str(tmp_path / "test.db"),
                "PROZORRO_OUTPUT_DIR": str(tmp_path / "out"),
            },
        )
        async with Client(params, read_timeout_seconds=60) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"status", "sync_tenders", "search_tenders", "get_tender", "list_documents"} <= names

            stats = payload(await client.call_tool("sync_tenders", {"since": "today"}))
            assert stats["relevant_found"] == 3

            found = payload(await client.call_tool("search_tenders", {"sort": "value_desc"}))
            assert found["total"] == 3
            top = found["results"][0]
            assert top["title"] == "Серверне обладнання"

            card = payload(await client.call_tool("get_tender", {"tender": top["url"]}))
            assert card["tenderID"] == top["tender_id"] and card["filter"]["relevant"]

            st = payload(await client.call_tool("status", {}))
            assert st["counts"]["relevant_tenders"] == 3

            xlsx = payload(await client.call_tool("export_excel", {"stage": "all", "file_name": "звіт"}))
            assert xlsx["path"].endswith("звіт.xlsx") and xlsx["rows"]["Тендери"] == 3
            assert (tmp_path / "out" / "Експорт" / "звіт.xlsx").exists()

            docs = payload(
                await client.call_tool("download_documents", {"stage": "complete", "include_bid_documents": True})
            )
            assert docs["tenders"] == 1 and docs["files_downloaded"] == 5 and docs["files_failed"] == 0
            docs = payload(await client.call_tool("download_documents", {"stage": "active"}))
            assert docs["tenders"] == 2 and docs["files_downloaded"] == 8
    finally:
        server.shutdown()
