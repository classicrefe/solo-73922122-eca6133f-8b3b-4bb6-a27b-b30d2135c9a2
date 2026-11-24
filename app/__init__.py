"""Flask 核查页应用工厂与路由。"""

from __future__ import annotations

import os
import sqlite3

from flask import Flask, jsonify, render_template, request

from . import db as dbmod
from .csv_loader import CsvError, parse_events_csv
from .reconcile import MIN_ANCHORS, verify


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.getcwd(), "data", "inspection.db")
    )

    def get_db() -> sqlite3.Connection:
        conn = dbmod.connect(app.config["DB_PATH"])
        return conn

    def ok(**payload):
        return jsonify({"ok": True, **payload})

    def fail(message: str, status: int = 400, **extra):
        return jsonify({"ok": False, "error": message, **extra}), status

    # ---------- 页面 ----------
    @app.get("/")
    def index():
        return render_template("index.html", min_anchors=MIN_ANCHORS)

    # ---------- 状态 ----------
    @app.get("/api/state")
    def state():
        conn = get_db()
        try:
            meta = dbmod.get_dataset_meta(conn)
            return ok(
                dataset=meta,
                anchors=dbmod.list_anchors(conn),
                min_anchors=MIN_ANCHORS,
            )
        finally:
            conn.close()

    @app.get("/api/events")
    def events():
        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("尚未上传事件 CSV", 404)
            limit = request.args.get("limit", type=int)
            return ok(events=dbmod.list_events(conn, limit=limit))
        finally:
            conn.close()

    # ---------- 上传事件 CSV（重建账） ----------
    @app.post("/api/dataset")
    def upload_dataset():
        upload = request.files.get("file")
        if upload is None or upload.filename == "":
            return fail("请选择 CSV 文件")
        try:
            text = upload.read().decode("utf-8-sig")
            parsed = parse_events_csv(text)
        except UnicodeDecodeError:
            return fail("CSV 需为 UTF-8 编码")
        except CsvError as exc:
            return fail(f"CSV 校验失败：{exc}")

        conn = get_db()
        try:
            meta = dbmod.replace_dataset(
                conn, upload.filename, [ev["count"] for ev in parsed]
            )
            return ok(
                dataset=meta,
                anchors=dbmod.list_anchors(conn),
                message="事件账已重建，旧锚点已清空",
            )
        finally:
            conn.close()

    # ---------- 锚点登记（核查期间可随时补登记） ----------
    @app.post("/api/anchors")
    def create_anchor():
        payload = request.get_json(silent=True) or {}
        try:
            event_no = int(payload["event_no"])
            utc_ms = int(payload["utc_ms"])
        except (KeyError, TypeError, ValueError):
            return fail("event_no 与 utc_ms 必须是整数")
        if utc_ms < 0:
            return fail("UTC 毫秒不能为负")

        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("请先上传事件 CSV")
            if not dbmod.event_exists(conn, event_no):
                return fail(f"事件号 {event_no} 不在事件账内")
            try:
                with conn:
                    anchor = dbmod.add_anchor(conn, event_no, utc_ms)
            except sqlite3.IntegrityError:
                return fail(f"事件号 {event_no} 已登记锚点，原账未改动", 409)
            return ok(anchor=anchor, anchors=dbmod.list_anchors(conn))
        finally:
            conn.close()

    @app.delete("/api/anchors/<int:anchor_id>")
    def remove_anchor(anchor_id: int):
        conn = get_db()
        try:
            removed = dbmod.delete_anchor(conn, anchor_id)
            conn.commit()
            if not removed:
                return fail("锚点不存在", 404)
            return ok(anchors=dbmod.list_anchors(conn))
        finally:
            conn.close()

    # ---------- 核查 ----------
    @app.post("/api/verify")
    def run_verify():
        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("请先上传事件 CSV")
            records = dbmod.load_records(conn)
            anchors = dbmod.list_anchors(conn)
            result = verify(records, anchors)
            return ok(result=result, anchors=anchors)
        finally:
            conn.close()

    # ---------- 确认更正：按当时全部锚点复核，冲突/失败保留原账 ----------
    @app.post("/api/correct")
    def confirm_correction():
        payload = request.get_json(silent=True) or {}
        try:
            anchor_id = int(payload["anchor_id"])
            new_utc = int(payload["new_utc_ms"])
        except (KeyError, TypeError, ValueError):
            return fail("anchor_id 与 new_utc_ms 必须是整数")
        if new_utc < 0:
            return fail("UTC 毫秒不能为负")

        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("请先上传事件 CSV")
            anchor = conn.execute(
                "SELECT id, event_no, utc_ms FROM anchor WHERE id = ?", (anchor_id,)
            ).fetchone()
            if anchor is None:
                return fail("锚点不存在或已被删除", 404)

            records = dbmod.load_records(conn)
            try:
                # 单事务：改写 -> 按当时全部锚点复核 -> 不一致/异常则回滚
                with conn:
                    dbmod.update_anchor_utc(conn, anchor_id, new_utc)
                    anchors_now = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT id, event_no, utc_ms FROM anchor ORDER BY event_no"
                        )
                    ]
                    result = verify(records, anchors_now)
                    if result["status"] != "consistent":
                        raise RuntimeError("复核未通过：新时刻无法让全账一致")
            except sqlite3.Error as exc:
                return fail(f"保存失败，已保留原账：{exc}", 500)
            except RuntimeError as exc:
                return fail(
                    f"{exc}，更正已撤销",
                    409,
                    result=result,
                    original_utc_ms=anchor["utc_ms"],
                )
            return ok(
                result=result,
                anchors=dbmod.list_anchors(conn),
                corrected={
                    "anchor_id": anchor_id,
                    "event_no": anchor["event_no"],
                    "old_utc_ms": anchor["utc_ms"],
                    "new_utc_ms": new_utc,
                },
                message="更正已保存，全账复核一致",
            )
        finally:
            conn.close()

    return app
