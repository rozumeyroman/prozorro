"""Winner documents: rules from config/winner-docs.yaml, .p7s duplicates, contracts from the contracting module,
batch download, manifest after every file, pruning, fetching skipped documents."""

import json
from datetime import datetime

import pytest
from docfiles import make_pdf, make_zip, sign
from fake_prozorro import _doc
from test_offers import complete_tender, doc_key

from prozorro_mcp.batch import download_batch, fetch_skipped, prune_winner_docs
from prozorro_mcp.client import ProzorroClient
from prozorro_mcp.db import Database
from prozorro_mcp.documents import MANIFEST, DocumentDownloader, safe_name, tender_folder_name
from prozorro_mcp.offers import SUMMARY_FILE, prepare_offer, text_for_folder
from prozorro_mcp.settings import Settings
from prozorro_mcp.winner_docs import WinnerDocRules


@pytest.fixture
def rules():
    return WinnerDocRules.from_file()


@pytest.mark.parametrize(
    "title, doc_type, keep",
    [
        ("Авторизаційний лист Fortinet.pdf", "qualificationDocuments", True),
        ("MAF Cisco.pdf", "eligibilityDocuments", True),
        ("Лист партнера.pdf", None, True),
        ("Технічна пропозиція.docx", None, True),
        ("Відповідність технічним вимогам.pdf", "eligibilityDocuments", True),
        ("Специфікація.xlsx", "qualificationDocuments", True),
        ("Цінова пропозиція.pdf", None, True),
        ("Комерційна пропозиція.pdf.p7s", None, True),  # a signed document, nothing next to it
        ("Довідка про досвід.pdf", "qualificationDocuments", False),
        ("Статут.pdf", None, False),
        ("Витяг з ЄДР.pdf", None, False),
        ("Банківська гарантія.pdf", None, False),
        ("Довідка про відсутність судимості.pdf", "eligibilityDocuments", False),
        ("Будь-що.pdf", "technicalSpecifications", True),
        ("Будь-що.pdf", "commercialProposal", True),
        ("Інше.pdf", None, True),  # no type, nothing that says it is a certificate
        ("Документи.zip", None, False),  # archives only with a keep keyword
        ("Специфікація та листи.zip", None, True),
        ("sign.p7s", None, False),  # detached signature
    ],
)
def test_rules(rules, title, doc_type, keep):
    doc = {"title": title, "documentType": doc_type} if doc_type else {"title": title}
    assert rules.classify(doc)[0] is keep


def test_p7s_duplicate_is_skipped(rules):
    siblings = {"цінова пропозиція.pdf", "цінова пропозиція.pdf.p7s"}
    ok, why = rules.classify({"title": "Цінова пропозиція.pdf.p7s"}, siblings)
    assert not ok and "без .p7s" in why
    assert rules.classify({"title": "Цінова пропозиція.pdf"}, siblings)[0]


def test_rules_are_read_from_the_config(tmp_path):
    cfg = tmp_path / "w.yaml"
    cfg.write_text("keep_patterns:\n  x: 'досвід'\nskip_types: [qualificationDocuments]\n", encoding="utf-8")
    r = WinnerDocRules.from_file(cfg)
    assert r.classify({"title": "Довідка про досвід.pdf", "documentType": "qualificationDocuments"})[0]
    assert not r.classify({"title": "Специфікація.pdf", "documentType": "qualificationDocuments"})[0]


def winner_tender(fake):
    """The finished demo tender with a realistic winner bid: needed and unneeded files, a .p7s duplicate,
    a zip, and a contract whose documents live only in the contracting module."""
    t = complete_tender(fake)
    created = datetime.fromisoformat(t["awards"][0]["date"])
    bid = t["bids"][0]
    pdf = _doc("Цінова пропозиція форма.pdf", None, "application/pdf", created)
    dup = _doc("Цінова пропозиція форма.pdf.p7s", None, "application/pkcs7-signature", created)
    maf = _doc("Авторизаційний лист Cisco.pdf", "qualificationDocuments", "application/pdf", created)
    statute = _doc("Статут.pdf", None, "application/pdf", created)
    experience = _doc("Довідка про досвід.pdf", "qualificationDocuments", "application/pdf", created)
    archive = _doc("Специфікації.zip", None, "application/zip", created)
    bid["documents"] += [pdf, dup, maf, statute, experience, archive]
    fake.contents[doc_key(maf)] = make_pdf("Cisco authorizes Merezhevi Rishennia")
    fake.contents[doc_key(archive)] = make_zip(
        {"Специфікація 1.pdf": make_pdf("Cisco C9300"), "Статут.pdf": make_pdf("statute"), "довідка.pdf": b"%PDF"}
    )
    # contracting module: GET /contracts/{id}
    contract = t["contracts"][0]
    contract["documents"] = []
    annex = _doc("Додаток 2 Специфікація.pdf", "contractAnnexe", "application/pdf", created)
    annex_sig = _doc("Додаток 2 Специфікація.pdf.p7s", None, "application/pkcs7-signature", created)
    act = _doc("Акт приймання.pdf", None, "application/pdf", created)
    fake.contracts[contract["id"]] = {"id": contract["id"], "documents": [annex, annex_sig, act]}
    fake.contents[doc_key(annex)] = make_pdf("Specification: Cisco C9300-48P-E x 10")
    return t


def settings(tmp_path, **kw):
    return Settings(api_url="http://fake/api/2.5", max_retries=0, output_dir=tmp_path, feed_retry_delay=0, **kw)


def files_in(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file() and "_text" not in p.parts)


async def test_minimal_winner_docs(fake, tmp_path):
    t = winner_tender(fake)
    s = settings(tmp_path)
    db = Database(":memory:")
    async with ProzorroClient(s, transport=fake.transport()) as c:
        r = await prepare_offer(c, s, db, await c.get_tender(t["id"]))
    folder = tmp_path / "Документи" / tender_folder_name(t)
    files = files_in(folder)
    bid_dir = r["winners"][0]["documents_subdir"]
    assert f"{bid_dir}/Авторизаційний лист Cisco.pdf" in files
    assert f"{bid_dir}/Цінова пропозиція форма.pdf" in files
    assert f"{bid_dir}/Цінова пропозиція.docx" in files  # signed container unwrapped
    assert f"{bid_dir}/Специфікації (розпаковано)/Специфікація 1.pdf" in files
    assert not any("Статут" in f or "досвід" in f or f.endswith(".p7s") or f.endswith(".zip") for f in files)
    # contract documents come from /contracts/{id}
    assert "Договори/Додаток 2 Специфікація.pdf" in files and not any("Акт" in f for f in files)
    assert any(p.endswith(f"/contracts/{t['contracts'][0]['id']}") for p in fake.requests)

    summary = json.loads((folder / SUMMARY_FILE).read_text(encoding="utf-8"))
    skipped = {d["title"]: d for d in summary["skipped_documents"]}
    assert {"Статут.pdf", "Довідка про досвід.pdf", "Цінова пропозиція форма.pdf.p7s", "Акт приймання.pdf"} <= set(
        skipped
    )
    assert all(d["status"] == "skipped" and d["url"] for d in skipped.values())
    assert summary["vendor_mentions"]["Cisco"] >= 3
    assert any("Специфікація 1.pdf" in d["file"] for d in summary["documents"])

    # fetch a skipped document on demand
    async with ProzorroClient(s, transport=fake.transport()) as c:
        got = await fetch_skipped(c, s, db, t["id"], ["Статут.pdf"])
    assert got["downloaded"] == [f"{bid_dir}/Статут.pdf"]
    summary = json.loads((folder / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert "Статут.pdf" not in {d["title"] for d in summary["skipped_documents"]}


async def test_all_winner_docs_mode(fake, tmp_path):
    t = winner_tender(fake)
    s = settings(tmp_path)
    async with ProzorroClient(s, transport=fake.transport()) as c:
        r = await prepare_offer(c, s, Database(":memory:"), await c.get_tender(t["id"]), winner_docs="all")
    files = files_in(tmp_path / "Документи" / tender_folder_name(t))
    assert any("Статут.pdf" in f for f in files) and r["skipped_documents"] == []


async def test_prune_winner_docs(fake, tmp_path, rules):
    t = winner_tender(fake)
    s = settings(tmp_path)
    async with ProzorroClient(s, transport=fake.transport()) as c:
        await prepare_offer(c, s, Database(":memory:"), await c.get_tender(t["id"]), winner_docs="all")
    root = tmp_path / "Документи"
    folder = root / tender_folder_name(t)
    dry = prune_winner_docs(root, rules)
    assert dry["files"] >= 3 and any("Статут" in f for f in dry["would_delete"][0]["files"])
    assert any("Статут" in f for f in files_in(folder))
    done = prune_winner_docs(root, rules, confirm=True)
    assert done["files"] == dry["files"]
    files = files_in(folder)
    assert not any("Статут" in f or "досвід" in f or "Акт" in f for f in files)
    assert any("Авторизаційний лист" in f for f in files)
    manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))["documents"]
    assert not any(e["title"] == "Статут.pdf" for e in manifest.values())
    summary = json.loads((folder / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert "Статут.pdf" in {d["title"] for d in summary["skipped_documents"]}
    assert prune_winner_docs(root, rules)["files"] == 0


async def test_batch_with_winners_and_remaining(fake, tender_filter, tmp_path):
    t = winner_tender(fake)
    other = next(x for x in fake.tenders.values() if x["title"] == "Серверне обладнання")
    s = settings(tmp_path)
    db = Database(":memory:")
    lines = []
    async with ProzorroClient(s, transport=fake.transport()) as c:
        out = await download_batch(
            c, s, db, [t["id"], other["id"]], with_winners=True, extract=False, progress=lines.append
        )
    assert out["tenders"] == 2 and not out["errors"]
    assert any(line.startswith("[2/2] UA-") and "файлів" in line and "МБ" in line for line in lines)
    folder = tmp_path / "Документи" / tender_folder_name(t)
    names = files_in(folder)
    assert "Технічні вимоги.pdf" in names  # tender documentation in the same pass
    assert any(f.startswith("Договори/") for f in names)
    summary = json.loads((folder / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert summary["text_extracted"] is False and not (folder / "_text").exists()
    tender_requests = sum(1 for p in fake.requests if p == f"/api/2.5/tenders/{t['id']}")
    assert tender_requests == 1  # one tender card per tender

    # text later, offline, for the winner documents only
    r = text_for_folder(folder)
    assert r["vendor_mentions"]["Cisco"] >= 2 and (folder / "_text").exists()

    # --remaining: the finished tender is not requested again; the active one is (it may get a winner)
    fake.requests.clear()
    async with ProzorroClient(s, transport=fake.transport()) as c:
        out = await download_batch(
            c,
            s,
            db,
            [t["tenderID"], other["tenderID"]],
            with_winners=True,
            extract=False,
            remaining=True,
        )
    assert out["already_complete"] == [t["tenderID"]]
    assert not any(p == f"/api/2.5/tenders/{t['id']}" for p in fake.requests)


async def test_manifest_is_written_after_every_file(fake, tenders, tmp_path):
    t = next(x for x in tenders if x["title"] == "Закупівля комутаторів для ЦОД")
    bad = doc_key(t["documents"][1])
    fake.fail_files = {bad}
    s = settings(tmp_path)
    async with ProzorroClient(s, transport=fake.transport()) as c:
        dl = DocumentDownloader(c, tmp_path, ("prozorro.gov.ua",), concurrency=1)
        r = await dl.download_tender(await c.get_tender(t["id"]), mode={"tender_docs": True})
    folder = tmp_path / tender_folder_name(t)
    manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    assert len(r.failed) == 1 and manifest["complete"] is False
    assert len(manifest["documents"]) == len(r.downloaded) >= 2
    assert not list(folder.rglob("*.part"))

    fake.fail_files = set()
    fake.requests.clear()
    async with ProzorroClient(s, transport=fake.transport()) as c:
        dl = DocumentDownloader(c, tmp_path, ("prozorro.gov.ua",))
        r2 = await dl.download_tender(await c.get_tender(t["id"]), mode={"tender_docs": True})
    assert len(r2.downloaded) == 1 and r2.skipped == len(r.downloaded)
    assert json.loads((folder / MANIFEST).read_text(encoding="utf-8"))["complete"] is True


async def test_manifest_names_are_reconciled(fake, tenders, tmp_path):
    """A manifest entry with a leading space and no extension, while the file on disk has .pdf: not again."""
    t = next(x for x in tenders if x["title"] == "Закупівля комутаторів для ЦОД")
    s = settings(tmp_path)
    async with ProzorroClient(s, transport=fake.transport()) as c:
        dl = DocumentDownloader(c, tmp_path, ("prozorro.gov.ua",))
        await dl.download_tender(await c.get_tender(t["id"]))
    folder = tmp_path / tender_folder_name(t)
    manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    entry = next(e for e in manifest["documents"].values() if e["file"] == "Технічні вимоги.pdf")
    entry["file"] = " Технічні вимоги"
    (folder / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    fake.requests.clear()
    async with ProzorroClient(s, transport=fake.transport()) as c:
        dl = DocumentDownloader(c, tmp_path, ("prozorro.gov.ua",))
        r = await dl.download_tender(await c.get_tender(t["id"]))
    assert r.downloaded == []
    manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    assert any(e["file"] == "Технічні вимоги.pdf" for e in manifest["documents"].values())


async def test_folder_with_line_break_is_renamed(fake, tenders, tmp_path):
    t = next(x for x in tenders if x["title"] == "Закупівля комутаторів для ЦОД")
    good = tender_folder_name(t)
    broken = tmp_path / good.replace(f" - {t['tenderID']}", f"\n - {t['tenderID']}")
    broken.mkdir()
    (broken / MANIFEST).write_text(json.dumps({"tenderID": t["tenderID"], "documents": {}}), encoding="utf-8")
    s = settings(tmp_path)
    async with ProzorroClient(s, transport=fake.transport()) as c:
        dl = DocumentDownloader(c, tmp_path, ("prozorro.gov.ua",))
        await dl.download_tender(await c.get_tender(t["id"]))
    assert not broken.exists() and (tmp_path / good / MANIFEST).exists()
    assert "\n" not in good and safe_name("Комп'ютери (30230000-0 …)\n - UA") == "Комп'ютери (30230000-0 …) - UA"


def test_title_without_extension_gets_one():
    from prozorro_mcp.documents import unique_file_name

    assert unique_file_name(" Пропозиція", "abc", set(), "application/pdf") == "Пропозиція.pdf"
    assert unique_file_name("Пропозиція.docx", "abc", set(), "application/pdf") == "Пропозиція.docx"


async def test_signed_contract_annex_inside_p7s(fake, tmp_path):
    """Only a signed container (no plain PDF next to it): downloaded and the PDF taken out of it."""
    t = winner_tender(fake)
    cid = t["contracts"][0]["id"]
    created = datetime.fromisoformat(t["awards"][0]["date"])
    only_signed = _doc("Специфікація до договору.pdf.p7s", None, "application/pkcs7-signature", created)
    fake.contracts[cid]["documents"] = [only_signed]
    fake.contents[doc_key(only_signed)] = sign(make_pdf("Spec: FortiGate-200F x 2"))
    s = settings(tmp_path)
    async with ProzorroClient(s, transport=fake.transport()) as c:
        r = await prepare_offer(c, s, Database(":memory:"), await c.get_tender(t["id"]))
    files = {d["file"]: d for d in r["documents"]}
    assert "Договори/Специфікація до договору.pdf" in files
    assert "FortiGate" in r["vendor_mentions"]
