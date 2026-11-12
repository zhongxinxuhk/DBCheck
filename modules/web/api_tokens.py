# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""API Token 管理：范围（scope）、过期、轮换与会话态管理接口。

与 ``modules/web/api.py`` 共用 ``api_keys.db``：老 Key 的 ``scopes`` 为空时按
``read,write`` 处理，``expires_at`` 为空表示永不过期，行为向后兼容。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request, session

VALID_SCOPES = ("read", "write")
bp_tokens = Blueprint("api_tokens", __name__, url_prefix="/api/tokens")


def _api():
    from modules.web import api

    return api


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_columns(conn) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(api_keys)")}
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN expires_at TEXT")
    if "scopes" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN scopes TEXT DEFAULT 'read,write'")
    conn.commit()


def _db():
    conn = _api()._get_key_db()
    _ensure_columns(conn)
    return conn


def _normalize_scopes(scopes: Any) -> str:
    if not scopes:
        return "read,write"
    if isinstance(scopes, str):
        items = [s.strip() for s in scopes.split(",") if s.strip()]
    else:
        items = [str(s).strip() for s in scopes]
    items = [s for s in items if s in VALID_SCOPES]
    if "write" in items and "read" not in items:
        items.append("read")
    return ",".join(dict.fromkeys(items)) or "read"


def create_token(name: str, scopes: Any = None, expires_days: int = 0,
                 user_id: Optional[int] = None) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("Token 名称不能为空")
    raw = "dk-" + secrets.token_hex(24)
    expires_at = ""
    if int(expires_days or 0) > 0:
        expires_at = (datetime.now() + timedelta(days=int(expires_days))).isoformat(timespec="seconds")
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO api_keys (id, name, key_hash, key_prefix, created_at, user_id, expires_at, scopes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                secrets.token_hex(8), name, _hash(raw), raw[:10],
                datetime.now().isoformat(timespec="seconds"), user_id, expires_at,
                _normalize_scopes(scopes),
            ),
        )
        conn.commit()
        return {"name": name, "token": raw, "expires_at": expires_at,
                "scopes": _normalize_scopes(scopes)}
    finally:
        conn.close()


def list_tokens() -> List[Dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, name, key_prefix, created_at, last_used_at, is_active, "
            "user_id, expires_at, scopes FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def revoke_token(token_id: str) -> bool:
    conn = _db()
    try:
        cur = conn.execute("UPDATE api_keys SET is_active=0 WHERE id=?", (token_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def rotate_token(token_id: str) -> Dict[str, Any]:
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM api_keys WHERE id=?", (token_id,)).fetchone()
        if not row:
            raise ValueError("Token 不存在")
        raw = "dk-" + secrets.token_hex(24)
        conn.execute(
            "UPDATE api_keys SET key_hash=?, key_prefix=?, is_active=1, last_used_at=NULL WHERE id=?",
            (_hash(raw), raw[:10], token_id),
        )
        conn.commit()
        return {"id": token_id, "name": row["name"], "token": raw}
    finally:
        conn.close()


def verify_token(raw: str, required_scope: Optional[str] = None) -> Tuple[bool, str]:
    if not raw:
        return False, "missing"
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE key_hash=? AND is_active=1", (_hash(raw),)
        ).fetchone()
        if not row:
            return False, "invalid"
        expires_at = row["expires_at"] if "expires_at" in row.keys() else None
        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) <= datetime.now():
                    return False, "expired"
            except ValueError:
                pass
        scopes = (row["scopes"] if "scopes" in row.keys() else "") or "read,write"
        granted = {s.strip() for s in scopes.split(",") if s.strip()}
        if required_scope and required_scope not in granted:
            if not (required_scope == "read" and "write" in granted):
                return False, "scope"
        conn.execute(
            "UPDATE api_keys SET last_used_at=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), row["id"]),
        )
        conn.commit()
        return True, "ok"
    finally:
        conn.close()


# ── 会话态管理接口（管理员）───────────────────────────────────

def _admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "未登录", "error_code": "NOT_LOGGED_IN"}), 401
        if not (session.get("is_admin") or session.get("role") == "admin"):
            return jsonify({"ok": False, "error": "需要管理员权限", "error_code": "FORBIDDEN"}), 403
        return f(*args, **kwargs)
    return wrapper


@bp_tokens.route("", methods=["GET"])
@_admin_required
def tokens_list():
    return jsonify({"ok": True, "tokens": list_tokens()})


@bp_tokens.route("", methods=["POST"])
@_admin_required
def tokens_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        created = create_token(
            data.get("name", ""),
            data.get("scopes"),
            int(data.get("expires_days") or 0),
            session.get("user_id"),
        )
        return jsonify({"ok": True, **created})
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp_tokens.route("/<token_id>", methods=["DELETE"])
@_admin_required
def tokens_revoke(token_id):
    if not revoke_token(token_id):
        return jsonify({"ok": False, "error": "Token 不存在"}), 404
    return jsonify({"ok": True})


@bp_tokens.route("/<token_id>/rotate", methods=["POST"])
@_admin_required
def tokens_rotate(token_id):
    try:
        return jsonify({"ok": True, **rotate_token(token_id)})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404


def register_token_routes(app) -> None:
    if getattr(app, "_tokens_registered", False):
        return
    app.register_blueprint(bp_tokens)
    app._tokens_registered = True
