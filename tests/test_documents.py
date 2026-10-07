import json

from conftest import NOW
from fake_prozorro import make_tender

from prozorro_mcp.client import ProzorroClient
from prozorro_mcp.documents import DocumentDownloader, safe_name, tender_folder_name
from prozorro_mcp.settings import Settings


def test_safe_name():
    assert safe_name("Додаток 1/2: ціни?.xlsx") == "Додаток 1 2 ціни .xlsx"
    assert safe_name("../../evil.txt") == "evil.txt"
    assert safe_name("CON.txt") == "_CON.txt"
    assert safe_name("   ") == "без назви"
    assert len(safe_name("а" * 300, 50)) == 51  # 50 chars + ellipsis


def test_folder_name(tenders):
    t = tenders[0]
    t["procuringEntity"]["name"] = "ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ «Тест»"
    name = tender_folder_name(t)
    assert name == f"ТОВ «Тест» - Закупівля комутаторів для ЦОД - {t['tenderID']}"


async def download(fake, tender, tmp_path, **kw):
    settings = Settings(api_url="http://fake/api/2.5", max_retries=0)
    async with ProzorroClient(settings, transport=fake.transport()) as client:
        data = await client.get_tender(tender["id"])
        return await DocumentDownloader(client, tmp_path, ("prozorro.gov.ua",)).download_tender(data, **kw)


async def test_download_tender_documents(fake, tenders, tmp_path):
    switches = next(t for t in tenders if t["title"] == "Закупівля комутаторів для ЦОД")
    r = await download(fake, switches, tmp_path, include_bids=True)
    folder = tmp_path / tender_folder_name(switches)
    assert r.folder == str(folder) and not r.failed
    files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    assert files == [
        "_documents.json",
        "evil.txt",
        "Додаток 1 2 ціни .xlsx",
        "Пропозиції учасників/ТОВ Мережеві Рішення (12345678)/Технічна пропозиція.pdf",
        "Тендерна документація.docx",
        "Технічні вимоги.pdf",
    ]
    # latest version of a document that has two versions; redirects are followed
    assert (folder / "Технічні вимоги.pdf").read_bytes().startswith(b"%PDF-fake /files/")
    manifest = json.loads((folder / "_documents.json").read_text(encoding="utf-8"))
    assert manifest["tenderID"] == switches["tenderID"] and len(manifest["documents"]) == 5

    again = await download(fake, switches, tmp_path, include_bids=True)
    assert again.downloaded == [] and again.skipped == 5


async def test_signatures_optional_and_host_check(fake, tmp_path):
    t = make_tender("Тест", [("32420000-3", "Комутатор")], 900_000, created=NOW)
    t["documents"].append(
        {**t["documents"][0], "id": "x" * 32, "url": "https://evil.example.com/a.pdf", "title": "a.pdf"}
    )
    fake.tenders[t["id"]] = t
    r = await download(fake, t, tmp_path, include_signatures=True)
    assert "sign.p7s" in r.downloaded
    assert len(r.failed) == 1 and "хост не дозволено" in r.failed[0]["error"]
