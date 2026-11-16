# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Device identity and offline authorization records for the system page.

The current activation code format is ``DBP2.<payload>.<signature>``. ``payload``
is a URL-safe base64 JSON document; ``signature`` is a full HMAC-SHA256 digest
(uppercase hex). The payload includes an auto-generated license id, the device
serial, the licensed object, deployment organization, edition, issue time and
expiry date. Legacy ``DBP1`` codes (20-character truncated signature) remain
accepted so existing installations keep working.

This is an offline integrity check, not a replacement for a commercial contract.

授权码由独立工具 ``license-console/license_console.py`` 生成；该工具不导入
本模块或 Flask，也不会进入应用发布包。两边使用同一套协议常量和环境变量
``DBPULSE_ACTIVATION_SECRET``。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import socket
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from modules.core import paths

_SERIAL_PATH = paths.DATA_DIR / "device_serial.txt"
_LICENSE_PATH = paths.DATA_DIR / "system_license.json"
_SECRET = os.environ.get(
    "DBPULSE_ACTIVATION_SECRET",
    "DBPulse-Offline-Activation-2026",
).encode("utf-8")
_LEGACY_CODE_PREFIX = "DBP1"
_CODE_PREFIX = "DBP2"
_LEGACY_SIGNATURE_LENGTH = 20
_SIGNATURE_LENGTH = 64

# 授权控制台下拉选项；仍然允许自定义版本名。
EDITION_OPTIONS = ("业务专用版", "商业专用版", "专业版", "企业版", "社区版")
DEFAULT_EDITION = "业务专用版"


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return ""


def _windows_machine_guid() -> str:
    try:
        import winreg

        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            access,
        ) as key:
            return str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
    except Exception:
        return ""


def _machine_fingerprint() -> str:
    """Collect stable, non-secret machine identifiers for a local serial."""
    machine_id = (
        _read_text("/etc/machine-id")
        or _read_text("/var/lib/dbus/machine-id")
        or _windows_machine_guid()
    )
    parts = [
        machine_id,
        platform.node() or socket.gethostname(),
        platform.system(),
        platform.machine(),
        str(uuid.getnode()),
    ]
    return "|".join(parts)


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, mode)
    except Exception:
        pass
    os.replace(tmp, path)


def get_device_serial() -> str:
    """Return a stable display serial, cached under the runtime data directory."""
    cached = _read_text(str(_SERIAL_PATH)).upper()
    if cached.startswith("DBP-") and len(cached) == 23:
        return cached

    digest = hashlib.sha256(
        ("DBPulse|" + _machine_fingerprint()).encode("utf-8")
    ).hexdigest().upper()
    serial = "DBP-" + "-".join(digest[i:i + 4] for i in range(0, 16, 4))
    try:
        _atomic_write(_SERIAL_PATH, serial + "\n")
    except Exception:
        pass
    return serial


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _signature(
    payload_b64: str,
    prefix: str = _CODE_PREFIX,
    length: int = _SIGNATURE_LENGTH,
) -> str:
    message = f"{prefix}.{payload_b64}".encode("ascii")
    return hmac.new(_SECRET, message, hashlib.sha256).hexdigest().upper()[:length]


def new_license_id() -> str:
    """生成可读的授权编号，写入授权码并由激活页展示。"""
    return "LIC-" + datetime.now().strftime("%Y%m%d") + "-" + secrets.token_hex(4).upper()


def _parse_date(value: str):
    """兼容日期和日期时间字符串，返回 date；空值返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def issue_activation_code(
    licensed_to_type: str,
    licensed_to: str,
    deployment_org: str = "",
    edition: str = DEFAULT_EDITION,
    expires_at: str = "",
    device_serial: str = None,
    issued_at: str = None,
    license_id: str = "",
    valid_days: int = 0,
    code_version: int = 2,
) -> str:
    """Create an offline activation code for a device.

    ``code_version=2`` emits the current ``DBP2`` format with an auto-generated
    license id; ``code_version=1`` remains available for compatibility tests.
    ``expires_at`` is a ``YYYY-MM-DD`` date (a datetime prefix is accepted);
    when ``valid_days`` is supplied and ``expires_at`` is empty, the expiry is
    calculated from today. An empty expiry means perpetual.
    """
    serial = (device_serial or get_device_serial()).strip().upper()
    if not serial:
        raise ValueError("device_serial is required")
    to = (licensed_to or "").strip()
    if not to:
        raise ValueError("licensed_to is required")
    if licensed_to_type not in {"personal", "organization"}:
        raise ValueError("licensed_to_type must be personal or organization")
    if code_version not in {1, 2}:
        raise ValueError("code_version must be 1 or 2")

    days = int(valid_days or 0)
    if days < 0:
        raise ValueError("valid_days must not be negative")
    expiry = _parse_date(expires_at)
    if days and expiry is None:
        expiry = date.today() + timedelta(days=days)
    expires_value = expiry.isoformat() if expiry else ""

    issued_value = str(issued_at or datetime.now().isoformat(timespec="seconds")).strip()
    lid = str(license_id or "").strip()
    if code_version == 2 and not lid:
        lid = new_license_id()

    payload = {
        "v": code_version,
        "typ": licensed_to_type,
        "to": to,
        "dep": (deployment_org or "").strip(),
        "ed": (edition or DEFAULT_EDITION).strip(),
        "iat": issued_value,
        "exp": expires_value,
        "dev": serial,
    }
    if code_version == 2:
        payload.update({"lid": lid, "days": days})

    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64encode(raw)
    prefix = _CODE_PREFIX if code_version == 2 else _LEGACY_CODE_PREFIX
    length = _SIGNATURE_LENGTH if code_version == 2 else _LEGACY_SIGNATURE_LENGTH
    return f"{prefix}.{payload_b64}.{_signature(payload_b64, prefix, length)}"


def verify_activation_code(code: str):
    """Validate a code and return ``(ok, error, payload)``.

    The payload is returned only after the signature, device binding and expiry
    checks pass. Both legacy ``DBP1`` and current ``DBP2`` codes are accepted.
    """
    normalized = "".join((code or "").split())
    if not normalized:
        return False, "请输入激活码", None
    parts = normalized.split(".")
    if len(parts) != 3 or parts[0] not in {_LEGACY_CODE_PREFIX, _CODE_PREFIX}:
        return False, "激活码格式不正确", None

    prefix, payload_b64, supplied_sig = parts[0], parts[1], parts[2].upper()
    expected_length = (
        _SIGNATURE_LENGTH if prefix == _CODE_PREFIX else _LEGACY_SIGNATURE_LENGTH
    )
    expected_sig = _signature(payload_b64, prefix, expected_length)
    if not hmac.compare_digest(supplied_sig, expected_sig):
        return False, "激活码校验失败，请确认是否完整复制", None

    try:
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return False, "激活码内容无法解析", None
    expected_version = 2 if prefix == _CODE_PREFIX else 1
    if not isinstance(payload, dict) or payload.get("v") != expected_version:
        return False, "激活码版本不受支持", None
    if payload.get("typ") not in {"personal", "organization"}:
        return False, "授权类型不正确", None
    if not str(payload.get("to") or "").strip():
        return False, "激活码缺少授权对象", None
    if expected_version == 2 and not str(payload.get("lid") or "").strip():
        return False, "激活码缺少授权编号", None

    serial = get_device_serial()
    if str(payload.get("dev") or "").strip().upper() != serial:
        return False, "激活码与本机设备序列号不匹配", None

    expires_at = str(payload.get("exp") or "").strip()
    if expires_at:
        try:
            expiry = _parse_date(expires_at)
            if expiry and expiry < date.today():
                return False, "激活码已过期", None
        except ValueError:
            return False, "激活码有效期格式不正确", None

    return True, "", payload


def _load_record() -> dict:
    try:
        with open(_LICENSE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _display_datetime(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def _empty_license() -> dict:
    return {
        "licensed_to_type": "",
        "licensed_to_type_label": "—",
        "licensed_to": "—",
        "deployment_org": "—",
        "edition": "—",
        "activated_at": "—",
        "expires_at": "—",
        "license_id": "—",
    }


def get_license_info() -> dict:
    """Return the full status payload consumed by the system-info page."""
    serial = get_device_serial()
    record = _load_record()
    result = {
        "ok": True,
        "device_serial": serial,
        "status": "unactivated",
        "status_label": "未激活",
        "status_tone": "muted",
        "license": _empty_license(),
    }
    if not record:
        return result

    record_serial = str(record.get("device_serial") or "").strip().upper()
    expires_at = str(record.get("expires_at") or "").strip()
    status = "activated"
    status_label = "已激活"
    status_tone = "success"
    if record_serial and record_serial != serial:
        status, status_label, status_tone = "invalid", "设备不匹配", "danger"
    elif expires_at:
        try:
            if date.fromisoformat(expires_at) < date.today():
                status, status_label, status_tone = "expired", "已过期", "danger"
        except ValueError:
            status, status_label, status_tone = "invalid", "有效期异常", "danger"

    licensed_to_type = str(record.get("licensed_to_type") or "")
    result.update({
        "status": status,
        "status_label": status_label,
        "status_tone": status_tone,
        "license": {
            "licensed_to_type": licensed_to_type,
            "licensed_to_type_label": {
                "personal": "个人",
                "organization": "单位",
            }.get(licensed_to_type, "—"),
            "licensed_to": record.get("licensed_to") or "—",
            "deployment_org": record.get("deployment_org") or "—",
            "edition": record.get("edition") or "—",
            "activated_at": _display_datetime(record.get("activated_at") or "") or "—",
            "expires_at": expires_at or "长期有效",
            "license_id": record.get("license_id") or "—",
        },
    })
    return result


def activate_license(code: str, actor: str = "") -> dict:
    """Validate and persist an activation code for this device."""
    ok, error, payload = verify_activation_code(code)
    if not ok:
        return {"ok": False, "error": error}

    normalized = "".join((code or "").split())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest().upper()
    license_id = str(payload.get("lid") or "").strip() or ("LIC-" + digest[:12])
    record = {
        "schema": 2 if payload.get("v") == 2 else 1,
        "license_id": license_id,
        "code_sha256": digest,
        "code_tail": normalized[-8:],
        "licensed_to_type": payload["typ"],
        "licensed_to": str(payload.get("to") or "").strip(),
        "deployment_org": str(payload.get("dep") or "").strip(),
        "edition": str(payload.get("ed") or "").strip(),
        "issued_at": str(payload.get("iat") or "").strip(),
        "expires_at": str(payload.get("exp") or "").strip(),
        "licensed_days": int(payload.get("days") or 0),
        "device_serial": get_device_serial(),
        "activated_at": datetime.now().isoformat(timespec="seconds"),
        "activated_by": actor or "",
    }
    try:
        _atomic_write(
            _LICENSE_PATH,
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        )
    except Exception as exc:
        return {"ok": False, "error": f"授权记录保存失败：{exc}"}

    info = get_license_info()
    info["license_id"] = record["license_id"]
    return info


def deactivate_license(actor: str = "") -> dict:
    """Remove the current local authorization and return the unactivated state.

    Removal is intentionally local and idempotent: the activation record is the
    only source of truth, so deleting it immediately restores the unactivated
    status for both the web page and the Windows status window.
    """
    removed = False
    try:
        candidates = (
            _LICENSE_PATH,
            _LICENSE_PATH.with_name(_LICENSE_PATH.name + ".tmp"),
        )
        for candidate in candidates:
            if candidate.exists():
                candidate.unlink()
                removed = True
    except OSError as exc:
        return {"ok": False, "error": f"移除授权失败：{exc}"}

    info = get_license_info()
    info["removed"] = removed
    return info
