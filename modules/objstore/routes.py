# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""对象存储备份 REST 接口（Blueprint: objstore, url_prefix=/api/objstore）。

接口契约（供 index.html「数据管理 → 对象存储备份」卡片调用）：

  GET    /api/objstore/config              读取配置（SecretKey / 压缩包密码掩码）
  POST   /api/objstore/config              保存配置
  GET    /api/objstore/categories          备份内容编目（含文件存在性/大小）
  POST   /api/objstore/test                测试连通性（可带表单值覆盖）
  POST   /api/objstore/backup              触发备份 {contents?: []}（异步，轮询 /status）
  GET    /api/objstore/objects             云端备份列表（受 prefix 限制）
  POST   /api/objstore/restore             从云端还原 {key, overwrite?, password?}
  POST   /api/objstore/delete              删除云端备份 {key}
  GET    /api/objstore/history             执行历史 (?limit=)
  GET    /api/objstore/status              调度与最近状态

错误语义（重要）
----------------
* **400** —— 可预期错误：参数缺失、配置不合法、凭据/连接失败、包已加密但没给密码等，
  `error` 直接是可展示给用户的中文原因；
* **500** —— 未预期错误：`NameError` / `AttributeError` / `TypeError` 这类**编码缺陷**。
  一律 `logger.exception` 落完整 traceback，响应里带 `unexpected: true` 与
  `异常类型: 消息`。

之所以区分：早先每个接口都写 `except Exception`，把 `NameError: name 'store' is not
defined` 吞成普通 400/500 业务错误，日志里连 traceback 都没有，只能靠用户报错定位。
"""

import logging
from functools import wraps

from flask import Blueprint, jsonify, request

from modules.objstore import engine, scheduler, store
from modules.objstore.crypto import CryptoError
from modules.objstore.s3_client import S3Error

bp_objstore = Blueprint("objstore", __name__, url_prefix="/api/objstore")
logger = logging.getLogger(__name__)

# 可预期异常：调用方（前端/运维）看到消息就能自行处理
EXPECTED_ERRORS = (ValueError, RuntimeError, KeyError, S3Error, CryptoError)


def _error(message, status: int = 400):
    return jsonify({"ok": False, "error": str(message)}), status


def api_endpoint(fn):
    """统一错误语义：可预期 → 400；未预期 → 记 traceback 再回 500。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except EXPECTED_ERRORS as exc:
            return _error(exc, 400)
        except Exception as exc:  # noqa: BLE001 - 兜底必须留痕
            logger.exception("对象存储接口 %s 执行失败（未预期错误）", fn.__name__)
            return jsonify({
                "ok": False,
                "unexpected": True,
                "error": f"{type(exc).__name__}: {exc}",
            }), 500

    return wrapper


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@bp_objstore.route("/config", methods=["GET"])
@api_endpoint
def get_config():
    return jsonify({"ok": True, "config": store.load_config(mask=True)})


@bp_objstore.route("/config", methods=["POST"])
@api_endpoint
def save_config():
    payload = request.get_json(force=True, silent=True) or {}
    return jsonify({"ok": True, "config": store.save_config(payload)})


@bp_objstore.route("/categories", methods=["GET"])
@api_endpoint
def get_categories():
    return jsonify({"ok": True, "categories": engine.catalog()})


@bp_objstore.route("/test", methods=["POST"])
@api_endpoint
def test_connection():
    payload = request.get_json(force=True, silent=True) or {}
    return jsonify(engine.test_connection(payload))


@bp_objstore.route("/backup", methods=["POST"])
@api_endpoint
def backup_now():
    """触发一次备份：后台线程执行，立即返回，前端轮询 /status 取结果。"""
    payload = request.get_json(force=True, silent=True) or {}
    result = scheduler.trigger_backup(contents=payload.get("contents"), trigger="manual")
    status_code = 200 if result.get("started") else 409
    return jsonify({"ok": bool(result.get("started")), **result}), status_code


@bp_objstore.route("/objects", methods=["GET"])
@api_endpoint
def list_objects():
    return jsonify({"ok": True, "objects": engine.list_remote(limit=_int_arg("limit", 200))})


@bp_objstore.route("/restore", methods=["POST"])
@api_endpoint
def restore():
    payload = request.get_json(force=True, silent=True) or {}
    key = (payload.get("key") or "").strip()
    if not key:
        raise ValueError("缺少对象 key")
    result = engine.restore_from_object(
        key,
        overwrite=bool(payload.get("overwrite", False)),
        password=(payload.get("password") or "").strip(),
    )
    return jsonify(result)


@bp_objstore.route("/delete", methods=["POST"])
@api_endpoint
def delete_object():
    payload = request.get_json(force=True, silent=True) or {}
    key = (payload.get("key") or "").strip()
    if not key:
        raise ValueError("缺少对象 key")
    engine.delete_remote(key)
    return jsonify({"ok": True, "deleted": key})


@bp_objstore.route("/history", methods=["GET"])
@api_endpoint
def history():
    return jsonify({"ok": True, "history": engine.list_history(limit=_int_arg("limit", 20))})


@bp_objstore.route("/status", methods=["GET"])
@api_endpoint
def status():
    return jsonify({"ok": True, "status": scheduler.status()})


def register_objstore(app) -> None:
    """注册蓝图并拉起后台调度线程；任何异常都不应阻断主程序启动。"""
    app.register_blueprint(bp_objstore)
    try:
        scheduler.start()
    except Exception as exc:  # pragma: no cover - 调度失败不应阻断启动
        logger.exception("对象存储备份调度器启动失败（不阻断主程序）")
        print(f"  [WARN] 对象存储备份调度器启动失败: {exc}")
