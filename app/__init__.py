"""Flask 核查页应用工厂与路由。"""

from __future__ import annotations

import os
import sqlite3

from flask import Flask, jsonify, render_template, request

from . import db as dbmod
from .csv_loader import CsvError, parse_events_csv
from .missing import MissingError, compute_missing
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

    # ---------- 漏导核算（只读同一已提交状态，不改动事件/锚点） ----------
    def _select_segment_anchors(conn, left_event, right_event):
        rows = conn.execute(
            "SELECT id, event_no, utc_ms FROM anchor "
            "WHERE event_no BETWEEN ? AND ? ORDER BY event_no",
            (left_event, right_event),
        ).fetchall()
        anchors = [dict(r) for r in rows]
        endpoint_ok = (
            len(anchors) >= 2
            and anchors[0]["event_no"] == left_event
            and anchors[-1]["event_no"] == right_event
        )
        return anchors, endpoint_ok

    @app.post("/api/missing/compute")
    def missing_compute():
        payload = request.get_json(silent=True) or {}
        try:
            left_event = int(payload["left_event"])
            right_event = int(payload["right_event"])
        except (KeyError, TypeError, ValueError):
            return fail("left_event 与 right_event 必须是整数")
        if left_event >= right_event:
            return fail("右端事件号必须大于左端事件号")

        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("请先上传事件 CSV")
            meta = dbmod.get_dataset_meta(conn)
            if not dbmod.event_exists(conn, left_event) or not dbmod.event_exists(
                conn, right_event
            ):
                return fail("选段端点事件号不在事件账内")

            anchors, endpoint_ok = _select_segment_anchors(
                conn, left_event, right_event
            )
            if not endpoint_ok:
                return fail(
                    "选段两端事件必须都已登记锚点，且段内至少两条锚点"
                )

            events = dbmod.fetch_event_range(conn, left_event, right_event)
            # 纯只读核算：不加锁改写，期间补登/改锚点/换账照常进行
            try:
                result = compute_missing(events, anchors)
            except MissingError as exc:
                return fail(str(exc))
            result["dataset_version"] = meta["version"]
            result["filename"] = meta["filename"]
            return ok(result=result)
        finally:
            conn.close()

    @app.post("/api/missing/records")
    def missing_save():
        payload = request.get_json(silent=True) or {}
        result = payload.get("result")
        if not isinstance(result, dict):
            return fail("请回传核算结果 result")
        try:
            version = int(result["dataset_version"])
            left_event = int(result["selection"]["left_event"])
            right_event = int(result["selection"]["right_event"])
        except (KeyError, TypeError, ValueError):
            return fail("核算结果缺少选段或版本信息，请重新核算")

        conn = get_db()
        try:
            if not dbmod.has_dataset(conn):
                return fail("当前无事件账，无法登记核算结果")
            meta = dbmod.get_dataset_meta(conn)

            # 落账前复检：换账或段内锚点有出入则拒绝，要求重算；
            # 段外锚点变更不拦截。失败时不写入，已有记录原样保留。
            if meta["version"] != version:
                return fail(
                    "核算后已换账（版本变化），请重新核算后再登记",
                    409,
                    stale_reason="dataset_changed",
                )
            anchors, endpoint_ok = _select_segment_anchors(
                conn, left_event, right_event
            )
            snapshot = result["selection"].get("anchors", [])
            snapshot_norm = [
                {
                    "anchor_id": a.get("anchor_id"),
                    "event_no": a["event_no"],
                    "utc_ms": a["utc_ms"],
                }
                for a in snapshot
            ]
            current_norm = [
                {"anchor_id": a["id"], "event_no": a["event_no"], "utc_ms": a["utc_ms"]}
                for a in anchors
            ]
            if not endpoint_ok or current_norm != snapshot_norm:
                return fail(
                    "段内锚点与核算时有出入，请重新核算后再登记",
                    409,
                    stale_reason="anchors_changed",
                )

            try:
                with conn:
                    record = dbmod.save_missing_record(conn, result)
            except sqlite3.Error as exc:
                return fail(f"登记失败，已有记录保留：{exc}", 500)
            record["freshness"] = dbmod.assess_missing_freshness(conn, record)
            return ok(record=record, message="漏导核算结果已登记，可凭编号回查")
        finally:
            conn.close()

    @app.get("/api/missing/records")
    def missing_list():
        conn = get_db()
        try:
            records = dbmod.list_missing_records(conn)
            for record in records:
                record["freshness"] = dbmod.assess_missing_freshness(conn, record)
            return ok(records=records)
        finally:
            conn.close()

    @app.get("/api/missing/records/<int:record_id>")
    def missing_detail(record_id: int):
        conn = get_db()
        try:
            record = dbmod.get_missing_record(conn, record_id)
            if record is None:
                return fail("核算记录不存在", 404)
            record["freshness"] = dbmod.assess_missing_freshness(conn, record)
            return ok(record=record)
        finally:
            conn.close()

    return app
