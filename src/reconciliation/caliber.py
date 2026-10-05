"""统计口径版本：规则定义、默认值与校验。

口径规则键：
- dedupe_window_minutes：同一身份在同一活动内的签到去重窗口（分钟）
- late_grace_minutes：活动结束后仍计入的迟到宽限（分钟）
- cross_month_rule：跨月分摊规则，by_start=计入开始月，by_day_prorate=按天分摊
- service_person_mode：服务人数口径，unique_person=去重人数，checkin_count=签到人次
- duration_mode：讲解时长口径，actual=实际时长（缺省回退计划），planned=计划时长
"""
from __future__ import annotations

DEFAULT_RULES = {
    "dedupe_window_minutes": 30,
    "late_grace_minutes": 15,
    "cross_month_rule": "by_start",
    "service_person_mode": "unique_person",
    "duration_mode": "actual",
}

CROSS_MONTH_RULES = ("by_start", "by_day_prorate")
SERVICE_PERSON_MODES = ("unique_person", "checkin_count")
DURATION_MODES = ("actual", "planned")


def normalize_rules(rules: dict | None) -> dict:
    """合并默认值并校验口径规则，返回规范化后的完整规则。"""
    merged = {**DEFAULT_RULES, **(rules or {})}
    unknown = set(merged) - set(DEFAULT_RULES)
    if unknown:
        raise ValueError("未知口径规则：" + "、".join(sorted(unknown)))
    for key in ("dedupe_window_minutes", "late_grace_minutes"):
        value = merged[key]
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{key} 必须是非负数值")
        merged[key] = float(value)
    if merged["cross_month_rule"] not in CROSS_MONTH_RULES:
        raise ValueError(f"cross_month_rule 必须是 {'/'.join(CROSS_MONTH_RULES)}")
    if merged["service_person_mode"] not in SERVICE_PERSON_MODES:
        raise ValueError(f"service_person_mode 必须是 {'/'.join(SERVICE_PERSON_MODES)}")
    if merged["duration_mode"] not in DURATION_MODES:
        raise ValueError(f"duration_mode 必须是 {'/'.join(DURATION_MODES)}")
    return merged
