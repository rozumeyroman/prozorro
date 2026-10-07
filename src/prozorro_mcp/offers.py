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
from pathlib import Path
from typing import Any

from .client import NotFound, ProzorroClient
from .db import Database
from .doctext import extract_text
from .documents import (
    CONTRACTS_DIR,
    MANIFEST,
    DocumentDownloader,
    bid_documents,
    bid_subdir,
    is_public,
    tender_folder_name,
)
from .settings import Settings
from .summary import _org, tender_url

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
    src = (folder / rel).resolve()
    if folder.resolve() not in src.parents or not src.is_file():
        raise ValueError(f"Файл {rel!r} не знайдено в теці тендера")
    cache = folder / TEXT_DIR / (rel + ".txt")
    meta = folder / TEXT_DIR / (rel + ".json")
    stamp = f"{src.stat().st_size}:{int(src.stat().st_mtime)}"
    if cache.exists() and meta.exists():
        info = json.loads(meta.read_text(encoding="utf-8"))
        if info.get("stamp") == stamp:
            return cache.read_text(encoding="utf-8"), info.get("note")
    r = extract_text(src)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(r.text, encoding="utf-8")
    meta.write_text(json.dumps({"stamp": stamp, "note": r.note}, ensure_ascii=False), encoding="utf-8")
    return r.text, r.note


def winner_files(folder: Path, subdirs: set[str]) -> list[dict[str, Any]]:
    """Downloaded files of the winners (and contracts), from the folder manifest."""
    manifest_path = folder / MANIFEST
    if not manifest_path.exists():
        return []
    entries = json.loads(manifest_path.read_text(encoding="utf-8")).get("documents", {})
    out = []
    for e in entries.values():
        if e.get("subdir") not in subdirs:
            continue
        rel = f"{e['subdir']}/{e['file']}" if e.get("subdir") else e["file"]
        out.append({"file": rel, "title": e.get("title"), "kind": doc_kind(e), "subdir": e.get("subdir")})
    out.sort(key=lambda f: (KIND_ORDER.get(f["kind"], 9), f["file"]))
    return out


async def prepare_offer(
    client: ProzorroClient, settings: Settings, db: Database, tender: dict[str, Any], max_chars: int = 30_000
) -> dict[str, Any]:
    """Download the winners' and contract documents, extract text, and put together what Claude needs to fill in
    the winning offer rows. Also writes _winner.json into the tender folder (for reading outside MCP)."""
    wins = winners(tender)
    root = settings.output_dir / "Документи"
    folder = root / tender_folder_name(tender)
    download = None
    if wins:
        downloader = DocumentDownloader(client, root, settings.doc_hosts, settings.concurrency)
        download = await downloader.download_tender(
            tender,
            include_bids=True,
            include_tender=False,
            bid_ids={w["bid_id"] for w in wins if w["bid_id"]},
            include_contracts=True,
        )
    subdirs = {w["documents_subdir"] for w in wins if w["documents_subdir"]} | {CONTRACTS_DIR}
    files = winner_files(folder, subdirs) if wins else []
    budget = max_chars
    vendors: Counter[str] = Counter()
    for w in wins:
        for r in w["requirement_responses"]:
            vendors.update(m.group(0) for m in VENDORS.finditer(str(r["value"])))
        for i in w["items"]:
            vendors.update(m.group(0) for m in VENDORS.finditer(i["description"] or ""))
    for f in files:
        try:
            text, note = document_text(folder, f["file"])
        except ValueError as e:  # listed in the manifest but removed from disk
            text, note = "", str(e)
        f["chars"] = len(text)
        f["note"] = note
        vendors.update(m.group(0) for m in VENDORS.finditer(text))
        if budget > 0 and text:
            f["excerpt"] = text[:budget]
            f["truncated"] = len(text) > budget
            budget -= len(f["excerpt"])
    result = {
        "tender": tender.get("id"),
        "tenderID": tender.get("tenderID"),
        "url": tender_url(tender),
        "title": tender.get("title"),
        "status": tender.get("status"),
        "folder": str(folder),
        "winners": wins,
        "documents": files,
        "vendor_mentions": dict(vendors.most_common(20)),
        "saved_rows": db.offers(tender=tender.get("id")),
        "download_errors": download.failed if download else [],
    }
    if wins:
        folder.mkdir(parents=True, exist_ok=True)
        light = {**result, "documents": [{k: v for k, v in f.items() if k != "excerpt"} for f in files]}
        (folder / SUMMARY_FILE).write_text(json.dumps(light, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


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
