import json

from docfiles import make_docx, make_pdf, sign
from fake_prozorro import _doc, _value
from openpyxl import load_workbook
from test_sync import run_sync

from prozorro_mcp.client import ProzorroClient
from prozorro_mcp.db import Database
from prozorro_mcp.export import export_tenders
from prozorro_mcp.offers import normalize_rows, prepare_offer, winners
from prozorro_mcp.settings import Settings


def doc_key(d):
    return d["url"].rsplit("/", 1)[-1]


def complete_tender(fake):
    """The finished demo tender, with what real winners publish: criteria responses, a signed price proposal,
    a contract with unit prices and its annex."""
    t = next(t for t in fake.tenders.values() if t["status"] == "complete")
    created = t["awards"][0]["date"]
    from datetime import datetime

    created = datetime.fromisoformat(created)
    bid = t["bids"][0]
    bid["requirementResponses"] = [
        {"requirement": {"title": "Строк поставки"}, "value": "30 днів"},
        {"requirement": {"title": "Виробник товару"}, "value": "Cisco Systems"},
        {"requirement": {"title": "Модель"}, "value": "Catalyst 9300-48P"},
    ]
    price = _doc("Цінова пропозиція.docx.p7s", "commercialProposal", "application/pkcs7-signature", created)
    bid["documents"].append(price)
    fake.contents[doc_key(price)] = sign(
        make_docx(["Цінова пропозиція"], [["Товар", "К-сть", "Ціна"], ["Cisco C9300-48P-E", "10", "98000"]])
    )
    tech = bid["documents"][0]
    fake.contents[doc_key(tech)] = make_pdf("Cisco Catalyst C9300-48P-E, DNA Advantage 3Y")
    contract = t["contracts"][0]
    contract["awardID"] = t["awards"][0]["id"]
    contract["items"] = [
        {"id": t["items"][0]["id"], "quantity": 10, "unit": {"name": "штука", "value": _value(97_500.0)}}
    ]
    annex = _doc("Додаток 1 Специфікація.pdf", "contractAnnexe", "application/pdf", created)
    contract["documents"] = [annex]
    fake.contents[doc_key(annex)] = make_pdf("Specification: Cisco C9300-48P-E x 10, 97500 UAH")
    # a losing bid's documents must not be downloaded
    t["bids"][1]["documents"] = [_doc("Пропозиція конкурента.pdf", None, "application/pdf", created)]
    return t


def test_winners_structure(fake):
    t = complete_tender(fake)
    (w,) = winners(t)
    assert w["supplier"] == 'ТОВ "Мережеві Рішення"' and w["supplier_edrpou"] == "12345678"
    assert w["items"][0]["unit_price"]["amount"] == 97_500.0 and w["items"][0]["price_source"] == "договір"
    # product-related criteria responses first
    assert [r["requirement"] for r in w["requirement_responses"]][:2] == ["Виробник товару", "Модель"]
    kinds = {d["title"]: d["kind"] for d in w["documents"]}
    assert kinds["Цінова пропозиція.docx.p7s"] == "price" and kinds["Технічна пропозиція.pdf"] == "technical"


async def test_prepare_save_and_export(fake, tender_filter, tmp_path):
    db = Database(":memory:")
    await run_sync(fake, tender_filter, db)
    complete_tender(fake)
    settings = Settings(api_url="http://fake/api/2.5", max_retries=0, output_dir=tmp_path)
    t = next(t for t in fake.tenders.values() if t["status"] == "complete")
    async with ProzorroClient(settings, transport=fake.transport()) as c:
        tender = await c.get_tender(t["id"])
        r = await prepare_offer(c, settings, db, tender)

    files = {d["file"]: d for d in r["documents"]}
    # price proposal first; the signed DOCX was unwrapped; buyerOnly, losers' and tender documents are skipped
    assert r["documents"][0]["kind"] == "price"
    price = next(d for f, d in files.items() if f.endswith("Цінова пропозиція.docx"))
    assert "Cisco C9300-48P-E | 10 | 98000" in price["excerpt"]
    assert any(f.startswith("Договори/") and "97500" in d["excerpt"] for f, d in files.items())
    assert not any("конкурента" in f or "Комерційна таємниця" in f or "Технічні вимоги" in f for f in files)
    assert r["vendor_mentions"]["Cisco"] >= 3
    folder = tmp_path / "Документи" / r["folder"].rsplit("/", 1)[-1]
    summary = json.loads((folder / "_winner.json").read_text(encoding="utf-8"))
    assert summary["winners"][0]["supplier_edrpou"] == "12345678" and "excerpt" not in summary["documents"][0]

    rows = normalize_rows(
        tender,
        [
            {
                "tender_item": tender["items"][0]["description"],
                "vendor": "Cisco",
                "product": "Catalyst 9300 48-port PoE+, Network Advantage",
                "part_number": "C9300-48P-E",
                "quantity": 10,
                "unit_price": 97_500,
                "source": "договір",
                "confidence": "висока",
            }
        ],
    )
    assert rows[0]["supplier"] == 'ТОВ "Мережеві Рішення"' and rows[0]["total"] == 975_000
    assert db.save_offers(tender["id"], tender["tenderID"], rows) == 1
    # saving the same lot again replaces its rows
    assert db.save_offers(tender["id"], tender["tenderID"], rows) == 1
    assert len(db.offers(tender=tender["tenderID"])) == 1
    assert db.offers(vendor="cisco")[0]["part_number"] == "C9300-48P-E"
    assert db.offers(query="9300") and not db.offers(vendor="Fortinet")

    path = tmp_path / "e.xlsx"
    counts = export_tenders([tender], path, tender_filter, offers=db.offers(tenders=[tender["id"]]))
    assert counts["Що виграло"] == 1
    ws = load_workbook(path)["Що виграло"]
    assert ws["G2"].value == "Cisco" and ws["I2"].value == "C9300-48P-E"


async def test_prepare_without_winner(fake, tender_filter, tmp_path):
    db = Database(":memory:")
    settings = Settings(api_url="http://fake/api/2.5", max_retries=0, output_dir=tmp_path)
    t = next(t for t in fake.tenders.values() if t["status"] == "active.tendering")
    async with ProzorroClient(settings, transport=fake.transport()) as c:
        r = await prepare_offer(c, settings, db, await c.get_tender(t["id"]))
    assert r["winners"] == [] and r["documents"] == [] and not (tmp_path / "Документи").exists()
