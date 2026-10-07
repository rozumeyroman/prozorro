"""Downloading tender documents into per-tender folders.

Folder: "<Замовник> - <Предмет закупівлі> - <UA-ID>" under <output_dir>/Документи.
Each folder keeps a manifest (_documents.json) so repeated runs download only new or changed documents.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import ProzorroClient, ProzorroError
from .summary import tender_url

MANIFEST = "_documents.json"
BIDS_DIR = "Пропозиції учасників"
# Windows forbids these characters in file names; the rest keeps names readable in Explorer/Finder.
FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
INVISIBLE_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn"}
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
LEGAL_FORMS = [
    (re.compile(r"ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ", re.I), "ТОВ"),
    (re.compile(r"ПРИВАТНЕ АКЦІОНЕРНЕ ТОВАРИСТВО", re.I), "ПрАТ"),
    (re.compile(r"АКЦІОНЕРНЕ ТОВАРИСТВО", re.I), "АТ"),
    (re.compile(r"ДЕРЖАВНЕ ПІДПРИЄМСТВО", re.I), "ДП"),
    (re.compile(r"КОМУНАЛЬНЕ НЕКОМЕРЦІЙНЕ ПІДПРИЄМСТВО", re.I), "КНП"),
    (re.compile(r"КОМУНАЛЬНЕ ПІДПРИЄМСТВО", re.I), "КП"),
    (re.compile(r"ФІЗИЧНА ОСОБА[- ]ПІДПРИЄМЕЦЬ", re.I), "ФОП"),
]


def safe_name(text: str | None, max_len: int = 80, fallback: str = "без назви") -> str:
    """Make a string safe as a single path component on Windows, macOS and Linux."""
    s = unicodedata.normalize("NFC", text or "")
    # Invisible characters (zero-width spaces, BOM, direction marks) make names that look identical differ;
    # cloud drives (e.g. Google Drive) silently drop them, so the file would not be found again.
    s = "".join(ch for ch in s if unicodedata.category(ch) not in INVISIBLE_CATEGORIES)
    s = FORBIDDEN.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")  # after \s+ -> " " only plain spaces are left
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .") + "…"
    if not s or s in {".", ".."}:
        s = fallback
    if s.split(".")[0].upper() in RESERVED:
        s = "_" + s
    return s


def short_entity(name: str | None) -> str | None:
    if not name:
        return name
    for rx, short in LEGAL_FORMS:
        name = rx.sub(short, name)
    return name


def tender_folder_name(tender: dict[str, Any]) -> str:
    pe = tender.get("procuringEntity") or {}
    entity = safe_name(short_entity(pe.get("name") or (pe.get("identifier") or {}).get("legalName")), 60, "Замовник")
    subject = safe_name(tender.get("title"), 70, "Предмет")
    tid = safe_name(tender.get("tenderID") or tender.get("id"), 40)
    return f"{entity} - {subject} - {tid}"


def unique_file_name(title: str | None, doc_id: str, taken: set[str]) -> str:
    """File name from the document title; adds a short id suffix if the name is already used in the folder."""
    name = safe_name(title, 120, f"document-{doc_id[:8]}")
    if name.lower() in taken:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem} ({doc_id[:6]}).{ext}" if dot and stem else f"{name} ({doc_id[:6]})"
    taken.add(name.lower())
    return name


@dataclass
class DocTask:
    doc: dict[str, Any]
    subdir: str  # "" for tender documents, "Пропозиції учасників/<учасник>" for bid documents


@dataclass
class TenderDownload:
    tender_id: str | None
    folder: str
    url: str | None
    downloaded: list[str] = field(default_factory=list)
    skipped: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    bytes: int = 0


def latest_documents(docs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for d in docs or []:
        key = d.get("id") or d.get("url")
        if key:
            latest[key] = d
    return list(latest.values())


def collect_tasks(tender: dict[str, Any], include_signatures: bool, include_bids: bool) -> list[DocTask]:
    def keep(d: dict[str, Any]) -> bool:
        if not d.get("url") or d.get("confidentiality") == "buyerOnly":
            return False
        is_sig = (d.get("title") or "").lower().endswith(".p7s") or d.get("format") == "application/pkcs7-signature"
        return include_signatures or not is_sig

    tasks = [DocTask(d, "") for d in latest_documents(tender.get("documents")) if keep(d)]
    if include_bids:
        for b in tender.get("bids") or []:
            org = (b.get("tenderers") or [{}])[0]
            who = safe_name(short_entity(org.get("name")), 60, "Учасник")
            code = (org.get("identifier") or {}).get("id")
            sub = f"{BIDS_DIR}/{who}" + (f" ({code})" if code else "")
            docs = []
            for key in ("documents", "financialDocuments", "eligibilityDocuments", "qualificationDocuments"):
                docs += b.get(key) or []
            tasks += [DocTask(d, sub) for d in latest_documents(docs) if keep(d)]
    return tasks


class DocumentDownloader:
    def __init__(self, client: ProzorroClient, root: Path, allowed_hosts: tuple[str, ...], concurrency: int = 4):
        self.client = client
        self.root = root
        self.allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self.sem = asyncio.Semaphore(concurrency)

    def host_allowed(self, url: str) -> bool:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        api_host = (urlparse(self.client.settings.api_url).hostname or "").lower()
        return u.scheme in ("http", "https") and (
            host == api_host or any(host == h or host.endswith("." + h) for h in self.allowed_hosts)
        )

    async def download_tender(
        self, tender: dict[str, Any], include_signatures: bool = False, include_bids: bool = False
    ) -> TenderDownload:
        folder = self.root / tender_folder_name(tender)
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / MANIFEST
        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")).get("documents", {})

        result = TenderDownload(tender.get("tenderID"), str(folder), tender_url(tender))

        def save_manifest() -> None:
            """Written after every file (atomically), so an interrupted run resumes where it stopped."""
            tmp = manifest_path.with_name(MANIFEST + ".part")
            tmp.write_text(
                json.dumps(
                    {
                        "tenderID": tender.get("tenderID"),
                        "url": tender_url(tender),
                        "title": tender.get("title"),
                        "status": tender.get("status"),
                        "documents": manifest,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(manifest_path)

        taken: dict[str, set[str]] = {}
        for entry in manifest.values():
            # Manifests written by older versions may hold names that safe_name now cleans up.
            entry["file"] = safe_name(entry["file"], 200, entry["file"])
            taken.setdefault(entry["subdir"], set()).add(entry["file"].lower())

        jobs = []
        for task in collect_tasks(tender, include_signatures, include_bids):
            d = task.doc
            key = d.get("id") or d["url"]
            version = d.get("dateModified") or d.get("datePublished") or d["url"]
            known = manifest.get(key)
            target_dir = folder / task.subdir if task.subdir else folder
            if known and known.get("version") == version and (target_dir / known["file"]).exists():
                result.skipped += 1
                continue
            if not self.host_allowed(d["url"]):
                result.failed.append({"title": d.get("title") or key, "error": f"хост не дозволено: {d['url']}"})
                continue
            names = taken.setdefault(task.subdir, set())
            file_name = (
                known["file"]
                if known and known.get("subdir") == task.subdir
                else unique_file_name(d.get("title"), key, names)
            )
            target = (target_dir / file_name).resolve()
            if folder.resolve() not in target.parents:  # defence in depth: never write outside the folder
                result.failed.append({"title": d.get("title") or key, "error": "некоректна назва файлу"})
                continue
            jobs.append((key, version, task.subdir, d, target))

        async def run(key: str, version: str, subdir: str, d: dict[str, Any], target: Path) -> None:
            async with self.sem:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    size = await self.client.download(d["url"], target)
                except ProzorroError as e:
                    result.failed.append({"title": d.get("title") or key, "error": str(e)})
                    return
            result.bytes += size
            rel = f"{subdir}/{target.name}" if subdir else target.name
            result.downloaded.append(rel)
            manifest[key] = {
                "file": target.name,
                "subdir": subdir,
                "version": version,
                "title": d.get("title"),
                "documentType": d.get("documentType"),
                "url": d["url"],
                "hash": d.get("hash"),
                "size": size,
            }
            save_manifest()

        await asyncio.gather(*(run(*j) for j in jobs))
        save_manifest()
        return result


def list_tender_folders(root: Path) -> list[dict[str, Any]]:
    """Folders under `root` that this tool created (they hold a manifest), with their tender id and size."""
    out = []
    if not root.is_dir():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest = folder / MANIFEST
        if not manifest.exists():
            continue
        try:
            tender_id = json.loads(manifest.read_text(encoding="utf-8")).get("tenderID")
        except (OSError, ValueError):
            continue
        size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        out.append({"folder": str(folder), "tenderID": tender_id, "megabytes": round(size / 1_048_576, 1)})
    return out


def prune_folders(root: Path, keep_tender_ids: set[str], confirm: bool = False) -> dict[str, Any]:
    """Folders of tenders that are not in `keep_tender_ids`. Deleted only with confirm=True.

    Only folders with a manifest (created by download_documents) are considered; anything else is left alone.
    """
    stale = [f for f in list_tender_folders(root) if f["tenderID"] not in keep_tender_ids]
    if confirm:
        for f in stale:
            shutil.rmtree(f["folder"])
    return {
        "deleted" if confirm else "would_delete": stale,
        "count": len(stale),
        "megabytes": round(sum(f["megabytes"] for f in stale), 1),
    }
