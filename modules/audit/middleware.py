# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Flask 请求审计中间件：所有写操作自动留痕，业务代码零改动。"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Dict

from flask import g, request, session

from modules.audit import store

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SKIP_PREFIXES = ("/static/", "/assets/", "/favicon", "/api/audit/")
_SECRET_KEY = re.compile(
    r"(pass(word)?|pwd|token|secret|api[_-]?key|credential|authorization|signature|cookie)",
    re.IGNORECASE,
)
_MAX_TEXT = 2000


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "***"
    if isinstance(value, dict):
        return {
            key: ("***" if _SECRET_KEY.search(str(key)) else _redact(val, depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:_MAX_TEXT] + ("..." if len(value) > _MAX_TEXT else "")
    return value


def _snapshot() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        if request.is_json:
            body = request.get_json(silent=True)
            if body is not None:
                data["body"] = _redact(body)
        elif request.form:
            data["form"] = _redact(dict(request.form))
        if request.args:
            data["query"] = _redact(dict(request.args))
        if request.files:
            data["files"] = list(request.files.keys())
    except Exception:
        pass
    return data


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64]


def _actor() -> tuple:
    username = session.get("username")
    user_id = session.get("user_id")
    client = "web"
    if not username:
        key = request.headers.get("X-API-Key", "")
        if not key:
            key = request.headers.get("Authorization", "").replace("Bearer ", "")
        if key:
            username = "api:" + key[:8]
            client = "api"
    return (username or "anonymous"), (str(user_id) if user_id else ""), client


def register_middleware(app) -> None:
    @app.before_request
    def _audit_begin():
        g._audit_start = time.perf_counter()
        g._audit_request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]

    @app.after_request
    def _audit_finish(response):
        try:
            if request.method not in WRITE_METHODS:
                return response
            path = request.path or ""
            if path.startswith(SKIP_PREFIXES):
                return response
            actor, actor_id, client = _actor()
            detail = _snapshot()
            detail["duration_ms"] = round(
                (time.perf_counter() - getattr(g, "_audit_start", time.perf_counter())) * 1000, 1
            )
            store.append_event({
                "actor": actor,
                "actor_id": actor_id,
                "action": request.endpoint or f"{request.method} {path}",
                "method": request.method,
                "path": path[:500],
                "target": path[:300],
                "status_code": response.status_code,
                "result": "success" if response.status_code < 400 else "failure",
                "ip": _client_ip(),
                "ua": (request.headers.get("User-Agent") or "")[:200],
                "client": client,
                "request_id": getattr(g, "_audit_request_id", ""),
                "detail": json.dumps(detail, ensure_ascii=False)[:8000],
            })
        except Exception:
            # 审计失败不能影响业务响应；缺失会在 /api/audit/stats 的计数里体现
            pass
        return response
