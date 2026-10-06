"""Compact, Claude-friendly views of Prozorro objects."""

from __future__ import annotations

from typing import Any

PROZORRO_TENDER_URL = "https://prozorro.gov.ua/tender/{}"


def _org(org: dict[str, Any] | None) -> dict[str, Any] | None:
    if not org:
        return None
    ident = org.get("identifier") or {}
    return {
        "name": org.get("name") or ident.get("legalName"),
        "edrpou": ident.get("id"),
        "region": (org.get("address") or {}).get("region"),
    }


def _value(v: dict[str, Any] | None) -> str | None:
    if not v or v.get("amount") is None:
        return None
    vat = " з ПДВ" if v.get("valueAddedTaxIncluded") else " без ПДВ"
    return f"{v['amount']:,.2f} {v.get('currency', '')}{vat}".replace(",", " ")


def _unit_price(item: dict[str, Any]) -> str | None:
    return _value((item.get("unit") or {}).get("value"))


def tender_url(tender: dict[str, Any]) -> str | None:
    tid = tender.get("tenderID")
    return PROZORRO_TENDER_URL.format(tid) if tid else None


def tender_summary(tender: dict[str, Any], include_documents: bool = True) -> dict[str, Any]:
    lots = tender.get("lots") or []
    lot_titles = {lot["id"]: lot.get("title") for lot in lots if lot.get("id")}
    bids = tender.get("bids") or []
    bid_names = {b.get("id"): (_org((b.get("tenderers") or [None])[0]) or {}).get("name") for b in bids}

    out: dict[str, Any] = {
        "id": tender.get("id"),
        "tenderID": tender.get("tenderID"),
        "url": tender_url(tender),
        "title": tender.get("title"),
        "status": tender.get("status"),
        "procurementMethodType": tender.get("procurementMethodType"),
        "procuringEntity": _org(tender.get("procuringEntity")),
        "value": _value(tender.get("value")),
        "dateCreated": tender.get("dateCreated") or tender.get("date"),
        "tenderPeriodEnd": (tender.get("tenderPeriod") or {}).get("endDate"),
        "lots": [
            {
                "id": lot.get("id"),
                "title": lot.get("title"),
                "value": _value(lot.get("value")),
                "status": lot.get("status"),
            }
            for lot in lots
        ],
        "items": [
            {
                "description": it.get("description"),
                "cpv": (it.get("classification") or {}).get("id"),
                "cpvDescription": (it.get("classification") or {}).get("description"),
                "quantity": it.get("quantity"),
                "unit": (it.get("unit") or {}).get("name"),
                "lot": lot_titles.get(it.get("relatedLot") or ""),
            }
            for it in tender.get("items") or []
        ],
    }
    if bids:
        out["bids"] = [
            {
                "supplier": _org((b.get("tenderers") or [None])[0]),
                "status": b.get("status"),
                "value": _value(b.get("value")),
                "lotValues": [
                    {"lot": lot_titles.get(lv.get("relatedLot") or ""), "value": _value(lv.get("value"))}
                    for lv in b.get("lotValues") or []
                ],
                "unitPrices": [
                    {"description": i.get("description"), "quantity": i.get("quantity"), "unitPrice": _unit_price(i)}
                    for i in b.get("items") or []
                    if _unit_price(i)
                ],
            }
            for b in bids
        ]
    awards = tender.get("awards") or []
    if awards:
        out["awards"] = [
            {
                "status": a.get("status"),
                "winner": a.get("status") == "active",
                "supplier": _org((a.get("suppliers") or [None])[0]),
                "value": _value(a.get("value")),
                "lot": lot_titles.get(a.get("lotID") or ""),
                "date": a.get("date"),
                "bidder": bid_names.get(a.get("bid_id")),
            }
            for a in awards
        ]
    contracts = tender.get("contracts") or []
    if contracts:
        out["contracts"] = [
            {
                "id": c.get("id"),
                "status": c.get("status"),
                "value": _value(c.get("value")),
                "dateSigned": c.get("dateSigned"),
            }
            for c in contracts
        ]
    docs = tender.get("documents") or []
    if include_documents:
        out["documents"] = documents_list(tender)
    else:
        out["documentsCount"] = len(docs)
    return out


def documents_list(tender: dict[str, Any]) -> list[dict[str, Any]]:
    """Latest version of every document (Prozorro keeps old versions under the same id)."""
    latest: dict[str, dict[str, Any]] = {}
    for d in tender.get("documents") or []:
        latest[d.get("id") or d.get("url") or str(len(latest))] = d
    return [
        {
            "id": d.get("id"),
            "documentType": d.get("documentType"),
            "title": d.get("title"),
            "format": d.get("format"),
            "url": d.get("url"),
            "datePublished": d.get("datePublished"),
        }
        for d in latest.values()
    ]
