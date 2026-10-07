"""Selecting stored tenders for exports and document downloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .db import Database
from .sync import parse_since

Stage = Literal["active", "complete", "all"]

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


def select_tenders(db: Database, q: TenderQuery, sort: str = "date_desc") -> list[dict[str, Any]]:
    rows, _ = db.search(
        query=q.query,
        topic=q.topic,
        min_value=q.min_value,
        created_from=_dt(q.created_from).isoformat() if q.created_from else None,
        created_to=_dt(q.created_to).isoformat() if q.created_to else None,
        statuses=q.statuses(),
        sort=sort,
        limit=None,
    )
    tenders = db.tenders_data([r["id"] for r in rows])
    lo, hi = _dt(q.awarded_from), _dt(q.awarded_to)
    if lo or hi:
        tenders = [t for t in tenders if any((not lo or d >= lo) and (not hi or d < hi) for d in award_dates(t))]
    return tenders[: q.limit] if q.limit else tenders
