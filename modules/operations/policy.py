# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Inspection admission windows and bounded per-task query settings."""


import re
from datetime import datetime
from zoneinfo import ZoneInfo


def normalize_policy(data):
    result = {}
    for key, default, lower, upper in (
        ("cluster_concurrency", data.get("concurrency", 1), 1, 8),
        ("query_timeout", 15, 1, 15),
        ("row_limit", 1000, 1, 1000),
    ):
        value = data.get(key, default)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not lower <= value <= upper
        ):
            raise ValueError(f"{key} 须为{lower}到{upper}的整数")
        result[key] = value
    window = data.get("execution_window") or {}
    if not isinstance(window, dict) or set(window) - {"start", "end", "weekdays"}:
        raise ValueError("运行时段格式无效")
    if window:
        for key in ("start", "end"):
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(window.get(key, ""))):
                raise ValueError("运行时段请填写HH:mm")
        days = window.get("weekdays", list(range(7)))
        if (
            not isinstance(days, list)
            or not days
            or any(type(day) is not int or day not in range(7) for day in days)
        ):
            raise ValueError("运行日期须选择周一到周日")
        window = {
            "start": window["start"],
            "end": window["end"],
            "weekdays": sorted(set(days)),
        }
    result["execution_window"] = window
    return result


def window_open(window, now=None):
    if not window:
        return True
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    clock = now.strftime("%H:%M")
    start, end = window["start"], window["end"]
    day = now.weekday()
    if start == end:
        return day in window["weekdays"]
    if start < end:
        return day in window["weekdays"] and start <= clock < end
    if clock >= start:
        return day in window["weekdays"]
    return clock < end and (day - 1) % 7 in window["weekdays"]
