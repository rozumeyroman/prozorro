"""Summary analytics over a selection of tenders: totals, breakdowns, top winners and buyers, competition, discounts."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from .export import STATUS_UA, _discount, _expected_for_award, _org
from .filter import TenderFilter
from .settings import KYIV_TZ

# Bids in these statuses were withdrawn or never submitted, so they are not counted as participants.
NOT_PARTICIPATING = {"deleted", "draft"}


def _uah(v: dict[str, Any] | None) -> float:
    v = v or {}
    return float(v["amount"]) if v.get("amount") is not None and v.get("currency", "UAH") == "UAH" else 0.0


def bidders_for_award(tender: dict[str, Any], award: dict[str, Any]) -> int:
    """Number of participants in the lot the award belongs to (or in the whole tender)."""
    lot_id = award.get("lotID")
    count = 0
    for b in tender.get("bids") or []:
        if b.get("status") in NOT_PARTICIPATING:
            continue
        if lot_id and not any(lv.get("relatedLot") == lot_id for lv in b.get("lotValues") or []):
            continue
        count += 1
    return count


def winning_awards(tender: dict[str, Any]) -> list[dict[str, Any]]:
    return [a for a in tender.get("awards") or [] if a.get("status") == "active"]


def _top(acc: dict[str, dict[str, Any]], n: int, by: str = "amount") -> list[dict[str, Any]]:
    other = "count" if by == "amount" else "amount"
    rows = sorted(acc.values(), key=lambda r: (-r[by], -r[other]))
    return [{**r, "amount": round(r["amount"], 2)} for r in rows[:n]]


def summarize(tenders: list[dict[str, Any]], tender_filter: TenderFilter, top: int = 10) -> dict[str, Any]:
    """All monetary totals are in UAH (other currencies are left out of sums and counted separately)."""
    by_status: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "expected": 0.0})
    by_topic: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "expected": 0.0})
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "expected": 0.0, "awarded": 0.0})
    winners: dict[str, dict[str, Any]] = {}
    buyers: dict[str, dict[str, Any]] = {}
    discounts: list[float] = []
    bidders: list[int] = []
    total_expected = total_awarded = 0.0
    non_uah = 0

    for t in tenders:
        d = tender_filter.evaluate(t)
        expected = d.relevant_value if d.currency == "UAH" and d.relevant_value is not None else 0.0
        if d.currency not in (None, "UAH"):
            non_uah += 1
        total_expected += expected
        status = STATUS_UA.get(t.get("status"), t.get("status") or "—")
        by_status[status]["count"] += 1
        by_status[status]["expected"] += expected
        for topic in d.topics or ["—"]:
            by_topic[topic]["count"] += 1
            by_topic[topic]["expected"] += expected
        created = t.get("dateCreated") or t.get("date")
        month = datetime.fromisoformat(created).astimezone(KYIV_TZ).strftime("%Y-%m") if created else "—"
        by_month[month]["count"] += 1
        by_month[month]["expected"] += expected

        name, code = _org(t.get("procuringEntity"))
        key = code or name or "—"
        b = buyers.setdefault(key, {"name": name, "edrpou": code, "count": 0, "amount": 0.0})
        b["count"] += 1
        b["amount"] += expected

        lots = {lot["id"]: lot for lot in t.get("lots") or [] if lot.get("id")}
        for a in winning_awards(t):
            amount = _uah(a.get("value"))
            total_awarded += amount
            by_month[month]["awarded"] += amount
            w_name, w_code = _org((a.get("suppliers") or [None])[0])
            wkey = w_code or w_name or "—"
            w = winners.setdefault(wkey, {"name": w_name, "edrpou": w_code, "count": 0, "amount": 0.0})
            w["count"] += 1
            w["amount"] += amount
            disc = _discount(a.get("value"), _expected_for_award(a, t, lots))
            if disc is not None:
                discounts.append(disc)
            if t.get("bids"):
                bidders.append(bidders_for_award(t, a))

    def rounded(rows: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        return {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in rows.items()}

    competition: dict[str, Any] = {"awards_with_bids": len(bidders)}
    if bidders:
        competition.update(
            avg_bidders=round(statistics.mean(bidders), 2),
            single_bidder_share=round(sum(1 for n in bidders if n <= 1) / len(bidders), 4),
        )
    discount: dict[str, Any] = {"n": len(discounts)}
    if discounts:
        discount.update(median=round(statistics.median(discounts), 4), mean=round(statistics.mean(discounts), 4))

    return {
        "filter": tender_filter.name,
        "tenders": len(tenders),
        "expected_value_uah": round(total_expected, 2),
        "awarded_value_uah": round(total_awarded, 2),
        "awards": sum(len(winning_awards(t)) for t in tenders),
        "non_uah_tenders": non_uah,
        "by_status": rounded(by_status),
        "by_topic": rounded(by_topic),
        "by_month": dict(sorted(rounded(by_month).items())),
        "top_winners_by_amount": _top(winners, top),
        "top_winners_by_count": _top(winners, top, by="count"),
        "top_buyers_by_amount": _top(buyers, top),
        "top_buyers_by_count": _top(buyers, top, by="count"),
        "competition": competition,
        "discount": discount,
    }
