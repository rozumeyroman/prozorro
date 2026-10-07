"""SQLite storage: relevant tenders, filter decisions and sync history."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .filter import Decision

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    id TEXT PRIMARY KEY,
    tender_id TEXT,
    title TEXT,
    status TEXT,
    procurement_method_type TEXT,
    entity_name TEXT,
    entity_edrpou TEXT,
    entity_region TEXT,
    value_amount REAL,
    currency TEXT,
    relevant_value REAL,
    topics TEXT,
    date_created TEXT,
    date_modified TEXT,
    tender_period_end TEXT,
    data TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tenders_tender_id ON tenders(tender_id);
CREATE INDEX IF NOT EXISTS tenders_date_created ON tenders(date_created);

CREATE TABLE IF NOT EXISTS items (
    tender TEXT NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    item_id TEXT,
    description TEXT,
    cpv TEXT,
    cpv_description TEXT,
    quantity REAL,
    unit TEXT,
    related_lot TEXT,
    topic TEXT,
    match_reason TEXT
);
CREATE INDEX IF NOT EXISTS items_tender ON items(tender);
CREATE INDEX IF NOT EXISTS items_cpv ON items(cpv);

CREATE VIRTUAL TABLE IF NOT EXISTS tenders_fts USING fts5(
    id UNINDEXED, title, entity, items, tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    tender_id TEXT,
    relevant INTEGER NOT NULL,
    stage TEXT,
    reason TEXT,
    status TEXT,
    date_modified TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    params TEXT,
    stats TEXT
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # decisions -----------------------------------------------------------------------------------

    def get_decision(self, tender_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM decisions WHERE id = ?", (tender_id,)).fetchone()

    def save_decision(self, feed_item: dict[str, Any], decision: Decision) -> None:
        self.conn.execute(
            """INSERT INTO decisions (id, tender_id, relevant, stage, reason, status, date_modified, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET tender_id=excluded.tender_id, relevant=excluded.relevant,
                 stage=excluded.stage, reason=excluded.reason, status=excluded.status,
                 date_modified=excluded.date_modified, checked_at=excluded.checked_at""",
            (
                feed_item["id"],
                feed_item.get("tenderID"),
                int(decision.relevant),
                decision.stage,
                decision.reason,
                feed_item.get("status"),
                feed_item.get("dateModified"),
                now_iso(),
            ),
        )

    # tenders -------------------------------------------------------------------------------------

    def save_tender(self, tender: dict[str, Any], decision: Decision) -> None:
        pe = tender.get("procuringEntity") or {}
        value = tender.get("value") or {}
        matched = {m.item_id: m for m in decision.matches}
        with self.conn:
            self.conn.execute("DELETE FROM items WHERE tender = ?", (tender["id"],))
            self.conn.execute("DELETE FROM tenders_fts WHERE id = ?", (tender["id"],))
            self.conn.execute(
                """INSERT INTO tenders (id, tender_id, title, status, procurement_method_type, entity_name,
                     entity_edrpou, entity_region, value_amount, currency, relevant_value, topics, date_created,
                     date_modified, tender_period_end, data, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET tender_id=excluded.tender_id, title=excluded.title,
                     status=excluded.status, procurement_method_type=excluded.procurement_method_type,
                     entity_name=excluded.entity_name, entity_edrpou=excluded.entity_edrpou,
                     entity_region=excluded.entity_region, value_amount=excluded.value_amount,
                     currency=excluded.currency, relevant_value=excluded.relevant_value, topics=excluded.topics,
                     date_created=excluded.date_created, date_modified=excluded.date_modified,
                     tender_period_end=excluded.tender_period_end, data=excluded.data,
                     fetched_at=excluded.fetched_at""",
                (
                    tender["id"],
                    tender.get("tenderID"),
                    tender.get("title"),
                    tender.get("status"),
                    tender.get("procurementMethodType"),
                    pe.get("name"),
                    (pe.get("identifier") or {}).get("id"),
                    (pe.get("address") or {}).get("region"),
                    value.get("amount"),
                    value.get("currency"),
                    decision.relevant_value,
                    ",".join(decision.topics),
                    tender.get("dateCreated") or tender.get("date"),
                    tender.get("dateModified"),
                    (tender.get("tenderPeriod") or {}).get("endDate"),
                    json.dumps(tender, ensure_ascii=False),
                    now_iso(),
                ),
            )
            rows = []
            for it in tender.get("items") or []:
                cls = it.get("classification") or {}
                m = matched.get(it.get("id", ""))
                rows.append(
                    (
                        tender["id"],
                        it.get("id"),
                        it.get("description"),
                        cls.get("id"),
                        cls.get("description"),
                        it.get("quantity"),
                        (it.get("unit") or {}).get("name"),
                        it.get("relatedLot"),
                        m.topic if m else None,
                        m.reason if m else None,
                    )
                )
            self.conn.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            items_text = " ".join(f"{r[2] or ''} {r[4] or ''} {r[3] or ''}" for r in rows)
            self.conn.execute(
                "INSERT INTO tenders_fts (id, title, entity, items) VALUES (?,?,?,?)",
                (tender["id"], tender.get("title") or "", pe.get("name") or "", items_text),
            )

    def get_tender(self, ref: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT data FROM tenders WHERE id = ? OR tender_id = ?", (ref, ref)).fetchone()
        return json.loads(row["data"]) if row else None

    def search(
        self,
        query: str | None = None,
        topic: str | None = None,
        min_value: float | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        statuses: list[str] | None = None,
        sort: str = "date_desc",
        limit: int | None = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Search stored tenders. limit=None returns all matches."""
        where, args = [], []
        if query:
            where.append("t.id IN (SELECT id FROM tenders_fts WHERE tenders_fts MATCH ?)")
            args.append(to_fts_query(query))
        if topic:
            where.append("(',' || t.topics || ',') LIKE ?")
            args.append(f"%,{topic},%")
        if min_value is not None:
            where.append("t.relevant_value >= ?")
            args.append(min_value)
        if created_from:
            where.append("t.date_created >= ?")
            args.append(created_from)
        if created_to:
            where.append("t.date_created < ?")
            args.append(created_to)
        if statuses:
            where.append(f"t.status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        order = {
            "date_desc": "t.date_created DESC",
            "date_asc": "t.date_created ASC",
            "value_desc": "t.relevant_value DESC",
            "value_asc": "t.relevant_value ASC",
            "deadline_asc": "t.tender_period_end ASC",
        }.get(sort, "t.date_created DESC")
        total = self.conn.execute(f"SELECT count(*) FROM tenders t {sql_where}", args).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT t.id, t.tender_id, t.title, t.status, t.procurement_method_type, t.entity_name,
                       t.entity_edrpou, t.entity_region, t.value_amount, t.relevant_value, t.currency, t.topics,
                       t.date_created, t.tender_period_end
                FROM tenders t {sql_where} ORDER BY {order} LIMIT ? OFFSET ?""",
            [*args, -1 if limit is None else limit, offset],
        ).fetchall()
        return [dict(r) for r in rows], total

    def tenders_data(self, ids: list[str]) -> list[dict[str, Any]]:
        """Full tender JSON for the given ids, in the same order."""
        out = []
        for tid in ids:
            row = self.conn.execute("SELECT data FROM tenders WHERE id = ?", (tid,)).fetchone()
            if row:
                out.append(json.loads(row["data"]))
        return out

    def matched_items(self, tender_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT description, cpv, quantity, unit, topic, match_reason
               FROM items WHERE tender = ? AND topic IS NOT NULL""",
            (tender_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # sync runs / stats ---------------------------------------------------------------------------

    def start_run(self, params: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs (started_at, params) VALUES (?, ?)", (now_iso(), json.dumps(params))
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE sync_runs SET finished_at = ?, stats = ? WHERE id = ?",
            (now_iso(), json.dumps(stats, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    def last_run(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        d["params"] = json.loads(d["params"] or "{}")
        d["stats"] = json.loads(d["stats"] or "null")
        return d

    def counts(self) -> dict[str, int]:
        c = self.conn.execute
        return {
            "relevant_tenders": c("SELECT count(*) FROM tenders").fetchone()[0],
            "decisions": c("SELECT count(*) FROM decisions").fetchone()[0],
            "rejected": c("SELECT count(*) FROM decisions WHERE relevant = 0").fetchone()[0],
        }

    def commit(self) -> None:
        self.conn.commit()


def to_fts_query(text: str) -> str:
    """Turn free text into an FTS5 query where every word must match as a prefix.

    Long words lose their last two letters, a crude stemmer for Ukrainian endings,
    so 'комутатори' also finds 'комутатор' and 'комутаторів'.
    """
    words = [w for w in "".join(ch if ch.isalnum() else " " for ch in text).split() if w]
    stems = [w[:-2] if len(w) > 6 else w for w in words]
    return " ".join(f'"{w}"*' for w in stems) or '""'
