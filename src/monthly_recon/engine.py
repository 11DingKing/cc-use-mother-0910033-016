"""统计引擎：按口径版本计算月度指标，并生成包含/排除台账供下钻。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .caliber import CaliberVersion
from .models import (
    AdjustmentEvent,
    AdjustmentType,
    CheckIn,
    LedgerEntry,
    ReasonCode,
    Session,
    SourceSnapshot,
    month_of,
    month_range,
    num,
    parse_month,
    shift_month,
)


@dataclass
class VenueComputation:
    """单场馆月度计算结果：三条台账（场次、签到、时长）加身份合并记录。"""

    venue_id: str
    session_entries: list[LedgerEntry] = field(default_factory=list)
    checkin_entries: list[LedgerEntry] = field(default_factory=list)
    duration_entries: list[LedgerEntry] = field(default_factory=list)
    merges: list[dict] = field(default_factory=list)

    def ledger(self, metric: str) -> list[LedgerEntry]:
        return {
            "sessions": self.session_entries,
            "headcount": self.checkin_entries,
            "explanation_minutes": self.duration_entries,
        }[metric]

    def totals(self) -> dict[str, float | int]:
        sessions = sum(1 for e in self.session_entries if e.included)
        people = {e.extra["canonical_person"] for e in self.checkin_entries if e.included}
        minutes = sum(e.value for e in self.duration_entries if e.included)
        return {"sessions": sessions, "headcount": len(people), "explanation_minutes": num(minutes)}

    def reason_summary(self, metric: str) -> dict[str, int]:
        summary: dict[str, int] = {}
        for entry in self.ledger(metric):
            if not entry.included and entry.reason:
                summary[entry.reason] = summary.get(entry.reason, 0) + 1
        return summary


@dataclass
class MonthComputation:
    """整月计算结果，随报告保存以支撑下钻与审计。"""

    month: str
    caliber_version: str
    venues: dict[str, VenueComputation]
    snapshot_refs: list[dict]
    adjustment_ids: list[str]

    def venue_totals(self, venue_id: str) -> dict[str, float | int]:
        comp = self.venues.get(venue_id)
        if comp is None:
            return {"sessions": 0, "headcount": 0, "explanation_minutes": 0}
        return comp.totals()

    def county_totals(self, headcount_scope: str) -> dict[str, float | int]:
        sessions = sum(v.totals()["sessions"] for v in self.venues.values())
        minutes = num(sum(v.totals()["explanation_minutes"] for v in self.venues.values()))
        if headcount_scope == "county":
            people = {
                e.extra["canonical_person"]
                for v in self.venues.values()
                for e in v.checkin_entries
                if e.included
            }
            headcount = len(people)
        else:
            headcount = sum(v.totals()["headcount"] for v in self.venues.values())
        return {"sessions": sessions, "headcount": headcount, "explanation_minutes": minutes}


def split_minutes_by_month(start: datetime, end: datetime) -> dict[str, float]:
    """把 [start, end) 的分钟数按月切分。"""
    result: dict[str, float] = {}
    cursor = start
    while cursor < end:
        if cursor.month == 12:
            boundary = datetime(cursor.year + 1, 1, 1)
        else:
            boundary = datetime(cursor.year, cursor.month + 1, 1)
        chunk_end = min(boundary, end)
        key = month_of(cursor.date())
        result[key] = result.get(key, 0.0) + (chunk_end - cursor).total_seconds() / 60
        cursor = chunk_end
    if not result:
        result[month_of(start.date())] = 0.0
    return result


def _home_month(session: Session, rule: str) -> str:
    """场次的归属月（场次计数与签到人数的归属）。"""
    if rule == "end_month" and session.end > session.start:
        return month_of((session.end - timedelta(seconds=1)).date())
    return month_of(session.start.date())


def _collect_records(snaps: list[SourceSnapshot]):
    """合并同场馆相邻月快照中的记录，按记录自然归属月优先、版本次之去重。"""
    act_candidates: dict[str, list] = {}
    sess_candidates: dict[str, list] = {}
    ck_candidates: dict[str, list] = {}
    for snap in snaps:
        for activity in snap.activities:
            act_candidates.setdefault(activity.activity_id, []).append((snap, activity))
        for session in snap.sessions:
            sess_candidates.setdefault(session.session_id, []).append((snap, session))
        for checkin in snap.checkins:
            ck_candidates.setdefault(checkin.checkin_id, []).append((snap, checkin))
    activities = {k: max(v, key=lambda i: i[0].version)[1] for k, v in act_candidates.items()}
    sessions = {
        k: max(v, key=lambda i: (i[0].month == month_of(i[1].start.date()), i[0].version))[1]
        for k, v in sess_candidates.items()
    }
    checkins = {
        k: max(v, key=lambda i: (i[0].month == month_of(i[1].checked_at.date()), i[0].version))[1]
        for k, v in ck_candidates.items()
    }
    return activities, sessions, checkins


def compute_month(
    month: str,
    snapshots: list[SourceSnapshot],
    adjustments: list[AdjustmentEvent],
    caliber: CaliberVersion,
) -> MonthComputation:
    """计算某月各场馆指标。

    snapshots 为全部历史快照（引擎自行选取相邻月最新版本），adjustments 为
    服务层按截止时间过滤后的调整事件。返回包含完整台账的 MonthComputation。
    """
    parse_month(month)
    _, m_end = month_range(month)
    grace_deadline = m_end + timedelta(days=caliber.late_checkin_grace_days)
    relevant = {shift_month(month, -1), month, shift_month(month, 1)}

    latest: dict[tuple[str, str], SourceSnapshot] = {}
    for snap in snapshots:
        if snap.month not in relevant:
            continue
        key = (snap.venue_id, snap.month)
        if key not in latest or snap.version > latest[key].version:
            latest[key] = snap

    alias: dict[str, str] = {}
    manual_alloc: dict[str, dict[str, float]] = {}
    extra_checkins: dict[str, list[CheckIn]] = {}
    used_adjustments: list[str] = []
    for event in adjustments:
        if event.type is AdjustmentType.IDENTITY_MERGE:
            alias[event.payload["alias_person_id"]] = event.payload["canonical_person_id"]
            used_adjustments.append(event.event_id)
        elif event.type is AdjustmentType.CROSS_MONTH_ALLOCATION:
            if month in event.payload.get("allocations", {}):
                manual_alloc[event.payload["session_id"]] = {
                    k: float(v) for k, v in event.payload["allocations"].items()
                }
                used_adjustments.append(event.event_id)
        elif event.type is AdjustmentType.LATE_CHECKIN:
            if event.payload.get("month") == month:
                extra_checkins.setdefault(event.payload["venue_id"], []).append(
                    CheckIn.from_dict(event.payload["checkin"])
                )
                used_adjustments.append(event.event_id)

    def canonical(person_id: str) -> str:
        seen = set()
        current = person_id
        while current in alias and current not in seen:
            seen.add(current)
            current = alias[current]
        return current

    venue_ids = sorted({vid for vid, _ in latest} | set(extra_checkins))
    venues_out: dict[str, VenueComputation] = {}
    for venue_id in venue_ids:
        snaps = sorted(
            (snap for (vid, _), snap in latest.items() if vid == venue_id),
            key=lambda s: (s.month, s.version),
        )
        activities, sessions, checkins = _collect_records(snaps)
        for extra in extra_checkins.get(venue_id, []):
            checkins.setdefault(extra.checkin_id, extra)
        venues_out[venue_id] = _compute_venue(
            month, venue_id, activities, sessions, checkins, caliber, canonical, manual_alloc, grace_deadline
        )

    snapshot_refs = [
        {
            "snapshot_id": snap.snapshot_id,
            "venue_id": snap.venue_id,
            "month": snap.month,
            "version": snap.version,
            "checksum": snap.checksum,
        }
        for _, snap in sorted(latest.items())
    ]
    return MonthComputation(
        month=month,
        caliber_version=caliber.version,
        venues=venues_out,
        snapshot_refs=snapshot_refs,
        adjustment_ids=sorted(set(used_adjustments)),
    )


def _compute_venue(
    month: str,
    venue_id: str,
    activities: dict,
    sessions: dict[str, Session],
    checkins: dict[str, CheckIn],
    caliber: CaliberVersion,
    canonical,
    manual_alloc: dict[str, dict[str, float]],
    grace_deadline,
) -> VenueComputation:
    comp = VenueComputation(venue_id)
    rebooked_by = {a.rebooked_from: a for a in activities.values() if a.rebooked_from}
    home_of: dict[str, str] = {}
    cancelled_sessions: set[str] = set()
    ledgered_sessions: set[str] = set()

    for session in sorted(sessions.values(), key=lambda s: (s.start, s.session_id)):
        activity = activities.get(session.activity_id)
        if activity is None:
            raise ValueError(f"场次 {session.session_id} 缺少所属活动 {session.activity_id}")
        minutes_map = split_minutes_by_month(session.start, session.end)
        total_minutes = sum(minutes_map.values())
        in_month_minutes = minutes_map.get(month, 0.0)
        alloc = manual_alloc.get(session.session_id)
        relates = (
            month_of(session.start.date()) == month
            or in_month_minutes > 0
            or (alloc or {}).get(month, 0) > 0
        )
        if not relates:
            continue
        ledgered_sessions.add(session.session_id)
        base_extra = {"activity_id": activity.activity_id, "start": session.start.isoformat()}

        if activity.is_cancelled:
            if caliber.count_rebooked_once and activity.activity_id in rebooked_by:
                reason = ReasonCode.CANCELLED_REBOOKED
                detail = f"取消后由补办活动 {rebooked_by[activity.activity_id].activity_id} 承接"
            else:
                reason = ReasonCode.CANCELLED
                detail = "活动已取消"
            comp.session_entries.append(
                LedgerEntry(session.session_id, False, 0, reason.value, detail, dict(base_extra))
            )
            comp.duration_entries.append(
                LedgerEntry(session.session_id, False, 0, reason.value, detail, dict(base_extra))
            )
            cancelled_sessions.add(session.session_id)
            continue

        home = _home_month(session, caliber.cross_month_rule)
        home_of[session.session_id] = home
        too_short = session.duration_minutes < caliber.min_session_minutes

        if home == month:
            if too_short:
                comp.session_entries.append(
                    LedgerEntry(
                        session.session_id,
                        False,
                        0,
                        ReasonCode.TOO_SHORT.value,
                        f"时长 {num(session.duration_minutes)} 分钟低于口径下限 {caliber.min_session_minutes} 分钟",
                        dict(base_extra),
                    )
                )
            else:
                comp.session_entries.append(
                    LedgerEntry(session.session_id, True, 1, extra=dict(base_extra, home_month=home))
                )
        else:
            comp.session_entries.append(
                LedgerEntry(
                    session.session_id,
                    False,
                    0,
                    ReasonCode.OUT_OF_MONTH.value,
                    f"按口径计入 {home}",
                    dict(base_extra, home_month=home),
                )
            )

        if too_short:
            comp.duration_entries.append(
                LedgerEntry(
                    session.session_id,
                    False,
                    0,
                    ReasonCode.TOO_SHORT.value,
                    f"时长 {num(session.duration_minutes)} 分钟低于口径下限 {caliber.min_session_minutes} 分钟",
                    dict(base_extra),
                )
            )
            continue

        if alloc is not None:
            fraction = alloc.get(month, 0.0)
            rule_note = "人工分摊"
        elif caliber.cross_month_rule == "by_minutes" and total_minutes > 0:
            fraction = in_month_minutes / total_minutes
            rule_note = "按分钟分摊"
        else:
            fraction = 1.0 if home == month else 0.0
            rule_note = f"计入{('开始月' if caliber.cross_month_rule == 'start_month' else '结束月')}"

        value = num(session.effective_explanation_minutes * fraction)
        if fraction > 0:
            detail = "" if fraction >= 1 else f"{rule_note}：本月占比 {fraction:.2%}"
            comp.duration_entries.append(
                LedgerEntry(
                    session.session_id,
                    True,
                    value,
                    detail=detail,
                    extra=dict(base_extra, fraction=round(fraction, 6)),
                )
            )
        else:
            comp.duration_entries.append(
                LedgerEntry(
                    session.session_id,
                    False,
                    0,
                    ReasonCode.CROSS_MONTH_OUT.value,
                    f"{rule_note}后本月占比为 0，计入其他月份",
                    dict(base_extra),
                )
            )

    _compute_checkins(
        month, comp, sessions, checkins, home_of, cancelled_sessions, ledgered_sessions, caliber, canonical, grace_deadline
    )
    return comp


def _compute_checkins(
    month: str,
    comp: VenueComputation,
    sessions: dict[str, Session],
    checkins: dict[str, CheckIn],
    home_of: dict[str, str],
    cancelled_sessions: set[str],
    ledgered_sessions: set[str],
    caliber: CaliberVersion,
    canonical,
    grace_deadline,
) -> None:
    by_session: dict[str, list[CheckIn]] = {}
    for checkin in checkins.values():
        by_session.setdefault(checkin.session_id, []).append(checkin)

    candidates: list[tuple[CheckIn, str]] = []
    for session_id, session_checkins in by_session.items():
        if session_id not in ledgered_sessions:
            continue
        for checkin in sorted(session_checkins, key=lambda c: (c.checked_at, c.checkin_id)):
            extra = {
                "session_id": session_id,
                "original_person_id": checkin.person_id,
                "checked_at": checkin.checked_at.isoformat(),
                "recorded_at": checkin.recorded_at.isoformat(),
            }
            if session_id in cancelled_sessions:
                comp.checkin_entries.append(
                    LedgerEntry(checkin.checkin_id, False, 0, ReasonCode.ACTIVITY_CANCELLED.value, "所属活动已取消", extra)
                )
            elif home_of.get(session_id) != month:
                comp.checkin_entries.append(
                    LedgerEntry(
                        checkin.checkin_id,
                        False,
                        0,
                        ReasonCode.OUT_OF_MONTH.value,
                        f"签到计入 {home_of[session_id]}",
                        extra,
                    )
                )
            elif checkin.recorded_at.date() > grace_deadline:
                comp.checkin_entries.append(
                    LedgerEntry(
                        checkin.checkin_id,
                        False,
                        0,
                        ReasonCode.LATE_BEYOND_GRACE.value,
                        f"录入于 {checkin.recorded_at.date()}，超出宽限截止 {grace_deadline}",
                        extra,
                    )
                )
            else:
                candidates.append((checkin, extra))

    candidates.sort(key=lambda item: (item[0].checked_at, item[0].checkin_id))
    kept: list[tuple[CheckIn, str, dict]] = []
    seen_session_person: set[tuple[str, str]] = set()
    for checkin, extra in candidates:
        canon = canonical(checkin.person_id)
        if canon != checkin.person_id:
            comp.merges.append(
                {
                    "checkin_id": checkin.checkin_id,
                    "alias_person_id": checkin.person_id,
                    "canonical_person_id": canon,
                }
            )
        key = (checkin.session_id, canon)
        if key in seen_session_person:
            comp.checkin_entries.append(
                LedgerEntry(
                    checkin.checkin_id,
                    False,
                    0,
                    ReasonCode.DUPLICATE_CHECKIN.value,
                    "同一人同场次重复签到",
                    dict(extra, canonical_person=canon),
                )
            )
            continue
        seen_session_person.add(key)
        kept.append((checkin, canon, extra))

    if caliber.duplicate_window_minutes > 0:
        window = timedelta(minutes=caliber.duplicate_window_minutes)
        last_kept: dict[str, CheckIn] = {}
        final: list[tuple[CheckIn, str, dict]] = []
        for checkin, canon, extra in kept:
            previous = last_kept.get(canon)
            if previous is not None and timedelta(0) <= checkin.checked_at - previous.checked_at <= window:
                comp.checkin_entries.append(
                    LedgerEntry(
                        checkin.checkin_id,
                        False,
                        0,
                        ReasonCode.DUPLICATE_CHECKIN.value,
                        f"同一人在 {caliber.duplicate_window_minutes} 分钟窗口内重复签到（首次 {previous.checkin_id}）",
                        dict(extra, canonical_person=canon),
                    )
                )
                continue
            last_kept[canon] = checkin
            final.append((checkin, canon, extra))
        kept = final

    for checkin, canon, extra in kept:
        comp.checkin_entries.append(
            LedgerEntry(checkin.checkin_id, True, 1, extra=dict(extra, canonical_person=canon))
        )
