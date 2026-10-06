"""应用服务：快照报送、差异对账、场馆确认、封账、调整事件、复算与下钻。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .caliber import DEFAULT_CALIBERS, CaliberVersion
from .engine import MonthComputation, compute_month
from .models import (
    METRICS,
    Activity,
    AdjustmentEvent,
    AdjustmentType,
    CheckIn,
    Confirmation,
    DiffItem,
    MonthlyReport,
    Session,
    SourceSnapshot,
    Venue,
    num,
    parse_month,
    stable_hash,
)


class ReconError(Exception):
    """业务错误，status 供 HTTP 层映射。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class ReconCycle:
    """单月对账周期：一次执行产生差异清单，场馆确认后可封账。"""

    month: str
    caliber_version: str
    run_at: str
    snapshot_versions: dict[str, int]
    diffs: list[DiffItem] = field(default_factory=list)
    confirmations: dict[str, Confirmation] = field(default_factory=dict)
    sealed_report_id: str | None = None

    @property
    def state(self) -> str:
        if self.sealed_report_id:
            return "CLOSED"
        venues = set(self.snapshot_versions)
        if venues and venues <= set(self.confirmations):
            return "CONFIRMED"
        return "DIFF_READY"

    def to_dict(self) -> dict:
        return {
            "month": self.month,
            "state": self.state,
            "caliber_version": self.caliber_version,
            "run_at": self.run_at,
            "snapshot_versions": dict(self.snapshot_versions),
            "diffs": [d.to_dict() for d in self.diffs],
            "confirmations": [c.to_dict() for c in self.confirmations.values()],
            "sealed_report_id": self.sealed_report_id,
        }


class MonthlyReconService:
    """月度活动统计对账服务。"""

    def __init__(self, clock=None, register_defaults: bool = True):
        self._clock = clock or datetime.now
        self.venues: dict[str, Venue] = {}
        self.calibers: dict[str, CaliberVersion] = {}
        self.snapshots: list[SourceSnapshot] = []
        self.cycles: dict[str, ReconCycle] = {}
        self.adjustments: list[AdjustmentEvent] = []
        self.reports: dict[str, list[MonthlyReport]] = {}
        self._computations: dict[str, MonthComputation] = {}
        self._seq = {"snap": 0, "adj": 0, "rpt": 0}
        if register_defaults:
            for caliber in DEFAULT_CALIBERS:
                self.register_caliber(caliber)

    # ---------- 基础登记 ----------

    def _now(self) -> datetime:
        return self._clock()

    def _next_id(self, kind: str) -> str:
        self._seq[kind] += 1
        return f"{kind}-{self._seq[kind]}"

    def register_venue(self, venue_id: str, name: str) -> Venue:
        if not venue_id:
            raise ReconError("场馆编号不能为空")
        venue = Venue(venue_id=venue_id, name=name or venue_id)
        self.venues[venue_id] = venue
        return venue

    def register_caliber(self, caliber: CaliberVersion) -> CaliberVersion:
        if caliber.version in self.calibers:
            raise ReconError(f"口径版本已存在：{caliber.version}", 409)
        self.calibers[caliber.version] = caliber
        return caliber

    def _caliber(self, version: str | None) -> CaliberVersion:
        if version is None:
            if not self.calibers:
                raise ReconError("尚未注册任何统计口径版本")
            return max(self.calibers.values(), key=lambda c: c.effective_from)
        try:
            return self.calibers[version]
        except KeyError:
            raise ReconError(f"未知统计口径版本：{version}", 404) from None

    # ---------- 来源快照 ----------

    def submit_snapshot(
        self,
        venue_id: str,
        month: str,
        reported: dict,
        activities: list[dict],
        sessions: list[dict],
        checkins: list[dict],
        submitted_at: str | None = None,
    ) -> SourceSnapshot:
        if venue_id not in self.venues:
            raise ReconError(f"未知场馆：{venue_id}", 404)
        try:
            parse_month(month)
        except ValueError as exc:
            raise ReconError(str(exc)) from exc
        unknown_metrics = set(reported) - set(METRICS)
        if unknown_metrics:
            raise ReconError(f"自报指标未知：{'、'.join(sorted(unknown_metrics))}")
        for metric, value in reported.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise ReconError(f"自报指标 {metric} 必须是非负数值")

        acts = [Activity.from_dict(a) for a in activities]
        sess = [Session.from_dict(s) for s in sessions]
        cks = [CheckIn.from_dict(c) for c in checkins]
        self._validate_snapshot_payload(acts, sess, cks)

        version = max(
            (s.version for s in self.snapshots if s.venue_id == venue_id and s.month == month),
            default=0,
        ) + 1
        checksum = stable_hash(
            {
                "venue_id": venue_id,
                "month": month,
                "reported": reported,
                "activities": [a.to_dict() for a in acts],
                "sessions": [s.to_dict() for s in sess],
                "checkins": [c.to_dict() for c in cks],
            }
        )
        snapshot = SourceSnapshot(
            snapshot_id=self._next_id("snap"),
            venue_id=venue_id,
            month=month,
            version=version,
            submitted_at=submitted_at or self._now().isoformat(),
            reported={k: num(v) for k, v in reported.items()},
            activities=tuple(acts),
            sessions=tuple(sess),
            checkins=tuple(cks),
            checksum=checksum,
        )
        self.snapshots.append(snapshot)
        return snapshot

    @staticmethod
    def _validate_snapshot_payload(acts: list[Activity], sess: list[Session], cks: list[CheckIn]) -> None:
        act_ids = [a.activity_id for a in acts]
        if len(act_ids) != len(set(act_ids)):
            raise ReconError("活动编号重复")
        act_id_set = set(act_ids)
        for activity in acts:
            if activity.rebooked_from and activity.rebooked_from not in act_id_set:
                raise ReconError(f"补办活动 {activity.activity_id} 的原活动 {activity.rebooked_from} 不在快照中")
        sess_ids = [s.session_id for s in sess]
        if len(sess_ids) != len(set(sess_ids)):
            raise ReconError("场次编号重复")
        sess_id_set = set(sess_ids)
        for session in sess:
            if session.activity_id not in act_id_set:
                raise ReconError(f"场次 {session.session_id} 的活动 {session.activity_id} 不在快照中")
            if session.end < session.start:
                raise ReconError(f"场次 {session.session_id} 结束时间早于开始时间")
            if session.explanation_minutes is not None and session.explanation_minutes < 0:
                raise ReconError(f"场次 {session.session_id} 讲解时长不能为负")
        ck_ids = [c.checkin_id for c in cks]
        if len(ck_ids) != len(set(ck_ids)):
            raise ReconError("签到编号重复")
        for checkin in cks:
            if checkin.session_id not in sess_id_set:
                raise ReconError(f"签到 {checkin.checkin_id} 的场次 {checkin.session_id} 不在快照中")
            if checkin.recorded_at < checkin.checked_at:
                raise ReconError(f"签到 {checkin.checkin_id} 录入时间早于签到时间")

    def list_snapshots(self, venue_id: str | None = None, month: str | None = None) -> list[SourceSnapshot]:
        return [
            s
            for s in self.snapshots
            if (venue_id is None or s.venue_id == venue_id) and (month is None or s.month == month)
        ]

    def _latest_exact_month(self, month: str) -> dict[str, SourceSnapshot]:
        latest: dict[str, SourceSnapshot] = {}
        for snap in self.snapshots:
            if snap.month != month:
                continue
            if snap.venue_id not in latest or snap.version > latest[snap.venue_id].version:
                latest[snap.venue_id] = snap
        return latest

    # ---------- 对账与封账 ----------

    def run_reconciliation(self, month: str, caliber_version: str | None = None) -> ReconCycle:
        if self._sealed_report(month):
            raise ReconError(f"{month} 已封账，不能再执行对账，请使用复算", 409)
        exact = self._latest_exact_month(month)
        if not exact:
            raise ReconError(f"{month} 没有任何来源快照，无法对账", 404)
        caliber = self._caliber(caliber_version)
        computation = compute_month(month, self.snapshots, self._adjustments_as_of(None), caliber)

        diffs: list[DiffItem] = []
        for venue_id, snap in sorted(exact.items()):
            computed = computation.venue_totals(venue_id)
            comp = computation.venues.get(venue_id)
            for metric in METRICS:
                reported = snap.reported.get(metric)
                if reported is None:
                    continue
                delta = num(computed[metric] - reported)
                if delta != 0:
                    diffs.append(
                        DiffItem(
                            diff_id=f"{month}:{venue_id}:{metric}",
                            month=month,
                            venue_id=venue_id,
                            metric=metric,
                            reported=reported,
                            computed=computed[metric],
                            delta=delta,
                            reasons=comp.reason_summary(metric) if comp else {},
                        )
                    )
        cycle = ReconCycle(
            month=month,
            caliber_version=caliber.version,
            run_at=self._now().isoformat(),
            snapshot_versions={vid: s.version for vid, s in exact.items()},
            diffs=diffs,
        )
        self.cycles[month] = cycle
        return cycle

    def get_cycle(self, month: str) -> ReconCycle:
        try:
            return self.cycles[month]
        except KeyError:
            raise ReconError(f"{month} 尚未执行对账", 404) from None

    def _assert_snapshot_fresh(self, cycle: ReconCycle, venue_id: str) -> None:
        latest = self._latest_exact_month(cycle.month).get(venue_id)
        if latest is None or latest.version != cycle.snapshot_versions.get(venue_id):
            raise ReconError(f"场馆 {venue_id} 的快照在对账后已变更，请重新执行对账", 409)

    def confirm(self, month: str, venue_id: str, actor: str, note: str = "") -> Confirmation:
        cycle = self.get_cycle(month)
        if cycle.sealed_report_id:
            raise ReconError(f"{month} 已封账，无法再确认", 409)
        if venue_id not in cycle.snapshot_versions:
            raise ReconError(f"场馆 {venue_id} 未参与 {month} 对账", 404)
        self._assert_snapshot_fresh(cycle, venue_id)
        confirmation = Confirmation(
            venue_id=venue_id,
            month=month,
            actor=actor,
            note=note,
            confirmed_at=self._now().isoformat(),
        )
        cycle.confirmations[venue_id] = confirmation
        return confirmation

    def close_month(self, month: str, actor: str) -> MonthlyReport:
        cycle = self.get_cycle(month)
        if cycle.sealed_report_id:
            raise ReconError(f"{month} 已封账", 409)
        for venue_id in cycle.snapshot_versions:
            self._assert_snapshot_fresh(cycle, venue_id)
        missing = sorted(set(cycle.snapshot_versions) - set(cycle.confirmations))
        if missing:
            raise ReconError(f"以下场馆尚未确认差异清单：{'、'.join(missing)}", 409)
        caliber = self._caliber(cycle.caliber_version)
        computation = compute_month(month, self.snapshots, self._adjustments_as_of(None), caliber)
        report = self._build_report(month, caliber, computation, sealed=True, corrections=[])
        cycle.sealed_report_id = report.report_id
        return report

    # ---------- 调整事件 ----------

    def post_adjustment(
        self,
        type: str,
        month: str,
        posted_by: str,
        reason: str,
        payload: dict,
        posted_at: str | None = None,
    ) -> AdjustmentEvent:
        try:
            adj_type = AdjustmentType(type)
        except ValueError:
            raise ReconError(f"未知调整事件类型：{type}") from None
        try:
            parse_month(month)
        except ValueError as exc:
            raise ReconError(str(exc)) from exc
        payload = dict(payload)
        if adj_type is AdjustmentType.LATE_CHECKIN:
            self._validate_late_checkin(month, payload)
        elif adj_type is AdjustmentType.IDENTITY_MERGE:
            self._validate_identity_merge(payload)
        elif adj_type is AdjustmentType.CROSS_MONTH_ALLOCATION:
            self._validate_cross_month(payload)
        elif adj_type is AdjustmentType.REPORT_CORRECTION:
            self._validate_report_correction(month, payload)
        event = AdjustmentEvent(
            event_id=self._next_id("adj"),
            type=adj_type,
            month=month,
            posted_by=posted_by,
            reason=reason,
            payload=payload,
            posted_at=posted_at or self._now().isoformat(),
        )
        self.adjustments.append(event)
        return event

    def _validate_late_checkin(self, month: str, payload: dict) -> None:
        venue_id = payload.get("venue_id")
        if venue_id not in self.venues:
            raise ReconError(f"未知场馆：{venue_id}", 404)
        raw = payload.get("checkin")
        if not isinstance(raw, dict):
            raise ReconError("迟到签到缺少 checkin 内容")
        checkin = CheckIn.from_dict(raw)
        known_sessions = {s.session_id for snap in self.snapshots if snap.venue_id == venue_id for s in snap.sessions}
        if checkin.session_id not in known_sessions:
            raise ReconError(f"迟到签到引用的场次 {checkin.session_id} 不存在")
        known_ids = {c.checkin_id for snap in self.snapshots for c in snap.checkins}
        known_ids |= {
            e.payload["checkin"]["checkin_id"]
            for e in self.adjustments
            if e.type is AdjustmentType.LATE_CHECKIN
        }
        if checkin.checkin_id in known_ids:
            raise ReconError(f"签到编号已存在：{checkin.checkin_id}", 409)
        payload["month"] = month
        payload["checkin"] = checkin.to_dict()

    def _validate_identity_merge(self, payload: dict) -> None:
        alias = payload.get("alias_person_id")
        canonical = payload.get("canonical_person_id")
        if not alias or not canonical:
            raise ReconError("身份合并需要 alias_person_id 与 canonical_person_id")
        if alias == canonical:
            raise ReconError("身份合并的双方不能相同")

    def _validate_cross_month(self, payload: dict) -> None:
        session_id = payload.get("session_id")
        known_sessions = {s.session_id for snap in self.snapshots for s in snap.sessions}
        if session_id not in known_sessions:
            raise ReconError(f"跨月分摊引用的场次 {session_id} 不存在", 404)
        allocations = payload.get("allocations")
        if not isinstance(allocations, dict) or not allocations:
            raise ReconError("跨月分摊需要 allocations（月份 -> 占比）")
        total = 0.0
        for key, value in allocations.items():
            try:
                parse_month(key)
            except ValueError as exc:
                raise ReconError(str(exc)) from exc
            if not isinstance(value, (int, float)) or value < 0:
                raise ReconError("分摊占比必须是非负数值")
            total += float(value)
        if abs(total - 1.0) > 1e-6:
            raise ReconError(f"分摊占比之和必须为 1，当前为 {total}")

    def _validate_report_correction(self, month: str, payload: dict) -> None:
        if not self._sealed_report(month):
            raise ReconError(f"{month} 尚未封账，无需报告更正，请直接调整来源数据", 409)
        if payload.get("venue_id") not in self.venues:
            raise ReconError(f"未知场馆：{payload.get('venue_id')}", 404)
        if payload.get("metric") not in METRICS:
            raise ReconError(f"未知指标：{payload.get('metric')}")
        if not isinstance(payload.get("delta"), (int, float)):
            raise ReconError("报告更正需要数值型 delta")

    def list_adjustments(self, month: str | None = None) -> list[AdjustmentEvent]:
        return [e for e in self.adjustments if month is None or e.month == month]

    def _adjustments_as_of(self, as_of: datetime | None) -> list[AdjustmentEvent]:
        if as_of is None:
            return list(self.adjustments)
        return [e for e in self.adjustments if datetime.fromisoformat(e.posted_at) <= as_of]

    # ---------- 报告与复算 ----------

    def _sealed_report(self, month: str) -> MonthlyReport | None:
        for report in self.reports.get(month, []):
            if report.sealed:
                return report
        return None

    def _build_report(
        self,
        month: str,
        caliber: CaliberVersion,
        computation: MonthComputation,
        sealed: bool,
        corrections: list[dict],
    ) -> MonthlyReport:
        venue_lines = {vid: computation.venue_totals(vid) for vid in sorted(computation.venues)}
        for correction in corrections:
            line = venue_lines.setdefault(
                correction["venue_id"], {"sessions": 0, "headcount": 0, "explanation_minutes": 0}
            )
            line[correction["metric"]] = num(line[correction["metric"]] + correction["delta"])
        if corrections:
            totals = {metric: num(sum(line[metric] for line in venue_lines.values())) for metric in METRICS}
        else:
            totals = computation.county_totals(caliber.headcount_scope)
        history = self.reports.setdefault(month, [])
        report = MonthlyReport(
            report_id=self._next_id("rpt"),
            month=month,
            revision=len(history) + 1,
            sealed=sealed,
            caliber_version=caliber.version,
            generated_at=self._now().isoformat(),
            venue_lines=venue_lines,
            totals=totals,
            corrections_applied=corrections,
            inputs_fingerprint=stable_hash(
                {
                    "month": month,
                    "caliber": caliber.to_dict(),
                    "snapshots": computation.snapshot_refs,
                    "adjustments": computation.adjustment_ids,
                    "corrections": corrections,
                }
            ),
            supersedes=history[-1].report_id if history else None,
        )
        history.append(report)
        self._computations[report.report_id] = computation
        return report

    def recalculate(self, month: str, caliber_version: str | None = None, as_of: str | None = None) -> MonthlyReport:
        """历史口径复算：用指定口径与截止时间重算，产出未封账的新修订版本。"""
        if month not in self.cycles and month not in self.reports:
            raise ReconError(f"{month} 尚未对账，无法复算", 404)
        cycle = self.cycles.get(month)
        default_version = cycle.caliber_version if cycle else None
        caliber = self._caliber(caliber_version or default_version)
        as_of_dt = datetime.fromisoformat(as_of) if as_of else None
        adjustments = self._adjustments_as_of(as_of_dt)
        computation = compute_month(month, self.snapshots, adjustments, caliber)
        corrections = [
            {
                "event_id": e.event_id,
                "venue_id": e.payload["venue_id"],
                "metric": e.payload["metric"],
                "delta": e.payload["delta"],
                "reason": e.reason,
            }
            for e in adjustments
            if e.type is AdjustmentType.REPORT_CORRECTION and e.month == month
        ]
        return self._build_report(month, caliber, computation, sealed=False, corrections=corrections)

    def get_report(self, month: str, revision: int | None = None) -> MonthlyReport:
        history = self.reports.get(month)
        if not history:
            raise ReconError(f"{month} 还没有任何报告", 404)
        if revision is not None:
            for report in history:
                if report.revision == revision:
                    return report
            raise ReconError(f"{month} 不存在修订版本 {revision}", 404)
        sealed = self._sealed_report(month)
        return sealed or history[-1]

    # ---------- 指标下钻 ----------

    def drilldown(
        self,
        month: str,
        metric: str,
        venue_id: str | None = None,
        revision: int | None = None,
    ) -> dict:
        if metric not in METRICS:
            raise ReconError(f"未知指标：{metric}")
        report = self.get_report(month, revision)
        computation = self._computations[report.report_id]
        venues: dict[str, dict] = {}
        for vid, comp in sorted(computation.venues.items()):
            if venue_id is not None and vid != venue_id:
                continue
            entries = comp.ledger(metric)
            venues[vid] = {
                "included": [e.to_dict() for e in entries if e.included],
                "excluded": [e.to_dict() for e in entries if not e.included],
                "merges": list(comp.merges) if metric == "headcount" else [],
                "totals": computation.venue_totals(vid),
            }
        return {
            "month": month,
            "metric": metric,
            "report_id": report.report_id,
            "revision": report.revision,
            "sealed": report.sealed,
            "caliber_version": report.caliber_version,
            "venues": venues,
        }
