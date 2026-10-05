"""HTTP API 端到端测试：从录入到封账、下钻与复算。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from reconciliation.api import create_server
from support import MONTH, make_service


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = make_service()
        cls.server = create_server(cls.service, port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.service.close()

    def request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()
        return response.status, data

    def test_full_reconciliation_flow(self) -> None:
        status, snap = self.request("POST", "/snapshots", {})
        self.assertEqual(status, 200)
        self.assertEqual(snap["activity_count"], 6)

        status, run = self.request("POST", "/runs", {
            "month": MONTH, "caliber_version": "v1",
            "snapshot_id": snap["snapshot_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(run["differences"]), 3)

        # 未确认完不能封账
        status, err = self.request("POST", f"/runs/{run['run_id']}/close", {})
        self.assertEqual(status, 409)

        for diff in run["differences"]:
            status, confirmed = self.request(
                "POST", f"/differences/{diff['difference_id']}/confirm",
                {"actor": "场馆管理员", "note": "以县级口径为准"})
            self.assertEqual(status, 200)
            self.assertEqual(confirmed["status"], "confirmed")

        status, closed = self.request("POST", f"/runs/{run['run_id']}/close", {})
        self.assertEqual(status, 200)
        self.assertEqual(closed["status"], "closed")

        status, report = self.request("GET", f"/reports?venue_id=V1&month={MONTH}")
        self.assertEqual(status, 200)
        effective = report["effective"]
        self.assertEqual(effective["sessions"], 3.0)

        status, drill = self.request(
            "GET", f"/reports/{effective['report_id']}/drilldown?metric=service_persons")
        self.assertEqual(status, 200)
        self.assertEqual(len(drill["included"]), 5)
        self.assertEqual(len(drill["excluded"]), 2)

        # 封账后迟到签到补录 -> 自动生成修订版
        status, adjusted = self.request("POST", "/adjustments", {
            "type": "late_checkin", "period": MONTH,
            "payload": {"check_in_id": "C4"},
            "reason": "补录", "actor": "场馆管理员"})
        self.assertEqual(status, 200)
        self.assertEqual(len(adjusted["revised_report_ids"]), 1)

        status, report = self.request("GET", f"/reports?venue_id=V1&month={MONTH}")
        self.assertEqual(report["effective"]["revision"], 2)
        self.assertEqual(report["effective"]["service_persons"], 6)

        # 历史口径复算
        status, recalc = self.request("POST", "/recalculations", {
            "month": MONTH, "caliber_version": "v1"})
        self.assertEqual(status, 200)
        self.assertEqual(recalc["venues"]["V1"]["current_effective"]["service_persons"], 6)

    def test_error_mapping(self) -> None:
        status, err = self.request("GET", "/reports?venue_id=V9&month=2026-09")
        self.assertEqual(status, 404)
        status, err = self.request("POST", "/runs", {
            "month": "2026/09", "caliber_version": "v1", "snapshot_id": "x"})
        self.assertEqual(status, 400)
        status, err = self.request("GET", "/no-such-endpoint")
        self.assertEqual(status, 404)
        status, err = self.request("POST", "/adjustments", {
            "type": "cross_month_override", "period": "2026-09",
            "payload": {"activity_id": "A5", "allocations": [
                {"month": "2026-09", "weight": 0.3}]}})
        self.assertEqual(status, 400)  # 权重之和不为 1


if __name__ == "__main__":
    unittest.main()
