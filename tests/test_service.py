"""服务流程测试：快照、差异清单、场馆确认、封账、调整事件、下钻与复算。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from reconciliation.models import ConflictError, NotFoundError
from support import MONTH, make_service


class ServiceFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.snapshot = self.service.capture_snapshot()
        self.service.register_caliber("v2", {
            "cross_month_rule": "by_day_prorate",
            "service_person_mode": "checkin_count",
            "duration_mode": "planned",
        })

    def tearDown(self) -> None:
        self.service.close()

    def _run_and_close(self):
        run = self.service.create_run(MONTH, "v1", self.snapshot["snapshot_id"])
        for diff in run["differences"]:
            self.service.confirm_difference(diff["difference_id"], "场馆管理员")
        return self.service.close_run(run["run_id"])

    def test_snapshot_is_immutable_and_hashed(self) -> None:
        snap = self.snapshot
        self.assertEqual(snap["activity_count"], 6)
        self.assertEqual(len(snap["payload_hash"]), 64)
        # 快照后新增数据不影响旧快照
        self.service.add_activity("A9", "V2", "新增活动", "2026-09-15T10:00:00",
                                  "2026-09-15T11:00:00", "已结算", actual_minutes=60)
        stored = self.service.store.get_snapshot(snap["snapshot_id"])
        self.assertEqual(len(stored["payload"]["activities"]), 6)

    def test_run_generates_differences(self) -> None:
        run = self.service.create_run(MONTH, "v1", self.snapshot["snapshot_id"])
        self.assertEqual(run["status"], "open")
        diffs = {(d["venue_id"], d["metric"]): d for d in run["differences"]}
        # V1 三项都有差异；V2 与自报一致，无差异
        self.assertEqual(set(diffs), {("V1", "sessions"), ("V1", "service_persons"),
                                      ("V1", "lecture_minutes")})
        self.assertEqual(diffs[("V1", "sessions")]["county_value"], 3.0)
        self.assertEqual(diffs[("V1", "sessions")]["venue_value"], 4.0)
        self.assertEqual(diffs[("V1", "sessions")]["status"], "pending")

    def test_close_requires_all_differences_confirmed(self) -> None:
        run = self.service.create_run(MONTH, "v1", self.snapshot["snapshot_id"])
        with self.assertRaises(ConflictError):
            self.service.close_run(run["run_id"])
        for diff in run["differences"]:
            self.service.confirm_difference(diff["difference_id"], "场馆管理员")
        closed = self.service.close_run(run["run_id"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(len(closed["report_ids"]), 2)
        with self.assertRaises(ConflictError):
            self.service.close_run(run["run_id"])  # 不能重复封账

    def test_sealed_report_and_drilldown(self) -> None:
        self._run_and_close()
        report = self.service.get_report("V1", MONTH)["effective"]
        self.assertEqual(report["revision"], 1)
        self.assertEqual(report["sessions"], 3.0)
        self.assertEqual(report["service_persons"], 5)
        self.assertEqual(report["lecture_minutes"], 245.0)

        drill = self.service.report_drilldown(report["report_id"], "service_persons")
        self.assertEqual(drill["metric_name"], "服务人数")
        self.assertEqual(len(drill["included"]), 5)
        reasons = {e["check_in_id"]: e["reason"] for e in drill["excluded"]}
        self.assertEqual(reasons["C2"], "重复签到")
        self.assertEqual(reasons["C4"], "迟到超出宽限")

        sessions = self.service.report_drilldown(report["report_id"], "sessions")
        cancelled = {e["activity_id"]: e["reason"] for e in sessions["excluded"]}
        self.assertEqual(cancelled["A3"], "活动已取消，由补办活动承接")

    def test_adjustments_revise_sealed_report(self) -> None:
        self._run_and_close()
        # 迟到签到补录：C4 被采纳，服务人数 5 -> 6
        result = self.service.record_adjustment(
            "late_checkin", MONTH, {"check_in_id": "C4"},
            reason="签到机故障补录", actor="场馆管理员")
        self.assertEqual(len(result["revised_report_ids"]), 1)
        report = self.service.get_report("V1", MONTH)["effective"]
        self.assertEqual(report["revision"], 2)
        self.assertEqual(report["service_persons"], 6)

        # 身份合并：手机号并入身份证，服务人数 6 -> 5
        self.service.record_adjustment(
            "identity_merge", MONTH,
            {"source": "phone-zhangsan", "target": "id-zhangsan"},
            reason="同一人两种证件", actor="县级审核员")
        report = self.service.get_report("V1", MONTH)["effective"]
        self.assertEqual(report["revision"], 3)
        self.assertEqual(report["service_persons"], 5)

        # 跨月分摊：A5 九月、十月各半
        self.service.record_adjustment(
            "cross_month_override", MONTH,
            {"activity_id": "A5", "allocations": [
                {"month": "2026-09", "weight": 0.5},
                {"month": "2026-10", "weight": 0.5}]},
            reason="读书营主体在国庆", actor="县级审核员")
        report = self.service.get_report("V1", MONTH)["effective"]
        self.assertEqual(report["revision"], 4)
        self.assertEqual(report["sessions"], 2.5)
        self.assertEqual(report["lecture_minutes"], 195.0)

        # 历史版本全部保留，旧版状态为 superseded
        revisions = self.service.get_report("V1", MONTH)["revisions"]
        self.assertEqual(len(revisions), 4)
        self.assertEqual([r["status"] for r in revisions],
                         ["superseded", "superseded", "superseded", "effective"])

    def test_report_correction_on_signed_report(self) -> None:
        self._run_and_close()
        result = self.service.record_adjustment(
            "report_correction", MONTH,
            {"venue_id": "V1", "metric": "lecture_minutes", "delta": 5},
            reason="补录开场讲解 5 分钟", actor="县级审核员")
        self.assertEqual(len(result["revised_report_ids"]), 1)
        report = self.service.get_report("V1", MONTH)["effective"]
        self.assertEqual(report["lecture_minutes"], 250.0)
        drill = self.service.report_drilldown(report["report_id"], "lecture_minutes")
        self.assertEqual(len(drill["corrections"]), 1)
        self.assertEqual(drill["corrections"][0]["reason"], "补录开场讲解 5 分钟")

    def test_correction_requires_sealed_report(self) -> None:
        with self.assertRaises(ConflictError):
            self.service.record_adjustment(
                "report_correction", MONTH,
                {"venue_id": "V1", "metric": "sessions", "delta": 1})

    def test_adjustment_before_sealing_applies_to_run(self) -> None:
        # 封账前登记的调整事件直接进入对账批次
        self.service.record_adjustment(
            "late_checkin", MONTH, {"check_in_id": "C4"}, reason="补录")
        run = self.service.create_run(MONTH, "v1", self.snapshot["snapshot_id"])
        self.assertEqual(len(run["adjustment_ids"]), 1)
        v1_persons = next(d for d in run["differences"]
                          if d["venue_id"] == "V1" and d["metric"] == "service_persons")
        self.assertEqual(v1_persons["county_value"], 6)

    def test_recalculate_with_historical_caliber(self) -> None:
        self._run_and_close()
        recalc = self.service.recalculate(MONTH, "v2")
        v1 = recalc["venues"]["V1"]
        self.assertAlmostEqual(v1["recalculated"]["sessions"], 1 + 1 + 1 / 3, places=2)
        self.assertEqual(v1["recalculated"]["service_persons"], 5)
        self.assertEqual(v1["current_effective"]["sessions"], 3.0)
        self.assertAlmostEqual(v1["delta"]["sessions"], -0.67, places=2)
        # 复算不改写已封账月报
        report = self.service.get_report("V1", MONTH)["effective"]
        self.assertEqual(report["revision"], 1)
        self.assertEqual(report["sessions"], 3.0)
        # 复算结果同样支持下钻
        reasons = {e["check_in_id"]: e["reason"]
                   for e in v1["excluded"]["service_persons"]}
        self.assertEqual(reasons["C4"], "迟到超出宽限")

    def test_missing_references(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.create_run(MONTH, "v1", "snap_missing")
        with self.assertRaises(NotFoundError):
            self.service.create_run(MONTH, "v9", self.snapshot["snapshot_id"])
        with self.assertRaises(ValueError):
            self.service.create_run("2026-9", "v1", self.snapshot["snapshot_id"])
        with self.assertRaises(NotFoundError):
            self.service.get_report("V9", MONTH)


if __name__ == "__main__":
    unittest.main()
