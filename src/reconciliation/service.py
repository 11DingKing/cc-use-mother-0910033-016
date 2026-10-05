"""应用服务：来源数据、快照、口径、调整事件、差异对账、封账月报与复算。

流程约定：
1. 录入来源数据（场馆、活动、签到、场馆自报数）；
2. capture_snapshot 冻结来源快照（含 SHA-256 校验和），之后计算只读快照；
3. create_run 按口径版本计算县级数并与场馆自报数比对，生成差异清单；
4. 场馆逐条确认差异；全部确认后 close_run 封账，输出稳定月报；
5. 封账后的一切变更只通过调整事件产生新的月报修订版，旧版保留可查；
6. recalculate 用任一口径版本对历史月份复算，不改写已封账月报。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from .caliber import DEFAULT_RULES, normalize_rules
from .engine import compute_month, empty_result, parse_dt
from .models import (
    ACTIVITY_STATES,
    ADJUSTMENT_TYPES,
    METRIC_NAMES,
    METRICS,
    ConflictError,
    NotFoundError,
)
from .store import Store

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _check_month(month: str) -> None:
    if not isinstance(month, str) or not MONTH_RE.match(month):
        raise ValueError(f"月份格式应为 YYYY-MM：{month!r}")


class ReconciliationService:
    """月度活动统计对账应用服务。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.store = Store(db_path)
        if not self.store.list_calibers():
            self.register_caliber("v1", dict(DEFAULT_RULES))

    def close(self) -> None:
        self.store.close()

    # ---- 来源数据录入 ----

    def add_venue(self, venue_id: str, name: str) -> dict:
        try:
            self.store.add_venue(venue_id, name)
        except sqlite3.IntegrityError:
            raise ConflictError(f"场馆已存在：{venue_id}") from None
        return {"venue_id": venue_id, "name": name}

    def add_activity(self, activity_id: str, venue_id: str, title: str,
                     start_at: str, end_at: str, status: str,
                     replaces_id: str | None = None,
                     planned_minutes: float | None = None,
                     actual_minutes: float | None = None) -> dict:
        if self.store.get_venue(venue_id) is None:
            raise NotFoundError(f"场馆不存在：{venue_id}")
        if status not in ACTIVITY_STATES:
            raise ValueError(f"活动状态无效：{status}（可选：{'/'.join(ACTIVITY_STATES)}）")
        start, end = parse_dt(start_at), parse_dt(end_at)
        if end < start:
            raise ValueError("活动结束时间早于开始时间")
        if replaces_id is not None and self.store.find_activity(replaces_id) is None:
            raise NotFoundError(f"补办关联的原活动不存在：{replaces_id}")
        activity = {
            "activity_id": activity_id, "venue_id": venue_id, "title": title,
            "start_at": start_at, "end_at": end_at, "status": status,
            "replaces_id": replaces_id,
            "planned_minutes": planned_minutes, "actual_minutes": actual_minutes,
        }
        try:
            self.store.add_activity(activity)
        except sqlite3.IntegrityError:
            raise ConflictError(f"活动已存在：{activity_id}") from None
        return activity

    def add_check_in(self, check_in_id: str, activity_id: str,
                     person_key: str, checked_in_at: str) -> dict:
        if self.store.find_activity(activity_id) is None:
            raise NotFoundError(f"活动不存在：{activity_id}")
        parse_dt(checked_in_at)
        check_in = {
            "check_in_id": check_in_id, "activity_id": activity_id,
            "person_key": person_key, "checked_in_at": checked_in_at,
        }
        try:
            self.store.add_check_in(check_in)
        except sqlite3.IntegrityError:
            raise ConflictError(f"签到记录已存在：{check_in_id}") from None
        return check_in

    def submit_venue_report(self, venue_id: str, month: str, sessions: float,
                            service_persons: float, lecture_minutes: float) -> dict:
        """登记场馆自报月度数据（对账的比对基准，随快照冻结）。"""
        if self.store.get_venue(venue_id) is None:
            raise NotFoundError(f"场馆不存在：{venue_id}")
        _check_month(month)
        report = {
            "venue_id": venue_id, "month": month,
            "sessions": float(sessions), "service_persons": float(service_persons),
            "lecture_minutes": float(lecture_minutes),
        }
        for metric in METRICS:
            if report[metric] < 0:
                raise ValueError(f"{METRIC_NAMES[metric]}不能为负数")
        self.store.upsert_venue_report(report)
        return report

    # ---- 来源快照与统计口径 ----

    def capture_snapshot(self) -> dict:
        """冻结当前来源数据为不可变快照，返回快照号与校验和。"""
        payload = {
            "activities": self.store.list_activities(),
            "check_ins": self.store.list_check_ins(),
            "venue_reports": self.store.list_venue_reports(),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        snapshot_id = _new_id("snap")
        created_at = _now()
        self.store.insert_snapshot(snapshot_id, created_at, payload_hash, payload)
        return {
            "snapshot_id": snapshot_id, "created_at": created_at,
            "payload_hash": payload_hash,
            "activity_count": len(payload["activities"]),
            "check_in_count": len(payload["check_ins"]),
            "venue_report_count": len(payload["venue_reports"]),
        }

    def register_caliber(self, version: str, rules: dict | None = None) -> dict:
        normalized = normalize_rules(rules)
        try:
            self.store.insert_caliber(version, normalized, _now())
        except sqlite3.IntegrityError:
            raise ConflictError(f"统计口径版本已存在：{version}") from None
        return {"version": version, "rules": normalized}

    def list_calibers(self) -> list[dict]:
        return self.store.list_calibers()

    # ---- 调整事件 ----

    def record_adjustment(self, type: str, period: str, payload: dict,
                          reason: str = "", actor: str = "") -> dict:
        """登记调整事件。迟到签到/身份合并/跨月分摊影响后续计算；
        若该月已封账则自动复算并生成新月报修订版；已签报告更正直接出修订版。"""
        if type not in ADJUSTMENT_TYPES:
            raise ValueError(f"调整事件类型无效：{type}（可选：{'/'.join(ADJUSTMENT_TYPES)}）")
        _check_month(period)
        payload = self._validate_adjustment_payload(type, payload)
        if type == "report_correction":
            return self._apply_report_correction(period, payload, reason, actor)
        adjustment = {
            "adjustment_id": _new_id("adj"), "type": type, "period": period,
            "payload": payload, "reason": reason, "actor": actor,
            "status": "applied", "created_at": _now(),
        }
        self.store.insert_adjustment(adjustment)
        revised = self._recompute_sealed_period(period)
        return {"adjustment": adjustment, "revised_report_ids": revised}

    def list_adjustments(self, period: str | None = None) -> list[dict]:
        return self.store.list_adjustments(period=period)

    def _validate_adjustment_payload(self, type: str, payload: dict) -> dict:
        if type == "late_checkin":
            check_in_id = payload.get("check_in_id")
            if self.store.find_check_in(check_in_id) is None:
                raise NotFoundError(f"签到记录不存在：{check_in_id}")
            return {"check_in_id": check_in_id}
        if type == "identity_merge":
            source, target = payload.get("source"), payload.get("target")
            if not source or not target or source == target:
                raise ValueError("身份合并需要不同的 source 与 target")
            return {"source": source, "target": target}
        if type == "cross_month_override":
            activity_id = payload.get("activity_id")
            if self.store.find_activity(activity_id) is None:
                raise NotFoundError(f"活动不存在：{activity_id}")
            allocations = payload.get("allocations") or []
            if not allocations:
                raise ValueError("跨月分摊需要非空 allocations")
            total = 0.0
            for item in allocations:
                _check_month(item.get("month", ""))
                weight = float(item.get("weight", 0))
                if weight < 0:
                    raise ValueError("分摊权重不能为负")
                total += weight
            if abs(total - 1.0) > 1e-6:
                raise ValueError(f"分摊权重之和应为 1，实际为 {total}")
            return {"activity_id": activity_id, "allocations": allocations}
        # report_correction
        venue_id, metric = payload.get("venue_id"), payload.get("metric")
        if self.store.get_venue(venue_id) is None:
            raise NotFoundError(f"场馆不存在：{venue_id}")
        if metric not in METRICS:
            raise ValueError(f"未知指标：{metric}（可选：{'/'.join(METRICS)}）")
        has_delta = "delta" in payload
        has_set = "set_value" in payload
        if has_delta == has_set:
            raise ValueError("报告更正需且仅需提供 delta 或 set_value 之一")
        result = {"venue_id": venue_id, "metric": metric}
        if has_delta:
            result["delta"] = float(payload["delta"])
        else:
            result["set_value"] = float(payload["set_value"])
        return result

    # ---- 对账批次：差异清单 -> 场馆确认 -> 封账 ----

    def create_run(self, month: str, caliber_version: str, snapshot_id: str) -> dict:
        _check_month(month)
        snapshot = self.store.get_snapshot(snapshot_id)
        if snapshot is None:
            raise NotFoundError(f"来源快照不存在：{snapshot_id}")
        caliber = self.store.get_caliber(caliber_version)
        if caliber is None:
            raise NotFoundError(f"统计口径版本不存在：{caliber_version}")
        adjustments = [a for a in self.store.list_adjustments(period=month)
                       if a["status"] == "applied"]
        results = compute_month(snapshot["payload"], caliber["rules"], adjustments, month)

        run_id = _new_id("run")
        self.store.insert_run({
            "run_id": run_id, "month": month, "caliber_version": caliber_version,
            "snapshot_id": snapshot_id,
            "adjustment_ids": [a["adjustment_id"] for a in adjustments],
            "status": "open", "created_at": _now(), "closed_at": None,
        })

        reported = {vr["venue_id"]: vr for vr in snapshot["payload"].get("venue_reports", [])
                    if vr["month"] == month}
        for venue_id in sorted(set(results) | set(reported)):
            result = results.get(venue_id) or empty_result()
            for metric in METRICS:
                self.store.insert_run_result(
                    run_id, venue_id, metric, result[metric],
                    result["included"][metric], result["excluded"][metric])
            venue_report = reported.get(venue_id)
            for metric in METRICS:
                county_value = result[metric]
                venue_value = venue_report.get(metric) if venue_report else None
                if venue_value is None or round(float(venue_value), 2) != round(float(county_value), 2):
                    self.store.insert_difference({
                        "difference_id": _new_id("diff"), "run_id": run_id,
                        "venue_id": venue_id, "metric": metric,
                        "county_value": county_value, "venue_value": venue_value,
                        "status": "pending",
                    })
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if run is None:
            raise NotFoundError(f"对账批次不存在：{run_id}")
        results: dict[str, dict] = {}
        for row in self.store.list_run_results(run_id):
            results.setdefault(row["venue_id"], {})[row["metric"]] = {
                "value": row["value"], "included": row["included"], "excluded": row["excluded"],
            }
        return {**run, "results": results,
                "differences": self._decorate_differences(self.store.list_differences(run_id))}

    def list_differences(self, run_id: str) -> list[dict]:
        if self.store.get_run(run_id) is None:
            raise NotFoundError(f"对账批次不存在：{run_id}")
        return self._decorate_differences(self.store.list_differences(run_id))

    @staticmethod
    def _decorate_differences(differences: list[dict]) -> list[dict]:
        for diff in differences:
            diff["metric_name"] = METRIC_NAMES[diff["metric"]]
        return differences

    def confirm_difference(self, difference_id: str, actor: str, note: str = "") -> dict:
        diff = self.store.get_difference(difference_id)
        if diff is None:
            raise NotFoundError(f"差异记录不存在：{difference_id}")
        run = self.store.get_run(diff["run_id"])
        if run["status"] != "open":
            raise ConflictError("对账批次已封账，不能再确认差异")
        if diff["status"] != "pending":
            raise ConflictError(f"差异已确认：{difference_id}")
        self.store.confirm_difference(difference_id, actor, note, _now())
        return self.store.get_difference(difference_id)

    def close_run(self, run_id: str) -> dict:
        """全部差异确认后封账，为每个场馆输出稳定月报（修订版递增）。"""
        run = self.store.get_run(run_id)
        if run is None:
            raise NotFoundError(f"对账批次不存在：{run_id}")
        if run["status"] != "open":
            raise ConflictError("对账批次已封账")
        pending = self.store.pending_difference_count(run_id)
        if pending:
            raise ConflictError(f"尚有 {pending} 条差异未确认，不能封账")
        by_venue: dict[str, dict] = {}
        lineage: dict[str, dict] = {}
        for row in self.store.list_run_results(run_id):
            by_venue.setdefault(row["venue_id"], {})[row["metric"]] = row["value"]
            venue_lineage = lineage.setdefault(row["venue_id"], {"included": {}, "excluded": {}})
            venue_lineage["included"][row["metric"]] = row["included"]
            venue_lineage["excluded"][row["metric"]] = row["excluded"]
        report_ids = []
        for venue_id in sorted(by_venue):
            venue_lineage = lineage[venue_id]
            venue_lineage["adjustments"] = list(run["adjustment_ids"])
            venue_lineage["corrections"] = []
            report_ids.append(self._create_report_revision(
                venue_id, run["month"], by_venue[venue_id],
                run["caliber_version"], run["snapshot_id"], run_id, venue_lineage))
        self.store.set_run_closed(run_id, _now())
        return {"run_id": run_id, "status": "closed", "report_ids": report_ids}

    # ---- 月报与修订版 ----

    def _create_report_revision(self, venue_id: str, month: str, values: dict,
                                caliber_version: str, snapshot_id: str,
                                run_id: str | None, lineage: dict) -> str:
        previous = self.store.effective_report(venue_id, month)
        revision = 1 if previous is None else previous["revision"] + 1
        report_id = _new_id("rep")
        if previous is not None:
            self.store.supersede_report(previous["report_id"])
        self.store.insert_report({
            "report_id": report_id, "venue_id": venue_id, "month": month,
            "revision": revision, "caliber_version": caliber_version,
            "snapshot_id": snapshot_id, "run_id": run_id, "status": "effective",
            "sessions": float(values["sessions"]),
            "service_persons": int(values["service_persons"]),
            "lecture_minutes": float(values["lecture_minutes"]),
            "lineage": lineage,
            "corrects_id": None if previous is None else previous["report_id"],
            "sealed_at": _now(),
        })
        return report_id

    def get_report(self, venue_id: str, month: str) -> dict:
        """返回场馆某月当前生效月报及全部修订历史。"""
        _check_month(month)
        revisions = self.store.list_report_revisions(venue_id, month)
        if not revisions:
            raise NotFoundError(f"月报不存在：{venue_id} {month}")
        effective = next(r for r in revisions if r["status"] == "effective")
        return {"effective": effective, "revisions": revisions}

    def get_report_by_id(self, report_id: str) -> dict:
        report = self.store.get_report(report_id)
        if report is None:
            raise NotFoundError(f"月报不存在：{report_id}")
        return report

    def report_drilldown(self, report_id: str, metric: str) -> dict:
        """从任一指标下钻到包含与排除记录（含排除原因与调整轨迹）。"""
        if metric not in METRICS:
            raise ValueError(f"未知指标：{metric}（可选：{'/'.join(METRICS)}）")
        report = self.get_report_by_id(report_id)
        lineage = report["lineage"]
        return {
            "report_id": report_id, "venue_id": report["venue_id"], "month": report["month"],
            "metric": metric, "metric_name": METRIC_NAMES[metric], "value": report[metric],
            "included": lineage.get("included", {}).get(metric, []),
            "excluded": lineage.get("excluded", {}).get(metric, []),
            "adjustments": lineage.get("adjustments", []),
            "corrections": lineage.get("corrections", []),
        }

    # ---- 调整事件的后续处理 ----

    def _recompute_sealed_period(self, period: str) -> list[str]:
        """月份已封账时，用同一快照与口径加全部调整事件复算，出修订版。"""
        revised = []
        for report in self.store.effective_reports_for_month(period):
            snapshot = self.store.get_snapshot(report["snapshot_id"])
            caliber = self.store.get_caliber(report["caliber_version"])
            adjustments = [a for a in self.store.list_adjustments(period=period)
                           if a["status"] == "applied"]
            results = compute_month(snapshot["payload"], caliber["rules"], adjustments, period)
            result = results.get(report["venue_id"]) or empty_result()
            changed = any(round(float(result[m]), 2) != round(float(report[m]), 2)
                          for m in METRICS)
            if not changed:
                continue
            lineage = {
                "included": result["included"], "excluded": result["excluded"],
                "adjustments": [a["adjustment_id"] for a in adjustments],
                "corrections": list(report["lineage"].get("corrections", [])),
            }
            revised.append(self._create_report_revision(
                report["venue_id"], period, result,
                report["caliber_version"], report["snapshot_id"],
                report["run_id"], lineage))
        return revised

    def _apply_report_correction(self, period: str, payload: dict,
                                 reason: str, actor: str) -> dict:
        """已签报告更正：以调整事件为载体生成新修订版，原版本保留。"""
        report = self.store.effective_report(payload["venue_id"], period)
        if report is None:
            raise ConflictError("该月报告尚未封账，请通过对账批次修正，而非报告更正")
        adjustment = {
            "adjustment_id": _new_id("adj"), "type": "report_correction",
            "period": period, "payload": payload, "reason": reason, "actor": actor,
            "status": "applied", "created_at": _now(),
        }
        self.store.insert_adjustment(adjustment)
        values = {metric: report[metric] for metric in METRICS}
        metric = payload["metric"]
        if "set_value" in payload:
            values[metric] = payload["set_value"]
        else:
            values[metric] = float(report[metric]) + payload["delta"]
        if metric == "service_persons":
            values[metric] = int(values[metric])
        lineage = copy.deepcopy(report["lineage"])
        lineage.setdefault("adjustments", []).append(adjustment["adjustment_id"])
        lineage.setdefault("corrections", []).append({
            "adjustment_id": adjustment["adjustment_id"], "metric": metric,
            "delta": payload.get("delta"), "set_value": payload.get("set_value"),
            "reason": reason, "actor": actor,
        })
        report_id = self._create_report_revision(
            report["venue_id"], period, values,
            report["caliber_version"], report["snapshot_id"],
            report["run_id"], lineage)
        return {"adjustment": adjustment, "revised_report_ids": [report_id]}

    # ---- 历史口径复算 ----

    def recalculate(self, month: str, caliber_version: str,
                    snapshot_id: str | None = None) -> dict:
        """用任一口径版本复算历史月份，与当前生效月报对比；不改写任何月报。"""
        _check_month(month)
        caliber = self.store.get_caliber(caliber_version)
        if caliber is None:
            raise NotFoundError(f"统计口径版本不存在：{caliber_version}")
        if snapshot_id is None:
            effective = self.store.effective_reports_for_month(month)
            if effective:
                snapshot_id = effective[0]["snapshot_id"]
            else:
                latest = self.store.latest_snapshot()
                if latest is None:
                    raise NotFoundError("没有可用的来源快照")
                snapshot_id = latest["snapshot_id"]
        snapshot = self.store.get_snapshot(snapshot_id)
        if snapshot is None:
            raise NotFoundError(f"来源快照不存在：{snapshot_id}")
        adjustments = [a for a in self.store.list_adjustments(period=month)
                       if a["status"] == "applied"]
        results = compute_month(snapshot["payload"], caliber["rules"], adjustments, month)

        current = {r["venue_id"]: r for r in self.store.effective_reports_for_month(month)}
        venues = {}
        for venue_id in sorted(set(results) | set(current)):
            result = results.get(venue_id) or empty_result()
            report = current.get(venue_id)
            entry = {
                "recalculated": {metric: result[metric] for metric in METRICS},
                "current_effective": None,
                "delta": None,
                "included": result["included"],
                "excluded": result["excluded"],
            }
            if report is not None:
                entry["current_effective"] = {metric: report[metric] for metric in METRICS}
                entry["delta"] = {
                    metric: round(float(result[metric]) - float(report[metric]), 2)
                    for metric in METRICS
                }
            venues[venue_id] = entry
        return {
            "month": month, "caliber_version": caliber_version,
            "snapshot_id": snapshot_id,
            "adjustment_ids": [a["adjustment_id"] for a in adjustments],
            "venues": venues,
        }
