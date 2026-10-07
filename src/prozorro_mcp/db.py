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

-- Replaced by filter_decisions (decisions are per filter profile now).
DROP TABLE IF EXISTS decisions;

-- Filter decisions per filter version (filter_key = "<profile>:<rules hash>").
CREATE TABLE IF NOT EXISTS filter_decisions (
    id TEXT NOT NULL,
    filter_key TEXT NOT NULL,
    tender_id TEXT,
    relevant INTEGER NOT NULL,
    stage TEXT,
    reason TEXT,
    status TEXT,
    date_modified TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (id, filter_key)
);

-- Which stored tenders are relevant under which filter profile.
CREATE TABLE IF NOT EXISTS tender_matches (
    tender TEXT NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    filter_key TEXT NOT NULL,
    topics TEXT,
    relevant_value REAL,
    reason TEXT,
    items TEXT,
    PRIMARY KEY (tender, profile)
);

-- For ad-hoc SQL: one row per (tender, profile) with profile-specific topics and relevant value.
CREATE VIEW IF NOT EXISTS tender_profile_view AS
SELECT m.profile, m.topics, m.relevant_value, m.reason AS filter_reason,
       t.id, t.tender_id, t.title, t.status, t.procurement_method_type, t.entity_name, t.entity_edrpou,
       t.entity_region, t.value_amount, t.currency, t.date_created, t.date_modified, t.tender_period_end, t.data
FROM tender_matches m JOIN tenders t ON t.id = m.tender;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
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
        # relevant_value/topics used to hold the values of the profile a tender was synced with, which went stale
        # after switching profiles; the per-profile values live in tender_matches.
        self.conn.execute(
            "UPDATE tenders SET relevant_value = NULL, topics = NULL "
            "WHERE relevant_value IS NOT NULL OR topics IS NOT NULL"
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # decisions -----------------------------------------------------------------------------------

    def get_decision(self, tender_id: str, filter_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM filter_decisions WHERE id = ? AND filter_key = ?", (tender_id, filter_key)
        ).fetchone()

    def save_decision(self, feed_item: dict[str, Any], decision: Decision, filter_key: str) -> None:
        self.conn.execute(
            """INSERT INTO filter_decisions
                 (id, filter_key, tender_id, relevant, stage, reason, status, date_modified, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id, filter_key) DO UPDATE SET tender_id=excluded.tender_id, relevant=excluded.relevant,
                 stage=excluded.stage, reason=excluded.reason, status=excluded.status,
                 date_modified=excluded.date_modified, checked_at=excluded.checked_at""",
            (
                feed_item["id"],
                filter_key,
                feed_item.get("tenderID"),
                int(decision.relevant),
                decision.stage,
                decision.reason,
                feed_item.get("status"),
                feed_item.get("dateModified"),
                now_iso(),
            ),
        )

    def rejected_decisions(self, profile: str, stages: tuple[str, ...] = ("topic", "value")) -> list[dict[str, Any]]:
        """Latest negative decision per tender for any version of `profile` (filter keys "<profile>:<hash>")."""
        marks = ",".join("?" * len(stages))
        rows = self.conn.execute(
            f"""SELECT id, tender_id, stage, reason, max(checked_at) AS checked_at FROM filter_decisions
                WHERE filter_key LIKE ? AND relevant = 0 AND stage IN ({marks}) GROUP BY id""",
            (f"{profile}:%", *stages),
        ).fetchall()
        return [dict(r) for r in rows]

    # filter matches ------------------------------------------------------------------------------

    def save_match(self, tender_id: str, profile: str, filter_key: str, decision: Decision) -> None:
        items = [
            {"description": m.description, "cpv": m.cpv, "topic": m.topic, "match_reason": m.reason}
            for m in decision.matches
        ]
        self.conn.execute(
            """INSERT INTO tender_matches (tender, profile, filter_key, topics, relevant_value, reason, items)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tender, profile) DO UPDATE SET filter_key=excluded.filter_key, topics=excluded.topics,
                 relevant_value=excluded.relevant_value, reason=excluded.reason, items=excluded.items""",
            (
                tender_id,
                profile,
                filter_key,
                ",".join(decision.topics),
                decision.relevant_value,
                decision.reason,
                json.dumps(items, ensure_ascii=False),
            ),
        )

    def delete_match(self, tender_id: str, profile: str) -> None:
        self.conn.execute("DELETE FROM tender_matches WHERE tender = ? AND profile = ?", (tender_id, profile))

    def tenders_stamp(self) -> str:
        """Changes whenever a tender is added or re-saved."""
        n, last = self.conn.execute("SELECT count(*), max(fetched_at) FROM tenders").fetchone()
        return f"{n}:{last}"

    def all_tender_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT id FROM tenders")]

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

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
                    None,  # relevant_value: per filter profile, see tender_matches / tender_profile_view
                    None,  # topics: per filter profile, see tender_matches / tender_profile_view
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
        profile: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Search stored tenders. limit=None returns all matches.

        With `profile`, only tenders relevant under that filter profile are returned, and topics/relevant_value
        come from that profile's evaluation.
        """
        where, args = [], []
        # Without a profile there is no relevant value or topic: fall back to the whole tender value.
        join, topics_col, value_col = "", "NULL", "t.value_amount"
        if profile:
            join = "JOIN tender_matches m ON m.tender = t.id AND m.profile = ?"
            args.append(profile)
            topics_col, value_col = "m.topics", "m.relevant_value"
        if query:
            where.append("t.id IN (SELECT id FROM tenders_fts WHERE tenders_fts MATCH ?)")
            args.append(to_fts_query(query))
        if topic:
            where.append(f"(',' || {topics_col} || ',') LIKE ?")
            args.append(f"%,{topic},%")
        if min_value is not None:
            where.append(f"{value_col} >= ?")
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
            "value_desc": f"{value_col} DESC",
            "value_asc": f"{value_col} ASC",
            "deadline_asc": "t.tender_period_end ASC",
        }.get(sort, "t.date_created DESC")
        total = self.conn.execute(f"SELECT count(*) FROM tenders t {join} {sql_where}", args).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT t.id, t.tender_id, t.title, t.status, t.procurement_method_type, t.entity_name,
                       t.entity_edrpou, t.entity_region, t.value_amount, {value_col} AS relevant_value, t.currency,
                       {topics_col} AS topics, t.date_created, t.tender_period_end
                FROM tenders t {join} {sql_where} ORDER BY {order} LIMIT ? OFFSET ?""",
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

    def matched_items(self, tender_id: str, profile: str | None = None) -> list[dict[str, Any]]:
        if profile:
            row = self.conn.execute(
                "SELECT items FROM tender_matches WHERE tender = ? AND profile = ?", (tender_id, profile)
            ).fetchone()
            return json.loads(row[0]) if row and row[0] else []
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

    def last_finished_run(self, filter_name: str) -> dict[str, Any] | None:
        for row in self.conn.execute(
            "SELECT * FROM sync_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 200"
        ).fetchall():
            params = json.loads(row["params"] or "{}")
            if params.get("filter") == filter_name:
                return {**dict(row), "params": params}
        return None

    def last_run(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        d["params"] = json.loads(d["params"] or "{}")
        d["stats"] = json.loads(d["stats"] or "null")
        return d

    def counts(self, profile: str | None = None) -> dict[str, Any]:
        c = self.conn.execute
        out: dict[str, Any] = {
            "stored_tenders": c("SELECT count(*) FROM tenders").fetchone()[0],
            "by_profile": {
                r[0]: r[1] for r in c("SELECT profile, count(*) FROM tender_matches GROUP BY profile").fetchall()
            },
        }
        if profile:
            out["relevant_tenders"] = out["by_profile"].get(profile, 0)
        return out

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
