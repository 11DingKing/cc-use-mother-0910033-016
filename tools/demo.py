"""端到端演示：报送快照 -> 差异清单 -> 场馆确认 -> 封账 -> 调整事件 -> 复算 -> 下钻。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from monthly_recon.service import MonthlyReconService

MONTH = "2026-06"


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    service = MonthlyReconService()
    service.register_venue("V01", "县科技馆")
    service.register_venue("V02", "县博物馆")

    service.submit_snapshot(
        venue_id="V01",
        month=MONTH,
        reported={"sessions": 4, "headcount": 6, "explanation_minutes": 300},
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
        ],
        checkins=[
            {"checkin_id": "C1", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:01:00"},
            {"checkin_id": "C2", "session_id": "S1", "person_id": "P1", "checked_at": "2026-06-10T09:02:00"},
            {"checkin_id": "C3", "session_id": "S1", "person_id": "P2", "checked_at": "2026-06-10T09:03:00"},
            {"checkin_id": "C4", "session_id": "S3", "person_id": "P3", "checked_at": "2026-06-13T14:05:00"},
            {"checkin_id": "C5", "session_id": "S4", "person_id": "P4", "checked_at": "2026-06-30T23:05:00"},
            {"checkin_id": "C6", "session_id": "S1", "person_id": "P5", "checked_at": "2026-06-10T09:05:00", "recorded_at": "2026-07-10T08:00:00"},
        ],
    )
    service.submit_snapshot(
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

    cycle = service.run_reconciliation(MONTH)
    show("差异清单（县级重算 vs 场馆自报）", [d.to_dict() for d in cycle.diffs])

    service.confirm(MONTH, "V01", actor="场馆管理员-科技馆", note="差异认可")
    service.confirm(MONTH, "V02", actor="场馆管理员-博物馆")
    sealed = service.close_month(MONTH, actor="县级统计员")
    show("封账月报（稳定版本）", sealed.to_dict())

    service.post_adjustment(
        "LATE_CHECKIN", MONTH, posted_by="学校联系人", reason="集体活动回执迟到",
        payload={"venue_id": "V01", "checkin": {"checkin_id": "C100", "session_id": "S1", "person_id": "P7",
                 "checked_at": "2026-06-10T09:10:00", "recorded_at": "2026-07-02T09:00:00"}},
    )
    service.post_adjustment(
        "IDENTITY_MERGE", MONTH, posted_by="县级统计员", reason="同人不同证件",
        payload={"alias_person_id": "P3", "canonical_person_id": "P1"},
    )
    service.post_adjustment(
        "CROSS_MONTH_ALLOCATION", MONTH, posted_by="县级统计员", reason="跨月场次协商分摊",
        payload={"session_id": "S4", "allocations": {"2026-06": 0.25, "2026-07": 0.75}},
    )
    service.post_adjustment(
        "REPORT_CORRECTION", MONTH, posted_by="县级统计员", reason="学校补报名单",
        payload={"venue_id": "V02", "metric": "headcount", "delta": 1},
    )

    recalc = service.recalculate(MONTH)
    show("复算报告（含调整事件与已签更正，未封账）", recalc.to_dict())

    old = service.recalculate(MONTH, caliber_version="v0.9")
    show("历史口径复算（v0.9）", old.to_dict())

    drill = service.drilldown(MONTH, "headcount", venue_id="V01")
    show("指标下钻：V01 服务人数（封账版）", drill)


if __name__ == "__main__":
    main()
