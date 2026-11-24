"""漏导核算 Flask 端到端测试：python -m unittest tests.test_missing_app -v

覆盖：
* 核算前提校验（端点锚点、跨度/条数上限、事件号范围）；
* 最少漏导结论与逐间隔安排；对不上时提示核查锚点或计数；
* 核算只读：期间补登锚点不影响核算本身；
* 登记前复检：换账/段内锚点变更 -> 409 要求重算，已有记录保留；
  段外锚点变更可照常登记；
* 换账后记录不删除，凭编号可回查并标记需重算。
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
GAPS = [1000, 4464, 20000, 70000, 50000]
UTC = [1700000000000]
for g in GAPS:
    UTC.append(UTC[-1] + g)


class MissingAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(db_path=os.path.join(self.tmp.name, "test.db"))
        self.client = self.app.test_client()
        with open(os.path.join(DEMO_DIR, "events.csv"), "rb") as fh:
            self.csv_bytes = fh.read()
        self.upload()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self):
        return self.client.post(
            "/api/dataset",
            data={"file": (io.BytesIO(self.csv_bytes), "events.csv")},
            content_type="multipart/form-data",
        )

    def add_anchor(self, event_no, utc_ms):
        return self.client.post(
            "/api/anchors", json={"event_no": event_no, "utc_ms": utc_ms}
        )

    def compute(self, left=1, right=6):
        return self.client.post(
            "/api/missing/compute",
            json={"left_event": left, "right_event": right},
        )

    def seed_two_endpoints(self, extra_turns=0):
        self.add_anchor(1, UTC[0])
        self.add_anchor(6, UTC[5] + extra_turns * MOD)

    def test_state_exposes_dataset_version(self):
        meta = self.client.get("/api/state").get_json()["dataset"]
        self.assertEqual(meta["version"], 1)

    def test_compute_requires_endpoint_anchors(self):
        self.add_anchor(1, UTC[0])
        resp = self.compute()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("两端事件必须都已登记锚点", resp.get_json()["error"])

    def test_compute_zero_missing_and_breakdown(self):
        self.seed_two_endpoints()
        resp = self.compute()
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        result = resp.get_json()["result"]
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["total_missing"], 0)
        interval = result["subsegments"][0]["intervals"][0]
        self.assertEqual(interval["left_event"], 1)
        self.assertEqual(interval["right_event"], 2)
        self.assertEqual(interval["elapsed_ms"], 1000)
        self.assertEqual(interval["missing"], 0)

    def test_compute_one_missing(self):
        self.seed_two_endpoints(extra_turns=4)
        result = self.compute().get_json()["result"]
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["total_missing"], 1)
        sub = result["subsegments"][0]
        self.assertEqual(sum(it["missing"] for it in sub["intervals"]), 1)
        self.assertEqual(
            sum(it["elapsed_ms"] for it in sub["intervals"]),
            UTC[5] - UTC[0] + 4 * MOD,
        )

    def test_compute_impossible(self):
        # 时差比最小可达耗时还小 1ms -> 即使允许漏导也对不上
        self.add_anchor(1, UTC[0])
        self.add_anchor(6, UTC[0] + 1)
        result = self.compute().get_json()["result"]
        self.assertEqual(result["status"], "impossible")
        self.assertIsNone(result["total_missing"])
        self.assertIn("核查锚点或计数", result["message"])

    def test_compute_rejects_overlong_span(self):
        self.add_anchor(1, 0)
        self.add_anchor(6, 30 * 60 * 1000 + 1)
        resp = self.compute()
        self.assertEqual(resp.status_code, 400)
        self.assertIn("30 分钟", resp.get_json()["error"])

    def test_compute_is_read_only(self):
        self.seed_two_endpoints()
        before = self.client.get("/api/state").get_json()["anchors"]
        self.compute()
        after = self.client.get("/api/state").get_json()["anchors"]
        self.assertEqual(before, after)

    def test_save_then_lookup(self):
        self.seed_two_endpoints(extra_turns=4)
        result = self.compute().get_json()["result"]
        saved = self.client.post(
            "/api/missing/records", json={"result": result}
        ).get_json()["record"]
        self.assertEqual(saved["status"], "feasible")
        self.assertEqual(saved["total_missing"], 1)
        self.assertEqual(saved["freshness"]["freshness"], "current")

        listing = self.client.get("/api/missing/records").get_json()["records"]
        self.assertEqual([r["id"] for r in listing], [saved["id"]])

        detail = self.client.get(
            f"/api/missing/records/{saved['id']}"
        ).get_json()["record"]
        self.assertEqual(detail["result"]["selection"]["left_event"], 1)
        self.assertEqual(detail["result"]["total_missing"], 1)
        self.assertEqual(len(detail["anchor_snapshot"]), 2)
        self.assertEqual(detail["freshness"]["freshness"], "current")

    def test_save_impossible_result_is_kept(self):
        self.add_anchor(1, UTC[0])
        self.add_anchor(6, UTC[0] + 1)
        result = self.compute().get_json()["result"]
        saved = self.client.post(
            "/api/missing/records", json={"result": result}
        ).get_json()["record"]
        self.assertEqual(saved["status"], "impossible")
        self.assertIsNone(saved["total_missing"])

    def test_segment_anchor_change_forces_recompute_but_keeps_old_records(self):
        self.seed_two_endpoints()
        result = self.compute().get_json()["result"]
        saved = self.client.post(
            "/api/missing/records", json={"result": result}
        ).get_json()["record"]

        # 段内补登锚点 -> 用旧结果登记被拒绝
        self.add_anchor(3, UTC[2])
        resp = self.client.post("/api/missing/records", json={"result": result})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["stale_reason"], "anchors_changed")

        # 旧记录保留，且回查标记需重算
        detail = self.client.get(
            f"/api/missing/records/{saved['id']}"
        ).get_json()["record"]
        self.assertEqual(detail["freshness"]["freshness"], "stale")
        self.assertTrue(detail["freshness"]["reasons"])

        # 重算后可登记新记录
        new_result = self.compute().get_json()["result"]
        saved2 = self.client.post(
            "/api/missing/records", json={"result": new_result}
        ).get_json()["record"]
        self.assertEqual(saved2["freshness"]["freshness"], "current")
        ids = [r["id"] for r in self.client.get(
            "/api/missing/records").get_json()["records"]]
        self.assertEqual(sorted(ids), sorted([saved["id"], saved2["id"]]))

    def test_out_of_segment_anchor_change_allows_save(self):
        self.seed_two_endpoints(extra_turns=4)
        result = self.compute().get_json()["result"]
        # 段外（此处选段为 1..6，无段外事件；改用更短选段验证段外不拦截）
        self.upload()
        self.add_anchor(3, UTC[2])
        self.add_anchor(5, UTC[4])
        sub = self.client.post(
            "/api/missing/compute",
            json={"left_event": 3, "right_event": 5},
        ).get_json()["result"]
        # 段外锚点：事件 1（选段 3..5 之外）
        self.add_anchor(1, UTC[0])
        resp = self.client.post("/api/missing/records", json={"result": sub})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        record = resp.get_json()["record"]
        self.assertEqual(record["freshness"]["freshness"], "current")

    def test_reupload_makes_record_stale_but_lookup_survives(self):
        self.seed_two_endpoints(extra_turns=4)
        result = self.compute().get_json()["result"]
        saved = self.client.post(
            "/api/missing/records", json={"result": result}
        ).get_json()["record"]
        self.assertEqual(saved["dataset_version"], 1)

        # 换账：版本 +1，锚点清空，记录不删除
        self.upload()
        meta = self.client.get("/api/state").get_json()["dataset"]
        self.assertEqual(meta["version"], 2)

        resp = self.client.post("/api/missing/records", json={"result": result})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["stale_reason"], "dataset_changed")

        detail = self.client.get(
            f"/api/missing/records/{saved['id']}"
        ).get_json()["record"]
        self.assertEqual(detail["dataset_version"], 1)
        self.assertEqual(detail["freshness"]["freshness"], "stale")
        self.assertIn("已换账", detail["freshness"]["reasons"][0])
        # 记录中的核算结果完整可回查
        self.assertEqual(detail["result"]["total_missing"], 1)

    def test_unknown_record_404(self):
        resp = self.client.get("/api/missing/records/999")
        self.assertEqual(resp.status_code, 404)

    def test_compute_without_dataset(self):
        tmp = tempfile.TemporaryDirectory()
        app = create_app(db_path=os.path.join(tmp.name, "fresh.db"))
        client = app.test_client()
        self.assertEqual(
            client.post("/api/missing/compute",
                        json={"left_event": 1, "right_event": 2}).status_code,
            400,
        )
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
