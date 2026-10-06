"""统计引擎的口径规则回归测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from monthly_recon.caliber import CaliberVersion
from monthly_recon.engine import compute_month, split_minutes_by_month
from monthly_recon.models import AdjustmentEvent, AdjustmentType, ReasonCode, SourceSnapshot

CALIBER = CaliberVersion(
    version="t1",
    effective_from="2026-01-01",
    duplicate_window_minutes=30,
    late_checkin_grace_days=3,
    cross_month_rule="by_minutes",
    headcount_scope="venue",
    count_rebooked_once=True,
    min_session_minutes=10,
)


def make_snapshot(
    venue_id="V1",
    month="2026-06",
    activities=(),
    sessions=(),
    checkins=(),
    reported=None,
    version=1,
):
    return SourceSnapshot(
        snapshot_id=f"snap-{venue_id}-{month}-{version}",
        venue_id=venue_id,
        month=month,
        version=version,
        submitted_at="2026-07-01T09:00:00",
        reported=reported or {},
        activities=tuple(_activity(a) for a in activities),
        sessions=tuple(_session(s) for s in sessions),
        checkins=tuple(_checkin(c) for c in checkins),
        checksum="x" * 64,
    )


def _activity(a):
    from monthly_recon.models import Activity

    defaults = {"title": a.get("activity_id", ""), "status": "已结算", "is_cancelled": False, "rebooked_from": None}
    defaults.update(a)
    return Activity(**defaults)


def _session(s):
    from monthly_recon.models import Session
    from monthly_recon.models import parse_dt

    return Session(
        session_id=s["session_id"],
        activity_id=s.get("activity_id", "A1"),
        start=parse_dt(s["start"]),
        end=parse_dt(s["end"]),
        explanation_minutes=s.get("explanation_minutes"),
    )


def _checkin(c):
    from monthly_recon.models import CheckIn
    from monthly_recon.models import parse_dt

    checked = parse_dt(c["checked_at"])
    return CheckIn(
        checkin_id=c["checkin_id"],
        session_id=c["session_id"],
        person_id=c["person_id"],
        checked_at=checked,
        recorded_at=parse_dt(c["recorded_at"]) if "recorded_at" in c else checked,
    )


def _adjustment(type_, payload, month="2026-06", event_id="adj-t"):
    return AdjustmentEvent(
        event_id=event_id,
        type=type_,
        month=month,
        posted_by="tester",
        reason="测试",
        payload=payload,
        posted_at="2026-07-02T10:00:00",
    )


def reasons(comp, metric):
    return [e.reason for e in comp.ledger(metric) if not e.included]


class SplitMinutesTest(unittest.TestCase):
    def test_split_within_one_month(self):
        from monthly_recon.models import parse_dt

        result = split_minutes_by_month(parse_dt("2026-06-10T09:00:00"), parse_dt("2026-06-10T10:30:00"))
        self.assertEqual(result, {"2026-06": 90.0})

    def test_split_across_boundary(self):
        from monthly_recon.models import parse_dt

        result = split_minutes_by_month(parse_dt("2026-06-30T23:00:00"), parse_dt("2026-07-01T01:00:00"))
        self.assertEqual(result["2026-06"], 60.0)
        self.assertEqual(result["2026-07"], 60.0)


class SessionRuleTest(unittest.TestCase):
    def test_basic_totals(self):
        snap = make_snapshot(
            activities=[{"activity_id": "A1"}],
            sessions=[{"session_id": "S1", "start": "2026-06-10T09:00:00", "end": "2026-06-10T10:00:00"}],
            checkins=[
                {"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:01:00"},
                {"checkin_id": "C2", "session_id": "S1", "person_id": "P2", "checked_at": "2026-06-10T09:02:00"},
            ],
        )
        comp = compute_month("2026-06", [snap], [], CALIBER)
        self.assertEqual(comp.venue_totals("V1"), {"sessions": 1, "headcount": 2, "explanation_minutes": 60})

    def test_cancelled_rebooked_counted_once(self):
        snap = make_snapshot(
            activities=[
                {"activity_id": "A1", "is_cancelled": True},
                {"activity_id": "A2", "rebooked_from": "A1"},
            ],
            sessions=[
                {"session_id": "S1", "activity_id": "A1", "start": "2026-06-12T14:00:00", "end": "2026-06-12T15:00:00"},
                {"session_id": "S2", "activity_id": "A2", "start": "2026-06-13T14:00:00", "end": "2026-06-13T15:00:00"},
            ],
        )
        comp = compute_month("2026-06", [snap], [], CALIBER)
        v = comp.venues["V1"]
        self.assertEqual(v.totals()["sessions"], 1)
        self.assertIn(ReasonCode.CANCELLED_REBOOKED.value, reasons(v, "sessions"))

    def test_too_short_session_excluded_but_checkins_count(self):
        snap = make_snapshot(
            activities=[{"activity_id": "A1"}],
            sessions=[{"session_id": "S1", "start": "2026-06-20T10:00:00", "end": "2026-06-20T10:05:00"}],
            checkins=[{"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-20T10:01:00"}],
        )
        comp = compute_month("2026-06", [snap], [], CALIBER)
        totals = comp.venue_totals("V1")
        self.assertEqual(totals["sessions"], 0)
        self.assertEqual(totals["explanation_minutes"], 0)
        self.assertEqual(totals["headcount"], 1)
        self.assertIn(ReasonCode.TOO_SHORT.value, reasons(comp.venues["V1"], "sessions"))

    def test_cross_month_by_minutes(self):
        snap = make_snapshot(
            activities=[{"activity_id": "A1"}],
            sessions=[
                {
                    "session_id": "S1",
                    "start": "2026-06-30T23:00:00",
                    "end": "2026-07-01T01:00:00",
                    "explanation_minutes": 120,
                }
            ],
            checkins=[{"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-30T23:05:00"}],
        )
        june = compute_month("2026-06", [snap], [], CALIBER)
        july = compute_month("2026-07", [snap], [], CALIBER)
        self.assertEqual(june.venue_totals("V1"), {"sessions": 1, "headcount": 1, "explanation_minutes": 60})
        self.assertEqual(july.venue_totals("V1"), {"sessions": 0, "headcount": 0, "explanation_minutes": 60})
        self.assertIn(ReasonCode.OUT_OF_MONTH.value, reasons(july.venues["V1"], "sessions"))

    def test_cross_month_start_month_rule(self):
        caliber = CaliberVersion(version="t2", effective_from="2026-01-01", cross_month_rule="start_month")
        snap = make_snapshot(
            activities=[{"activity_id": "A1"}],
            sessions=[
                {
                    "session_id": "S1",
                    "start": "2026-06-30T23:00:00",
                    "end": "2026-07-01T01:00:00",
                    "explanation_minutes": 120,
                }
            ],
        )
        june = compute_month("2026-06", [snap], [], caliber)
        july = compute_month("2026-07", [snap], [], caliber)
        self.assertEqual(june.venue_totals("V1")["explanation_minutes"], 120)
        self.assertEqual(july.venue_totals("V1")["explanation_minutes"], 0)
        self.assertIn(ReasonCode.CROSS_MONTH_OUT.value, reasons(july.venues["V1"], "explanation_minutes"))

    def test_manual_cross_month_allocation_overrides(self):
        snap = make_snapshot(
            activities=[{"activity_id": "A1"}],
            sessions=[
                {
                    "session_id": "S1",
                    "start": "2026-06-30T23:00:00",
                    "end": "2026-07-01T01:00:00",
                    "explanation_minutes": 120,
                }
            ],
        )
        event = _adjustment(
            AdjustmentType.CROSS_MONTH_ALLOCATION,
            {"session_id": "S1", "allocations": {"2026-06": 0.25, "2026-07": 0.75}},
        )
        june = compute_month("2026-06", [snap], [event], CALIBER)
        july = compute_month("2026-07", [snap], [event], CALIBER)
        self.assertEqual(june.venue_totals("V1")["explanation_minutes"], 30)
        self.assertEqual(july.venue_totals("V1")["explanation_minutes"], 90)


class CheckInRuleTest(unittest.TestCase):
    def _snap(self, checkins, sessions=None):
        sessions = sessions or [
            {"session_id": "S1", "start": "2026-06-10T09:00:00", "end": "2026-06-10T10:00:00"},
            {"session_id": "S2", "start": "2026-06-10T09:15:00", "end": "2026-06-10T10:15:00"},
        ]
        return make_snapshot(activities=[{"activity_id": "A1"}], sessions=sessions, checkins=checkins)

    def test_duplicate_same_session(self):
        snap = self._snap(
            [
                {"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:01:00"},
                {"checkin_id": "C2", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:02:00"},
            ]
        )
        comp = compute_month("2026-06", [snap], [], CALIBER)
        self.assertEqual(comp.venue_totals("V1")["headcount"], 1)
        self.assertIn(ReasonCode.DUPLICATE_CHECKIN.value, reasons(comp.venues["V1"], "headcount"))

    def test_duplicate_across_sessions_within_window(self):
        snap = self._snap(
            [
                {"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:03:00"},
                {"checkin_id": "C2", "session_id": "S2", "person_id": "P1", "checked_at": "2026-06-10T09:20:00"},
            ]
        )
        comp = compute_month("2026-06", [snap], [], CALIBER)
        self.assertEqual(comp.venue_totals("V1")["headcount"], 1)

    def test_window_disabled_counts_both(self):
        caliber = CaliberVersion(version="t3", effective_from="2026-01-01", duplicate_window_minutes=0)
        snap = self._snap(
            [
                {"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:03:00"},
                {"checkin_id": "C2", "session_id": "S2", "person_id": "P1", "checked_at": "2026-06-10T09:20:00"},
            ]
        )
        comp = compute_month("2026-06", [snap], [], caliber)
        # 窗口关闭后两条都保留，但同一人只贡献一次人数
        self.assertEqual(comp.venue_totals("V1")["headcount"], 1)
        included = [e for e in comp.venues["V1"].checkin_entries if e.included]
        self.assertEqual(len(included), 2)

    def test_late_checkin_grace(self):
        snap = self._snap(
            [
                {
                    "checkin_id": "C1",
                    "session_id": "S1",
                    "person_id": "P1",
                    "checked_at": "2026-06-10T09:01:00",
                    "recorded_at": "2026-07-02T08:00:00",
                },
                {
                    "checkin_id": "C2",
                    "session_id": "S1",
                    "person_id": "P2",
                    "checked_at": "2026-06-10T09:02:00",
                    "recorded_at": "2026-07-10T08:00:00",
                },
            ]
        )
        comp = compute_month("2026-06", [snap], [], CALIBER)
        self.assertEqual(comp.venue_totals("V1")["headcount"], 1)
        self.assertIn(ReasonCode.LATE_BEYOND_GRACE.value, reasons(comp.venues["V1"], "headcount"))

    def test_identity_merge(self):
        snap = self._snap(
            [
                {"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:01:00"},
                {"checkin_id": "C2", "session_id": "S2", "person_id": "P1-alias", "checked_at": "2026-06-10T11:00:00"},
            ]
        )
        event = _adjustment(
            AdjustmentType.IDENTITY_MERGE, {"alias_person_id": "P1-alias", "canonical_person_id": "P1"}
        )
        comp = compute_month("2026-06", [snap], [event], CALIBER)
        v = comp.venues["V1"]
        self.assertEqual(v.totals()["headcount"], 1)
        self.assertEqual(len(v.merges), 1)
        self.assertEqual(v.merges[0]["alias_person_id"], "P1-alias")

    def test_late_checkin_adjustment_event(self):
        snap = self._snap(
            [{"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:01:00"}]
        )
        event = _adjustment(
            AdjustmentType.LATE_CHECKIN,
            {
                "venue_id": "V1",
                "month": "2026-06",
                "checkin": {
                    "checkin_id": "C9",
                    "session_id": "S1",
                    "person_id": "P9",
                    "checked_at": "2026-06-10T09:30:00",
                    "recorded_at": "2026-07-02T09:00:00",
                },
            },
        )
        comp = compute_month("2026-06", [snap], [event], CALIBER)
        self.assertEqual(comp.venue_totals("V1")["headcount"], 2)

    def test_county_scope_dedupes_across_venues(self):
        caliber = CaliberVersion(version="t4", effective_from="2026-01-01", headcount_scope="county")
        snaps = [
            make_snapshot(
                venue_id="V1",
                activities=[{"activity_id": "A1"}],
                sessions=[{"session_id": "S1", "start": "2026-06-10T09:00:00", "end": "2026-06-10T10:00:00"}],
                checkins=[{"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:01:00"}],
            ),
            make_snapshot(
                venue_id="V2",
                activities=[{"activity_id": "A1"}],
                sessions=[{"session_id": "S1", "start": "2026-06-11T09:00:00", "end": "2026-06-11T10:00:00"}],
                checkins=[{"checkin_id": "C2", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-11T09:01:00"}],
            ),
        ]
        comp = compute_month("2026-06", snaps, [], caliber)
        self.assertEqual(comp.county_totals("venue")["headcount"], 2)
        self.assertEqual(comp.county_totals("county")["headcount"], 1)


if __name__ == "__main__":
    unittest.main()
