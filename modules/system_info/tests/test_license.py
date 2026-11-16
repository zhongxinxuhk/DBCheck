# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""离线授权码签发、校验和激活记录测试。"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from modules.system_info import license as license_service

ROOT = Path(__file__).resolve().parents[3]


def load_standalone_console():
    """Load the independent console without importing the DBCheck package."""
    path = ROOT / "license-console" / "license_console.py"
    spec = importlib.util.spec_from_file_location("standalone_license_console", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def isolated_license(tmp_path, monkeypatch):
    serial = "DBP-AAAA-BBBB-CCCC-DDDD"
    monkeypatch.setattr(license_service, "_SERIAL_PATH", tmp_path / "serial.txt")
    monkeypatch.setattr(license_service, "_LICENSE_PATH", tmp_path / "license.json")
    monkeypatch.setattr(license_service, "get_device_serial", lambda: serial)
    return serial, tmp_path / "license.json"


def test_db2_code_contains_license_id_and_fields(isolated_license):
    serial, _ = isolated_license
    expiry = (date.today() + timedelta(days=365)).isoformat()
    code = license_service.issue_activation_code(
        licensed_to_type="organization",
        licensed_to="示例单位",
        deployment_org="信息中心",
        edition="企业版",
        expires_at=expiry,
        device_serial=serial,
        issued_at="2026-11-16T10:20:30",
        license_id="LIC-20261116-ABCDEF12",
        valid_days=365,
    )

    assert code.startswith("DBP2.")
    ok, error, payload = license_service.verify_activation_code(code)
    assert ok, error
    assert payload["lid"] == "LIC-20261116-ABCDEF12"
    assert payload["typ"] == "organization"
    assert payload["to"] == "示例单位"
    assert payload["dep"] == "信息中心"
    assert payload["ed"] == "企业版"
    assert payload["exp"] == expiry
    assert payload["days"] == 365


def test_auto_license_id_and_days_calculate_expiry(isolated_license):
    serial, _ = isolated_license
    code = license_service.issue_activation_code(
        licensed_to_type="personal",
        licensed_to="张三",
        device_serial=serial,
        valid_days=30,
    )
    ok, error, payload = license_service.verify_activation_code(code)
    assert ok, error
    assert payload["lid"].startswith("LIC-")
    assert payload["exp"] == (date.today() + timedelta(days=30)).isoformat()


def test_activate_license_persists_generated_license_id(isolated_license):
    serial, license_path = isolated_license
    code = license_service.issue_activation_code(
        licensed_to_type="organization",
        licensed_to="示例单位",
        deployment_org="信息中心",
        edition="业务专用版",
        device_serial=serial,
        license_id="LIC-20261116-12345678",
        valid_days=10,
    )

    info = license_service.activate_license(code, actor="tester")
    assert info["ok"]
    assert info["license"]["license_id"] == "LIC-20261116-12345678"
    assert info["license"]["licensed_to"] == "示例单位"
    assert info["license"]["deployment_org"] == "信息中心"
    assert info["license"]["edition"] == "业务专用版"

    record = json.loads(license_path.read_text(encoding="utf-8"))
    assert record["schema"] == 2
    assert record["license_id"] == "LIC-20261116-12345678"
    assert record["licensed_days"] == 10


def test_deactivate_license_removes_record_and_restores_unactivated(isolated_license):
    serial, license_path = isolated_license
    code = license_service.issue_activation_code(
        licensed_to_type="organization",
        licensed_to="待移除授权单位",
        device_serial=serial,
        license_id="LIC-20261116-87654321",
        valid_days=30,
    )
    assert license_service.activate_license(code)["ok"]
    assert license_path.is_file()

    info = license_service.deactivate_license(actor="tester")
    assert info["ok"]
    assert info["removed"] is True
    assert info["status"] == "unactivated"
    assert info["license"]["license_id"] == "—"
    assert not license_path.exists()

    again = license_service.deactivate_license()
    assert again["ok"]
    assert again["removed"] is False
    assert again["status"] == "unactivated"


def test_legacy_db1_code_is_still_accepted(isolated_license):
    serial, _ = isolated_license
    code = license_service.issue_activation_code(
        licensed_to_type="organization",
        licensed_to="旧授权单位",
        device_serial=serial,
        code_version=1,
    )
    assert code.startswith("DBP1.")
    ok, error, payload = license_service.verify_activation_code(code)
    assert ok, error
    assert payload["v"] == 1


def test_expired_or_wrong_device_code_is_rejected(isolated_license):
    serial, _ = isolated_license
    expired = license_service.issue_activation_code(
        licensed_to_type="organization",
        licensed_to="过期单位",
        expires_at=(date.today() - timedelta(days=1)).isoformat(),
        device_serial=serial,
    )
    ok, error, _ = license_service.verify_activation_code(expired)
    assert not ok
    assert "过期" in error

    wrong_device = license_service.issue_activation_code(
        licensed_to_type="organization",
        licensed_to="别的设备",
        device_serial="DBP-9999-9999-9999-9999",
    )
    ok, error, _ = license_service.verify_activation_code(wrong_device)
    assert not ok
    assert "设备序列号" in error


def test_tampered_code_is_rejected(isolated_license):
    serial, _ = isolated_license
    code = license_service.issue_activation_code(
        licensed_to_type="personal",
        licensed_to="张三",
        device_serial=serial,
        valid_days=10,
    )
    tampered = code[:-1] + ("0" if code[-1] != "0" else "1")
    ok, error, _ = license_service.verify_activation_code(tampered)
    assert not ok
    assert "校验失败" in error


def test_standalone_console_generates_codes_accepted_by_application(isolated_license):
    serial, _ = isolated_license
    console = load_standalone_console()
    assert console._SECRET == license_service._SECRET
    assert console._CODE_PREFIX == license_service._CODE_PREFIX

    code = console.issue_activation_code(
        licensed_to_type="organization",
        licensed_to="独立控制台单位",
        deployment_org="信息中心",
        edition="企业版",
        device_serial=serial,
        issued_at="2026-11-16T10:20:30",
        license_id="LIC-20261116-ABCDEF12",
        valid_days=365,
    )
    ok, error, payload = license_service.verify_activation_code(code)
    assert ok, error
    assert payload["lid"] == "LIC-20261116-ABCDEF12"
    assert payload["to"] == "独立控制台单位"


def test_standalone_console_matches_application_codec_exactly(isolated_license):
    serial, _ = isolated_license
    console = load_standalone_console()
    arguments = dict(
        licensed_to_type="organization",
        licensed_to="独立控制台单位",
        deployment_org="信息中心",
        edition="企业版",
        expires_at="2027-12-31",
        device_serial=serial,
        issued_at="2026-11-16T10:20:30",
        license_id="LIC-20261116-ABCDEF12",
        valid_days=365,
    )
    assert console.issue_activation_code(
        **arguments
    ) == license_service.issue_activation_code(**arguments)


def test_standalone_console_has_no_application_imports():
    source = (ROOT / "license-console" / "license_console.py").read_text(
        encoding="utf-8"
    )
    assert "from modules" not in source
    assert "import modules" not in source
    assert "import flask" not in source
    assert "from flask" not in source


def test_standalone_console_web_fallback_uses_same_interpreter():
    console = load_standalone_console()
    source = (ROOT / "license-console" / "license_console.py").read_text(
        encoding="utf-8"
    )
    assert "_run_web_console" in source
    assert "ThreadingHTTPServer" in source
    assert "subprocess" not in source
    assert "os.exec" not in source
    assert "sys.executable" not in source

    for required in (
        'id="serial"',
        'id="type"',
        'id="target"',
        'id="org"',
        'id="edition"',
        'id="activated"',
        'id="days"',
        'id="license_id"',
        'id="code"',
    ):
        assert required in console._WEB_PAGE
