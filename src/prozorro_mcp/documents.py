"""Downloading tender documents into per-tender folders.

Folder: "<Замовник> - <Предмет закупівлі> - <UA-ID>" under <output_dir>/Документи.
Each folder keeps a manifest (_documents.json) so repeated runs download only new or changed documents.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from .client import ProzorroClient, ProzorroError
from .summary import tender_url

if TYPE_CHECKING:
    from .winner_docs import WinnerDocRules

MANIFEST = "_documents.json"
BIDS_DIR = "Пропозиції учасників"
CONTRACTS_DIR = "Договори"
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


FORMAT_SUFFIXES = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/zip": ".zip",
    "application/x-zip-compressed": ".zip",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "text/plain": ".txt",
}
HAS_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,5}$")


def unique_file_name(title: str | None, doc_id: str, taken: set[str], fmt: str | None = None) -> str:
    """File name from the document title; adds a short id suffix if the name is already used in the folder.

    A title without an extension gets one from the document format (`Пропозиція` + application/pdf ->
    `Пропозиція.pdf`), so the file opens and is not renamed by hand (which made the manifest lose track of it).
    """
    name = safe_name(title, 120, f"document-{doc_id[:8]}")
    if not HAS_SUFFIX.search(name) and (fmt or "").lower() in FORMAT_SUFFIXES:
        name += FORMAT_SUFFIXES[(fmt or "").lower()]
    if name.lower() in taken:
        stem, dot, ext = name.rpartition(".")
        name = f"{stem} ({doc_id[:6]}).{ext}" if dot and stem else f"{name} ({doc_id[:6]})"
    taken.add(name.lower())
    return name


@dataclass
class DocTask:
    doc: dict[str, Any]
    subdir: str  # "" for tender documents, "Пропозиції учасників/<учасник>" for bid documents
    unpack: bool = False  # minimal winner mode: unwrap signed containers, unpack zips keeping only needed files


@dataclass
class TenderDownload:
    tender_id: str | None
    folder: str
    url: str | None
    downloaded: list[str] = field(default_factory=list)
    skipped: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    bytes: int = 0
    # documents left out by the winner-document rules: title, type, url, subdir, reason
    not_downloaded: list[dict[str, Any]] = field(default_factory=list)


def latest_documents(docs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for d in docs or []:
        key = d.get("id") or d.get("url")
        if key:
            latest[key] = d
    return list(latest.values())


def bid_subdir(bid: dict[str, Any]) -> str:
    org = (bid.get("tenderers") or [{}])[0]
    who = safe_name(short_entity(org.get("name")), 60, "Учасник")
    code = (org.get("identifier") or {}).get("id")
    return f"{BIDS_DIR}/{who}" + (f" ({code})" if code else "")


def bid_documents(bid: dict[str, Any]) -> list[dict[str, Any]]:
    docs = []
    for key in ("documents", "financialDocuments", "eligibilityDocuments", "qualificationDocuments"):
        docs += bid.get(key) or []
    return latest_documents(docs)


def is_public(d: dict[str, Any]) -> bool:
    return bool(d.get("url")) and d.get("confidentiality") != "buyerOnly"


SIGNED_DOCUMENT = re.compile(r"\.(pdf|docx?|xlsx?|odt|ods|rtf|txt|zip|rar|7z|jpe?g|png|tiff?|xml)\.p7[sm]$", re.I)


def is_signature(d: dict[str, Any]) -> bool:
    """A detached signature file. «Пропозиція.pdf.p7s» is a signed document with the PDF inside, not a signature."""
    title = (d.get("title") or "").lower()
    if SIGNED_DOCUMENT.search(title):
        return False
    return title.endswith((".p7s", ".p7m")) or d.get("format") == "application/pkcs7-signature"


def collect_tasks(
    tender: dict[str, Any],
    include_signatures: bool,
    include_bids: bool,
    *,
    include_tender: bool = True,
    bid_ids: set[str] | None = None,
    include_contracts: bool = False,
    contract_docs: dict[str, list[dict[str, Any]]] | None = None,
    rules: WinnerDocRules | None = None,
    only_titles: set[str] | None = None,
    not_downloaded: list[dict[str, Any]] | None = None,
) -> list[DocTask]:
    """Documents to download. `bid_ids` limits bid documents to these bids (e.g. the winners).

    contract_docs: documents of the contracts from the contracting module (GET /contracts/{id}), by contract id;
    the contract objects inside the tender usually have none.
    rules: the "minimal" winner mode: bid and contract documents are selected by the rules from
    config/winner-docs.yaml; the rest goes to `not_downloaded`.
    only_titles: download just these documents (by title, case-insensitive), whatever the rules say.
    """
    skipped = not_downloaded if not_downloaded is not None else []
    wanted = {t.strip().lower() for t in only_titles} if only_titles else None

    def keep(d: dict[str, Any]) -> bool:
        if not is_public(d):
            return False
        if wanted is not None:
            return (d.get("title") or "").strip().lower() in wanted
        return include_signatures or not is_signature(d) or rules is not None

    def note(d: dict[str, Any], subdir: str, why: str) -> None:
        skipped.append(
            {
                "title": d.get("title"),
                "documentType": d.get("documentType"),
                "size": d.get("size"),
                "url": d.get("url"),
                "subdir": subdir,
                "reason": why,
                "status": "skipped",
            }
        )

    tasks = []
    if include_tender and wanted is None:
        tasks = [
            DocTask(d, "")
            for d in latest_documents(tender.get("documents"))
            if is_public(d) and (include_signatures or not is_signature(d))
        ]
    if include_bids:
        for b in tender.get("bids") or []:
            if bid_ids is not None and b.get("id") not in bid_ids:
                continue
            docs = [d for d in bid_documents(b) if keep(d)]
            sub = bid_subdir(b)
            siblings = {(d.get("title") or "").strip().lower() for d in docs}
            for d in docs:
                if rules is not None and wanted is None:
                    ok, why = rules.classify(d, siblings)
                    if not ok:
                        note(d, sub, why)
                        continue
                    tasks.append(DocTask(d, sub, unpack=True))
                else:
                    tasks.append(DocTask(d, sub))
    if include_contracts:
        for c in tender.get("contracts") or []:
            if c.get("status") == "cancelled":
                continue
            docs = latest_documents([*(c.get("documents") or []), *((contract_docs or {}).get(c.get("id")) or [])])
            docs = [d for d in docs if keep(d)]
            siblings = {(d.get("title") or "").strip().lower() for d in docs}
            for d in docs:
                if rules is not None and wanted is None:
                    ok, why = rules.classify_contract(d, siblings)
                    if not ok:
                        note(d, CONTRACTS_DIR, why)
                        continue
                    tasks.append(DocTask(d, CONTRACTS_DIR, unpack=True))
                elif include_signatures or not is_signature(d) or wanted is not None:
                    tasks.append(DocTask(d, CONTRACTS_DIR))
    return tasks


def find_existing(target_dir: Path, name: str, size: int | None) -> str | None:
    """A file already on disk that a manifest entry refers to under a slightly different name: older versions
    kept the raw title (leading space, no extension), and people add the extension by hand."""
    if not target_dir.is_dir():
        return None
    want = safe_name(name, 200, name).lower()
    for f in target_dir.iterdir():
        if not f.is_file() or f.name.endswith(".part"):
            continue
        clean = safe_name(f.name, 200, f.name).lower()
        stem = clean[: -len(f.suffix)] if f.suffix else clean
        if clean == want or (stem == want and f.suffix):
            if size is None or f.stat().st_size == size:
                return f.name
    return None


def find_tender_folder(root: Path, tender: dict[str, Any]) -> Path:
    """The folder of a tender. If a folder of the same tender exists under another name (title changed, or an
    older version left a line break in it), it is renamed to the current name."""
    folder = root / tender_folder_name(tender)
    if folder.exists() or not root.is_dir():
        return folder
    tid = tender.get("tenderID")
    if not tid:
        return folder
    for f in root.iterdir():
        if f.is_dir() and f.name.rstrip().endswith(tid) and (f / MANIFEST).exists():
            try:
                if json.loads((f / MANIFEST).read_text(encoding="utf-8")).get("tenderID") != tid:
                    continue
            except (OSError, ValueError):
                continue
            f.rename(folder)
            break
    return folder


def _unwrap_signed(data: bytes) -> bytes | None:
    """The document inside a file signed with an attached signature (CMS), or None."""
    try:
        from asn1crypto import cms

        info = cms.ContentInfo.load(data)
        if info["content_type"].native != "signed_data":
            return None
        content = info["content"]["encap_content_info"]["content"]
        inner = content.native if content is not None else None
        return bytes(inner) if inner else None
    except Exception:  # noqa: BLE001 - not a CMS file
        return None


def postprocess(path: Path, rules: WinnerDocRules) -> tuple[str, list[str]]:
    """Minimal winner mode, after a download: a signed container is replaced with the document inside it; a zip is
    unpacked into a folder keeping only the files the rules want. Returns (name to record, files kept)."""
    name = path.name
    inner_name = rules.signed_inner(name)
    if inner_name:
        inner = _unwrap_signed(path.read_bytes())
        if inner:
            target = path.with_name(safe_name(inner_name, 200, inner_name))
            target.write_bytes(inner)
            path.unlink()
            path, name = target, target.name
    if (
        path.suffix.lower() == ".zip"
        or path.read_bytes()[:4] == b"PK\x03\x04"
        and path.suffix.lower() not in (".docx", ".xlsx", ".odt", ".ods", ".pptx")
    ):
        try:
            z = zipfile.ZipFile(io.BytesIO(path.read_bytes()))
        except zipfile.BadZipFile:
            return name, []
        out_dir = path.with_name(safe_name(path.stem, 120, "архів") + " (розпаковано)")
        kept = []
        for m in z.infolist():
            if m.is_dir() or m.file_size > 200 * 1_048_576:
                continue
            member = m.filename
            try:  # zips made on Windows store names in cp866
                if not (m.flag_bits & 0x800):
                    member = member.encode("cp437").decode("cp866")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            base = safe_name(Path(member).name, 150, "файл")
            if not rules.keep_member(base):
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            data = z.read(m)
            inner_name = rules.signed_inner(base)
            unwrapped = _unwrap_signed(data) if inner_name else None
            if unwrapped:
                data, base = unwrapped, safe_name(inner_name, 150, "файл")
            (out_dir / base).write_bytes(data)
            kept.append(base)
        if kept:
            path.unlink()
            return out_dir.name, kept
    return name, []


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
        self,
        tender: dict[str, Any],
        include_signatures: bool = False,
        include_bids: bool = False,
        *,
        mode: dict[str, Any] | None = None,
        **selection: Any,  # see collect_tasks: include_tender, bid_ids, include_contracts, contract_docs, rules, …
    ) -> TenderDownload:
        """Download the selected documents. The manifest is written after every file (via a temporary file), files
        are written as *.part and renamed when complete, so an interrupted run resumes where it stopped.

        `mode` describes what was asked for; it is stored in the manifest together with `complete` (no failures),
        so `docs --remaining` can skip finished tenders without asking the API."""
        folder = find_tender_folder(self.root, tender)
        folder.mkdir(parents=True, exist_ok=True)
        manifest_path = folder / MANIFEST
        manifest: dict[str, Any] = {}
        previous: dict[str, Any] = {}
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = previous.get("documents", {})

        result = TenderDownload(tender.get("tenderID"), str(folder), tender_url(tender))
        rules = selection.get("rules")
        status: dict[str, Any] = {"complete": False}

        def save_manifest() -> None:
            tmp = manifest_path.with_name(MANIFEST + ".part")
            tmp.write_text(
                json.dumps(
                    {
                        "tenderID": tender.get("tenderID"),
                        "url": tender_url(tender),
                        "title": tender.get("title"),
                        "status": tender.get("status"),
                        "dateModified": tender.get("dateModified"),
                        **status,
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
        for task in collect_tasks(
            tender, include_signatures, include_bids, not_downloaded=result.not_downloaded, **selection
        ):
            d = task.doc
            key = d.get("id") or d["url"]
            version = d.get("dateModified") or d.get("datePublished") or d["url"]
            known = manifest.get(key)
            target_dir = folder / task.subdir if task.subdir else folder
            if known and known.get("version") == version and not (target_dir / known["file"]).exists():
                found = find_existing(target_dir, known["file"], None if known.get("unpacked") else known.get("size"))
                if found:
                    known["file"] = found
                    taken.setdefault(task.subdir, set()).add(found.lower())
            if known and known.get("version") == version and (target_dir / known["file"]).exists():
                result.skipped += 1
                continue
            if not self.host_allowed(d["url"]):
                result.failed.append({"title": d.get("title") or key, "error": f"хост не дозволено: {d['url']}"})
                continue
            names = taken.setdefault(task.subdir, set())
            file_name = (
                known["file"]
                if known and known.get("subdir") == task.subdir and not known.get("unpacked")
                else unique_file_name(d.get("title"), key, names, d.get("format"))
            )
            target = (target_dir / file_name).resolve()
            if folder.resolve() not in target.parents:  # defence in depth: never write outside the folder
                result.failed.append({"title": d.get("title") or key, "error": "некоректна назва файлу"})
                continue
            jobs.append((key, version, task, target))

        async def run(key: str, version: str, task: DocTask, target: Path) -> None:
            d, subdir = task.doc, task.subdir
            async with self.sem:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    size = await self.client.download(d["url"], target)
                except ProzorroError as e:
                    result.failed.append({"title": d.get("title") or key, "error": str(e)})
                    return
            result.bytes += size
            name, unpacked = target.name, []
            if task.unpack and rules is not None:
                name, unpacked = postprocess(target, rules)
            rel = f"{subdir}/{name}" if subdir else name
            result.downloaded.append(rel)
            manifest[key] = {
                "file": name,
                "subdir": subdir,
                "version": version,
                "title": d.get("title"),
                "documentType": d.get("documentType"),
                "url": d["url"],
                "hash": d.get("hash"),
                "size": size,
            }
            if unpacked:
                manifest[key]["unpacked"] = unpacked
            save_manifest()

        await asyncio.gather(*(run(*j) for j in jobs))
        if mode is not None:
            status.update(complete=not result.failed, mode=mode)
        else:
            status.update({k: previous[k] for k in ("complete", "mode") if k in previous})
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
