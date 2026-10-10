"""What exactly won a tender: the winning bid's prices, criteria responses and documents, and the rows Claude
fills in from them (vendor, product, part number, quantity, unit price) that are kept in the database.

Prozorro has no structured "vendor/model" field. Sources, from most to least structured:
1. bid unit prices (bids[].items[].unit.value) and contract item prices;
2. responses to tender criteria (bids[].requirementResponses), which often include manufacturer/model;
3. the winner's documents (price and technical proposals, manufacturer authorization letters) and the contract
   annexes: text is extracted here and read by Claude.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from .client import NotFound, ProzorroClient, ProzorroError
from .db import Database
from .doctext import extract_text, ocr_file
from .documents import (
    CONTRACTS_DIR,
    MANIFEST,
    DocumentDownloader,
    bid_documents,
    bid_subdir,
    find_tender_folder,
    is_public,
)
from .settings import Settings
from .summary import _org, tender_url
from .winner_docs import WinnerDocRules

WinnerDocsMode = Literal["minimal", "all"]

TEXT_DIR = "_text"
SUMMARY_FILE = "_winner.json"
HEX_ID = re.compile(r"^[0-9a-f]{32}$")
UA_ID = re.compile(r"UA-\d{4}-\d{2}-\d{2}-\d{6}-[a-z]", re.I)

PRODUCT_REQUIREMENT = re.compile(
    r"виробник|модел|марк|торгов\w* (назв|марк)|артикул|найменуванн\w* товар|країн\w* походж|"
    r"manufactur|vendor|brand|model|part ?number|\bP/?N\b|SKU",
    re.I,
)
DOC_KINDS = [
    ("price", re.compile(r"цінов|комерційн\w* пропоз|price|кошторис|калькуляц", re.I)),
    ("authorization", re.compile(r"авториз|лист\w* виробник|гарантійн\w* лист|\bMAF\b|дилер|партнер", re.I)),
    ("technical", re.compile(r"техніч|специфікац|відповідн|характеристик|tech|datasheet|паспорт", re.I)),
    ("contract", re.compile(r"договір|договор|контракт|contract|додаток|додаткова угода", re.I)),
]
DOC_TYPE_KINDS = {
    "commercialProposal": "price",
    "billOfQuantity": "price",
    "technicalSpecifications": "technical",
    "qualificationDocuments": "other",
    "contractSigned": "contract",
    "contractAnnexe": "contract",
}
KIND_ORDER = {"price": 0, "contract": 1, "technical": 2, "authorization": 3, "other": 4}
# Vendors whose names in the winner's documents hint at what was supplied (security, network, servers).
VENDORS = re.compile(
    r"\b(Fortinet|FortiGate|FortiAnalyzer|FortiMail|FortiWeb|FortiSIEM|FortiClient|FortiEDR|Palo Alto|Cortex|"
    r"Check ?Point|Sophos|ESET|Bitdefender|CrowdStrike|SentinelOne|Trellix|Skyhigh|McAfee|Trend Micro|Kaspersky|"
    r"Splunk|QRadar|Wazuh|ArcSight|Tenable|Nessus|Qualys|Rapid7|Acunetix|Invicti|Checkmarx|Positive Technologies|"
    r"Imperva|F5|Radware|Cloudflare|Akamai|CyberArk|Senhasegura|Fudo|Zscaler|Netskope|Varonis|Forcepoint|GTB|"
    r"Safetica|Zecurion|Cymulate|Picus|SafeBreach|Recorded Future|Microsoft|Cisco|Firepower|Duo|Juniper|Aruba|"
    r"HPE|Hewlett Packard|Huawei|MikroTik|Ubiquiti|Extreme Networks|Arista|Allied Telesis|Dell|Lenovo|Supermicro|"
    r"NetApp|Synology|QNAP|VMware|Veeam|Acronis|Commvault|Red Hat|Oracle|IBM|Gigamon|Infoblox|Kerio|"
    r"Vicarius|vRx|ARCON|Forestall|Hideez|TimeWise|Keysight|Ixia|One Identity|ThreatLocker|Labyrinth|"
    r"Автограф|Крипто ?сервер|Гриф|ІІТ|Інтелектуальні інформаційні технології|SecureToken|Алмаз)\b",
    re.I,
)


async def fetch_tender(client: ProzorroClient, db: Database, ref: str) -> dict[str, Any]:
    """Fresh tender from the API by internal id, UA-… id (must be in the local base) or prozorro.gov.ua link."""
    ref = ref.strip()
    hex_id = ref if HEX_ID.match(ref) else None
    if not hex_id:
        m = UA_ID.search(ref)
        stored = db.get_tender(m.group(0)[:-1].upper() + m.group(0)[-1].lower() if m else ref)
        if not stored:
            raise ValueError(f"Тендер {ref!r} не знайдено в локальній базі; передайте внутрішній id (32 hex)")
        hex_id = stored["id"]
    try:
        return await client.get_tender(hex_id)
    except NotFound as e:
        raise ValueError(f"Тендер {hex_id} не знайдено в Prozorro") from e


def doc_kind(d: dict[str, Any]) -> str:
    title = d.get("title") or ""
    for kind, rx in DOC_KINDS:
        if rx.search(title):
            return kind
    return DOC_TYPE_KINDS.get(d.get("documentType") or "", "other")


def _money(v: dict[str, Any] | None) -> dict[str, Any] | None:
    if not v or v.get("amount") is None:
        return None
    return {"amount": v["amount"], "currency": v.get("currency"), "vat_included": v.get("valueAddedTaxIncluded")}


def _response_value(rr: dict[str, Any]) -> Any:
    if rr.get("values"):
        return rr["values"]
    return rr.get("value")


def winners(tender: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per active award (winner of a lot or of the whole tender) with everything structured we know."""
    lots = {lot["id"]: lot for lot in tender.get("lots") or [] if lot.get("id")}
    bids = {b.get("id"): b for b in tender.get("bids") or []}
    contracts = {c.get("awardID"): c for c in tender.get("contracts") or [] if c.get("status") != "cancelled"}
    out = []
    for award in tender.get("awards") or []:
        if award.get("status") != "active":
            continue
        lot_id = award.get("lotID")
        bid = bids.get(award.get("bid_id")) or {}
        items = [i for i in tender.get("items") or [] if not lot_id or i.get("relatedLot") == lot_id]
        item_ids = {i.get("id") for i in items}
        bid_prices = {i.get("id"): (i.get("unit") or {}).get("value") for i in bid.get("items") or []}
        contract = contracts.get(award.get("id")) or (next(iter(contracts.values())) if len(contracts) == 1 else {})
        contract_prices = {i.get("id"): (i.get("unit") or {}).get("value") for i in contract.get("items") or []}
        rows = []
        for i in items:
            price = contract_prices.get(i.get("id")) or bid_prices.get(i.get("id"))
            rows.append(
                {
                    "item_id": i.get("id"),
                    "description": i.get("description"),
                    "cpv": (i.get("classification") or {}).get("id"),
                    "quantity": i.get("quantity"),
                    "unit": (i.get("unit") or {}).get("name"),
                    "unit_price": _money(price),
                    "price_source": "договір"
                    if contract_prices.get(i.get("id"))
                    else ("пропозиція" if price else None),
                }
            )
        responses = []
        for rr in bid.get("requirementResponses") or []:
            related = rr.get("relatedItem")
            if related and related not in item_ids:
                continue
            title = (rr.get("requirement") or {}).get("title") or rr.get("title") or ""
            responses.append(
                {"requirement": title, "value": _response_value(rr), "product": bool(PRODUCT_REQUIREMENT.search(title))}
            )
        responses.sort(key=lambda r: not r["product"])
        lot_value = next((lv.get("value") for lv in bid.get("lotValues") or [] if lv.get("relatedLot") == lot_id), None)
        supplier = _org((award.get("suppliers") or [None])[0]) or {}
        out.append(
            {
                "award_id": award.get("id"),
                "bid_id": award.get("bid_id"),
                "lot_id": lot_id,
                "lot": (lots.get(lot_id) or {}).get("title"),
                "supplier": supplier.get("name"),
                "supplier_edrpou": supplier.get("edrpou"),
                "award_value": _money(award.get("value")),
                "bid_value": _money(lot_value or bid.get("value")),
                "award_date": award.get("date"),
                "contract": {
                    "status": contract.get("status"),
                    "value": _money(contract.get("value")),
                    "dateSigned": contract.get("dateSigned"),
                    "number": contract.get("contractNumber"),
                }
                if contract
                else None,
                "items": rows,
                "requirement_responses": responses[:80],
                "documents_subdir": bid_subdir(bid) if bid else None,
                "documents": [
                    {"title": d.get("title"), "kind": doc_kind(d), "public": is_public(d)} for d in bid_documents(bid)
                ],
            }
        )
    return out


def document_text(folder: Path, rel: str) -> tuple[str, str | None]:
    """Text of a downloaded file (cached under _text/), with a note when there is little or none."""
    src = _inside(folder, rel)
    return _cached(folder, rel, src, "", lambda: extract_text(src))


def document_ocr(folder: Path, rel: str, pages: int = 2) -> tuple[str, str | None]:
    """OCR text of the first pages of a scan (cached under _text/ as *.ocr.txt)."""
    src = _inside(folder, rel)
    return _cached(folder, rel, src, ".ocr", lambda: ocr_file(src, pages=pages))


def _inside(folder: Path, rel: str) -> Path:
    src = (folder / rel).resolve()
    if folder.resolve() not in src.parents or not src.is_file():
        raise ValueError(f"Файл {rel!r} не знайдено в теці тендера")
    return src


def _cached(folder: Path, rel: str, src: Path, kind: str, make: Callable[[], Any]) -> tuple[str, str | None]:
    cache = folder / TEXT_DIR / (rel + kind + ".txt")
    meta = folder / TEXT_DIR / (rel + kind + ".json")
    stamp = f"{src.stat().st_size}:{int(src.stat().st_mtime)}"
    if cache.exists() and meta.exists():
        info = json.loads(meta.read_text(encoding="utf-8"))
        if info.get("stamp") == stamp:
            return cache.read_text(encoding="utf-8"), info.get("note")
    r = make()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(r.text, encoding="utf-8")
    meta.write_text(json.dumps({"stamp": stamp, "note": r.note}, ensure_ascii=False), encoding="utf-8")
    return r.text, r.note


def winner_files(folder: Path, subdirs: set[str]) -> list[dict[str, Any]]:
    """Downloaded files of the winners (and contracts), from the folder manifest. An unpacked archive gives the
    files kept from it."""
    manifest_path = folder / MANIFEST
    if not manifest_path.exists():
        return []
    entries = json.loads(manifest_path.read_text(encoding="utf-8")).get("documents", {})
    out = []
    for e in entries.values():
        if e.get("subdir") not in subdirs:
            continue
        base = f"{e['subdir']}/{e['file']}" if e.get("subdir") else e["file"]
        rels = [f"{base}/{m}" for m in e["unpacked"]] if e.get("unpacked") else [base]
        for rel in rels:
            out.append({"file": rel, "title": e.get("title"), "kind": doc_kind(e), "subdir": e.get("subdir")})
    out.sort(key=lambda f: (KIND_ORDER.get(f["kind"], 9), f["file"]))
    return out


def _vendors(text: str) -> Counter[str]:
    return Counter(m.group(0) for m in VENDORS.finditer(text or ""))


def read_winner_texts(
    folder: Path,
    files: list[dict[str, Any]],
    *,
    max_chars: int = 0,
    ocr: bool = False,
    rules: WinnerDocRules | None = None,
) -> tuple[Counter[str], dict[str, list[str]]]:
    """Extract text of the winner's files (in place: chars, note, excerpt), OCR the scans that look useful.
    Returns vendor mention counts and, per vendor, where it was found ("файл" or "файл (OCR)")."""
    budget = max_chars
    vendors: Counter[str] = Counter()
    sources: dict[str, list[str]] = {}
    for f in files:
        try:
            text, note = document_text(folder, f["file"])
        except ValueError as e:  # listed in the manifest but removed from disk
            text, note = "", str(e)
        f["chars"] = len(text)
        f["note"] = note
        found = _vendors(text)
        label = f["file"]
        if ocr and note and ("скан" in note or "OCR" in note) and (rules is None or rules.wants_ocr(f["file"])):
            ocr_text, ocr_note = document_ocr(folder, f["file"])
            f["ocr_chars"] = len(ocr_text)
            f["ocr_note"] = ocr_note
            if ocr_text:
                text = ocr_text
                ocr_found = _vendors(ocr_text)
                for v in ocr_found:
                    sources.setdefault(v, []).append(f"{label} (OCR)")
                vendors.update(ocr_found)
        for v in found:
            sources.setdefault(v, []).append(label)
        vendors.update(found)
        if budget > 0 and text:
            f["excerpt"] = text[:budget]
            f["truncated"] = len(text) > budget
            budget -= len(f["excerpt"])
    return vendors, sources


async def contract_documents(
    client: ProzorroClient, tender: dict[str, Any]
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    """Documents of the tender's contracts from the contracting module (GET /contracts/{id}): the signed contract
    and its annexes (specification, list of goods) live there, not in the tender object."""
    docs: dict[str, list[dict[str, Any]]] = {}
    errors = []
    for c in tender.get("contracts") or []:
        cid = c.get("id")
        if not cid or c.get("status") == "cancelled":
            continue
        try:
            contract = await client.get_contract(cid)
        except NotFound:
            continue  # not moved to contracting yet (pending contracts)
        except ProzorroError as e:
            errors.append({"title": f"договір {cid}", "error": str(e)})
            continue
        docs[cid] = contract.get("documents") or []
    return docs, errors


def load_rules(settings: Settings) -> WinnerDocRules:
    return WinnerDocRules.from_file(settings.winner_docs_config)


async def prepare_offer(
    client: ProzorroClient,
    settings: Settings,
    db: Database,
    tender: dict[str, Any],
    max_chars: int = 30_000,
    *,
    winner_docs: WinnerDocsMode = "minimal",
    include_tender_docs: bool = False,
    extract: bool = True,
    ocr: bool = False,
    downloader: DocumentDownloader | None = None,
    rules: WinnerDocRules | None = None,
) -> dict[str, Any]:
    """Download the winners' and contract documents (one pass, together with the tender documentation if asked),
    extract text, and put together what Claude needs to fill in the winning offer rows. Also writes _winner.json
    into the tender folder (for reading outside MCP).

    winner_docs="minimal" downloads only what tells what won (config/winner-docs.yaml); the rest is listed in
    _winner.json as skipped. extract=False leaves text extraction for later (`offers text`)."""
    wins = winners(tender)
    root = settings.output_dir / "Документи"
    rules = rules or (load_rules(settings) if winner_docs == "minimal" else None)
    downloader = downloader or DocumentDownloader(client, root, settings.doc_hosts, settings.concurrency)
    download = None
    contract_errors: list[dict[str, str]] = []
    if wins or include_tender_docs:
        contract_docs, contract_errors = await contract_documents(client, tender) if wins else ({}, [])
        download = await downloader.download_tender(
            tender,
            include_bids=bool(wins),
            include_tender=include_tender_docs,
            bid_ids={w["bid_id"] for w in wins if w["bid_id"]},
            include_contracts=bool(wins),
            contract_docs=contract_docs,
            rules=rules if winner_docs == "minimal" else None,
            mode={"tender_docs": include_tender_docs, "winners": bool(wins), "winner_docs": winner_docs},
        )
    folder = find_tender_folder(root, tender)
    subdirs = {w["documents_subdir"] for w in wins if w["documents_subdir"]} | {CONTRACTS_DIR}
    files = winner_files(folder, subdirs) if wins else []
    vendors: Counter[str] = Counter()
    for w in wins:
        for r in w["requirement_responses"]:
            vendors.update(_vendors(str(r["value"])))
        for i in w["items"]:
            vendors.update(_vendors(i["description"] or ""))
    sources: dict[str, list[str]] = {v: ["критерії / позиції"] for v in vendors}
    if extract:
        found, file_sources = read_winner_texts(folder, files, max_chars=max_chars, ocr=ocr, rules=rules)
        vendors.update(found)
        for v, src in file_sources.items():
            sources.setdefault(v, []).extend(src)
    result = {
        "tender": tender.get("id"),
        "tenderID": tender.get("tenderID"),
        "url": tender_url(tender),
        "title": tender.get("title"),
        "status": tender.get("status"),
        "folder": str(folder),
        "winner_docs": winner_docs,
        "winners": wins,
        "documents": files,
        "text_extracted": extract,
        "vendor_mentions": dict(vendors.most_common(20)),
        "vendor_sources": {v: sorted(set(s))[:10] for v, s in sources.items()},
        "skipped_documents": download.not_downloaded if download else [],
        "saved_rows": db.offers(tender=tender.get("id")),
        "download_errors": (download.failed if download else []) + contract_errors,
        "download": {
            "files": len(download.downloaded),
            "bytes": download.bytes,
            "already": download.skipped,
        }
        if download
        else None,
    }
    if wins:
        write_winner_summary(folder, result)
    return result


def write_winner_summary(folder: Path, result: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    light = {**result, "documents": [{k: v for k, v in f.items() if k != "excerpt"} for f in result["documents"]]}
    tmp = folder / (SUMMARY_FILE + ".part")
    tmp.write_text(json.dumps(light, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(folder / SUMMARY_FILE)


def text_for_folder(folder: Path, *, ocr: bool = False, rules: WinnerDocRules | None = None) -> dict[str, Any]:
    """`offers text`: extract text (and OCR scans) of an already downloaded winner folder, offline, and update
    _winner.json. Only the winners' and contract files are read."""
    path = folder / SUMMARY_FILE
    if not path.exists():
        raise ValueError(
            f"У теці {folder.name} немає {SUMMARY_FILE}: спершу `docs --with-winners` або `offers prepare`"
        )
    summary = json.loads(path.read_text(encoding="utf-8"))
    subdirs = {w["documents_subdir"] for w in summary.get("winners") or [] if w.get("documents_subdir")}
    files = winner_files(folder, subdirs | {CONTRACTS_DIR})
    vendors, sources = read_winner_texts(folder, files, ocr=ocr, rules=rules)
    for w in summary.get("winners") or []:
        for r in w.get("requirement_responses") or []:
            for v in _vendors(str(r.get("value"))):
                vendors[v] += 1
                sources.setdefault(v, []).append("критерії / позиції")
    summary.update(
        documents=files,
        text_extracted=True,
        vendor_mentions=dict(vendors.most_common(20)),
        vendor_sources={v: sorted(set(s))[:10] for v, s in sources.items()},
    )
    write_winner_summary(folder, summary)
    return {
        "tenderID": summary.get("tenderID"),
        "folder": str(folder),
        "documents": len(files),
        "without_text": [f["file"] for f in files if not f.get("chars") and not f.get("ocr_chars")],
        "ocr": [f["file"] for f in files if f.get("ocr_chars")],
        "vendor_mentions": summary["vendor_mentions"],
    }


OFFER_FIELDS = (
    "award_id",
    "lot_id",
    "supplier",
    "supplier_edrpou",
    "tender_item",
    "vendor",
    "product",
    "part_number",
    "quantity",
    "unit",
    "unit_price",
    "currency",
    "vat_included",
    "total",
    "source",
    "confidence",
    "note",
)


def normalize_rows(tender: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill supplier/award defaults from the tender so Claude only has to give what it read."""
    wins = winners(tender)
    by_award = {w["award_id"]: w for w in wins}
    by_lot = {w["lot_id"]: w for w in wins}
    out = []
    for r in rows:
        w = by_award.get(r.get("award_id")) or by_lot.get(r.get("lot_id")) or (wins[0] if len(wins) == 1 else {})
        row = {k: r.get(k) for k in OFFER_FIELDS}
        row["award_id"] = row["award_id"] or w.get("award_id")
        row["lot_id"] = row["lot_id"] or w.get("lot_id")
        row["supplier"] = row["supplier"] or w.get("supplier")
        row["supplier_edrpou"] = row["supplier_edrpou"] or w.get("supplier_edrpou")
        if row["total"] is None and row["quantity"] is not None and row["unit_price"] is not None:
            row["total"] = round(float(row["quantity"]) * float(row["unit_price"]), 2)
        out.append(row)
    return out
