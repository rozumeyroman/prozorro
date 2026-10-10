"""Which documents of the winning offer (and of the contract) are worth downloading.

The rules live in config/winner-docs.yaml (PROZORRO_WINNER_DOCS) so the word lists can be edited without code.
In the "minimal" mode only what tells what exactly won is downloaded: authorisation letters, specifications /
technical proposals and price proposals; certificates, statutes, extracts and the like are listed as skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .settings import DEFAULT_WINNER_DOCS_CONFIG

SIGNED_SUFFIXES = (".p7s", ".p7m")
SIGNED_DOCUMENT = re.compile(r"\.(pdf|docx?|xlsx?|odt|ods|rtf|txt|zip|rar|7z|jpe?g|png|tiff?|xml)\.p7[sm]$", re.I)


def _rx(pattern: str) -> re.Pattern[str]:
    # YAML folded scalars put spaces around "|" at line breaks.
    return re.compile(re.sub(r"\s*\|\s*", "|", pattern.strip()), re.I)


@dataclass
class WinnerDocRules:
    keep_patterns: dict[str, re.Pattern[str]]
    keep_types: set[str]
    skip_types: set[str]
    skip_untyped: re.Pattern[str]
    contract: re.Pattern[str]
    ocr: re.Pattern[str]
    archive_suffixes: tuple[str, ...] = (".zip", ".rar", ".7z")
    source: str | None = field(default=None, compare=False)

    @classmethod
    def from_file(cls, path: Path | None = None) -> WinnerDocRules:
        path = Path(path or DEFAULT_WINNER_DOCS_CONFIG)
        cfg: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            keep_patterns={k: _rx(v) for k, v in (cfg.get("keep_patterns") or {}).items()},
            keep_types=set(cfg.get("keep_types") or []),
            skip_types=set(cfg.get("skip_types") or []),
            skip_untyped=_rx(cfg.get("skip_untyped_pattern") or r"(?!x)x"),
            contract=_rx(cfg.get("contract_pattern") or "."),
            ocr=_rx(cfg.get("ocr_pattern") or "."),
            archive_suffixes=tuple(s.lower() for s in cfg.get("archive_suffixes") or (".zip", ".rar", ".7z")),
            source=str(path),
        )

    def keep_match(self, title: str) -> str | None:
        for name, rx in self.keep_patterns.items():
            if rx.search(title):
                return name
        return None

    @staticmethod
    def signed_inner(title: str) -> str | None:
        """'Пропозиція.pdf.p7s' -> 'Пропозиція.pdf'; None for anything that is not signed."""
        return title[:-4] if title.lower().endswith(SIGNED_SUFFIXES) else None

    def _signature_check(self, title: str, siblings: set[str]) -> tuple[str | None, str | None]:
        """(name to classify by, skip reason)."""
        inner = self.signed_inner(title)
        if inner is None:
            return title, None
        if inner.strip().lower() in siblings:
            return None, "підпис: поруч є той самий файл без .p7s"
        if not SIGNED_DOCUMENT.search(title):
            return None, "окремий файл підпису"
        return inner, None

    def classify(self, doc: dict[str, Any], siblings: set[str] | None = None) -> tuple[bool, str]:
        """(download?, why) for a document of the winning offer. `siblings`: lowercased titles of the other
        documents of the same bid (to drop .p7s duplicates)."""
        title = (doc.get("title") or "").strip()
        name, skip = self._signature_check(title, siblings or set())
        if name is None:
            return False, skip or ""
        doc_type = doc.get("documentType")
        kept_by = self.keep_match(name)
        if Path(name).suffix.lower() in self.archive_suffixes:
            return (True, f"архів: {kept_by}") if kept_by else (False, "архів без ключових слів у назві")
        if kept_by:
            return True, kept_by
        if doc_type in self.keep_types:
            return True, f"тип {doc_type}"
        if doc_type in self.skip_types:
            return False, f"тип {doc_type}"
        if not doc_type:
            if self.skip_untyped.search(name):
                return False, "назва: довідка / юридичний документ"
            return True, "без типу"
        return True, f"тип {doc_type}"

    def classify_contract(self, doc: dict[str, Any], siblings: set[str] | None = None) -> tuple[bool, str]:
        title = (doc.get("title") or "").strip()
        name, skip = self._signature_check(title, siblings or set())
        if name is None:
            return False, skip or ""
        if self.contract.search(name) or doc.get("documentType") in ("contractSigned", "contractAnnexe"):
            return True, "договір / специфікація"
        return False, "документ договору без ключових слів у назві"

    def keep_member(self, name: str) -> bool:
        """A file inside a downloaded archive: kept under the rules for a document without type."""
        return self.classify({"title": Path(name).name})[0]

    def wants_ocr(self, name: str) -> bool:
        return bool(self.ocr.search(Path(name).name))


def siblings_of(docs: list[dict[str, Any]]) -> set[str]:
    return {(d.get("title") or "").strip().lower() for d in docs}
