"""Flask 端到端测试：python -m unittest tests.test_app -v

覆盖：
* CSV 上传建账与非法 CSV 拒绝；
* 锚点补登记、重复事件号冲突保留原账；
* 不足三条 / 一致 / 不一致三种核查结论；
* 确认更正：合法值保存且全账一致；非法值被拒且原时刻回滚保留；
* 双错账提示“单处更正无法对平”；
* 重新上传重建账并清空旧锚点。
"""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.reconcile import MOD  # noqa: E402

DEMO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "demo")


def read_anchor_csv(name):
    import csv
    with open(os.path.join(DEMO_DIR, name), newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))[1:]
    return [(int(r[0]), int(r[1])) for r in rows]


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app(db_path=db_path)
        self.client = self.app.test_client()
        with open(os.path.join(DEMO_DIR, "events.csv"), "rb") as fh:
            self.csv_bytes = fh.read()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, data: bytes | None = None, filename: str = "events.csv"):
        return self.client.post(
            "/api/dataset",
            data={"file": (io.BytesIO(data if data is not None else self.csv_bytes), filename)},
            content_type="multipart/form-data",
        )

    def add_anchor(self, event_no, utc_ms):
        return self.client.post(
            "/api/anchors", json={"event_no": event_no, "utc_ms": utc_ms}
        )

    def test_index_renders(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("振动记录仪", resp.get_data(as_text=True))

    def test_upload_builds_dataset(self):
        resp = self.upload()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["dataset"]["event_count"], 6)

        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["dataset"]["event_count"], 6)
        self.assertEqual(state["anchors"], [])

    def test_upload_rejects_bad_csv(self):
        bad = "事件号,计数\n1,99999\n2,1\n".encode()
        resp = self.upload(bad)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("CSV 校验失败", resp.get_json()["error"])

    def test_full_flow_good_ledger(self):
        self.upload()
        for event_no, utc in read_anchor_csv("anchors_good.csv"):
            resp = self.add_anchor(event_no, utc)
            self.assertEqual(resp.status_code, 200)

        result = self.client.post("/api/verify").get_json()["result"]
        self.assertEqual(result["status"], "consistent")
        self.assertTrue(all(p["ok"] for p in result["pairs"]))

    def test_duplicate_anchor_keeps_original(self):
        self.upload()
        self.add_anchor(1, 123)
        resp = self.add_anchor(1, 456)
        self.assertEqual(resp.status_code, 409)
        anchors = self.client.get("/api/state").get_json()["anchors"]
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["utc_ms"], 123)  # 原值保留

    def test_anchor_event_must_exist(self):
        self.upload()
        resp = self.add_anchor(99, 1000)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不在事件账内", resp.get_json()["error"])

    def test_insufficient_anchors(self):
        self.upload()
        self.add_anchor(1, 1)
        self.add_anchor(3, 2)
        result = self.client.post("/api/verify").get_json()["result"]
        self.assertEqual(result["status"], "insufficient")

    def test_single_error_and_successful_correction(self):
        self.upload()
        bad = read_anchor_csv("anchors_bad_single.csv")
        for event_no, utc in bad:
            self.add_anchor(event_no, utc)

        verified = self.client.post("/api/verify").get_json()["result"]
        self.assertEqual(verified["status"], "inconsistent")
        self.assertEqual(len(verified["candidates"]), 1)
        cand = verified["candidates"][0]
        self.assertEqual(cand["event_no"], 3)
        self.assertEqual(cand["step"], MOD)
        self.assertTrue(cand["first"] <= cand["last"])

        # 用候选首值确认更正 -> 应保存并复核一致
        resp = self.client.post(
            "/api/correct",
            json={"anchor_id": cand["anchor_id"], "new_utc_ms": cand["first"]},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(body["result"]["status"], "consistent")
        anchors = {a["event_no"]: a["utc_ms"] for a in body["anchors"]}
        self.assertEqual(anchors[3], cand["first"])

    def test_invalid_correction_rolls_back(self):
        self.upload()
        bad = read_anchor_csv("anchors_bad_single.csv")
        for event_no, utc in bad:
            self.add_anchor(event_no, utc)
        anchors_before = self.client.get("/api/state").get_json()["anchors"]
        anchor_id = next(a["id"] for a in anchors_before if a["event_no"] == 3)
        original = next(a["utc_ms"] for a in anchors_before if a["event_no"] == 3)

        # 故意提交一个无法对平的值（候选末值再加一圈）
        verified = self.client.post("/api/verify").get_json()["result"]
        bad_value = verified["candidates"][0]["last"] + MOD
        resp = self.client.post(
            "/api/correct", json={"anchor_id": anchor_id, "new_utc_ms": bad_value}
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("更正已撤销", resp.get_json()["error"])

        # 原账保留
        anchors_after = self.client.get("/api/state").get_json()["anchors"]
        kept = next(a for a in anchors_after if a["id"] == anchor_id)
        self.assertEqual(kept["utc_ms"], original)

        # 再核查仍是原来的不一致状态
        again = self.client.post("/api/verify").get_json()["result"]
        self.assertEqual(again["status"], "inconsistent")
        self.assertEqual(len(again["candidates"]), 1)

    def test_double_error_reports_no_single_fix(self):
        self.upload()
        for event_no, utc in read_anchor_csv("anchors_bad_double.csv"):
            self.add_anchor(event_no, utc)
        result = self.client.post("/api/verify").get_json()["result"]
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual(result["candidates"], [])
        self.assertIn("单处更正无法对平", result["message"])

    def test_reupload_resets_anchors(self):
        self.upload()
        self.add_anchor(1, 123)
        resp = self.upload()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["anchors"], [])

    def test_add_anchors_during_verification_session(self):
        # 模拟“核查期间补登记”：先两条 -> 不足；补第三条 -> 可核查
        self.upload()
        good = read_anchor_csv("anchors_good.csv")
        self.add_anchor(*good[0])
        self.add_anchor(*good[1])
        self.assertEqual(
            self.client.post("/api/verify").get_json()["result"]["status"],
            "insufficient",
        )
        self.add_anchor(*good[2])
        result = self.client.post("/api/verify").get_json()["result"]
        self.assertEqual(result["status"], "consistent")


class FreshDbTest(unittest.TestCase):
    def test_operations_without_dataset(self):
        tmp = tempfile.TemporaryDirectory()
        app = create_app(db_path=os.path.join(tmp.name, "fresh.db"))
        client = app.test_client()
        self.assertEqual(client.post("/api/verify").status_code, 400)
        self.assertEqual(
            client.post("/api/anchors", json={"event_no": 1, "utc_ms": 1}).status_code,
            400,
        )
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
