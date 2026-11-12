# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""统一审计：HTTP 写操作自动留痕 + 领域事件显式记录。

用法::

    from modules.audit import record
    record("dr.verify.run", target=plan_name, detail={"status": "passed"})

HTTP 侧由 :func:`register_audit` 注册蓝图与中间件，业务代码无需改动。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from modules.audit import store

__all__ = ["record", "register_audit", "store"]


def record(action: str, target: str = "", detail: Optional[Any] = None,
           actor: str = "system", client: str = "system",
           result: str = "success", **extra: Any) -> Dict[str, Any]:
    """记录一条领域事件（调度器、MCP、后台任务等非 HTTP 路径）。"""
    payload: Dict[str, Any] = {
        "actor": actor,
        "actor_id": extra.get("actor_id", ""),
        "action": action,
        "method": extra.get("method", ""),
        "path": extra.get("path", ""),
        "target": target,
        "status_code": extra.get("status_code"),
        "result": result,
        "ip": extra.get("ip", ""),
        "ua": extra.get("ua", ""),
        "client": client,
        "request_id": extra.get("request_id", ""),
        "detail": detail if isinstance(detail, str) else json.dumps(detail or {}, ensure_ascii=False),
    }
    return store.append_event(payload)


def register_audit(app) -> None:
    """注册 /api/audit 蓝图并安装请求审计中间件（幂等）。"""
    if getattr(app, "_audit_registered", False):
        return
    store.connect()
    from modules.audit.middleware import register_middleware
    from modules.audit.routes import bp_audit

    app.register_blueprint(bp_audit)
    register_middleware(app)
    app._audit_registered = True
