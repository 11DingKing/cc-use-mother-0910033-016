"""月度活动统计对账的数据模型与基础工具。"""
from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

METRICS = ("sessions", "headcount", "explanation_minutes")
METRIC_LABELS = {
    "sessions": "场次",
    "headcount": "服务人数",
    "explanation_minutes": "讲解时长(分钟)",
}


def parse_month(month: str) -> tuple[int, int]:
    """校验并解析 YYYY-MM 月份。"""
    try:
        year_s, month_s = str(month).split("-")
        year, mon = int(year_s), int(month_s)
    except ValueError as exc:
        raise ValueError(f"月份格式应为 YYYY-MM：{month!r}") from exc
    if not 1 <= mon <= 12:
        raise ValueError(f"月份超出范围：{month!r}")
    return year, mon


def month_range(month: str) -> tuple[date, date]:
    """返回月份的首尾日期。"""
    year, mon = parse_month(month)
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def shift_month(month: str, delta: int) -> str:
    """月份平移，delta 可为负。"""
    year, mon = parse_month(month)
    idx = year * 12 + (mon - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def stable_hash(payload: Any) -> str:
    """对任意可 JSON 化对象生成确定性摘要。"""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def num(value: float) -> float | int:
    """数值规整：保留两位小数，整数则返回 int。"""
    rounded = round(float(value), 2)
    return int(rounded) if float(rounded).is_integer() else rounded


class ReasonCode(str, Enum):
    """排除原因代码，用于下钻清单。"""

    OUT_OF_MONTH = "OUT_OF_MONTH"  # 归属于其他月份
    CROSS_MONTH_OUT = "CROSS_MONTH_OUT"  # 跨月分摊出本月
    CANCELLED = "CANCELLED"  # 活动取消
    CANCELLED_REBOOKED = "CANCELLED_REBOOKED"  # 取消后补办，由补办活动承接
    TOO_SHORT = "TOO_SHORT"  # 低于口径最短场次时长
    DUPLICATE_CHECKIN = "DUPLICATE_CHECKIN"  # 重复签到
    LATE_BEYOND_GRACE = "LATE_BEYOND_GRACE"  # 迟到签到超出宽限期
    ACTIVITY_CANCELLED = "ACTIVITY_CANCELLED"  # 所属活动取消，签到连带排除


REASON_LABELS = {
    ReasonCode.OUT_OF_MONTH.value: "归属于其他月份",
    ReasonCode.CROSS_MONTH_OUT.value: "跨月分摊出本月",
    ReasonCode.CANCELLED.value: "活动取消",
    ReasonCode.CANCELLED_REBOOKED.value: "取消后补办，由补办活动承接",
    ReasonCode.TOO_SHORT.value: "低于口径最短场次时长",
    ReasonCode.DUPLICATE_CHECKIN.value: "重复签到",
    ReasonCode.LATE_BEYOND_GRACE.value: "迟到签到超出宽限期",
    ReasonCode.ACTIVITY_CANCELLED.value: "所属活动取消，签到连带排除",
}


class AdjustmentType(str, Enum):
    """调整事件类型。"""

    LATE_CHECKIN = "LATE_CHECKIN"  # 迟到签到补录
    IDENTITY_MERGE = "IDENTITY_MERGE"  # 身份合并
    CROSS_MONTH_ALLOCATION = "CROSS_MONTH_ALLOCATION"  # 跨月分摊
    REPORT_CORRECTION = "REPORT_CORRECTION"  # 已签报告更正


@dataclass(frozen=True)
class Venue:
    venue_id: str
    name: str

    def to_dict(self) -> dict:
        return {"venue_id": self.venue_id, "name": self.name}


@dataclass(frozen=True)
class Activity:
    activity_id: str
    title: str
    status: str = "已排定"
    is_cancelled: bool = False
    rebooked_from: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Activity":
        return cls(
            activity_id=str(raw["activity_id"]),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", "已排定")),
            is_cancelled=bool(raw.get("is_cancelled", False)),
            rebooked_from=raw.get("rebooked_from"),
        )

    def to_dict(self) -> dict:
        return {
            "activity_id": self.activity_id,
            "title": self.title,
            "status": self.status,
            "is_cancelled": self.is_cancelled,
            "rebooked_from": self.rebooked_from,
        }


@dataclass(frozen=True)
class Session:
    """场次：一次有明确起止时间的活动执行。"""

    session_id: str
    activity_id: str
    start: datetime
    end: datetime
    explanation_minutes: float | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Session":
        return cls(
            session_id=str(raw["session_id"]),
            activity_id=str(raw["activity_id"]),
            start=parse_dt(raw["start"]),
            end=parse_dt(raw["end"]),
            explanation_minutes=raw.get("explanation_minutes"),
        )

    @property
    def duration_minutes(self) -> float:
        return max((self.end - self.start).total_seconds() / 60, 0.0)

    @property
    def effective_explanation_minutes(self) -> float:
        if self.explanation_minutes is None:
            return self.duration_minutes
        return float(self.explanation_minutes)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "activity_id": self.activity_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "explanation_minutes": self.explanation_minutes,
        }


@dataclass(frozen=True)
class CheckIn:
    """签到记录：checked_at 为签到时间，recorded_at 为系统录入时间。"""

    checkin_id: str
    session_id: str
    person_id: str
    checked_at: datetime
    recorded_at: datetime

    @classmethod
    def from_dict(cls, raw: dict) -> "CheckIn":
        checked_at = parse_dt(raw["checked_at"])
        recorded_raw = raw.get("recorded_at")
        return cls(
            checkin_id=str(raw["checkin_id"]),
            session_id=str(raw["session_id"]),
            person_id=str(raw["person_id"]),
            checked_at=checked_at,
            recorded_at=parse_dt(recorded_raw) if recorded_raw else checked_at,
        )

    def to_dict(self) -> dict:
        return {
            "checkin_id": self.checkin_id,
            "session_id": self.session_id,
            "person_id": self.person_id,
            "checked_at": self.checked_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True)
class SourceSnapshot:
    """来源快照：场馆某月报送数据的不可留痕版本。"""

    snapshot_id: str
    venue_id: str
    month: str
    version: int
    submitted_at: str
    reported: dict[str, float]
    activities: tuple[Activity, ...]
    sessions: tuple[Session, ...]
    checkins: tuple[CheckIn, ...]
    checksum: str

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "venue_id": self.venue_id,
            "month": self.month,
            "version": self.version,
            "submitted_at": self.submitted_at,
            "reported": dict(self.reported),
            "checksum": self.checksum,
            "activity_count": len(self.activities),
            "session_count": len(self.sessions),
            "checkin_count": len(self.checkins),
        }


@dataclass(frozen=True)
class LedgerEntry:
    """台账条目：一条记录在某指标下的包含/排除结论。"""

    record_id: str
    included: bool
    value: float
    reason: str | None = None
    detail: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {
            "record_id": self.record_id,
            "included": self.included,
            "value": num(self.value),
        }
        if self.reason:
            result["reason"] = self.reason
            result["reason_label"] = REASON_LABELS.get(self.reason, self.reason)
        if self.detail:
            result["detail"] = self.detail
        result.update(self.extra)
        return result


@dataclass(frozen=True)
class DiffItem:
    """差异清单条目：场馆自报与县级重算的偏差。"""

    diff_id: str
    month: str
    venue_id: str
    metric: str
    reported: float
    computed: float
    delta: float
    reasons: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "diff_id": self.diff_id,
            "month": self.month,
            "venue_id": self.venue_id,
            "metric": self.metric,
            "metric_label": METRIC_LABELS[self.metric],
            "reported": num(self.reported),
            "computed": num(self.computed),
            "delta": num(self.delta),
            "reasons": self.reasons,
        }


@dataclass(frozen=True)
class Confirmation:
    """场馆对差异清单的确认。"""

    venue_id: str
    month: str
    actor: str
    note: str
    confirmed_at: str

    def to_dict(self) -> dict:
        return {
            "venue_id": self.venue_id,
            "month": self.month,
            "actor": self.actor,
            "note": self.note,
            "confirmed_at": self.confirmed_at,
        }


@dataclass(frozen=True)
class AdjustmentEvent:
    """调整事件：迟到签到、身份合并、跨月分摊、已签报告更正。"""

    event_id: str
    type: AdjustmentType
    month: str
    posted_by: str
    reason: str
    payload: dict
    posted_at: str

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "type": self.type.value,
            "month": self.month,
            "posted_by": self.posted_by,
            "reason": self.reason,
            "payload": self.payload,
            "posted_at": self.posted_at,
        }


@dataclass(frozen=True)
class MonthlyReport:
    """月度报告：封账产出为稳定版本，复算产出为新的修订版本。"""

    report_id: str
    month: str
    revision: int
    sealed: bool
    caliber_version: str
    generated_at: str
    venue_lines: dict[str, dict[str, float]]
    totals: dict[str, float]
    corrections_applied: list[dict]
    inputs_fingerprint: str
    supersedes: str | None

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "month": self.month,
            "revision": self.revision,
            "sealed": self.sealed,
            "caliber_version": self.caliber_version,
            "generated_at": self.generated_at,
            "venues": {vid: {m: num(v) for m, v in line.items()} for vid, line in self.venue_lines.items()},
            "totals": {m: num(v) for m, v in self.totals.items()},
            "metric_labels": METRIC_LABELS,
            "corrections_applied": self.corrections_applied,
            "inputs_fingerprint": self.inputs_fingerprint,
            "supersedes": self.supersedes,
        }
