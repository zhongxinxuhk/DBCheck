# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""HTTP endpoints for system information and offline authorization."""
from flask import jsonify, request, session

from . import bp
from . import license as license_service


@bp.route("/status", methods=["GET"])
def system_status():
    return jsonify(license_service.get_license_info())


@bp.route("/activate", methods=["POST"])
def activate():
    if not session.get("user_id"):
        return jsonify({"ok": False, "error": "请先登录后再激活"}), 401

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    result = license_service.activate_license(
        code,
        actor=session.get("username") or "",
    )
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)
