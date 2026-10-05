"""SQLite 持久化层：来源数据、快照、口径、调整事件、对账批次与月报。

月报一旦生成即为封账状态，只允许新增修订版（revision 递增），
旧版本置为 superseded 保留审计轨迹，绝不原地改写。
"""
from __future__ import annotations

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    venue_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    title TEXT,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    replaces_id TEXT,
    planned_minutes REAL,
    actual_minutes REAL
);
CREATE TABLE IF NOT EXISTS check_ins (
    check_in_id TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL,
    person_key TEXT NOT NULL,
    checked_in_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venue_reports (
    venue_id TEXT NOT NULL,
    month TEXT NOT NULL,
    sessions REAL NOT NULL,
    service_persons REAL NOT NULL,
    lecture_minutes REAL NOT NULL,
    PRIMARY KEY (venue_id, month)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibers (
    version TEXT PRIMARY KEY,
    rules TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    period TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT,
    actor TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    caliber_version TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    adjustment_ids TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS run_results (
    run_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    included TEXT NOT NULL,
    excluded TEXT NOT NULL,
    PRIMARY KEY (run_id, venue_id, metric)
);
CREATE TABLE IF NOT EXISTS differences (
    difference_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    county_value REAL,
    venue_value REAL,
    status TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    report_id TEXT PRIMARY KEY,
    venue_id TEXT NOT NULL,
    month TEXT NOT NULL,
    revision INTEGER NOT NULL,
    caliber_version TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL,
    sessions REAL NOT NULL,
    service_persons REAL NOT NULL,
    lecture_minutes REAL NOT NULL,
    lineage TEXT NOT NULL,
    corrects_id TEXT,
    sealed_at TEXT NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 存储。"""

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 基础读写 ----

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self._execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self._execute(sql, params).fetchall()]

    # ---- 来源数据 ----

    def add_venue(self, venue_id: str, name: str) -> None:
        self._execute("INSERT INTO venues (venue_id, name) VALUES (?, ?)", (venue_id, name))

    def get_venue(self, venue_id: str) -> dict | None:
        return self._one("SELECT * FROM venues WHERE venue_id = ?", (venue_id,))

    def add_activity(self, activity: dict) -> None:
        self._execute(
            "INSERT INTO activities (activity_id, venue_id, title, start_at, end_at,"
            " status, replaces_id, planned_minutes, actual_minutes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (activity["activity_id"], activity["venue_id"], activity.get("title"),
             activity["start_at"], activity["end_at"], activity["status"],
             activity.get("replaces_id"), activity.get("planned_minutes"),
             activity.get("actual_minutes")),
        )

    def find_activity(self, activity_id: str) -> dict | None:
        return self._one("SELECT * FROM activities WHERE activity_id = ?", (activity_id,))

    def list_activities(self) -> list[dict]:
        return self._all("SELECT * FROM activities ORDER BY activity_id")

    def add_check_in(self, check_in: dict) -> None:
        self._execute(
            "INSERT INTO check_ins (check_in_id, activity_id, person_key, checked_in_at)"
            " VALUES (?, ?, ?, ?)",
            (check_in["check_in_id"], check_in["activity_id"],
             check_in["person_key"], check_in["checked_in_at"]),
        )

    def find_check_in(self, check_in_id: str) -> dict | None:
        return self._one("SELECT * FROM check_ins WHERE check_in_id = ?", (check_in_id,))

    def list_check_ins(self) -> list[dict]:
        return self._all("SELECT * FROM check_ins ORDER BY check_in_id")

    def upsert_venue_report(self, report: dict) -> None:
        self._execute(
            "INSERT INTO venue_reports (venue_id, month, sessions, service_persons, lecture_minutes)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (venue_id, month) DO UPDATE SET"
            " sessions = excluded.sessions, service_persons = excluded.service_persons,"
            " lecture_minutes = excluded.lecture_minutes",
            (report["venue_id"], report["month"], report["sessions"],
             report["service_persons"], report["lecture_minutes"]),
        )

    def list_venue_reports(self) -> list[dict]:
        return self._all("SELECT * FROM venue_reports ORDER BY venue_id, month")

    # ---- 来源快照 ----

    def insert_snapshot(self, snapshot_id: str, created_at: str, payload_hash: str, payload: dict) -> None:
        self._execute(
            "INSERT INTO snapshots (snapshot_id, created_at, payload_hash, payload) VALUES (?, ?, ?, ?)",
            (snapshot_id, created_at, payload_hash, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )

    def _decode_snapshot(self, row: dict | None) -> dict | None:
        if row is not None:
            row["payload"] = json.loads(row["payload"])
        return row

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        return self._decode_snapshot(self._one("SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)))

    def latest_snapshot(self) -> dict | None:
        return self._decode_snapshot(self._one("SELECT * FROM snapshots ORDER BY created_at DESC, snapshot_id DESC LIMIT 1"))

    # ---- 统计口径版本 ----

    def insert_caliber(self, version: str, rules: dict, created_at: str) -> None:
        self._execute(
            "INSERT INTO calibers (version, rules, created_at) VALUES (?, ?, ?)",
            (version, json.dumps(rules, ensure_ascii=False, sort_keys=True), created_at),
        )

    def get_caliber(self, version: str) -> dict | None:
        row = self._one("SELECT * FROM calibers WHERE version = ?", (version,))
        if row is not None:
            row["rules"] = json.loads(row["rules"])
        return row

    def list_calibers(self) -> list[dict]:
        rows = self._all("SELECT * FROM calibers ORDER BY version")
        for row in rows:
            row["rules"] = json.loads(row["rules"])
        return rows

    # ---- 调整事件 ----

    def insert_adjustment(self, adjustment: dict) -> None:
        self._execute(
            "INSERT INTO adjustments (adjustment_id, type, period, payload, reason, actor, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (adjustment["adjustment_id"], adjustment["type"], adjustment["period"],
             json.dumps(adjustment["payload"], ensure_ascii=False, sort_keys=True),
             adjustment.get("reason", ""), adjustment.get("actor", ""),
             adjustment["status"], adjustment["created_at"]),
        )

    def list_adjustments(self, period: str | None = None) -> list[dict]:
        if period is None:
            rows = self._all("SELECT * FROM adjustments ORDER BY created_at, adjustment_id")
        else:
            rows = self._all(
                "SELECT * FROM adjustments WHERE period = ? ORDER BY created_at, adjustment_id", (period,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    # ---- 对账批次 ----

    def insert_run(self, run: dict) -> None:
        self._execute(
            "INSERT INTO runs (run_id, month, caliber_version, snapshot_id, adjustment_ids, status, created_at, closed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run["run_id"], run["month"], run["caliber_version"], run["snapshot_id"],
             json.dumps(run["adjustment_ids"]), run["status"], run["created_at"], run.get("closed_at")),
        )

    def get_run(self, run_id: str) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if row is not None:
            row["adjustment_ids"] = json.loads(row["adjustment_ids"])
        return row

    def set_run_closed(self, run_id: str, closed_at: str) -> None:
        self._execute("UPDATE runs SET status = 'closed', closed_at = ? WHERE run_id = ?", (closed_at, run_id))

    def insert_run_result(self, run_id: str, venue_id: str, metric: str,
                          value: float, included: list, excluded: list) -> None:
        self._execute(
            "INSERT INTO run_results (run_id, venue_id, metric, value, included, excluded)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, venue_id, metric, value,
             json.dumps(included, ensure_ascii=False), json.dumps(excluded, ensure_ascii=False)),
        )

    def list_run_results(self, run_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM run_results WHERE run_id = ? ORDER BY venue_id, metric", (run_id,))
        for row in rows:
            row["included"] = json.loads(row["included"])
            row["excluded"] = json.loads(row["excluded"])
        return rows

    # ---- 差异清单 ----

    def insert_difference(self, difference: dict) -> None:
        self._execute(
            "INSERT INTO differences (difference_id, run_id, venue_id, metric,"
            " county_value, venue_value, status, confirmed_by, confirmed_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (difference["difference_id"], difference["run_id"], difference["venue_id"],
             difference["metric"], difference.get("county_value"), difference.get("venue_value"),
             difference["status"], difference.get("confirmed_by"),
             difference.get("confirmed_at"), difference.get("note")),
        )

    def get_difference(self, difference_id: str) -> dict | None:
        return self._one("SELECT * FROM differences WHERE difference_id = ?", (difference_id,))

    def list_differences(self, run_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM differences WHERE run_id = ? ORDER BY venue_id, metric", (run_id,))

    def confirm_difference(self, difference_id: str, actor: str, note: str, confirmed_at: str) -> None:
        self._execute(
            "UPDATE differences SET status = 'confirmed', confirmed_by = ?, confirmed_at = ?, note = ?"
            " WHERE difference_id = ?",
            (actor, confirmed_at, note, difference_id),
        )

    def pending_difference_count(self, run_id: str) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM differences WHERE run_id = ? AND status = 'pending'", (run_id,))
        return int(row["n"])

    # ---- 月报（封账后稳定，只允许新增修订版） ----

    def insert_report(self, report: dict) -> None:
        self._execute(
            "INSERT INTO reports (report_id, venue_id, month, revision, caliber_version, snapshot_id,"
            " run_id, status, sessions, service_persons, lecture_minutes, lineage, corrects_id, sealed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (report["report_id"], report["venue_id"], report["month"], report["revision"],
             report["caliber_version"], report["snapshot_id"], report.get("run_id"),
             report["status"], report["sessions"], report["service_persons"],
             report["lecture_minutes"], json.dumps(report["lineage"], ensure_ascii=False),
             report.get("corrects_id"), report["sealed_at"]),
        )

    def _decode_report(self, row: dict | None) -> dict | None:
        if row is not None:
            row["lineage"] = json.loads(row["lineage"])
        return row

    def get_report(self, report_id: str) -> dict | None:
        return self._decode_report(self._one("SELECT * FROM reports WHERE report_id = ?", (report_id,)))

    def effective_report(self, venue_id: str, month: str) -> dict | None:
        return self._decode_report(self._one(
            "SELECT * FROM reports WHERE venue_id = ? AND month = ? AND status = 'effective'",
            (venue_id, month)))

    def effective_reports_for_month(self, month: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM reports WHERE month = ? AND status = 'effective' ORDER BY venue_id", (month,))
        return [self._decode_report(row) for row in rows]

    def list_report_revisions(self, venue_id: str, month: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM reports WHERE venue_id = ? AND month = ? ORDER BY revision",
            (venue_id, month))
        return [self._decode_report(row) for row in rows]

    def supersede_report(self, report_id: str) -> None:
        self._execute("UPDATE reports SET status = 'superseded' WHERE report_id = ?", (report_id,))
