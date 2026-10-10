"""Downloading documents for many tenders in one run, and tidying up winner folders.

`docs --ids-file F --with-winners` does in one pass what `docs` + `offers prepare` did per tender: one request for
the tender card, its documentation, the winners' documents (by the rules of config/winner-docs.yaml), the contract
documents from the contracting module and _winner.json. Tenders are processed in parallel (tender_concurrency),
files of each tender too (settings.concurrency, shared). `remaining` skips tenders whose folder manifest says
they are complete, without asking the API.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import NotFound, ProzorroClient, ProzorroError
from .db import Database
from .documents import CONTRACTS_DIR, MANIFEST, DocumentDownloader
from .filter import TenderFilter
from .offers import (
    SUMMARY_FILE,
    TEXT_DIR,
    WinnerDocsMode,
    contract_documents,
    fetch_tender,
    load_rules,
    prepare_offer,
    winners,
    write_winner_summary,
)
from .settings import Settings
from .site import HEX_ID, SiteClient, normalize_tender_id, resolve_internal_id
from .winner_docs import WinnerDocRules


def index_folders(root: Path) -> dict[str, dict[str, Any]]:
    """tenderID -> {"folder": Path, "manifest": top-level manifest fields} for folders this tool created."""
    out: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return out
    for folder in root.iterdir():
        manifest = folder / MANIFEST
        if not folder.is_dir() or not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("tenderID"):
            data.pop("documents", None)
            out[data["tenderID"]] = {"folder": folder, "manifest": data}
    return out


def mode_covers(have: dict[str, Any] | None, want: dict[str, Any]) -> bool:
    if not have or not have.get("complete"):
        return False
    mode = have.get("mode") or {}
    for key in ("tender_docs", "winners", "bids", "signatures"):
        if want.get(key) and not mode.get(key):
            return False
    if want.get("winners"):
        return mode.get("winner_docs") in (want.get("winner_docs"), "all")
    return True


async def download_batch(
    client: ProzorroClient,
    settings: Settings,
    db: Database,
    refs: list[str],
    *,
    with_winners: bool = False,
    winner_docs: WinnerDocsMode = "minimal",
    tender_concurrency: int = 4,
    extract: bool = True,
    ocr: bool = False,
    include_signatures: bool = False,
    include_bids: bool = False,
    remaining: bool = False,
    tender_filter: TenderFilter | None = None,
    site: SiteClient | None = None,
    progress: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    progress = progress or (lambda msg: None)
    root = settings.output_dir / "Документи"
    downloader = DocumentDownloader(client, root, settings.doc_hosts, settings.concurrency)
    rules = load_rules(settings) if with_winners and winner_docs == "minimal" else None
    want = {
        "tender_docs": True,
        "winners": with_winners,
        "winner_docs": winner_docs,
        "bids": include_bids,
        "signatures": include_signatures,
    }
    done_folders = index_folders(root) if remaining else {}
    sem = asyncio.Semaphore(max(1, tender_concurrency))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    already: list[str] = []
    counter = {"n": 0}
    total = len(refs)

    def tick(text: str) -> None:
        counter["n"] += 1
        progress(f"[{counter['n']}/{total}] {text}")

    async def one(ref: str) -> None:
        ref = ref.strip()
        stored = db.get_tender(ref)
        tid = normalize_tender_id(ref) or (stored or {}).get("tenderID")
        if remaining and tid and tid in done_folders:
            manifest = done_folders[tid]["manifest"]
            # with winners, only finished tenders: an unfinished one may still get a winner or a contract
            finished = manifest.get("status") in ("complete", "unsuccessful", "cancelled")
            if mode_covers(manifest, want) and (finished or not with_winners):
                already.append(tid)
                tick(f"{tid}: уже завантажено")
                return
        async with sem:
            try:
                if stored:
                    hex_id = stored["id"]
                elif HEX_ID.fullmatch(ref):
                    hex_id = ref
                else:
                    hex_id = await resolve_internal_id(tid, db, client, site) if tid else None
                if not hex_id:
                    raise ValueError("не знайдено в базі; внутрішній id не вдалося визначити")
                tender = await client.get_tender(hex_id)
                if with_winners:
                    if tender_filter is not None:
                        db.save_tender(tender, tender_filter.evaluate(tender))
                    r = await prepare_offer(
                        client,
                        settings,
                        db,
                        tender,
                        max_chars=0,
                        winner_docs=winner_docs,
                        include_tender_docs=True,
                        extract=extract,
                        ocr=ocr,
                        downloader=downloader,
                        rules=rules,
                    )
                    d = r["download"] or {"files": 0, "bytes": 0, "already": 0}
                    item = {
                        "tenderID": r["tenderID"],
                        "folder": r["folder"],
                        "files": d["files"],
                        "megabytes": round(d["bytes"] / 1_048_576, 1),
                        "already": d["already"],
                        "winners": [w["supplier"] for w in r["winners"]],
                        "skipped_documents": len(r["skipped_documents"]),
                        "errors": r["download_errors"],
                        "vendor_mentions": r["vendor_mentions"],
                    }
                else:
                    dl = await downloader.download_tender(
                        tender,
                        include_signatures,
                        include_bids,
                        mode={"tender_docs": True, "bids": include_bids, "signatures": include_signatures},
                    )
                    item = {
                        "tenderID": dl.tender_id,
                        "folder": dl.folder,
                        "files": len(dl.downloaded),
                        "megabytes": round(dl.bytes / 1_048_576, 1),
                        "already": dl.skipped,
                        "errors": dl.failed,
                    }
            except (NotFound, ProzorroError, ValueError) as e:
                errors.append({"tender": ref, "error": str(e)})
                tick(f"{tid or ref}: помилка {e}")
                return
        results.append(item)
        extra = f", уже було {item['already']}" if item["already"] else ""
        if item.get("skipped_documents"):
            extra += f", не потрібні {item['skipped_documents']}"
        if item["errors"]:
            extra += f", помилок {len(item['errors'])}"
        tick(f"{item['tenderID']} +{item['files']} файлів {item['megabytes']} МБ{extra}")

    await asyncio.gather(*(one(r) for r in refs))
    return {
        "root": str(root),
        "tenders": len(results),
        "files_downloaded": sum(r["files"] for r in results),
        "files_skipped": sum(r["already"] for r in results),
        "files_failed": sum(len(r["errors"]) for r in results),
        "megabytes": round(sum(r["megabytes"] for r in results), 1),
        "already_complete": already,
        "details": results,
        "errors": errors,
    }


async def fetch_skipped(
    client: ProzorroClient, settings: Settings, db: Database, ref: str, titles: list[str]
) -> dict[str, Any]:
    """`offers fetch UA-… --file "<назва>"`: download documents of the winner or the contract that the minimal mode
    skipped, by title."""
    tender = await fetch_tender(client, db, ref)
    wins = winners(tender)
    contract_docs, errors = await contract_documents(client, tender)
    downloader = DocumentDownloader(client, settings.output_dir / "Документи", settings.doc_hosts, settings.concurrency)
    dl = await downloader.download_tender(
        tender,
        include_bids=True,
        include_tender=False,
        bid_ids={w["bid_id"] for w in wins if w["bid_id"]},
        include_contracts=True,
        contract_docs=contract_docs,
        only_titles=set(titles),
    )
    folder = Path(dl.folder)
    got = {t.strip().lower() for t in titles} if dl.downloaded or dl.skipped else set()
    summary_path = folder / SUMMARY_FILE
    if summary_path.exists() and got:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["skipped_documents"] = [
            s for s in summary.get("skipped_documents") or [] if (s.get("title") or "").strip().lower() not in got
        ]
        write_winner_summary(folder, summary)
    not_found = [] if dl.downloaded or dl.skipped else titles
    return {
        "tenderID": tender.get("tenderID"),
        "folder": dl.folder,
        "downloaded": dl.downloaded,
        "already": dl.skipped,
        "not_found": not_found,
        "errors": dl.failed + errors,
    }


def prune_winner_docs(root: Path, rules: WinnerDocRules, confirm: bool = False) -> dict[str, Any]:
    """Delete files of the winners' offers (and contracts) that the minimal rules would not download.

    Only folders with _winner.json are touched. The manifest and _winner.json are updated (the files move to
    skipped_documents), so nothing is downloaded again and every file can still be fetched on demand."""
    details = []
    total_bytes = total_files = 0
    if not root.is_dir():
        return {"folders": 0, "files": 0, "megabytes": 0.0, "details": []}
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        summary_path, manifest_path = folder / SUMMARY_FILE, folder / MANIFEST
        if not summary_path.exists() or not manifest_path.exists():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries: dict[str, dict[str, Any]] = manifest.get("documents", {})
        winner_dirs = {w.get("documents_subdir") for w in summary.get("winners") or [] if w.get("documents_subdir")}
        by_dir: dict[str, list[str]] = {}
        for e in entries.values():
            by_dir.setdefault(e.get("subdir") or "", []).append((e.get("title") or "").strip().lower())
        drop = []
        for key, e in entries.items():
            sub = e.get("subdir") or ""
            if sub in winner_dirs:
                ok, why = rules.classify(e, set(by_dir.get(sub, [])))
            elif sub == CONTRACTS_DIR:
                ok, why = rules.classify_contract(e, set(by_dir.get(sub, [])))
            else:
                continue
            if not ok:
                path = folder / sub / e["file"]
                size = _size(path)
                drop.append((key, e, why, path, size))
        if not drop:
            continue
        size = sum(d[4] for d in drop)
        total_bytes += size
        total_files += len(drop)
        details.append(
            {
                "folder": folder.name,
                "files": [f"{d[1].get('subdir')}/{d[1]['file']}" for d in drop],
                "megabytes": round(size / 1_048_576, 1),
            }
        )
        if not confirm:
            continue
        skipped = summary.get("skipped_documents") or []
        for key, e, why, path, _ in drop:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            for cache in (folder / TEXT_DIR / (e.get("subdir") or "")).glob(f"{_glob_escape(e['file'])}*"):
                if cache.is_file():
                    cache.unlink()
            entries.pop(key)
            skipped.append(
                {
                    "title": e.get("title"),
                    "documentType": e.get("documentType"),
                    "size": e.get("size"),
                    "url": e.get("url"),
                    "subdir": e.get("subdir"),
                    "reason": why,
                    "status": "skipped",
                }
            )
        tmp = manifest_path.with_name(MANIFEST + ".part")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(manifest_path)
        kept = {f"{e.get('subdir')}/{e['file']}" for e in entries.values()}
        summary["skipped_documents"] = skipped
        summary["documents"] = [d for d in summary.get("documents") or [] if any(d["file"].startswith(k) for k in kept)]
        summary["winner_docs"] = "minimal"
        write_winner_summary(folder, summary)
        for sub in winner_dirs:  # remove bid folders left empty
            d = folder / sub
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    return {
        "deleted" if confirm else "would_delete": details,
        "folders": len(details),
        "files": total_files,
        "megabytes": round(total_bytes / 1_048_576, 1),
    }


def _size(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size if path.exists() else 0


def _glob_escape(name: str) -> str:
    return "".join(f"[{c}]" if c in "[]*?" else c for c in name)


def winner_folder(root: Path, ref: str, db: Database) -> Path:
    """Folder of a downloaded tender by UA-… id / internal id (offline)."""
    tid = normalize_tender_id(ref) or ((db.get_tender(ref.strip()) or {}).get("tenderID"))
    entry = index_folders(root).get(tid or "")
    if not entry:
        raise ValueError(f"Теку тендера {ref!r} не знайдено в {root}")
    return entry["folder"]
