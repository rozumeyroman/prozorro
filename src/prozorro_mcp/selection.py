"""Selecting stored tenders for exports and document downloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .db import Database
from .sync import parse_since

Stage = Literal["active", "complete", "all"]
PeriodMode = Literal["created", "awarded", "either"]

# "Тривають": any active.* status (from accepting bids up to an awarded but unsigned contract).
ACTIVE_STATUSES = [
    "active",
    "active.enquiries",
    "active.tendering",
    "active.pre-qualification",
    "active.pre-qualification.stand-still",
    "active.auction",
    "active.qualification",
    "active.qualification.stand-still",
    "active.awarded",
    "active.stage2.pending",
    "active.stage2.waiting",
]
# "Завершені": contract signed.
COMPLETE_STATUSES = ["complete"]


@dataclass
class TenderQuery:
    query: str | None = None
    topic: str | None = None
    min_value: float | None = None
    created_from: str | None = None
    created_to: str | None = None
    stage: Stage = "all"
    status: list[str] | None = None
    awarded_from: str | None = None
    awarded_to: str | None = None
    limit: int | None = None
    profile: str | None = None  # only tenders relevant under this filter profile
    # One period applied to the announcement date, the award/contract date, or either of them (OR).
    # Combined with the created_*/awarded_* bounds above, which always apply (AND).
    period_from: str | None = None
    period_to: str | None = None
    period_mode: PeriodMode = "created"

    def statuses(self) -> list[str] | None:
        if self.status:
            return self.status
        return {"active": ACTIVE_STATUSES, "complete": COMPLETE_STATUSES}.get(self.stage)


def _dt(value: str | None) -> datetime | None:
    return parse_since(value) if value else None


def award_dates(tender: dict[str, Any]) -> list[datetime]:
    """Dates of winner decisions (active awards) and signed contracts."""
    dates = [a.get("date") for a in tender.get("awards") or [] if a.get("status") == "active"]
    dates += [c.get("dateSigned") for c in tender.get("contracts") or [] if c.get("status") == "active"]
    return [datetime.fromisoformat(d) for d in dates if d]


def _in(d: datetime | None, lo: datetime | None, hi: datetime | None) -> bool:
    return d is not None and (not lo or d >= lo) and (not hi or d < hi)


def created_date(tender: dict[str, Any]) -> datetime | None:
    value = tender.get("dateCreated") or tender.get("date")
    return datetime.fromisoformat(value) if value else None


def select_tenders(db: Database, q: TenderQuery, sort: str = "date_desc") -> list[dict[str, Any]]:
    created_from, created_to = q.created_from, q.created_to
    awarded_from, awarded_to = q.awarded_from, q.awarded_to
    either: tuple[datetime | None, datetime | None] | None = None
    if q.period_from or q.period_to:
        if q.period_mode == "created":
            created_from, created_to = created_from or q.period_from, created_to or q.period_to
        elif q.period_mode == "awarded":
            awarded_from, awarded_to = awarded_from or q.period_from, awarded_to or q.period_to
        else:
            either = (_dt(q.period_from), _dt(q.period_to))

    rows, _ = db.search(
        query=q.query,
        topic=q.topic,
        min_value=q.min_value,
        created_from=_dt(created_from).isoformat() if created_from else None,
        created_to=_dt(created_to).isoformat() if created_to else None,
        statuses=q.statuses(),
        sort=sort,
        limit=None,
        profile=q.profile,
    )
    tenders = db.tenders_data([r["id"] for r in rows])
    lo, hi = _dt(awarded_from), _dt(awarded_to)
    if lo or hi:
        tenders = [t for t in tenders if any(_in(d, lo, hi) for d in award_dates(t))]
    if either:
        lo, hi = either
        tenders = [t for t in tenders if _in(created_date(t), lo, hi) or any(_in(d, lo, hi) for d in award_dates(t))]
    return tenders[: q.limit] if q.limit else tenders


def select_rows(
    db: Database, q: TenderQuery, sort: str = "date_desc", limit: int | None = None, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """Search-result rows (as db.search returns them) for select_tenders(q): (page, total)."""
    rows, _ = db.search(profile=q.profile, sort=sort, limit=None)
    by_id = {r["id"]: r for r in rows}
    ids = [t["id"] for t in select_tenders(db, q, sort=sort)]
    page = ids[offset : offset + limit] if limit else ids[offset:]
    return [by_id[i] for i in page if i in by_id], len(ids)
