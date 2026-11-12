# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""统一审计查询接口（/api/audit）。"""

from __future__ import annotations

import csv
import io
from functools import wraps

from flask import Blueprint, Response, jsonify, request, session

from modules.audit import store

bp_audit = Blueprint("audit", __name__, url_prefix="/api/audit")


def _login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录", "error_code": "NOT_LOGGED_IN"}), 401
        return f(*args, **kwargs)
    return wrapper


def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录", "error_code": "NOT_LOGGED_IN"}), 401
        if not (session.get("is_admin") or session.get("role") == "admin"):
            return jsonify({"ok": False, "error": "需要管理员权限", "error_code": "FORBIDDEN"}), 403
        return f(*args, **kwargs)
    return wrapper


def _filters() -> dict:
    return {
        key: request.args.get(key, "").strip()
        for key in ("actor", "action", "path", "method", "result", "client", "since", "until")
        if request.args.get(key, "").strip()
    }


@bp_audit.route("/events", methods=["GET"])
@_login_required
def list_events():
    try:
        limit = int(request.args.get("limit", 100))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"ok": False, "error": "limit/offset 必须为整数"}), 400
    data = store.query_events(_filters(), limit=limit, offset=offset)
    return jsonify({"ok": True, **data})


@bp_audit.route("/stats", methods=["GET"])
@_login_required
def audit_stats():
    return jsonify({"ok": True, "stats": store.stats()})


@bp_audit.route("/integrity", methods=["GET"])
@_admin_required
def audit_integrity():
    return jsonify({"ok": True, "integrity": store.verify_chain()})


@bp_audit.route("/export", methods=["GET"])
@_login_required
def export_events():
    rows = store.iter_events(_filters(), limit=int(request.args.get("limit", 10000)))
    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out)
    writer.writerow(["id", "时间", "操作者", "动作", "方法", "路径", "状态码",
                     "结果", "IP", "客户端", "Request-ID", "详情"])
    for row in rows:
        writer.writerow([
            row.get("id"), row.get("ts"), row.get("actor"), row.get("action"),
            row.get("method"), row.get("path"), row.get("status_code"),
            row.get("result"), row.get("ip"), row.get("client"),
            row.get("request_id"), row.get("detail"),
        ])
    return Response(
        out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_events.csv"},
    )
