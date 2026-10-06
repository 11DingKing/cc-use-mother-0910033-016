"""统计口径版本：所有统计规则参数化并按版本管理。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

CROSS_MONTH_RULES = ("by_minutes", "start_month", "end_month")
HEADCOUNT_SCOPES = ("venue", "county")


@dataclass(frozen=True)
class CaliberVersion:
    """统计口径版本。

    - duplicate_window_minutes: 同一人在同场馆该时间窗口内的跨场次签到视为重复；同场次内永远去重。
    - late_checkin_grace_days: 月末后多少天内录入的迟到签到仍计入当月。
    - cross_month_rule: 跨月场次分摊规则，by_minutes 按分钟分摊、start_month 计入开始月、end_month 计入结束月。
    - headcount_scope: 服务人数去重范围，venue 按场馆去重后求和，county 全县统一去重。
    - count_rebooked_once: 取消补办链条只计一次（取消场次由补办活动承接）。
    - min_session_minutes: 低于该时长的场次不计入场次与讲解时长。
    """

    version: str
    effective_from: str
    duplicate_window_minutes: int = 30
    late_checkin_grace_days: int = 3
    cross_month_rule: str = "by_minutes"
    headcount_scope: str = "venue"
    count_rebooked_once: bool = True
    min_session_minutes: int = 10

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("口径版本号不能为空")
        date.fromisoformat(self.effective_from)
        if self.cross_month_rule not in CROSS_MONTH_RULES:
            raise ValueError(f"未知跨月分摊规则：{self.cross_month_rule}")
        if self.headcount_scope not in HEADCOUNT_SCOPES:
            raise ValueError(f"未知人数去重范围：{self.headcount_scope}")
        if self.duplicate_window_minutes < 0 or self.late_checkin_grace_days < 0 or self.min_session_minutes < 0:
            raise ValueError("口径参数不能为负数")

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "effective_from": self.effective_from,
            "duplicate_window_minutes": self.duplicate_window_minutes,
            "late_checkin_grace_days": self.late_checkin_grace_days,
            "cross_month_rule": self.cross_month_rule,
            "headcount_scope": self.headcount_scope,
            "count_rebooked_once": self.count_rebooked_once,
            "min_session_minutes": self.min_session_minutes,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "CaliberVersion":
        return cls(
            version=str(raw["version"]),
            effective_from=str(raw["effective_from"]),
            duplicate_window_minutes=int(raw.get("duplicate_window_minutes", 30)),
            late_checkin_grace_days=int(raw.get("late_checkin_grace_days", 3)),
            cross_month_rule=str(raw.get("cross_month_rule", "by_minutes")),
            headcount_scope=str(raw.get("headcount_scope", "venue")),
            count_rebooked_once=bool(raw.get("count_rebooked_once", True)),
            min_session_minutes=int(raw.get("min_session_minutes", 10)),
        )


DEFAULT_CALIBERS = (
    CaliberVersion(
        version="v0.9",
        effective_from="2025-01-01",
        duplicate_window_minutes=0,
        late_checkin_grace_days=0,
        cross_month_rule="start_month",
        headcount_scope="venue",
        count_rebooked_once=False,
        min_session_minutes=0,
    ),
    CaliberVersion(
        version="v1.0",
        effective_from="2026-01-01",
        duplicate_window_minutes=30,
        late_checkin_grace_days=3,
        cross_month_rule="by_minutes",
        headcount_scope="venue",
        count_rebooked_once=True,
        min_session_minutes=10,
    ),
)
