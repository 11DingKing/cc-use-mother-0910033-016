"""月度活动统计对账的领域常量与异常。"""
from __future__ import annotations

METRICS = ("sessions", "service_persons", "lecture_minutes")

METRIC_NAMES = {
    "sessions": "场次",
    "service_persons": "服务人数",
    "lecture_minutes": "讲解时长(分钟)",
}

# 调整事件类型：迟到签到、身份合并、跨月分摊、已签报告更正
ADJUSTMENT_TYPES = (
    "late_checkin",
    "identity_merge",
    "cross_month_override",
    "report_correction",
)

ACTIVITY_STATES = ("筹备", "待确认", "已排定", "执行中", "已结算", "已取消")


class DomainError(Exception):
    """领域错误基类。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class ConflictError(DomainError):
    """当前状态不允许该操作（如未确认差异就封账）。"""
