"""Tender relevance filter. Logic is described in docs/tender-filter.md, rules live in config/filters/*.yaml."""

from __future__ import annotations

import hashlib
import json
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
class ItemTrace:
    match: ItemMatch | None
    rule: str  # strong | weak_keyword | weak_no_keyword | not_in_lists | excluded_* | no_code
    detail: str
    keyword: str | None = None


def _snippet(text: str, m: re.Match[str], around: int = 50) -> str:
    """The words around a keyword match, to show where in a long tender title or description it was found."""
    return " ".join(text[max(0, m.start() - around) : m.end() + around].split())


RULE_UA = {
    "strong": "основний код",
    "weak_keyword": "загальний код + ключове слово",
    "weak_no_keyword": "загальний код без ключового слова",
    "not_in_lists": "код поза списками",
    "excluded_cpv": "виключений код",
    "excluded_item": "виключено за початком опису",
    "excluded_keyword": "слово-виключення",
    "no_code": "без коду",
}


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
    def __init__(self, config: dict[str, Any], name: str = "custom"):
        self.config = config
        self.name = name
        # Changes whenever the rules change: decisions cached under an old key are re-evaluated.
        digest = hashlib.sha1(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
        self.key = f"{name}:{digest}"
        self.description = config.get("description", "")
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
        self.cpv_exclude = [cpv_prefix(c) for c in config.get("cpv_exclude", [])]

        kw = config.get("keywords", {})
        include = kw.get("include", [])
        # Either a flat list (topic "keyword") or {group: [patterns]} (topic = group name).
        groups = include.items() if isinstance(include, dict) else [("keyword", include)]
        self.include = [(group, re.compile(p, re.I)) for group, patterns in groups for p in patterns]
        self.exclude = [re.compile(p, re.I) for p in kw.get("exclude", [])]
        # Checked against the start of an item description for every code (strong ones too): catches
        # cables/memory/parts that buyers file under a parent code such as 32420000-3 or 30230000-0.
        self.exclude_items = [re.compile(r"^\W*(?:" + p + ")", re.I) for p in kw.get("exclude_items", [])]
        # "always": tender title/description count as context for every item;
        # "single_item": only when the tender has one item (avoids e.g. an office suite riding on "антивірус").
        self.title_context = kw.get("title_context", "always")

    @classmethod
    def from_file(cls, path: Path, name: str | None = None) -> TenderFilter:
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f), name or path.stem)

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
        return self.trace_item(item, context_text).match

    def trace_item(self, item: dict[str, Any], context_text: str) -> ItemTrace:
        """Classify one item and say which rule decided it (used by matching, explain_filter and calibration)."""
        cls = item.get("classification") or {}
        code = cls.get("id") or ""
        if not code:
            return ItemTrace(None, "no_code", "позиція без коду CPV")
        digits = cpv_digits(code)
        excluded = next((p for p in self.cpv_exclude if digits.startswith(p)), None)
        if excluded:
            return ItemTrace(None, "excluded_cpv", f"код у cpv_exclude (гілка {excluded})")
        desc = item.get("description") or ""
        for rx in self.exclude_items:
            m = rx.search(desc)
            if m:
                return ItemTrace(None, "excluded_item", f"опис починається з «{m.group(0).strip()}»")
        base = dict(
            item_id=item.get("id", ""),
            description=desc,
            cpv=code,
            related_lot=item.get("relatedLot"),
        )
        for prefix, group in self.strong:
            if digits.startswith(prefix):
                match = ItemMatch(topic=group, reason=f"CPV {code} ({group})", **base)
                return ItemTrace(match, "strong", f"основний код ({group})")
        if not any(digits.startswith(p) for p in self.weak):
            return ItemTrace(None, "not_in_lists", "код не входить до основних чи загальних")
        for rx in self.exclude:
            m = rx.search(desc)
            if m:
                return ItemTrace(None, "excluded_keyword", f"слово-виключення «{m.group(0)}»")
        for where, text in (("опис", desc), ("назва тендера/лоту", context_text)):
            for group, rx in self.include:
                m = rx.search(text)
                if m:
                    match = ItemMatch(topic=group, reason=f"CPV {code} + «{m.group(0)}»", **base)
                    detail = f"загальний код + «{m.group(0)}» ({where})"
                    if where != "опис":
                        detail += f": …{_snippet(text, m)}…"
                    return ItemTrace(match, "weak_keyword", detail, m.group(0))
        return ItemTrace(None, "weak_no_keyword", "загальний код без ключового слова")

    def item_contexts(self, tender: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
        """Each item with the text its keywords may also be found in: the lot title and (see title_context)
        the tender title and description."""
        lots = {lot["id"]: lot for lot in tender.get("lots") or [] if lot.get("id")}
        tender_text = " ".join(filter(None, [tender.get("title"), tender.get("description")]))
        if self.title_context == "single_item" and len(tender.get("items") or []) > 1:
            tender_text = ""
        out = []
        for item in tender.get("items") or []:
            lot = lots.get(item.get("relatedLot") or "")
            out.append((item, " ".join(filter(None, [tender_text, (lot or {}).get("title")]))))
        return out

    def evaluate(self, tender: dict[str, Any]) -> Decision:
        if tender.get("status") in self.skip_statuses:
            return Decision(False, f"статус {tender.get('status')}", "prefilter")
        pre = self.prefilter(tender)
        if pre:
            return pre

        matches: list[ItemMatch] = []
        for item, context in self.item_contexts(tender):
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
            "name": self.name,
            "description": self.description,
            "min_value": f"{self.min_amount:,.0f} {self.currency} ({self.value_scope})",
            "procurement_method_types": sorted(self.method_types),
            "cpv_strong_groups": {g: len(c) for g, c in self.config["cpv_strong"].items()},
            "cpv_weak": len(self.weak),
            "cpv_exclude": len(self.cpv_exclude),
            "exclude_items": len(self.exclude_items),
            "keyword_groups": sorted({g for g, _ in self.include}),
            "keywords": len(self.include),
        }
