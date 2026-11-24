"""SQLite 持久化：事件（含前缀预算量）与锚点。

设计要点：
* 全局只维护一份在用事件账（dataset），重新上传即重建；
* 每个事件在导入时就写好 prefix_min/prefix_amb，核对锚点时
  只需按 event_no 做点查，不重读整份事件；
* 确认更正与复核放在同一事务内，失败或冲突即整体回滚，原账保留。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .reconcile import analyze_counts

SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    filename TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS event (
    event_no INTEGER PRIMARY KEY,
    count INTEGER NOT NULL,
    prefix_min INTEGER NOT NULL,
    prefix_amb INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS anchor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_no INTEGER NOT NULL UNIQUE,
    utc_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def has_dataset(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM dataset WHERE id = 1").fetchone() is not None


def get_dataset_meta(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT filename, event_count, created_at FROM dataset WHERE id = 1"
    ).fetchone()
    return dict(row) if row else None


def replace_dataset(conn: sqlite3.Connection, filename: str, counts: list[int]) -> dict:
    """重建事件账并清空旧锚点，单事务提交。"""
    records = analyze_counts(counts)
    try:
        with conn:
            conn.execute("DELETE FROM anchor")
            conn.execute("DELETE FROM event")
            conn.execute("DELETE FROM dataset")
            conn.executemany(
                "INSERT INTO event (event_no, count, prefix_min, prefix_amb)"
                " VALUES (:event_no, :count, :prefix_min, :prefix_amb)",
                records,
            )
            conn.execute(
                "INSERT INTO dataset (id, filename, event_count) VALUES (1, ?, ?)",
                (filename, len(records)),
            )
    except sqlite3.Error:
        raise
    return {"filename": filename, "event_count": len(records)}


def load_records(conn: sqlite3.Connection) -> dict[int, tuple[int, int]]:
    """取出全部事件的前缀量（一次小表加载；核对时为字典点查，不扫描事件）。"""
    return {
        row["event_no"]: (row["prefix_min"], row["prefix_amb"])
        for row in conn.execute(
            "SELECT event_no, prefix_min, prefix_amb FROM event ORDER BY event_no"
        )
    }


def event_exists(conn: sqlite3.Connection, event_no: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM event WHERE event_no = ?", (event_no,)
    ).fetchone() is not None


def list_events(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    sql = "SELECT event_no, count, prefix_min, prefix_amb FROM event ORDER BY event_no"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql)]


def list_anchors(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, event_no, utc_ms, created_at FROM anchor ORDER BY event_no"
        )
    ]


def add_anchor(conn: sqlite3.Connection, event_no: int, utc_ms: int) -> dict:
    """补登记锚点；事件号重复时抛 IntegrityError，由调用层保留原账。"""
    cur = conn.execute(
        "INSERT INTO anchor (event_no, utc_ms) VALUES (?, ?)", (event_no, utc_ms)
    )
    row = conn.execute(
        "SELECT id, event_no, utc_ms, created_at FROM anchor WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return dict(row)


def delete_anchor(conn: sqlite3.Connection, anchor_id: int) -> bool:
    cur = conn.execute("DELETE FROM anchor WHERE id = ?", (anchor_id,))
    return cur.rowcount > 0


def update_anchor_utc(conn: sqlite3.Connection, anchor_id: int, utc_ms: int) -> bool:
    cur = conn.execute(
        "UPDATE anchor SET utc_ms = ? WHERE id = ?", (utc_ms, anchor_id)
    )
    return cur.rowcount > 0
