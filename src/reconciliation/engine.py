"""统计引擎：按口径版本对来源快照计算月度指标，并保留包含/排除血缘。

引擎是纯函数：输入快照载荷、口径规则、当期调整事件与目标月份，
输出每个场馆的指标值与逐记录血缘，供差异对账、封账与下钻复算共用。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .models import METRICS

REASON_CANCELLED = "活动已取消"
REASON_CANCELLED_REHELD = "活动已取消，由补办活动承接"
REASON_CROSS_MONTH = "跨月分摊至其他月份"
REASON_DUPLICATE = "重复签到"
REASON_LATE = "迟到超出宽限"
NOTE_LATE_ACCEPTED = "迟到签到经调整事件采纳"


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def month_of(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """返回月份的起止时刻（闭区间）。"""
    year, mon = int(month[:4]), int(month[5:7])
    start = datetime(year, mon, 1)
    if mon == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, mon + 1, 1)
    return start, nxt - timedelta(microseconds=1)


def day_weights(start, end) -> dict[str, float]:
    """按自然日把活动分摊到各月份，返回 {月份: 权重}。"""
    if end < start:
        raise ValueError("活动结束时间早于开始时间")
    total = (end - start).days + 1
    counts: dict[str, int] = defaultdict(int)
    cursor = start
    while cursor <= end:
        counts[f"{cursor.year:04d}-{cursor.month:02d}"] += 1
        cursor += timedelta(days=1)
    return {key: count / total for key, count in counts.items()}


def empty_result() -> dict:
    """没有任何记录时的空统计结果。"""
    return {
        "sessions": 0.0,
        "service_persons": 0,
        "lecture_minutes": 0.0,
        "included": {metric: [] for metric in METRICS},
        "excluded": {metric: [] for metric in METRICS},
    }


class _VenueStat:
    """单场馆月度累计器。"""

    def __init__(self) -> None:
        self.sessions = 0.0
        self.lecture_minutes = 0.0
        self.persons: dict[str, str] = {}  # 归并后身份 -> 首个签到编号
        self.checkin_count = 0
        self.included = {metric: [] for metric in METRICS}
        self.excluded = {metric: [] for metric in METRICS}


def _duration_minutes(activity: dict, rules: dict) -> float:
    if rules["duration_mode"] == "planned":
        return float(activity.get("planned_minutes") or 0.0)
    actual = activity.get("actual_minutes")
    if actual is not None:
        return float(actual)
    return float(activity.get("planned_minutes") or 0.0)


def _default_weights(activity: dict, rules: dict) -> dict[str, float]:
    start = parse_dt(activity["start_at"])
    end = parse_dt(activity["end_at"])
    if rules["cross_month_rule"] == "by_start":
        return {month_of(start): 1.0}
    return day_weights(start.date(), end.date())


def _prepare_adjustments(adjustments: list[dict]):
    """把当期调整事件整理成引擎可查的索引。"""
    merge: dict[str, str] = {}
    late_ok: set[str] = set()
    overrides: dict[str, dict[str, float]] = {}
    for adjustment in adjustments:
        payload = adjustment["payload"]
        if adjustment["type"] == "identity_merge":
            merge[payload["source"]] = payload["target"]
        elif adjustment["type"] == "late_checkin":
            late_ok.add(payload["check_in_id"])
        elif adjustment["type"] == "cross_month_override":
            overrides[payload["activity_id"]] = {
                item["month"]: float(item["weight"])
                for item in payload["allocations"]
            }
    return merge, late_ok, overrides


def _canonical_resolver(merge: dict[str, str]):
    """身份合并归并：沿合并链解析到规范身份，带环路保护。"""
    def resolve(person: str) -> str:
        seen = set()
        while person in merge and person not in seen:
            seen.add(person)
            person = merge[person]
        return person
    return resolve


def _absorb_checkins(stat: _VenueStat, activity: dict, checkins: list[dict],
                     rules: dict, resolve, late_ok: set[str]) -> None:
    """处理单活动的签到：迟到宽限、窗口去重、身份归并。"""
    end = parse_dt(activity["end_at"])
    grace = timedelta(minutes=rules["late_grace_minutes"])
    window = timedelta(minutes=rules["dedupe_window_minutes"])
    last_accepted: dict[str, datetime] = {}
    ordered = sorted(checkins, key=lambda ci: (ci["checked_in_at"], ci["check_in_id"]))
    unique_mode = rules["service_person_mode"] == "unique_person"
    for checkin in ordered:
        check_in_id = checkin["check_in_id"]
        person = resolve(checkin["person_key"])
        moment = parse_dt(checkin["checked_in_at"])
        late = moment > end + grace
        if late and check_in_id not in late_ok:
            stat.excluded["service_persons"].append({
                "check_in_id": check_in_id, "person": person, "reason": REASON_LATE,
            })
            continue
        last = last_accepted.get(person)
        if last is not None and moment - last < window:
            stat.excluded["service_persons"].append({
                "check_in_id": check_in_id, "person": person, "reason": REASON_DUPLICATE,
            })
            continue
        last_accepted[person] = moment
        stat.checkin_count += 1
        if unique_mode:
            if person in stat.persons:
                continue  # 同场馆同月已计入，仅保留首次血缘
            stat.persons[person] = check_in_id
            entry = {"person": person, "check_in_id": check_in_id}
        else:
            entry = {"check_in_id": check_in_id, "person": person}
        if late:
            entry["note"] = NOTE_LATE_ACCEPTED
        stat.included["service_persons"].append(entry)


def compute_month(payload: dict, rules: dict, adjustments: list[dict], month: str) -> dict:
    """计算某月各场馆指标。返回 {场馆: {指标值..., included, excluded}}。"""
    start_bound, end_bound = month_bounds(month)
    merge, late_ok, overrides = _prepare_adjustments(adjustments)
    resolve = _canonical_resolver(merge)

    checkins_by_activity: dict[str, list[dict]] = defaultdict(list)
    for checkin in payload.get("check_ins", []):
        checkins_by_activity[checkin["activity_id"]].append(checkin)

    activities = payload.get("activities", [])
    reheld_sources = {a.get("replaces_id") for a in activities if a.get("replaces_id")}

    stats: dict[str, _VenueStat] = defaultdict(_VenueStat)
    for activity in activities:
        start = parse_dt(activity["start_at"])
        end = parse_dt(activity["end_at"])
        if end < start:
            raise ValueError(f"活动 {activity['activity_id']} 结束时间早于开始时间")
        if start > end_bound or end < start_bound:
            continue  # 与本月无交集，不进入本月口径
        stat = stats[activity["venue_id"]]
        activity_id = activity["activity_id"]
        related = checkins_by_activity.get(activity_id, [])

        if activity.get("status") == "已取消":
            reason = REASON_CANCELLED_REHELD if activity_id in reheld_sources else REASON_CANCELLED
            stat.excluded["sessions"].append({"activity_id": activity_id, "reason": reason})
            stat.excluded["lecture_minutes"].append({"activity_id": activity_id, "reason": reason})
            for checkin in related:
                stat.excluded["service_persons"].append({
                    "check_in_id": checkin["check_in_id"],
                    "person": resolve(checkin["person_key"]),
                    "reason": REASON_CANCELLED,
                })
            continue

        weights = overrides.get(activity_id) or _default_weights(activity, rules)
        weight = float(weights.get(month, 0.0))
        if weight <= 0:
            stat.excluded["sessions"].append({"activity_id": activity_id, "reason": REASON_CROSS_MONTH})
            stat.excluded["lecture_minutes"].append({"activity_id": activity_id, "reason": REASON_CROSS_MONTH})
            for checkin in related:
                stat.excluded["service_persons"].append({
                    "check_in_id": checkin["check_in_id"],
                    "person": resolve(checkin["person_key"]),
                    "reason": REASON_CROSS_MONTH,
                })
            continue

        stat.sessions += weight
        stat.included["sessions"].append({"activity_id": activity_id, "weight": round(weight, 4)})
        minutes = _duration_minutes(activity, rules) * weight
        stat.lecture_minutes += minutes
        stat.included["lecture_minutes"].append({"activity_id": activity_id, "minutes": round(minutes, 2)})
        _absorb_checkins(stat, activity, related, rules, resolve, late_ok)

    return {venue_id: _finalize(stat, rules) for venue_id, stat in stats.items()}


def _finalize(stat: _VenueStat, rules: dict) -> dict:
    if rules["service_person_mode"] == "unique_person":
        persons = len(stat.persons)
    else:
        persons = stat.checkin_count
    return {
        "sessions": round(stat.sessions, 2),
        "service_persons": persons,
        "lecture_minutes": round(stat.lecture_minutes, 2),
        "included": stat.included,
        "excluded": stat.excluded,
    }
