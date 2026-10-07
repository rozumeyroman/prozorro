"""CLI against a fake Prozorro API served over real HTTP."""

import json

from fake_prozorro import FakeProzorro, demo_tenders

from prozorro_mcp import cli


def run(capsys, *argv):
    cli.main(list(argv))
    return json.loads(capsys.readouterr().out)


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    fake = FakeProzorro(demo_tenders())
    server, api_url = fake.serve()
    try:
        monkeypatch.setenv("PROZORRO_API_URL", api_url)
        monkeypatch.setenv("PROZORRO_DB", str(tmp_path / "db.sqlite"))
        monkeypatch.setenv("PROZORRO_OUTPUT_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("PROZORRO_FILTER", "it-infrastructure")

        stats = run(capsys, "sync", "--since", "today")
        assert stats["relevant_found"] == 3

        found = run(capsys, "search", "--stage", "active", "--sort", "value_desc")
        assert found["total"] == 2 and found["results"][0]["title"] == "Серверне обладнання"
        assert run(capsys, "search", "--created-to", "2000-01-01")["total"] == 0

        summary = run(capsys, "summary", "--period-from", "today", "--period-mode", "either")
        assert summary["tenders"] == 3 and summary["awards"] == 1

        xlsx = run(capsys, "export", "--stage", "complete", "-o", str(tmp_path / "x.xlsx"))
        assert xlsx["rows"]["Тендери"] == 1 and xlsx["rows"]["Аналітика"] > 0

        ids = [
            t["tenderID"] for t in fake.tenders.values() if t["title"] in ("Серверне обладнання", "Антивірусний захист")
        ]
        docs = run(capsys, "docs", *ids)
        assert len(docs["tenders"]) == 2 and not docs["errors"]
        ids_file = tmp_path / "ids.txt"
        ids_file.write_text("# список\n" + "\n".join(ids) + "\nUA-2000-01-01-000000-a\n", encoding="utf-8")
        docs = run(capsys, "docs", "--ids-file", str(ids_file))
        assert all(not t["downloaded"] for t in docs["tenders"]) and len(docs["errors"]) == 1

        # switching to the cybersecurity profile leaves the servers tender folder stale
        run(capsys, "filters", "cybersecurity")
        dry = run(capsys, "docs", "--prune")
        assert dry["count"] == 1
        done = run(capsys, "docs", "--prune", "--yes")
        assert done["count"] == 1
    finally:
        server.shutdown()


def test_cli_offers(tmp_path, monkeypatch, capsys):
    fake = FakeProzorro(demo_tenders())
    server, api_url = fake.serve()
    try:
        monkeypatch.setenv("PROZORRO_API_URL", api_url)
        monkeypatch.setenv("PROZORRO_DB", str(tmp_path / "db.sqlite"))
        monkeypatch.setenv("PROZORRO_OUTPUT_DIR", str(tmp_path / "out"))
        monkeypatch.setenv("PROZORRO_FILTER", "it-infrastructure")
        run(capsys, "sync", "--since", "today")

        # without tender ids: completed tenders of the selection
        prepared = run(capsys, "offers", "prepare")
        (t,) = prepared["tenders"]
        assert t["winners"] == ['ТОВ "Мережеві Рішення"'] and t["documents"] == 1
        assert (tmp_path / "out" / "Документи" / t["folder"].rsplit("/", 1)[-1] / "_winner.json").exists()

        rows = tmp_path / "rows.json"
        rows.write_text(
            json.dumps(
                [{"tender": t["tenderID"], "rows": [{"vendor": "Cisco", "part_number": "C9300-48P-E", "quantity": 10}]}]
            ),
            encoding="utf-8",
        )
        assert run(capsys, "offers", "save", str(rows))[0]["saved_rows"] == 1
        listed = run(capsys, "offers", "list", "--vendor", "cisco")
        assert listed[0]["supplier"] == 'ТОВ "Мережеві Рішення"'
        out = run(capsys, "offers", "list", "-o", str(tmp_path / "o.xlsx"))
        assert out["rows"] == 1
    finally:
        server.shutdown()
