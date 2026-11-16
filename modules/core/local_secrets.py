# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""本地敏感配置字段的静态加密与原子读写。

``dbc_config.json`` 同时保存 SMTP、AI、BIC-QA、MCP 与 Webhook 配置。
其中邮件口令、对象存储 SecretKey 已有各自的加密格式；但 AI API Key、
BIC-QA Key、MCP Token、Webhook URL 等此前仍是明文。本模块把这类字段统一
封装为 ``enc:v1:`` 前缀的 Fernet 密文，复用 ``data/.db_key``，并提供：

* :func:`load_config` / :func:`save_config`：读取时透明解密、写入时透明加密；
* :func:`migrate_config`：把存量明文一次性迁移为密文（幂等）；
* :func:`encrypt_value` / :func:`decrypt_value`：字段级加解密；
* 写入采用同目录临时文件 + ``os.replace``，POSIX 下权限固定为 0600。

设计边界：

* 只加密明确的敏感字段（避免把模型名、URL 路径等普通配置一起加密）；
* 已带 ``enc:v1:`` 或既有 ``_enc_`` 前缀的值不重复加密；
* 解密失败保留原密文并告警，既不让密文被当成明文使用，也避免一次无关保存覆盖原值；
* 不改变 ``instances.db`` 中密码的既有格式，也不替代 SQLCipher 之类的
  SQLite 全库加密；本模块只保护配置字段与需要额外保护的连接串。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

PREFIX = "enc:v1:"

# 只对这些明确路径做字段级加密。新增字段必须同时确认所有读取方都走
# load_config()/decrypt_config()，否则会读到密文。
_SECRET_PATHS = frozenset(
    {
        ("ai", "api_key"),
        ("ai", "online_api_key"),
        ("bicqa", "api_key"),
        ("mcp_server", "token"),
        ("mcp_server", "api_key"),
        ("mcp_server", "auth_token"),
        ("notification", "webhook", "url"),
        ("notification", "webhook", "secret"),
        ("notification", "webhook", "token"),
        ("notification", "monitor_alert", "webhook"),
        ("notification", "monitor_alert", "webhook_url"),
        ("plugin_market", "token"),
        ("plugin_market", "api_key"),
    }
)

_LOCK = threading.RLock()
_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"[local_secrets][WARN] {message}")


def _encrypt_text(value: str) -> str:
    from modules.pro.instance_manager import _encrypt_pwd

    return _encrypt_pwd(value)


def _decrypt_text(value: str) -> str:
    from modules.pro.instance_manager import _decrypt_pwd

    return _decrypt_pwd(value, create_key=False)


def is_encrypted(value: Any) -> bool:
    """判断值是否为本模块产出的密文。"""
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_value(value: Any) -> Any:
    """加密单个敏感值；空值、已加密值与既有 ``_enc_`` 值原样返回。"""
    if not isinstance(value, str) or not value:
        return value
    if value.startswith((PREFIX, "_enc_", "jdbc:enc:v1:")):
        return value
    # _encrypt_text 内部会加载密钥；缺失/损坏时在写入前失败，而不是写半截。
    return PREFIX + _encrypt_text(value)


def decrypt_value(value: Any) -> Any:
    """解密本模块产出的值；非本模块格式原样返回。"""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        plain = _decrypt_text(value[len(PREFIX):])
    except Exception as exc:
        _warn_once(f"敏感配置解密失败，保留原密文以避免误覆盖（{type(exc).__name__}）")
        return value
    if not plain:
        _warn_once("敏感配置解密失败或密钥不匹配，保留原密文以避免误覆盖；请恢复密钥或重新录入")
        return value
    return plain


def _is_secret_path(path: tuple[str, ...]) -> bool:
    return path in _SECRET_PATHS


def _transform(node: Any, path: tuple[str, ...], encrypt: bool) -> Any:
    if isinstance(node, dict):
        return {
            key: _transform(value, path + (str(key),), encrypt)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_transform(value, path, encrypt) for value in node]
    if isinstance(node, str):
        if encrypt and _is_secret_path(path):
            return encrypt_value(node)
        if not encrypt and node.startswith(PREFIX):
            return decrypt_value(node)
    return node


_SENSITIVE_KEYS = frozenset(
    {
        "password", "passwd", "pwd", "ssh_password", "ssh_key_password",
        "jdbc_url", "api_key", "apikey", "token", "auth_token", "secret",
        "secret_key", "access_key", "private_key",
    }
)


def _encrypt_sensitive_tree(node: Any) -> Any:
    if isinstance(node, dict):
        output = {}
        for key, value in node.items():
            if (
                str(key).lower() in _SENSITIVE_KEYS
                and isinstance(value, str)
                and value
                and value != "***"
            ):
                output[key] = encrypt_value(value)
            else:
                output[key] = _encrypt_sensitive_tree(value)
        return output
    if isinstance(node, list):
        return [_encrypt_sensitive_tree(value) for value in node]
    return node


def encrypt_sensitive_tree(node: Any) -> Any:
    """按字段名递归加密任意 JSON 树（用于 scheduler_jobs.json 等）。

    只加密明确的凭据字段；不改变 ``***`` 掩码与空值。
    """
    return _encrypt_sensitive_tree(node)


def encrypt_config(config: Any) -> Any:
    """返回加密副本，不修改入参。"""
    return _transform(config, (), True)


def decrypt_config(config: Any) -> Any:
    """返回解密副本，不修改入参。"""
    return _transform(config, (), False)


def load_config(path: os.PathLike[str] | str, *, decrypt: bool = True, strict: bool = False) -> dict:
    """读取 JSON 配置；默认透明解密 ``enc:v1:`` 字段。

    文件不存在时返回空 dict；JSON 损坏时默认告警并返回空 dict。写入路径应传
    ``strict=True``，让损坏配置直接抛错，避免以空配置覆盖其他节点。
    ``decrypt=False`` 用于需要保留密文原样再写回的维护路径。
    """
    config_path = Path(path)
    if not config_path.is_file():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        if strict:
            raise ValueError("配置文件损坏，已拒绝覆盖写入") from exc
        _warn_once(f"配置文件读取失败，按空配置处理（{type(exc).__name__}）")
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError("配置文件根节点不是对象，已拒绝覆盖写入")
        _warn_once("配置文件根节点不是对象，按空配置处理")
        return {}
    return decrypt_config(data) if decrypt else data


def _write_raw(path: Path, data: dict) -> None:
    """原子写 JSON；同目录临时文件避免跨挂载点 rename 失败。"""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def save_config(path: os.PathLike[str] | str, config: dict) -> None:
    """加密敏感字段后原子写入配置。

    加密失败（``cryptography`` 不可用、``.db_key`` 缺失等）会直接抛出，
    不写明文降级文件。
    """
    if not isinstance(config, dict):
        raise TypeError("配置根节点必须是对象")
    config_path = Path(path)
    payload = encrypt_config(config)
    with _LOCK:
        _write_raw(config_path, payload)


def migrate_config(path: os.PathLike[str] | str) -> bool:
    """把存量明文敏感字段迁移为密文；返回是否发生写入。"""
    config_path = Path(path)
    raw = load_config(config_path, decrypt=False)
    if not raw:
        return False
    encrypted = encrypt_config(raw)
    if encrypted == raw:
        return False
    with _LOCK:
        _write_raw(config_path, encrypted)
    return True
