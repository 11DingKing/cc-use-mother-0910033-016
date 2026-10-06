"""HTTP API 端到端测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from monthly_recon.api import create_server
from monthly_recon.service import MonthlyReconService


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server(MonthlyReconService(), port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_full_flow_over_http(self):
        status, _ = self.request("GET", "/health")
        self.assertEqual(status, 200)

        status, _ = self.request("POST", "/venues", {"venue_id": "V01", "name": "县科技馆"})
        self.assertEqual(status, 201)

        snapshot_body = {
            "venue_id": "V01",
            "month": "2026-06",
            "reported": {"sessions": 2, "headcount": 3, "explanation_minutes": 120},
            "activities": [{"activity_id": "A1", "title": "航天科普"}],
            "sessions": [
                {"session_id": "S1", "activity_id": "A1", "start": "2026-06-10T09:00:00", "end": "2026-06-10T10:00:00", "explanation_minutes": 60},
                {"session_id": "S2", "activity_id": "A1", "start": "2026-06-30T23:00:00", "end": "2026-07-01T01:00:00", "explanation_minutes": 120},
            ],
            "checkins": [
                {"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:01:00"},
                {"checkin_id": "C2", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:02:00"},
                {"checkin_id": "C3", "session_id": "S2", "person_id": "P2", "checked_at": "2026-06-30T23:05:00"},
            ],
        }
        status, snap = self.request("POST", "/snapshots", snapshot_body)
        self.assertEqual(status, 201)
        self.assertEqual(snap["version"], 1)
        self.assertEqual(len(snap["checksum"]), 64)

        status, cycle = self.request("POST", "/reconciliations/run", {"month": "2026-06"})
        self.assertEqual(status, 200)
        self.assertEqual(cycle["state"], "DIFF_READY")
        diffs = {d["metric"]: d for d in cycle["diffs"]}
        # 场馆自报 3 人，去重后 2 人；自报时长 120，跨月分摊后 60+60=120
        self.assertEqual(diffs["headcount"]["computed"], 2)
        self.assertNotIn("explanation_minutes", diffs)

        status, cycle = self.request(
            "POST", "/reconciliations/2026-06/confirm", {"venue_id": "V01", "actor": "场馆管理员", "note": "认可"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(cycle["state"], "CONFIRMED")

        status, report = self.request("POST", "/reconciliations/2026-06/close", {"actor": "县级统计员"})
        self.assertEqual(status, 201)
        self.assertTrue(report["sealed"])
        self.assertEqual(report["venues"]["V01"], {"sessions": 2, "headcount": 2, "explanation_minutes": 120})

        status, event = self.request(
            "POST",
            "/adjustments",
            {
                "type": "LATE_CHECKIN",
                "month": "2026-06",
                "posted_by": "学校联系人",
                "reason": "回执迟到",
                "payload": {
                    "venue_id": "V01",
                    "checkin": {
                        "checkin_id": "C9",
                        "session_id": "S1",
                        "person_id": "P9",
                        "checked_at": "2026-06-10T09:30:00",
                        "recorded_at": "2026-07-02T09:00:00",
                    },
                },
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(event["type"], "LATE_CHECKIN")

        status, recalc = self.request("POST", "/reports/2026-06/recalculate", {})
        self.assertEqual(status, 201)
        self.assertFalse(recalc["sealed"])
        self.assertEqual(recalc["venues"]["V01"]["headcount"], 3)

        status, sealed = self.request("GET", "/reports/2026-06")
        self.assertEqual(status, 200)
        self.assertTrue(sealed["sealed"])
        self.assertEqual(sealed["venues"]["V01"]["headcount"], 2)

        status, drill = self.request("GET", "/reports/2026-06/drilldown?metric=headcount&venue_id=V01")
        self.assertEqual(status, 200)
        v01 = drill["venues"]["V01"]
        self.assertEqual({e["record_id"] for e in v01["included"]}, {"C1", "C3"})
        excluded = {e["record_id"]: e["reason"] for e in v01["excluded"]}
        self.assertEqual(excluded["C2"], "DUPLICATE_CHECKIN")

        status, old = self.request("POST", "/reports/2026-06/recalculate", {"caliber_version": "v0.9"})
        self.assertEqual(status, 201)
        self.assertEqual(old["caliber_version"], "v0.9")
        # v0.9 无跨月分摊，S2 的 120 分钟全部计入 6 月
        self.assertEqual(old["venues"]["V01"]["explanation_minutes"], 180)

    def test_error_responses(self):
        status, body = self.request("GET", "/reports/2099-01")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        status, body = self.request("POST", "/snapshots", {"venue_id": "NOPE", "month": "2026-06"})
        self.assertEqual(status, 404)
        status, body = self.request("GET", "/no-such-route")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
