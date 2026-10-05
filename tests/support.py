"""测试共享的种子数据：覆盖取消补办、重复签到、迟到、跨月与身份重复。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reconciliation.service import ReconciliationService

MONTH = "2026-09"


def seed_service(service: ReconciliationService) -> None:
    """构造县级月度对账的典型来源数据。

    场景要点：
    - A3 取消、A4 补办（补办落在 10 月）；
    - A5 跨月（9-30 至 10-02）；
    - C2 是窗口内重复签到，C4 迟到超出宽限；
    - C5/C6 是同一人（张三）的身份证与手机号两种身份；
    - C9 与 C8 间隔 45 分钟，超出 30 分钟去重窗口；
    - 场馆自报数与县级口径存在差异，用于生成差异清单。
    """
    service.add_venue("V1", "县图书馆")
    service.add_venue("V2", "县博物馆")

    service.add_activity("A1", "V1", "科普讲座", "2026-09-05T10:00:00",
                         "2026-09-05T11:00:00", "已结算",
                         planned_minutes=60, actual_minutes=55)
    service.add_activity("A2", "V1", "非遗讲解", "2026-09-20T14:00:00",
                         "2026-09-20T15:30:00", "已结算",
                         planned_minutes=90, actual_minutes=90)
    service.add_activity("A3", "V1", "临时展览导览", "2026-09-28T09:00:00",
                         "2026-09-28T10:00:00", "已取消", planned_minutes=60)
    service.add_activity("A4", "V1", "临时展览导览（补办）", "2026-10-02T09:00:00",
                         "2026-10-02T10:00:00", "已排定", replaces_id="A3",
                         planned_minutes=60)
    service.add_activity("A5", "V1", "跨月读书营", "2026-09-30T22:00:00",
                         "2026-10-02T10:00:00", "已结算",
                         planned_minutes=120, actual_minutes=100)
    service.add_activity("A6", "V2", "文物讲堂", "2026-09-10T10:00:00",
                         "2026-09-10T11:00:00", "已结算",
                         planned_minutes=60, actual_minutes=60)

    service.add_check_in("C1", "A1", "person-1", "2026-09-05T10:05:00")
    service.add_check_in("C2", "A1", "person-1", "2026-09-05T10:10:00")
    service.add_check_in("C3", "A1", "person-2", "2026-09-05T10:20:00")
    service.add_check_in("C4", "A1", "person-3", "2026-09-05T11:30:00")
    service.add_check_in("C5", "A2", "id-zhangsan", "2026-09-20T14:05:00")
    service.add_check_in("C6", "A2", "phone-zhangsan", "2026-09-20T14:10:00")
    service.add_check_in("C7", "A5", "person-5", "2026-09-30T22:30:00")
    service.add_check_in("C8", "A6", "person-9", "2026-09-10T10:00:00")
    service.add_check_in("C9", "A6", "person-9", "2026-09-10T10:45:00")

    service.submit_venue_report("V1", MONTH, sessions=4,
                                service_persons=8, lecture_minutes=260)
    service.submit_venue_report("V2", MONTH, sessions=1,
                                service_persons=1, lecture_minutes=60)


def make_service() -> ReconciliationService:
    service = ReconciliationService()
    seed_service(service)
    return service
