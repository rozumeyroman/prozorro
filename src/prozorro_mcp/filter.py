"""Tender relevance filter. Logic is described in docs/tender-filter.md, rules live in config/tender-filter.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DRAFT_STATUSES = {"draft", "draft.pending", "draft.unsuccessful", "draft.stage2"}
# After these statuses a tender's items and value no longer change.
OPEN_STATUSES = {"active.enquiries", "active.tendering"}


def cpv_prefix(code: str) -> str:
    """'32420000-3' -> '3242': the prefix shared by the code and all its descendants in DK021."""
    return code.split("-")[0].rstrip("0")


def cpv_digits(code: str) -> str:
    return code.split("-")[0]


@dataclass
class ItemMatch:
    item_id: str
    description: str
    cpv: str
    related_lot: str | None
    topic: str  # cpv_strong group name or "keyword"
    reason: str


@dataclass
class Decision:
    relevant: bool
    reason: str
    stage: str  # "prefilter" | "topic" | "value" | "ok"
    matches: list[ItemMatch] = field(default_factory=list)
    relevant_value: float | None = None
    currency: str | None = None

    @property
    def topics(self) -> list[str]:
        return sorted({m.topic for m in self.matches})


class TenderFilter:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        mv = config["min_value"]
        self.min_amount = float(mv["amount"])
        self.currency = mv.get("currency", "UAH")
        self.value_scope = mv.get("scope", "relevant_lots")

        pre = config.get("feed_prefilter", {})
        pmt = pre.get("procurement_method_types", {})
        self.method_types = set(pmt.get("include") or [])
        self.only_for_kinds = {k: set(v) for k, v in (pmt.get("only_for_entity_kinds") or {}).items()}
        self.skip_statuses = set(pre.get("skip_statuses") or DRAFT_STATUSES)

        # Longest prefix first so the most specific group wins.
        strong = [(cpv_prefix(c), group) for group, codes in config["cpv_strong"].items() for c in codes]
        self.strong = sorted(strong, key=lambda x: -len(x[0]))
        self.weak = sorted((cpv_prefix(c) for c in config.get("cpv_weak", [])), key=len, reverse=True)

        kw = config.get("keywords", {})
        self.include = [re.compile(p, re.I) for p in kw.get("include", [])]
        self.exclude = [re.compile(p, re.I) for p in kw.get("exclude", [])]

    @classmethod
    def from_file(cls, path: Path) -> TenderFilter:
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    # Layer 1: data available in the feed (opt_fields) -------------------------------------------------

    def feed_fields(self) -> list[str]:
        return ["dateCreated", "status", "tenderID", "procurementMethodType", "procuringEntity", "lots"]

    def prefilter(self, item: dict[str, Any]) -> Decision | None:
        """Return a negative Decision if the feed item can be rejected without fetching the tender, else None.

        Draft tenders are not decided at all (caller should skip them and look again later).
        """
        pmt = item.get("procurementMethodType")
        if pmt is not None and self.method_types and pmt not in self.method_types:
            return Decision(False, f"тип процедури {pmt} не входить до списку", "prefilter")
        kinds = self.only_for_kinds.get(pmt or "")
        if kinds:
            kind = (item.get("procuringEntity") or {}).get("kind")
            if kind not in kinds:
                return Decision(False, f"{pmt}: замовник kind={kind}, потрібно {sorted(kinds)}", "prefilter")
        lots = [lot for lot in item.get("lots") or [] if lot.get("status") != "cancelled"]
        amounts = [((lot.get("value") or {}).get("amount"), (lot.get("value") or {}).get("currency")) for lot in lots]
        if lots and all(a is not None and c == self.currency for a, c in amounts):
            total = sum(a for a, _ in amounts)
            if total < self.min_amount:
                return Decision(False, f"сума лотів {total:,.0f} < {self.min_amount:,.0f}", "prefilter")
        return None

    # Layers 2-3: full tender ------------------------------------------------------------------------

    def match_item(self, item: dict[str, Any], context_text: str) -> ItemMatch | None:
        cls = item.get("classification") or {}
        code = cls.get("id") or ""
        if not code:
            return None
        digits = cpv_digits(code)
        desc = item.get("description") or ""
        base = dict(
            item_id=item.get("id", ""),
            description=desc,
            cpv=code,
            related_lot=item.get("relatedLot"),
        )
        for prefix, group in self.strong:
            if digits.startswith(prefix):
                return ItemMatch(topic=group, reason=f"CPV {code} ({group})", **base)
        if any(digits.startswith(p) for p in self.weak):
            if any(rx.search(desc) for rx in self.exclude):
                return None
            for text in (desc, context_text):
                for rx in self.include:
                    m = rx.search(text)
                    if m:
                        return ItemMatch(topic="keyword", reason=f"CPV {code} + «{m.group(0)}»", **base)
        return None

    def evaluate(self, tender: dict[str, Any]) -> Decision:
        if tender.get("status") in self.skip_statuses:
            return Decision(False, f"статус {tender.get('status')}", "prefilter")
        pre = self.prefilter(tender)
        if pre:
            return pre

        lots = {lot["id"]: lot for lot in tender.get("lots") or [] if lot.get("id")}
        tender_text = " ".join(filter(None, [tender.get("title"), tender.get("description")]))
        matches: list[ItemMatch] = []
        for item in tender.get("items") or []:
            lot = lots.get(item.get("relatedLot") or "")
            context = " ".join(filter(None, [tender_text, (lot or {}).get("title")]))
            m = self.match_item(item, context)
            if m:
                matches.append(m)
        if not matches:
            return Decision(False, "немає позицій за темою (CPV/ключові слова)", "topic")

        value, currency = self.relevant_value(tender, matches)
        if currency != self.currency:
            return Decision(False, f"валюта {currency}", "value", matches, value, currency)
        if value is None or value < self.min_amount:
            shown = "невідома" if value is None else f"{value:,.0f}"
            return Decision(False, f"вартість {shown} < {self.min_amount:,.0f}", "value", matches, value, currency)
        return Decision(True, f"{len(matches)} позицій за темою, вартість {value:,.0f}", "ok", matches, value, currency)

    def relevant_value(self, tender: dict[str, Any], matches: list[ItemMatch]) -> tuple[float | None, str | None]:
        tender_value = tender.get("value") or {}
        lots = {
            lot["id"]: lot for lot in tender.get("lots") or [] if lot.get("id") and lot.get("status") != "cancelled"
        }
        lot_ids = {m.related_lot for m in matches if m.related_lot in lots}
        if self.value_scope == "relevant_lots" and lot_ids:
            values = [(lots[i].get("value") or {}) for i in lot_ids]
            if all(v.get("amount") is not None for v in values):
                currencies = {v.get("currency") for v in values}
                currency = currencies.pop() if len(currencies) == 1 else None
                return sum(v["amount"] for v in values), currency
        return tender_value.get("amount"), tender_value.get("currency")

    def summary(self) -> dict[str, Any]:
        return {
            "min_value": f"{self.min_amount:,.0f} {self.currency} ({self.value_scope})",
            "procurement_method_types": sorted(self.method_types),
            "cpv_strong_groups": {g: len(c) for g, c in self.config["cpv_strong"].items()},
            "cpv_weak": len(self.weak),
            "keywords": len(self.include),
        }
