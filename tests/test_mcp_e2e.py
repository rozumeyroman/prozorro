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
                "PROZORRO_FILTER": "it-infrastructure",
            },
        )
        async with Client(params, read_timeout_seconds=60) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert {"status", "sync_tenders", "search_tenders", "get_tender", "list_documents"} <= names
            assert {"exclude_tenders", "check_coverage"} <= names

            stats = payload(await client.call_tool("sync_tenders", {"since": "today"}))
            assert stats["relevant_found"] == 3 and stats["complete"] is True and stats["reached_modified"]

            found = payload(await client.call_tool("search_tenders", {"sort": "value_desc"}))
            assert found["total"] == 3
            top = found["results"][0]
            assert top["title"] == "Серверне обладнання"

            card = payload(await client.call_tool("get_tender", {"tender": top["url"]}))
            assert card["tenderID"] == top["tender_id"] and card["filter"]["relevant"]

            st = payload(await client.call_tool("status", {}))
            assert st["counts"]["relevant_tenders"] == 3

            summary = payload(
                await client.call_tool("summarize_tenders", {"period_from": "today", "period_mode": "either"})
            )
            assert summary["tenders"] == 3 and summary["top_winners_by_amount"][0]["edrpou"] == "12345678"

            xlsx = payload(await client.call_tool("export_excel", {"stage": "all", "file_name": "звіт"}))
            assert xlsx["path"].endswith("звіт.xlsx") and xlsx["rows"]["Тендери"] == 3
            assert (tmp_path / "out" / "Експорт" / "звіт.xlsx").exists()

            docs = payload(
                await client.call_tool("download_documents", {"stage": "complete", "include_bid_documents": True})
            )
            assert docs["tenders"] == 1 and docs["files_downloaded"] == 5 and docs["files_failed"] == 0
            docs = payload(await client.call_tool("download_documents", {"stage": "active"}))
            assert docs["tenders"] == 2 and docs["files_downloaded"] == 8

            # what exactly won: winner data and documents, then the rows Claude read from them
            done = payload(await client.call_tool("search_tenders", {"stage": "complete"}))["results"][0]
            offer = payload(await client.call_tool("get_winning_offer", {"tender": done["url"]}))
            assert offer["winners"][0]["supplier_edrpou"] == "12345678"
            assert [d["kind"] for d in offer["documents"]] == ["technical"]
            text = payload(
                await client.call_tool("read_document", {"tender": done["url"], "file": offer["documents"][0]["file"]})
            )
            assert text["note"]  # the fake "PDF" has no readable text
            saved = payload(
                await client.call_tool(
                    "save_winning_offer",
                    {
                        "tender": done["url"],
                        "rows": [
                            {"vendor": "Cisco", "product": "Catalyst 9300-48P", "quantity": 10, "unit_price": 98000}
                        ],
                    },
                )
            )
            assert saved["saved_rows"] == 1 and saved["rows"][0]["supplier"] == 'ТОВ "Мережеві Рішення"'
            found = payload(await client.call_tool("list_winning_offers", {"vendor": "cisco"}))
            assert found["count"] == 1 and found["rows"][0]["total"] == 980_000
            xlsx = payload(await client.call_tool("export_excel", {"stage": "complete"}))
            assert xlsx["rows"]["Що виграло"] == 1

            # switch to the cybersecurity profile: stored tenders are re-evaluated locally
            used = payload(await client.call_tool("use_filter", {"name": "cybersecurity"}))
            assert used["active_filter"]["name"] == "cybersecurity" and used["relevant_stored_tenders"] == 1
            found = payload(await client.call_tool("search_tenders", {}))
            assert found["filter"] == "cybersecurity" and [r["title"] for r in found["results"]] == [
                "Антивірусний захист"
            ]
            # one-off override without changing the active profile
            found = payload(await client.call_tool("search_tenders", {"filter": "it-infrastructure"}))
            assert found["total"] == 3
            st = payload(await client.call_tool("status", {}))
            assert st["active_filter"]["name"] == "cybersecurity"

            # change the rules on the fly: lower the threshold so the 400k firewall licence passes
            saved = payload(
                await client.call_tool(
                    "save_filter", {"name": "cyber-300k", "base": "cybersecurity", "min_value": 300000}
                )
            )
            assert saved["active"] and saved["filter"]["min_value"].startswith("300,000")
            names = {f["name"]: f for f in payload(await client.call_tool("list_filters", {}))}
            assert names["cyber-300k"]["active"] and names["cyber-300k"]["source"] == "user"
    finally:
        server.shutdown()
