"""对账流程回归测试：快照 -> 差异清单 -> 确认 -> 封账 -> 调整 -> 复算 -> 下钻。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from monthly_recon.service import MonthlyReconService, ReconError

MONTH = "2026-06"


def make_service() -> MonthlyReconService:
    clock_time = {"now": datetime(2026, 7, 1, 12, 0, 0)}

    def clock():
        clock_time["now"] += timedelta(seconds=1)
        return clock_time["now"]

    service = MonthlyReconService(clock=clock)
    service.register_venue("V01", "县科技馆")
    service.register_venue("V02", "县博物馆")
    return service


def submit_v01(service: MonthlyReconService):
    """科技馆快照：取消补办、跨月场次、过短场次、重复签到、迟到签到。"""
    return service.submit_snapshot(
        venue_id="V01",
        month=MONTH,
        reported={"sessions": 6, "headcount": 9, "explanation_minutes": 365},
        activities=[
            {"activity_id": "A1", "title": "航天科普", "status": "已结算"},
            {"activity_id": "A2", "title": "临时展览", "is_cancelled": True},
            {"activity_id": "A3", "title": "临时展览（补办）", "rebooked_from": "A2"},
        ],
        sessions=[
            {"session_id": "S1", "activity_id": "A1", "start": "2026-06-10T09:00:00", "end": "2026-06-10T10:00:00", "explanation_minutes": 60},
            {"session_id": "S2", "activity_id": "A2", "start": "2026-06-12T14:00:00", "end": "2026-06-12T15:00:00", "explanation_minutes": 60},
            {"session_id": "S3", "activity_id": "A3", "start": "2026-06-13T14:00:00", "end": "2026-06-13T15:00:00", "explanation_minutes": 60},
            {"session_id": "S4", "activity_id": "A1", "start": "2026-06-30T23:00:00", "end": "2026-07-01T01:00:00", "explanation_minutes": 120},
            {"session_id": "S5", "activity_id": "A1", "start": "2026-06-20T10:00:00", "end": "2026-06-20T10:05:00", "explanation_minutes": 5},
            {"session_id": "S6", "activity_id": "A1", "start": "2026-06-10T09:15:00", "end": "2026-06-10T10:15:00", "explanation_minutes": 60},
        ],
        checkins=[
            {"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:01:00"},
            {"checkin_id": "C2", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:02:00"},
            {"checkin_id": "C3", "session_id": "S1", "person_id": "P2", "checked_at": "2026-06-10T09:03:00"},
            {"checkin_id": "C4", "session_id": "S3", "person_id": "P3", "checked_at": "2026-06-13T14:05:00"},
            {"checkin_id": "C5", "session_id": "S4", "person_id": "P4", "checked_at": "2026-06-30T23:05:00"},
            {"checkin_id": "C6", "session_id": "S2", "person_id": "P9", "checked_at": "2026-06-12T14:05:00"},
            {"checkin_id": "C7", "session_id": "S6", "person_id": "P2", "checked_at": "2026-06-10T09:20:00"},
            {"checkin_id": "C8", "session_id": "S1", "person_id": "P5", "checked_at": "2026-06-10T09:05:00", "recorded_at": "2026-07-02T08:00:00"},
            {"checkin_id": "C9", "session_id": "S1", "person_id": "P6", "checked_at": "2026-06-10T09:06:00", "recorded_at": "2026-07-10T08:00:00"},
        ],
    )


def submit_v02(service: MonthlyReconService):
    """博物馆快照：数据干净，自报与重算一致。"""
    return service.submit_snapshot(
        venue_id="V02",
        month=MONTH,
        reported={"sessions": 1, "headcount": 2, "explanation_minutes": 60},
        activities=[{"activity_id": "B1", "title": "青铜器常设展", "status": "已结算"}],
        sessions=[
            {"session_id": "SB1", "activity_id": "B1", "start": "2026-06-15T10:00:00", "end": "2026-06-15T11:00:00", "explanation_minutes": 60},
        ],
        checkins=[
            {"checkin_id": "CB1", "session_id": "SB1", "person_id": "P10", "checked_at": "2026-06-15T10:01:00"},
            {"checkin_id": "CB2", "session_id": "SB1", "person_id": "P11", "checked_at": "2026-06-15T10:02:00"},
        ],
    )


def run_to_sealed(service: MonthlyReconService):
    submit_v01(service)
    submit_v02(service)
    cycle = service.run_reconciliation(MONTH)
    service.confirm(MONTH, "V01", actor="场馆管理员-科技馆", note="差异认可")
    service.confirm(MONTH, "V02", actor="场馆管理员-博物馆")
    report = service.close_month(MONTH, actor="县级统计员")
    return cycle, report


class ReconciliationFlowTest(unittest.TestCase):
    def test_diff_list_matches_expected(self):
        service = make_service()
        submit_v01(service)
        submit_v02(service)
        cycle = service.run_reconciliation(MONTH)
        self.assertEqual(cycle.state, "DIFF_READY")
        diffs = {d.metric: d for d in cycle.diffs if d.venue_id == "V01"}
        self.assertEqual(diffs["sessions"].reported, 6)
        self.assertEqual(diffs["sessions"].computed, 4)
        self.assertEqual(diffs["headcount"].computed, 5)
        self.assertEqual(diffs["explanation_minutes"].computed, 240)
        self.assertIn("CANCELLED_REBOOKED", diffs["sessions"].reasons)
        self.assertIn("DUPLICATE_CHECKIN", diffs["headcount"].reasons)
        self.assertFalse([d for d in cycle.diffs if d.venue_id == "V02"])

    def test_close_requires_all_confirmations(self):
        service = make_service()
        submit_v01(service)
        submit_v02(service)
        service.run_reconciliation(MONTH)
        service.confirm(MONTH, "V01", actor="场馆管理员-科技馆")
        with self.assertRaises(ReconError) as ctx:
            service.close_month(MONTH, actor="县级统计员")
        self.assertIn("V02", ctx.exception.message)

    def test_sealed_report_is_stable(self):
        service = make_service()
        _, report = run_to_sealed(service)
        self.assertTrue(report.sealed)
        self.assertEqual(report.venue_lines["V01"], {"sessions": 4, "headcount": 5, "explanation_minutes": 240})
        self.assertEqual(report.venue_lines["V02"], {"sessions": 1, "headcount": 2, "explanation_minutes": 60})
        self.assertEqual(report.totals, {"sessions": 5, "headcount": 7, "explanation_minutes": 300})
        # 封账后调整事件不改变已封报告
        service.post_adjustment(
            "IDENTITY_MERGE",
            MONTH,
            posted_by="县级统计员",
            reason="同人不同证",
            payload={"alias_person_id": "P3", "canonical_person_id": "P1"},
        )
        again = service.get_report(MONTH)
        self.assertEqual(again.report_id, report.report_id)
        self.assertEqual(again.venue_lines["V01"]["headcount"], 5)

    def test_cannot_rerun_or_confirm_after_close(self):
        service = make_service()
        run_to_sealed(service)
        with self.assertRaises(ReconError):
            service.run_reconciliation(MONTH)
        with self.assertRaises(ReconError):
            service.confirm(MONTH, "V01", actor="x")

    def test_snapshot_resubmission_invalidates_confirmation(self):
        service = make_service()
        submit_v01(service)
        service.run_reconciliation(MONTH)
        submit_v01(service)  # 报送新版本
        with self.assertRaises(ReconError) as ctx:
            service.confirm(MONTH, "V01", actor="场馆管理员-科技馆")
        self.assertIn("重新执行对账", ctx.exception.message)

    def test_recalculate_with_adjustments_and_corrections(self):
        service = make_service()
        _, sealed = run_to_sealed(service)
        # 已签报告更正在封账前不允许
        with self.assertRaises(ReconError):
            service.post_adjustment(
                "REPORT_CORRECTION",
                "2026-05",
                posted_by="县级统计员",
                reason="未封账",
                payload={"venue_id": "V01", "metric": "sessions", "delta": 1},
            )
        # 迟到签到补录（宽限内录入）
        service.post_adjustment(
            "LATE_CHECKIN",
            MONTH,
            posted_by="学校联系人",
            reason="集体活动回执迟到",
            payload={
                "venue_id": "V01",
                "checkin": {
                    "checkin_id": "C100",
                    "session_id": "S1",
                    "person_id": "P7",
                    "checked_at": "2026-06-10T09:10:00",
                    "recorded_at": "2026-07-02T09:00:00",
                },
            },
        )
        # 身份合并：P3 并入 P1
        service.post_adjustment(
            "IDENTITY_MERGE",
            MONTH,
            posted_by="县级统计员",
            reason="同人不同证",
            payload={"alias_person_id": "P3", "canonical_person_id": "P1"},
        )
        # 跨月人工分摊：S4 改为 25% / 75%
        service.post_adjustment(
            "CROSS_MONTH_ALLOCATION",
            MONTH,
            posted_by="县级统计员",
            reason="跨月场次经场馆协商分摊",
            payload={"session_id": "S4", "allocations": {"2026-06": 0.25, "2026-07": 0.75}},
        )
        # 已签报告更正：博物馆补报 1 人
        service.post_adjustment(
            "REPORT_CORRECTION",
            MONTH,
            posted_by="县级统计员",
            reason="学校联系人补报名单",
            payload={"venue_id": "V02", "metric": "headcount", "delta": 1},
        )
        recalc = service.recalculate(MONTH)
        self.assertFalse(recalc.sealed)
        self.assertEqual(recalc.revision, 2)
        self.assertEqual(recalc.supersedes, sealed.report_id)
        # 迟到签到 +1、身份合并 -1 -> 净 0；跨月分摊 120*0.25=30
        self.assertEqual(recalc.venue_lines["V01"], {"sessions": 4, "headcount": 5, "explanation_minutes": 210})
        self.assertEqual(recalc.venue_lines["V02"]["headcount"], 3)
        self.assertEqual(len(recalc.corrections_applied), 1)
        # 已封报告保持稳定
        self.assertEqual(service.get_report(MONTH).report_id, sealed.report_id)
        self.assertEqual(service.get_report(MONTH).venue_lines["V02"]["headcount"], 2)

    def test_recalculate_with_historical_caliber(self):
        service = make_service()
        run_to_sealed(service)
        recalc = service.recalculate(MONTH, caliber_version="v0.9")
        v01 = recalc.venue_lines["V01"]
        # v0.9：无窗口去重、无宽限、start_month、不豁免过短场次
        self.assertEqual(v01["sessions"], 5)
        self.assertEqual(v01["headcount"], 4)
        self.assertEqual(v01["explanation_minutes"], 305)
        self.assertEqual(recalc.caliber_version, "v0.9")

    def test_drilldown_included_and_excluded(self):
        service = make_service()
        run_to_sealed(service)
        result = service.drilldown(MONTH, "headcount", venue_id="V01")
        v01 = result["venues"]["V01"]
        included_ids = {e["record_id"] for e in v01["included"]}
        self.assertEqual(included_ids, {"C1", "C3", "C4", "C5", "C8"})
        excluded = {e["record_id"]: e["reason"] for e in v01["excluded"]}
        self.assertEqual(excluded["C2"], "DUPLICATE_CHECKIN")
        self.assertEqual(excluded["C7"], "DUPLICATE_CHECKIN")
        self.assertEqual(excluded["C6"], "ACTIVITY_CANCELLED")
        self.assertEqual(excluded["C9"], "LATE_BEYOND_GRACE")
        # 场次下钻
        sessions = service.drilldown(MONTH, "sessions", venue_id="V01")["venues"]["V01"]
        excluded_sessions = {e["record_id"]: e["reason"] for e in sessions["excluded"]}
        self.assertEqual(excluded_sessions["S2"], "CANCELLED_REBOOKED")
        self.assertEqual(excluded_sessions["S5"], "TOO_SHORT")
        # 时长下钻：跨月场次部分计入
        duration = service.drilldown(MONTH, "explanation_minutes", venue_id="V01")["venues"]["V01"]
        s4 = [e for e in duration["included"] if e["record_id"] == "S4"][0]
        self.assertEqual(s4["value"], 60)
        self.assertAlmostEqual(s4["fraction"], 0.5)

    def test_identity_merge_shows_in_drilldown(self):
        service = make_service()
        run_to_sealed(service)
        service.post_adjustment(
            "IDENTITY_MERGE",
            MONTH,
            posted_by="县级统计员",
            reason="同人不同证",
            payload={"alias_person_id": "P3", "canonical_person_id": "P1"},
        )
        recalc = service.recalculate(MONTH)
        result = service.drilldown(MONTH, "headcount", venue_id="V01", revision=recalc.revision)
        merges = result["venues"]["V01"]["merges"]
        self.assertEqual(merges[0]["alias_person_id"], "P3")
        self.assertEqual(result["venues"]["V01"]["totals"]["headcount"], 4)


if __name__ == "__main__":
    unittest.main()
