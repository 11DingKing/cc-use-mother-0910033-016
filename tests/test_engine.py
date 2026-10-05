"""统计引擎测试：去重、迟到、身份合并、跨月分摊、取消补办与血缘。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from reconciliation.caliber import DEFAULT_RULES, normalize_rules
from reconciliation.engine import compute_month, day_weights
from support import make_service

MONTH = "2026-09"


def _payload(service):
    return {
        "activities": service.store.list_activities(),
        "check_ins": service.store.list_check_ins(),
        "venue_reports": service.store.list_venue_reports(),
    }


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = make_service()
        self.payload = _payload(self.service)

    def tearDown(self) -> None:
        self.service.close()

    def compute(self, rules=None, adjustments=(), month=MONTH):
        return compute_month(self.payload, normalize_rules(rules), list(adjustments), month)

    def test_default_caliber_counts(self) -> None:
        result = self.compute()
        v1 = result["V1"]
        # A1/A2/A5 计入，A3 取消、A4 在 10 月
        self.assertEqual(v1["sessions"], 3.0)
        self.assertEqual(v1["lecture_minutes"], 245.0)  # 55 + 90 + 100
        # p1、p2（C4 迟到排除）、张三两种身份算两人、p5
        self.assertEqual(v1["service_persons"], 5)
        v2 = result["V2"]
        self.assertEqual(v2["sessions"], 1.0)
        self.assertEqual(v2["service_persons"], 1)  # C9 与 C8 同人去重
        self.assertEqual(v2["lecture_minutes"], 60.0)

    def test_lineage_explains_exclusions(self) -> None:
        result = self.compute()
        excluded_sessions = {e["activity_id"]: e["reason"]
                             for e in result["V1"]["excluded"]["sessions"]}
        self.assertEqual(excluded_sessions["A3"], "活动已取消，由补办活动承接")
        excluded_persons = {e["check_in_id"]: e["reason"]
                            for e in result["V1"]["excluded"]["service_persons"]}
        self.assertEqual(excluded_persons["C2"], "重复签到")
        self.assertEqual(excluded_persons["C4"], "迟到超出宽限")
        # A4 整月在 10 月，与 9 月无交集，不出现在 9 月血缘中
        self.assertNotIn("A4", excluded_sessions)

    def test_cross_month_by_day_prorate(self) -> None:
        result = self.compute(rules={"cross_month_rule": "by_day_prorate"})
        v1 = result["V1"]
        self.assertAlmostEqual(v1["sessions"], 1 + 1 + 1 / 3, places=2)
        self.assertAlmostEqual(v1["lecture_minutes"], 55 + 90 + 100 / 3, places=2)
        october = self.compute(rules={"cross_month_rule": "by_day_prorate"}, month="2026-10")
        self.assertAlmostEqual(october["V1"]["sessions"], 1 + 2 / 3, places=2)  # A4 + A5 两天

    def test_checkin_count_mode(self) -> None:
        result = self.compute(rules={"service_person_mode": "checkin_count"})
        self.assertEqual(result["V1"]["service_persons"], 5)  # C2/C4 排除后 5 人次
        self.assertEqual(result["V2"]["service_persons"], 2)  # C8、C9 间隔 45 分钟都算

    def test_planned_duration_mode(self) -> None:
        result = self.compute(rules={"duration_mode": "planned"})
        self.assertEqual(result["V1"]["lecture_minutes"], 270.0)  # 60 + 90 + 120

    def test_late_checkin_adjustment(self) -> None:
        adjustments = [{"type": "late_checkin", "payload": {"check_in_id": "C4"}}]
        result = self.compute(adjustments=adjustments)
        v1 = result["V1"]
        self.assertEqual(v1["service_persons"], 6)
        entry = next(e for e in v1["included"]["service_persons"]
                     if e["check_in_id"] == "C4")
        self.assertEqual(entry["note"], "迟到签到经调整事件采纳")

    def test_identity_merge_adjustment(self) -> None:
        adjustments = [{"type": "identity_merge",
                        "payload": {"source": "phone-zhangsan", "target": "id-zhangsan"}}]
        result = self.compute(adjustments=adjustments)
        v1 = result["V1"]
        self.assertEqual(v1["service_persons"], 4)
        # 合并后 C6 成为同人窗口内重复签到
        excluded = {e["check_in_id"]: e["reason"]
                    for e in v1["excluded"]["service_persons"]}
        self.assertEqual(excluded["C6"], "重复签到")

    def test_cross_month_override_adjustment(self) -> None:
        adjustments = [{"type": "cross_month_override", "payload": {
            "activity_id": "A5",
            "allocations": [{"month": "2026-09", "weight": 0.5},
                            {"month": "2026-10", "weight": 0.5}],
        }}]
        result = self.compute(adjustments=adjustments)
        v1 = result["V1"]
        self.assertEqual(v1["sessions"], 2.5)
        self.assertEqual(v1["lecture_minutes"], 195.0)  # 55 + 90 + 50

    def test_day_weights(self) -> None:
        import datetime as dt
        weights = day_weights(dt.date(2026, 9, 30), dt.date(2026, 10, 2))
        self.assertAlmostEqual(weights["2026-09"], 1 / 3)
        self.assertAlmostEqual(weights["2026-10"], 2 / 3)
        with self.assertRaises(ValueError):
            day_weights(dt.date(2026, 10, 2), dt.date(2026, 9, 30))

    def test_unknown_rule_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_rules({"not_a_rule": 1})


if __name__ == "__main__":
    unittest.main()
