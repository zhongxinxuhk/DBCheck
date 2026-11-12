# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""对象存储备份配置的读写与凭据保护。

配置节点落 ``dbc_config.json`` → ``object_storage``，与通知（SMTP/AI）配置同域，
因此 Docker 下跟着 ``/app/data`` 卷一起持久化，升级重建不会丢。

SecretKey 永远不以明文出网（API 返回掩码），落盘按以下优先级处理：

1. ``cryptography`` 可用 → Fernet（密钥存 ``object_storage._fernet_key``）
2. 不可用 → ``_plain_`` 前缀 + ``insecure_storage`` 标记，前端给出告警

之所以不直接复用 ``modules.notify`` 的 Fernet：通知模块的密钥节点与对象存储
生命周期不同（通知密钥可能被用户重置），一旦轮换会连带让 S3 凭据无法解密。
"""



from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from modules.core import paths

try:  # cryptography 是运行期硬依赖，但测试/极简环境可能缺失
    from cryptography.fernet import Fernet  # type: ignore

    _HAS_FERNET = True
except ImportError:  # pragma: no cover - 取决于运行环境
    Fernet = None  # type: ignore
    _HAS_FERNET = False

CONFIG_PATH = paths.DBC_CONFIG_PATH
NODE = "object_storage"
FERNET_KEY_FIELD = "_fernet_key"
MASK = "••••••••"
_LOCK = threading.RLock()

DEFAULT_PREFIX = "dbpulse-backup"
DEFAULT_REGION = "us-east-1"
DEFAULT_CONTENTS = ["engine", "baseline", "report", "history"]
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 30 * 24 * 60  # 30 天
MAX_RETENTION = 365

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "endpoint": "https://fgws3-ocloud.ihep.ac.cn",
    "bucket": "",
    "region": DEFAULT_REGION,
    "access_key": "",
    "secret_key": "",
    "archive_password": "",
    "prefix": DEFAULT_PREFIX,
    "path_style": True,
    "verify_tls": True,
    "timeout": 120,
    "contents": list(DEFAULT_CONTENTS),
    "interval_minutes": 24 * 60,
    "retention": 7,
    "last_run_at": "",
    "last_status": "",
    "last_message": "",
    "last_object": "",
}

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


def _encrypt_available() -> bool:
    """压缩包口令加密是否可用（惰性导入，避免与 crypto 循环引用）。"""
    try:
        from modules.objstore import crypto

        return bool(crypto.available())
    except Exception:
        return False


# ── 底层 JSON 读写（原子替换，避免并发读到半截文件）──────────────────────
def _read_root() -> Optional[Dict[str, Any]]:
    for attempt in (1, 2):
        if not CONFIG_PATH.exists():
            return {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            if attempt == 1:
                continue
    return None


def _write_root(data: Dict[str, Any]) -> None:
    os.makedirs(str(CONFIG_PATH.parent), exist_ok=True)
    tmp = f"{CONFIG_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, str(CONFIG_PATH))


# ── 凭据编解码 ────────────────────────────────────────────────────────
def _fernet(node: Dict[str, Any], create: bool):
    if not _HAS_FERNET:
        return None
    key = node.get(FERNET_KEY_FIELD)
    if key:
        try:
            return Fernet(key.encode("utf-8"))
        except Exception:
            return None
    if not create:
        return None
    key = Fernet.generate_key().decode("utf-8")
    node[FERNET_KEY_FIELD] = key
    return Fernet(key.encode("utf-8"))


def encode_secret(secret: str) -> Tuple[str, bool]:
    """返回 (待落盘字符串, 是否安全加密)。"""
    secret = (secret or "").strip()
    if not secret:
        return "", True
    with _LOCK:
        root = _read_root()
        if root is None:
            raise RuntimeError("dbc_config.json 读取失败，已放弃写入以避免覆盖其他配置")
        node = root.setdefault(NODE, {})
        fernet = _fernet(node, create=True)
        if fernet is not None:
            token = fernet.encrypt(secret.encode("utf-8")).decode("utf-8")
            root[NODE] = node
            _write_root(root)
            return f"_enc_{token}", True
        root[NODE] = node
        _write_root(root)
    return f"_plain_{secret}", False


def decode_secret(stored: str) -> str:
    stored = stored or ""
    if not stored:
        return ""
    if stored.startswith("_enc_"):
        if not _HAS_FERNET:
            return ""
        with _LOCK:
            root = _read_root() or {}
        fernet = _fernet(root.get(NODE) or {}, create=False)
        if fernet is None:
            return ""
        try:
            return fernet.decrypt(stored[5:].encode("utf-8")).decode("utf-8")
        except Exception:
            return ""
    if stored.startswith("_plain_"):
        return stored[7:]
    return stored


# ── 配置读写 ──────────────────────────────────────────────────────────
def load_config(mask: bool = True) -> Dict[str, Any]:
    """读取配置；mask=True 时 SecretKey 以掩码返回（供 API/前端使用）。"""
    root = _read_root()
    node = dict((root or {}).get(NODE) or {})
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in node:
            cfg[key] = node[key]
    cfg["contents"] = [c for c in (cfg.get("contents") or []) if isinstance(c, str)]
    cfg["secret_key_set"] = bool(node.get("secret_key"))
    cfg["secret_encrypted"] = str(node.get("secret_key", "")).startswith("_enc_")
    cfg["insecure_storage"] = str(node.get("secret_key", "")).startswith("_plain_")
    cfg["archive_password_set"] = bool(node.get("archive_password"))
    cfg["archive_password_encrypted"] = str(node.get("archive_password", "")).startswith("_enc_")
    cfg["encrypt_available"] = _encrypt_available()
    if mask:
        cfg["secret_key"] = MASK if node.get("secret_key") else ""
        cfg["archive_password"] = MASK if node.get("archive_password") else ""
    else:
        cfg["secret_key"] = decode_secret(str(node.get("secret_key", "")))
        cfg["archive_password"] = decode_secret(str(node.get("archive_password", "")))
    return cfg


def _sanitize_prefix(value: str) -> str:
    value = (value or "").strip().strip("/")
    value = re.sub(r"/{2,}", "/", value)
    # 只保留可安全出现在 URL 与对象键里的字符，避免 S3 签名编码歧义
    value = re.sub(r"[^A-Za-z0-9._/\-]", "-", value)
    return value or DEFAULT_PREFIX


def validate(payload: Dict[str, Any], current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """校验并归一化用户提交的配置（不落盘）。"""
    current = current or load_config(mask=False)
    cfg = dict(current)
    for key in DEFAULTS:
        if key in payload:
            cfg[key] = payload[key]

    cfg["endpoint"] = str(cfg.get("endpoint") or "").strip().rstrip("/")
    if cfg["endpoint"] and "://" not in cfg["endpoint"]:
        cfg["endpoint"] = "https://" + cfg["endpoint"]
    cfg["bucket"] = str(cfg.get("bucket") or "").strip()
    cfg["region"] = str(cfg.get("region") or DEFAULT_REGION).strip() or DEFAULT_REGION
    cfg["access_key"] = str(cfg.get("access_key") or "").strip()
    cfg["prefix"] = _sanitize_prefix(str(cfg.get("prefix") or ""))
    cfg["path_style"] = bool(cfg.get("path_style", True))
    cfg["verify_tls"] = bool(cfg.get("verify_tls", True))
    cfg["enabled"] = bool(cfg.get("enabled", False))
    try:
        timeout_val = cfg.get("timeout")
        cfg["timeout"] = min(1800, max(5, int(120 if timeout_val in (None, "") else timeout_val)))
    except Exception:
        cfg["timeout"] = 120
    try:
        interval_val = cfg.get("interval_minutes")
        cfg["interval_minutes"] = min(
            MAX_INTERVAL_MINUTES,
            max(MIN_INTERVAL_MINUTES, int(1440 if interval_val in (None, "") else interval_val)),
        )
    except Exception:
        cfg["interval_minutes"] = 1440
    try:
        retention_val = cfg.get("retention")
        cfg["retention"] = min(
            MAX_RETENTION, max(1, int(7 if retention_val in (None, "") else retention_val))
        )
    except Exception:
        cfg["retention"] = 7

    contents = cfg.get("contents")
    if isinstance(contents, str):
        contents = [c.strip() for c in contents.split(",") if c.strip()]
    if not isinstance(contents, list):
        contents = list(DEFAULT_CONTENTS)
    cfg["contents"] = [str(c) for c in contents]

    if cfg["enabled"]:
        errors = []
        if not cfg["endpoint"]:
            errors.append("Endpoint 不能为空")
        if not cfg["bucket"]:
            errors.append("Bucket 不能为空")
        elif not _BUCKET_RE.match(cfg["bucket"]):
            errors.append("Bucket 命名不合法（3-63 位小写字母、数字、点或连字符）")
        if not cfg["access_key"]:
            errors.append("AccessKey 不能为空")
        if not cfg.get("secret_key"):
            errors.append("SecretKey 不能为空")
        if not cfg["contents"]:
            errors.append("至少选择一项备份内容")
        if errors:
            raise ValueError("；".join(errors))

    # 口令加密与「是否启用定时」无关：设了密码但环境不支持，一律拒绝，
    # 否则用户会以为已加密，实际备份时才失败（或更糟：静默产出明文包）
    if cfg.get("archive_password") and cfg.get("archive_password") != MASK \
            and not _encrypt_available():
        raise ValueError("当前环境缺少 cryptography，无法启用压缩包密码")
    return cfg


def save_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """合并保存配置；SecretKey 传掩码表示「保持不变」。"""
    with _LOCK:
        current = load_config(mask=False)
        cfg = validate(payload, current)

        incoming_secret = payload.get("secret_key", None)
        if incoming_secret is None or incoming_secret == MASK:
            # 不修改凭据：沿用已落盘值（含加密标记）
            root = _read_root()
            if root is None:
                raise RuntimeError("dbc_config.json 读取失败，已放弃写入以避免覆盖其他配置")
            secret_stored = str((root.get(NODE) or {}).get("secret_key", ""))
            secure = secret_stored.startswith("_enc_")
        elif str(incoming_secret) == "":
            secret_stored, secure = "", True
        else:
            # encode_secret 会自行写回 _fernet_key；因此必须在其后重新读取根节点，
            # 否则下面 root[NODE] = node 会把刚生成的密钥覆盖掉，密文再也解不开。
            secret_stored, secure = encode_secret(str(incoming_secret))

        # 压缩包密码：留空/掩码 = 保持原值；要清除必须显式 clear_archive_password
        incoming_pwd = payload.get("archive_password", None)
        if payload.get("clear_archive_password"):
            pwd_stored = ""
        elif incoming_pwd is None or incoming_pwd == MASK or str(incoming_pwd) == "":
            root = _read_root()
            pwd_stored = str((root.get(NODE) or {}).get("archive_password", "")) \
                if root is not None else ""
        else:
            pwd_stored, _ = encode_secret(str(incoming_pwd))

        root = _read_root()
        if root is None:
            raise RuntimeError("dbc_config.json 读取失败，已放弃写入以避免覆盖其他配置")
        node = dict(root.get(NODE) or {})

        node.update({
            "enabled": cfg["enabled"],
            "endpoint": cfg["endpoint"],
            "bucket": cfg["bucket"],
            "region": cfg["region"],
            "access_key": cfg["access_key"],
            "prefix": cfg["prefix"],
            "path_style": cfg["path_style"],
            "verify_tls": cfg["verify_tls"],
            "timeout": cfg["timeout"],
            "contents": cfg["contents"],
            "interval_minutes": cfg["interval_minutes"],
            "retention": cfg["retention"],
            "secret_key": secret_stored,
            "archive_password": pwd_stored,
            "last_run_at": node.get("last_run_at", ""),
            "last_status": node.get("last_status", ""),
            "last_message": node.get("last_message", ""),
            "last_object": node.get("last_object", ""),
        })
        root[NODE] = node
        _write_root(root)
        if not secure:
            print("[objstore][WARN] cryptography 不可用，SecretKey 未经加密落盘；"
                  "生产环境请安装 cryptography")
    return load_config(mask=True)


def record_run(status: str, message: str, object_key: str = "") -> None:
    """把最近一次执行结果写回配置节点（供 UI 显示，无需额外查询）。"""
    with _LOCK:
        root = _read_root()
        if root is None:
            return
        node = dict(root.get(NODE) or {})
        node["last_run_at"] = _now_text()
        node["last_status"] = status
        node["last_message"] = (message or "")[:500]
        if object_key:
            node["last_object"] = object_key
        root[NODE] = node
        try:
            _write_root(root)
        except Exception:
            pass


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_client(cfg: Optional[Dict[str, Any]] = None):
    """按配置构造 S3Client（SecretKey 自动解密）。"""
    from modules.objstore.s3_client import S3Client

    cfg = cfg or load_config(mask=False)
    return S3Client(
        endpoint=cfg.get("endpoint", ""),
        access_key=cfg.get("access_key", ""),
        secret_key=cfg.get("secret_key", ""),
        bucket=cfg.get("bucket", ""),
        region=cfg.get("region", DEFAULT_REGION),
        path_style=bool(cfg.get("path_style", True)),
        timeout=int(cfg.get("timeout") or 120),
        verify_tls=bool(cfg.get("verify_tls", True)),
    )


def secret_preview(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-2:]}"


def config_summary(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给 UI 的摘要（不含任何明文凭据）。"""
    cfg = cfg or load_config(mask=True)
    target = ""
    if cfg.get("bucket") and cfg.get("endpoint"):
        target = f"{cfg['endpoint']}/{cfg['bucket']}"
    return {
        "enabled": bool(cfg.get("enabled")),
        "target": target,
        "prefix": cfg.get("prefix"),
        "interval_minutes": cfg.get("interval_minutes"),
        "retention": cfg.get("retention"),
        "contents": list(cfg.get("contents") or []),
        "last_run_at": cfg.get("last_run_at", ""),
        "last_status": cfg.get("last_status", ""),
        "last_message": cfg.get("last_message", ""),
        "encrypted_backup": bool(cfg.get("archive_password_set")),
    }


ALLOWED_CONTENT_IDS: List[str] = []  # 由 engine 在导入时回填，避免循环引用
