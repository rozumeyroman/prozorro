"""Completeness check: compare the local database with the prozorro.gov.ua search for the same CPV codes and period.

Every tender the site finds (value at least min_value, not cancelled) gets a reason:
- in_db: relevant under the filter profile and stored;
- excluded: excluded by hand (`exclude add`);
- rejected: the sync looked at it and the filter rejected it (with the filter's reason);
- missing: the sync never saw it, i.e. a gap in the feed walk.
With fetch=True the missing ones are fetched by id (sync by tenderID) and classified again.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from .client import ProzorroClient
from .db import Database
from .filter import TenderFilter
from .site import SiteClient, is_cancelled, resolve_internal_id, result_status, result_value
from .summary import _org
from .sync import Syncer

REASON_TITLES = {
    "in_db": "є в базі",
    "excluded": "виключено вручну",
    "rejected": "відкинуто фільтром",
    "missing": "немає в базі",
    "not_resolved": "немає в базі; внутрішній id не знайдено",
}


def default_cpvs(f: TenderFilter) -> list[str]:
    return [c for codes in f.config.get("cpv_strong", {}).values() for c in codes]


def classify(db: Database, f: TenderFilter, tender_id: str, excluded: dict[str, dict[str, Any]]) -> tuple[str, str]:
    if tender_id in excluded:
        return "excluded", excluded[tender_id].get("reason") or ""
    if db.is_relevant(tender_id, f.name):
        return "in_db", ""
    d = db.decision_by_tender_id(tender_id, f.name)
    if d and not d["relevant"]:
        old = "" if d["filter_key"] == f.key else " (попередня версія фільтра)"
        return "rejected", f"{d['stage']}: {d['reason']}{old}"
    return "missing", ""


async def check_coverage(
    db: Database,
    site: SiteClient,
    f: TenderFilter,
    *,
    date_from: str,
    date_to: str,
    cpvs: list[str] | None = None,
    min_value: float | None = None,
    fetch: bool = False,
    client: ProzorroClient | None = None,
    concurrency: int = 4,
    progress: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    cpvs = cpvs or default_cpvs(f)
    if min_value is None:
        min_value = float((f.config.get("min_value") or {}).get("amount") or 0)
    found = await site.search(cpvs=cpvs, date_from=date_from, date_to=date_to, progress=progress)
    selected = [
        r for r in found if not is_cancelled(r) and (result_value(r) is None or (result_value(r) or 0) >= min_value)
    ]
    excluded = {e["tender_id"]: e for e in db.exclusions()}
    rows = []
    for r in selected:
        reason, detail = classify(db, f, r["tenderID"], excluded)
        pe = r.get("procuringEntity")
        rows.append(
            {
                "tenderID": r["tenderID"],
                "title": r.get("title"),
                "value": result_value(r),
                "status": result_status(r),
                "buyer": (_org(pe) or {}).get("name") if isinstance(pe, dict) else pe,
                "reason": reason,
                "detail": detail,
            }
        )

    fetched = None
    if fetch and client is not None:
        missing = [row for row in rows if row["reason"] == "missing"]
        ids: dict[str, str] = {}
        for i, row in enumerate(missing, start=1):
            hex_id = await resolve_internal_id(row["tenderID"], db, client, site)
            if hex_id:
                ids[row["tenderID"]] = hex_id
            else:
                row["reason"] = "not_resolved"
            if progress and i % 10 == 0:
                progress(f"пошук внутрішніх id: {i}/{len(missing)}")
        if ids:
            fetched = await Syncer(client, db, f, concurrency, progress=progress).sync_ids(list(ids.values()))
            for row in missing:
                if row["tenderID"] in ids:
                    reason, detail = classify(db, f, row["tenderID"], excluded)
                    row["reason"] = reason
                    row["detail"] = ("дозавантажено; " + detail) if reason != "missing" else detail
                    row["fetched"] = True

    counts = Counter(r["reason"] for r in rows)
    gaps = [r for r in rows if r["reason"] in ("missing", "not_resolved")]
    total = len(rows)
    return {
        "filter": f.name,
        "period": {"from": date_from, "to": date_to, "by": "період подання пропозицій (date[tender])"},
        "cpv": cpvs,
        "min_value": min_value,
        "site_found": len(found),
        "site_selected": total,
        "by_reason": {REASON_TITLES[k]: v for k, v in counts.items()},
        "missing": len(gaps),
        "missing_pct": round(100 * len(gaps) / total, 1) if total else 0.0,
        "fetched": {k: v for k, v in (fetched or {}).items() if k != "tenders"} if fetched else None,
        "not_in_db": [{**r, "reason": REASON_TITLES[r["reason"]]} for r in rows if r["reason"] != "in_db"],
    }
