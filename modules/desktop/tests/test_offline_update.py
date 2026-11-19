# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Offline update package validation, replacement and rollback coverage."""


import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from modules.desktop.offline_update import (
    OfflineUpdate,
    OfflineUpdateError,
    helper_main,
    run_update_helper,
)


def pe_bytes(marker):
    data = bytearray(1024)
    data[:2] = b"MZ"
    data[60:64] = (128).to_bytes(4, "little")
    data[128:132] = b"PE\x00\x00"
    data[132:134] = (0x8664).to_bytes(2, "little")
    raw = marker.encode("utf-8")
    data[300 : 300 + len(raw)] = raw
    return bytes(data)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write_tree(root, files):
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(value, encoding="utf-8")


def make_zip(destination, root_name, files):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(root_name + "/" + name, value)


def capability():
    return json.dumps(
        {"schema": 1, "helper": "--offline-update-helper"}, ensure_ascii=False
    )


def standalone_installation(tmp_path, version="v1.0.6"):
    root = tmp_path / "install"
    write_tree(
        root,
        {
            "dbcheck.exe": pe_bytes("old-dbcheck"),
            "DBPulse.exe": pe_bytes("old-launcher"),
            "_internal/app.txt": "old app",
            "_internal/modules/pro/rules/custom/user.yaml": "user rule",
            "_internal/modules/pro/rules/overrides.yaml": "user override",
            "version.json": json.dumps({"version": version}),
            "data/secret.txt": "keep this",
        },
    )
    return root


def standalone_package(tmp_path, version="v1.0.7"):
    root_name = "DBPulse-Windows-x86_64-" + version
    files = {
        "dbcheck.exe": pe_bytes("new-dbcheck"),
        "DBPulse.exe": pe_bytes("new-launcher"),
        "_internal/app.txt": "new app",
        "_internal/modules/pro/rules/default.yaml": "new rule",
        "version.json": json.dumps({"version": version}),
        "offline-update.json": capability(),
    }
    destination = tmp_path / ("standalone-" + version + ".zip")
    make_zip(destination, root_name, files)
    return destination


def portable_installation(tmp_path, version="v1.0.6"):
    root = tmp_path / "portable"
    dbcheck = pe_bytes("old-portable-dbcheck")
    launcher = pe_bytes("old-portable-launcher")
    marker = {
        "schema_version": 1,
        "layout": "shared-multi-platform",
        "version": version,
        "platforms": {
            "windows-x86_64": {
                "executable": "bin/windows-x86_64/dbcheck.exe",
                "sha256": digest(dbcheck),
                "launcher": "bin/windows-x86_64/DBPulse.exe",
                "launcher_sha256": digest(launcher),
            }
        },
        "required_platforms": ["windows-x86_64"],
        "complete": True,
    }
    write_tree(
        root,
        {
            "dbpulse-portable.json": json.dumps(marker),
            "version.json": json.dumps({"version": version}),
            "bin/windows-x86_64/dbcheck.exe": dbcheck,
            "bin/windows-x86_64/DBPulse.exe": launcher,
            "bin/windows-x86_64/_internal/app.txt": "old portable app",
            "modules/pro/rules/custom/user.yaml": "user rule",
            "modules/pro/rules/overrides.yaml": "user override",
            "data/secret.txt": "keep this",
        },
    )
    return root


def portable_package(tmp_path, version="v1.0.7"):
    root_name = "DBPulse-Portable-" + version
    dbcheck = pe_bytes("new-portable-dbcheck")
    launcher = pe_bytes("new-portable-launcher")
    marker = {
        "schema_version": 1,
        "layout": "shared-multi-platform",
        "version": version,
        "platforms": {
            "windows-x86_64": {
                "executable": "bin/windows-x86_64/dbcheck.exe",
                "sha256": digest(dbcheck),
                "launcher": "bin/windows-x86_64/DBPulse.exe",
                "launcher_sha256": digest(launcher),
            }
        },
        "required_platforms": ["windows-x86_64"],
        "complete": True,
        "data_directory": "data",
    }
    files = {
        "dbpulse-portable.json": json.dumps(marker),
        "version.json": json.dumps({"version": version}),
        "offline-update.json": capability(),
        "bin/windows-x86_64/dbcheck.exe": dbcheck,
        "bin/windows-x86_64/DBPulse.exe": launcher,
        "bin/windows-x86_64/_internal/app.txt": "new portable app",
        "runtime/windows-x86_64/jre/bin/java.exe": b"new java",
        "modules/pro/rules/default.yaml": "new rule",
        "web_templates/index.html": "new template",
    }
    destination = tmp_path / ("portable-" + version + ".zip")
    make_zip(destination, root_name, files)
    return destination


def test_prepare_standalone_package(tmp_path):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    prepared = updater.prepare(standalone_package(tmp_path))
    try:
        summary = prepared.summary()
        assert summary["mode"] == "standalone"
        assert summary["version"] == "v1.0.7"
        assert summary["current_version"] == "v1.0.6"
        assert summary["has_launcher"] is True
        assert prepared.helper_executable.name == "dbcheck.exe"
    finally:
        prepared.cleanup()


def test_prepare_portable_package(tmp_path):
    updater = OfflineUpdate(portable_installation(tmp_path))
    prepared = updater.prepare(portable_package(tmp_path))
    try:
        assert prepared.summary()["mode_label"] == "Windows 便携版"
        assert prepared.launcher_source == (
            prepared.root / "bin/windows-x86_64/DBPulse.exe"
        )
    finally:
        prepared.cleanup()


def test_runtime_state_file_is_rejected(tmp_path):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    archive = standalone_package(tmp_path)
    with zipfile.ZipFile(archive) as package:
        content = {name: package.read(name) for name in package.namelist()}
    prefix = next(iter(content)).split("/", 1)[0] + "/"
    content[prefix + "desktop_process.json"] = b"{}"
    with zipfile.ZipFile(archive, "w") as package:
        for name, value in content.items():
            package.writestr(name, value)
    with pytest.raises(OfflineUpdateError, match="运行时状态"):
        updater.prepare(archive)


def test_portable_package_without_java_runtime_is_rejected(tmp_path):
    updater = OfflineUpdate(portable_installation(tmp_path))
    archive = portable_package(tmp_path)
    prefix = "DBPulse-Portable-v1.0.7/"
    with zipfile.ZipFile(archive) as package:
        content = {
            name: package.read(name)
            for name in package.namelist()
            if not name.startswith(prefix + "runtime/")
        }
    with zipfile.ZipFile(archive, "w") as package:
        for name, value in content.items():
            package.writestr(name, value)
    with pytest.raises(OfflineUpdateError, match="Java"):
        updater.prepare(archive)


def test_apply_requires_callbacks_for_running_service(tmp_path):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    prepared = updater.prepare(standalone_package(tmp_path))
    try:
        with pytest.raises(OfflineUpdateError, match="停止和启动"):
            updater.apply(prepared, was_running=True)
    finally:
        prepared.cleanup()


def test_older_package_is_rejected(tmp_path):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    with pytest.raises(OfflineUpdateError, match="低于当前版本"):
        updater.prepare(standalone_package(tmp_path, version="v1.0.5"))


def test_package_without_update_capability_is_rejected(tmp_path):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    archive = standalone_package(tmp_path)
    with zipfile.ZipFile(archive) as package:
        content = {
            name: package.read(name)
            for name in package.namelist()
            if not name.endswith("/offline-update.json")
        }
    with zipfile.ZipFile(archive, "w") as package:
        for name, value in content.items():
            package.writestr(name, value)
    with pytest.raises(OfflineUpdateError, match="offline-update.json"):
        updater.prepare(archive)


def test_package_must_match_installation_type(tmp_path):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    with pytest.raises(OfflineUpdateError, match="安装类型与离线包不匹配"):
        updater.prepare(portable_package(tmp_path))


def test_runtime_data_is_rejected(tmp_path):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    archive = standalone_package(tmp_path)
    with zipfile.ZipFile(archive) as package:
        content = {name: package.read(name) for name in package.namelist()}
    prefix = next(iter(content)).split("/", 1)[0] + "/"
    content[prefix + "data/instances.db"] = b"should-not-be-in-package"
    with zipfile.ZipFile(archive, "w") as package:
        for name, value in content.items():
            package.writestr(name, value)
    with pytest.raises(OfflineUpdateError, match="运行时数据"):
        updater.prepare(archive)


@pytest.mark.parametrize(
    "name",
    [
        "../escape.exe",
        "/absolute.exe",
        "folder\\escape.exe",
        "folder/CON",
        "folder/name:stream",
    ],
)
def test_unsafe_zip_paths_are_rejected(tmp_path, name):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    archive = standalone_package(tmp_path)
    with zipfile.ZipFile(archive, "a") as package:
        package.writestr(name, b"bad")
    with pytest.raises(OfflineUpdateError):
        updater.prepare(archive)


def test_duplicate_member_is_rejected(tmp_path):
    updater = OfflineUpdate(standalone_installation(tmp_path))
    archive = standalone_package(tmp_path)
    with zipfile.ZipFile(archive) as package:
        name = next(
            item
            for item in package.namelist()
            if item.endswith("/dbcheck.exe")
        )
        value = package.read(name)
    with zipfile.ZipFile(archive, "a") as package:
        package.writestr(name, value)
    with pytest.raises(OfflineUpdateError, match="重复"):
        updater.prepare(archive)


def test_standalone_update_preserves_data_and_user_rules(tmp_path):
    installation = standalone_installation(tmp_path)
    updater = OfflineUpdate(installation)
    prepared = updater.prepare(standalone_package(tmp_path))
    result = updater.apply(prepared, was_running=False)

    assert result["version"] == "v1.0.7"
    assert result["launcher_update"] == ""
    assert (installation / "dbcheck.exe").read_bytes() == pe_bytes("new-dbcheck")
    assert (installation / "_internal/app.txt").read_text() == "new app"
    assert (installation / "data/secret.txt").read_text() == "keep this"
    assert (
        installation / "_internal/modules/pro/rules/custom/user.yaml"
    ).read_text() == "user rule"
    assert (
        installation / "_internal/modules/pro/rules/overrides.yaml"
    ).read_text() == "user override"
    assert json.loads((installation / "version.json").read_text())["version"] == "v1.0.7"
    transaction = json.loads(
        (Path(result["backup"]) / "transaction.json").read_text(encoding="utf-8")
    )
    assert transaction["state"] == "complete"
    assert (
        Path(result["backup"]) / "previous/_internal/app.txt"
    ).read_text() == "old app"
    assert not prepared.stage.exists()


def test_portable_update_preserves_shared_user_rules_and_marker(tmp_path):
    installation = portable_installation(tmp_path)
    updater = OfflineUpdate(installation)
    prepared = updater.prepare(portable_package(tmp_path))
    result = updater.apply(prepared, was_running=False)

    assert (installation / "bin/windows-x86_64/dbcheck.exe").read_bytes() == pe_bytes(
        "new-portable-dbcheck"
    )
    assert (
        installation / "runtime/windows-x86_64/jre/bin/java.exe"
    ).read_bytes() == b"new java"
    assert (installation / "web_templates/index.html").read_text() == "new template"
    assert (installation / "data/secret.txt").read_text() == "keep this"
    assert (
        installation / "modules/pro/rules/custom/user.yaml"
    ).read_text() == "user rule"
    marker = json.loads((installation / "dbpulse-portable.json").read_text())
    assert marker["version"] == "v1.0.7"
    assert marker["platforms"]["windows-x86_64"]["sha256"] == digest(
        pe_bytes("new-portable-dbcheck")
    )
    assert Path(result["backup"]).is_dir()


def test_failed_health_check_rolls_back_everything(tmp_path):
    installation = standalone_installation(tmp_path)
    updater = OfflineUpdate(installation)
    prepared = updater.prepare(standalone_package(tmp_path))
    with pytest.raises(OfflineUpdateError, match="已回滚"):
        updater.apply(
            prepared,
            was_running=True,
            stop=Mock(),
            start=Mock(),
            healthy=lambda: False,
            health_timeout=0.05,
            health_interval=0.01,
        )

    assert (installation / "dbcheck.exe").read_bytes() == pe_bytes("old-dbcheck")
    assert (installation / "DBPulse.exe").read_bytes() == pe_bytes("old-launcher")
    assert (installation / "_internal/app.txt").read_text() == "old app"
    assert (installation / "data/secret.txt").read_text() == "keep this"
    assert (
        installation / "_internal/modules/pro/rules/custom/user.yaml"
    ).read_text() == "user rule"
    assert json.loads((installation / "version.json").read_text())["version"] == "v1.0.6"


def test_handoff_writes_transaction_and_starts_staged_helper(tmp_path, monkeypatch):
    installation = standalone_installation(tmp_path)
    updater = OfflineUpdate(installation)
    prepared = updater.prepare(standalone_package(tmp_path))
    monkeypatch.setattr(updater, "_launcher_locked", lambda: True)
    popen = Mock()
    popen.return_value.poll.return_value = None
    monkeypatch.setattr("modules.desktop.offline_update.subprocess.Popen", popen)
    monkeypatch.setattr("modules.desktop.offline_update.time.sleep", lambda _: None)
    transaction = updater.handoff(
        prepared, launcher_pid=4321, was_running=True, progress=Mock()
    )
    data = json.loads(transaction.read_text(encoding="utf-8"))
    assert data["launcher_pid"] == 4321
    assert data["package_root"] == str(prepared.root)
    assert data["mode"] == "standalone"
    command = popen.call_args.args[0]
    assert command[0] == str(prepared.helper_executable)
    assert command[1] == "--offline-update-helper"
    assert command[2] == str(transaction)
    prepared.cleanup()


def test_helper_applies_staged_package_after_launcher_exit(tmp_path, monkeypatch):
    installation = standalone_installation(tmp_path)
    updater = OfflineUpdate(installation)
    prepared = updater.prepare(standalone_package(tmp_path))
    monkeypatch.setattr(updater, "_launcher_locked", lambda: True)
    popen = Mock()
    popen.return_value.poll.return_value = None
    monkeypatch.setattr("modules.desktop.offline_update.subprocess.Popen", popen)
    monkeypatch.setattr("modules.desktop.offline_update.time.sleep", lambda _: None)
    transaction = updater.handoff(
        prepared, launcher_pid=4321, was_running=True, progress=Mock()
    )

    started = Mock()
    monkeypatch.setattr(
        "modules.desktop.offline_update._wait_process_exit", lambda *args: True
    )
    monkeypatch.setattr(
        "modules.desktop.offline_update._wait_health", lambda *args: True
    )
    monkeypatch.setattr(
        "modules.desktop.offline_update._start_launcher", started
    )
    result = run_update_helper(transaction)

    assert result["version"] == "v1.0.7"
    assert (installation / "dbcheck.exe").read_bytes() == pe_bytes("new-dbcheck")
    assert (installation / "data/secret.txt").read_text() == "keep this"
    assert (
        installation / "_internal/modules/pro/rules/custom/user.yaml"
    ).read_text() == "user rule"
    started.assert_called_once_with(installation / "DBPulse.exe")
    prepared.cleanup()


def test_helper_requires_transaction_argument():
    assert helper_main(["dbcheck.exe"]) == 2
    assert helper_main(["dbcheck.exe", "--offline-update-helper"]) == 2
