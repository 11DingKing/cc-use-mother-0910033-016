"""端到端演示：来源数据 -> 快照 -> 差异清单 -> 场馆确认 -> 封账 -> 调整与复算。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from reconciliation.service import ReconciliationService
from support import MONTH, seed_service


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    service = ReconciliationService()
    seed_service(service)
    service.register_caliber("v2", {
        "cross_month_rule": "by_day_prorate",
        "service_person_mode": "checkin_count",
        "duration_mode": "planned",
    })

    snapshot = service.capture_snapshot()
    show("来源快照", snapshot)

    run = service.create_run(MONTH, "v1", snapshot["snapshot_id"])
    show("差异清单（县级口径 vs 场馆自报）", run["differences"])

    for diff in run["differences"]:
        service.confirm_difference(diff["difference_id"], "场馆管理员", "以县级口径为准")
    closed = service.close_run(run["run_id"])
    show("封账结果", closed)

    report = service.get_report("V1", MONTH)["effective"]
    show("县图书馆 9 月稳定月报", {k: report[k] for k in
           ("venue_id", "month", "revision", "sessions", "service_persons", "lecture_minutes")})

    drill = service.report_drilldown(report["report_id"], "service_persons")
    show("服务人数下钻（包含/排除）", {
        "value": drill["value"], "included": drill["included"], "excluded": drill["excluded"]})

    service.record_adjustment("late_checkin", MONTH, {"check_in_id": "C4"},
                              reason="签到机故障补录", actor="场馆管理员")
    service.record_adjustment("identity_merge", MONTH,
                              {"source": "phone-zhangsan", "target": "id-zhangsan"},
                              reason="同一人两种证件", actor="县级审核员")
    revised = service.get_report("V1", MONTH)
    show("调整事件后的月报修订历史", [
        {"revision": r["revision"], "status": r["status"],
         "service_persons": r["service_persons"]} for r in revised["revisions"]])

    recalc = service.recalculate(MONTH, "v2")
    show("按 v2 口径复算（不改写已封账月报）", recalc["venues"]["V1"])


if __name__ == "__main__":
    main()
