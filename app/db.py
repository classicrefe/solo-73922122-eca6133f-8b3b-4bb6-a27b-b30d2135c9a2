"""SQLite 持久化：事件（含前缀预算量）与锚点。

设计要点：
* 全局只维护一份在用事件账（dataset），重新上传即重建；
* 每个事件在导入时就写好 prefix_min/prefix_amb，核对锚点时
  只需按 event_no 做点查，不重读整份事件；
* 确认更正与复核放在同一事务内，失败或冲突即整体回滚，原账保留。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .reconcile import analyze_counts

SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    filename TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
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
CREATE TABLE IF NOT EXISTS missing_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_version INTEGER NOT NULL,
    filename TEXT NOT NULL,
    left_event INTEGER NOT NULL,
    right_event INTEGER NOT NULL,
    left_utc_ms INTEGER NOT NULL,
    right_utc_ms INTEGER NOT NULL,
    span_ms INTEGER NOT NULL,
    imported_event_count INTEGER NOT NULL,
    anchor_snapshot TEXT NOT NULL,
    status TEXT NOT NULL,
    total_missing INTEGER,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 旧库（dataset 无 version 列）的在线迁移
_MIGRATIONS = [
    (
        "SELECT 1 FROM pragma_table_info('dataset') WHERE name = 'version'",
        "ALTER TABLE dataset ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
    ),
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for probe, ddl in _MIGRATIONS:
        if conn.execute(probe).fetchone() is None:
            conn.execute(ddl)
    conn.commit()
    return conn


def has_dataset(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM dataset WHERE id = 1").fetchone() is not None


def get_dataset_meta(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT filename, event_count, version, created_at "
        "FROM dataset WHERE id = 1"
    ).fetchone()
    return dict(row) if row else None


def replace_dataset(conn: sqlite3.Connection, filename: str, counts: list[int]) -> dict:
    """重建事件账并清空旧锚点，单事务提交。

    漏导核算记录（missing_record）属于历史留痕，不随换账删除，
    只把 dataset.version 加一，旧记录凭编号仍可回查并标记为换账后过期。
    """
    records = analyze_counts(counts)
    try:
        with conn:
            prev = conn.execute("SELECT version FROM dataset WHERE id = 1").fetchone()
            next_version = (prev["version"] if prev else 0) + 1
            conn.execute("DELETE FROM anchor")
            conn.execute("DELETE FROM event")
            conn.execute("DELETE FROM dataset")
            conn.executemany(
                "INSERT INTO event (event_no, count, prefix_min, prefix_amb)"
                " VALUES (:event_no, :count, :prefix_min, :prefix_amb)",
                records,
            )
            conn.execute(
                "INSERT INTO dataset (id, filename, event_count, version)"
                " VALUES (1, ?, ?, ?)",
                (filename, len(records), next_version),
            )
    except sqlite3.Error:
        raise
    return {
        "filename": filename,
        "event_count": len(records),
        "version": next_version,
    }


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


# ---------------------------------------------------------------- 漏导核算记录


def fetch_event_range(
    conn: sqlite3.Connection, left_event: int, right_event: int
) -> list[dict]:
    """取出选段 [left_event, right_event] 内的已导事件（含计数）。"""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT event_no, count FROM event "
            "WHERE event_no BETWEEN ? AND ? ORDER BY event_no",
            (left_event, right_event),
        )
    ]


def save_missing_record(conn: sqlite3.Connection, entry: dict) -> dict:
    """落账一条漏导核算结果（成功/对不上均留痕），返回含编号的行。"""
    anchors = entry["selection"]["anchors"]
    cur = conn.execute(
        "INSERT INTO missing_record ("
        "dataset_version, filename, left_event, right_event, left_utc_ms, "
        "right_utc_ms, span_ms, imported_event_count, anchor_snapshot, "
        "status, total_missing, result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry["dataset_version"],
            entry["filename"],
            entry["selection"]["left_event"],
            entry["selection"]["right_event"],
            entry["selection"]["left_utc_ms"],
            entry["selection"]["right_utc_ms"],
            entry["selection"]["span_ms"],
            entry["selection"]["imported_event_count"],
            json.dumps(anchors, ensure_ascii=False, sort_keys=True),
            entry["status"],
            entry["total_missing"],
            json.dumps(entry, ensure_ascii=False, sort_keys=True),
        ),
    )
    return get_missing_record(conn, cur.lastrowid)


def _row_to_record(row: sqlite3.Row) -> dict:
    record = dict(row)
    record["anchor_snapshot"] = json.loads(record["anchor_snapshot"])
    record["result"] = json.loads(record["result_json"])
    return record


def list_missing_records(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, dataset_version, filename, left_event, right_event, "
        "left_utc_ms, right_utc_ms, span_ms, imported_event_count, "
        "anchor_snapshot, status, total_missing, created_at "
        "FROM missing_record ORDER BY id DESC"
    ).fetchall()
    records = []
    for row in rows:
        record = dict(row)
        record["anchor_snapshot"] = json.loads(record["anchor_snapshot"])
        records.append(record)  # 列表不含完整 result，详情接口才带
    return records


def get_missing_record(conn: sqlite3.Connection, record_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM missing_record WHERE id = ?", (record_id,)
    ).fetchone()
    if row is None:
        return None
    record = _row_to_record(row)
    record.pop("result_json", None)  # _row_to_record 已解析为 result
    return record


def assess_missing_freshness(
    conn: sqlite3.Connection, record: dict
) -> dict:
    """判断已保存的核算记录当前是否仍可作准。

    换账（dataset.version 变化）或段内锚点与核算时有出入 -> stale；
    段外锚点的增删改不影响，仍为 current。
    """
    meta = get_dataset_meta(conn)
    reasons: list[str] = []
    if meta is None:
        reasons.append("当前无事件账")
    elif meta["version"] != record["dataset_version"]:
        reasons.append(
            f"已换账（核算时版本 {record['dataset_version']}，"
            f"当前版本 {meta['version']}），请重算"
        )

    current = conn.execute(
        "SELECT id, event_no, utc_ms FROM anchor "
        "WHERE event_no BETWEEN ? AND ? ORDER BY event_no",
        (record["left_event"], record["right_event"]),
    ).fetchall()
    current_norm = [
        {"anchor_id": r["id"], "event_no": r["event_no"], "utc_ms": r["utc_ms"]}
        for r in current
    ]
    snapshot_norm = [
        {
            "anchor_id": a.get("anchor_id"),
            "event_no": a["event_no"],
            "utc_ms": a["utc_ms"],
        }
        for a in record["anchor_snapshot"]
    ]
    if current_norm != snapshot_norm:
        reasons.append("段内锚点与核算时有出入（补登/删除/更正），请重算")

    return {
        "freshness": "stale" if reasons else "current",
        "reasons": reasons,
        "current_anchors": current_norm,
    }
