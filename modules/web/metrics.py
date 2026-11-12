# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Prometheus 指标导出（/metrics）。

- 文本格式 0.0.4，无第三方依赖。
- 默认只允许本机抓取、已登录会话或有效 API Key；设置
  ``DBPULSE_METRICS_PUBLIC=1`` 可对所有来源开放（仅建议内网使用）。
- 单个采集器失败不会影响其他指标：失败计入
  ``dbpulse_metrics_collect_errors_total{collector="..."}``。
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, List, Tuple

from flask import Response, request, session

_START_TS = time.time()
_CHAIN_CACHE: Dict[str, object] = {"ts": 0.0, "result": None}
_CHAIN_TTL = 60.0


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _header(name: str, help_text: str, mtype: str = "gauge") -> List[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}"]


def _collect_app() -> List[str]:
    version = "unknown"
    try:
        from modules.core import paths

        payload = json.loads((paths.CONFIG_DIR / "version.json").read_text(encoding="utf-8"))
        version = str(payload.get("version", "unknown"))
    except Exception:
        pass
    return (
        _header("dbpulse_up", "DBPulse 进程存活")
        + ["dbpulse_up 1"]
        + _header("dbpulse_info", "构建信息")
        + [f'dbpulse_info{{version="{_escape(version)}"}} 1']
        + _header("dbpulse_uptime_seconds", "进程运行时长（秒）")
        + [f"dbpulse_uptime_seconds {round(time.time() - _START_TS, 1)}"]
    )


def _collect_instances() -> List[str]:
    from modules.pro.instance_manager import InstanceManager

    instances = InstanceManager().get_all_instances(mask_password=True) or []
    counts: Dict[str, int] = {}
    for item in instances:
        key = (item.get("db_type") or "unknown").lower()
        counts[key] = counts.get(key, 0) + 1
    lines = _header("dbpulse_instances_total", "数据源实例数（按类型）")
    if counts:
        for db_type, count in sorted(counts.items()):
            lines.append(f'dbpulse_instances_total{{db_type="{_escape(db_type)}"}} {count}')
    else:
        lines.append("dbpulse_instances_total 0")
    return lines


def _collect_monitor() -> List[str]:
    from modules.monitor.engine import get_monitor_engine

    engine = get_monitor_engine()
    status = engine.get_status() if engine else {}
    running = 1 if status.get("running") else 0
    last = float(status.get("last_collect_ts") or 0)
    conns = engine.get_connections() if engine else {}
    total = 0
    for item in (conns or {}).values():
        try:
            total += int(item.get("total") or 0)
        except (TypeError, ValueError):
            pass
    return (
        _header("dbpulse_monitor_running", "监控采集线程是否运行")
        + [f"dbpulse_monitor_running {running}"]
        + _header("dbpulse_monitor_last_collect_timestamp", "最后一次采集的 Unix 时间戳")
        + [f"dbpulse_monitor_last_collect_timestamp {last}"]
        + _header("dbpulse_monitor_connections_total", "监控中的当前连接数合计")
        + [f"dbpulse_monitor_connections_total {total}"]
    )


def _collect_backup() -> List[str]:
    from modules.disaster_recovery import engine

    health = engine.get_health_metrics()
    stats = health.get("stats") or {}
    return (
        _header("dbpulse_backup_health_score", "备份健康度评分（0-100）")
        + [f"dbpulse_backup_health_score {health.get('score', 0)}"]
        + _header("dbpulse_backup_success_rate", "备份成功率（百分比）")
        + [f"dbpulse_backup_success_rate {stats.get('success_rate', 0)}"]
        + _header("dbpulse_backups_total", "备份运行总次数")
        + [f"dbpulse_backups_total {stats.get('total_runs', 0)}"]
    )


def _collect_restore() -> List[str]:
    from modules.disaster_recovery import verify

    summary = verify.get_summary()
    runs = verify.list_runs(limit=200)
    counts: Dict[str, int] = {}
    for run in runs:
        key = run.get("status") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    lines = (
        _header("dbpulse_restore_verified", "最近一次备份是否已通过恢复演练")
        + [f"dbpulse_restore_verified {1 if summary.get('verified') else 0}"]
        + _header("dbpulse_restore_pass_rate", "最近 20 次恢复演练通过率（百分比）")
        + [f"dbpulse_restore_pass_rate {summary.get('pass_rate', 0)}"]
    )
    lines += _header("dbpulse_restore_runs_total", "恢复演练次数（按结果）", "counter")
    for status, count in sorted(counts.items()):
        lines.append(f'dbpulse_restore_runs_total{{status="{_escape(status)}"}} {count}')
    return lines


def _collect_audit() -> List[str]:
    from modules.audit import store

    stats = store.stats()
    now = time.time()
    if now - float(_CHAIN_CACHE["ts"] or 0) > _CHAIN_TTL or _CHAIN_CACHE["result"] is None:
        try:
            _CHAIN_CACHE["result"] = store.verify_chain(limit=20000)
        except Exception:
            _CHAIN_CACHE["result"] = {"ok": False}
        _CHAIN_CACHE["ts"] = now
    chain_ok = bool((_CHAIN_CACHE["result"] or {}).get("ok"))
    return (
        _header("dbpulse_audit_events_total", "审计事件总数", "counter")
        + [f"dbpulse_audit_events_total {stats.get('total', 0)}"]
        + _header("dbpulse_audit_events_24h", "最近 24 小时审计事件数")
        + [f"dbpulse_audit_events_24h {stats.get('last_24h', 0)}"]
        + _header("dbpulse_audit_chain_ok", "审计哈希链完整性（1=完整）")
        + [f"dbpulse_audit_chain_ok {1 if chain_ok else 0}"]
    )


def _collect_keys() -> List[str]:
    from modules.web import api

    conn = api._get_key_db()
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM api_keys WHERE is_active=1"
        ).fetchone()["n"]
    finally:
        conn.close()
    return (
        _header("dbpulse_api_keys_active", "启用中的 API Token 数量")
        + [f"dbpulse_api_keys_active {active}"]
    )


_COLLECTORS: Tuple[Tuple[str, Callable[[], List[str]]], ...] = (
    ("app", _collect_app),
    ("instances", _collect_instances),
    ("monitor", _collect_monitor),
    ("backup", _collect_backup),
    ("restore", _collect_restore),
    ("audit", _collect_audit),
    ("api_keys", _collect_keys),
)


def _authorized() -> bool:
    if os.environ.get("DBPULSE_METRICS_PUBLIC") == "1":
        return True
    if (request.remote_addr or "") in ("127.0.0.1", "::1"):
        return True
    if session.get("user_id"):
        return True
    key = request.headers.get("X-API-Key", "")
    if not key:
        key = request.headers.get("Authorization", "").replace("Bearer ", "")
    if key:
        try:
            from modules.web import api

            ok, _ = api._verify_api_key(key)
            return bool(ok)
        except Exception:
            return False
    return False


def render_metrics() -> str:
    lines: List[str] = []
    errors = 0
    for name, collector in _COLLECTORS:
        try:
            lines.extend(collector())
        except Exception:
            errors += 1
            lines.extend(_header(
                "dbpulse_metrics_collect_errors_total",
                "指标采集失败次数（按采集器）",
                "counter",
            ))
            lines.append(f'dbpulse_metrics_collect_errors_total{{collector="{name}"}} 1')
    lines.extend(_header("dbpulse_metrics_collect_errors_total", "指标采集失败次数（按采集器）", "counter"))
    if not errors:
        lines.append('dbpulse_metrics_collect_errors_total{collector="none"} 0')
    return "\n".join(lines) + "\n"


def register_metrics(app) -> None:
    if getattr(app, "_metrics_registered", False):
        return

    @app.route("/metrics", methods=["GET"])
    def prometheus_metrics():
        if not _authorized():
            return Response("unauthorized\n", status=401, mimetype="text/plain")
        return Response(
            render_metrics(),
            mimetype="text/plain; version=0.0.4; charset=utf-8",
        )

    app._metrics_registered = True
