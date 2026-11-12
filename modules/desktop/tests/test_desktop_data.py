# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Regression coverage for desktop backup integrity, recovery and log streaming."""


import base64
import json
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from modules.desktop.local_backup import LocalBackup, RecoveryRequired
from modules.desktop.log_output import LogOutput
from modules.desktop.windows_launcher import BackendProcess


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "app"
    data = root / "data"
    data.mkdir(parents=True)
    (data / ".db_key").write_bytes(base64.urlsafe_b64encode(b"k" * 32))
    (data / "config.json").write_text('{"value": "original"}')
    (data / "desktop_process.json").write_text('{"pid": 1}')
    with sqlite3.connect(data / "accounts.db") as database:
        database.execute("CREATE TABLE accounts (password TEXT)")
        database.execute("INSERT INTO accounts VALUES (?)", ("test-original-password",))
    rules = root / "modules/pro/rules/custom"
    rules.mkdir(parents=True)
    (rules / "test.yaml").write_text("original rule")
    (rules.parent / "overrides.yaml").write_text("original override")
    return root


def rewrite_archive(path, change):
    with zipfile.ZipFile(path) as source:
        content = {name: source.read(name) for name in source.namelist()}
    change(content)
    with zipfile.ZipFile(path, "w") as destination:
        for name, value in content.items():
            destination.writestr(name, value)


def test_backup_round_trip_and_previous_data(installation, tmp_path):
    backup = LocalBackup(installation)
    archive = tmp_path / "backup.zip"
    backup.export(archive)
    (backup.data / "config.json").write_text("changed")
    (backup.data / ".db_key").write_bytes(b"changed")
    (backup.scopes["custom_rules"] / "test.yaml").write_text("changed")
    before = Mock()
    result = backup.restore(archive, before_apply=before)
    before.assert_called_once()
    assert json.loads((backup.data / "config.json").read_text())["value"] == "original"
    assert (backup.data / ".db_key").read_bytes() == base64.urlsafe_b64encode(b"k" * 32)
    assert (backup.scopes["custom_rules"] / "test.yaml").read_text() == "original rule"
    with sqlite3.connect(backup.data / "accounts.db") as database:
        assert database.execute("SELECT password FROM accounts").fetchone() == (
            "test-original-password",
        )
    previous = Path(result["previous_data"])
    assert (previous / "data/config.json").read_text() == "changed"
    assert (previous / "recovery.json").is_file()
    with zipfile.ZipFile(archive) as contents:
        assert "data/desktop_process.json" not in contents.namelist()


def test_sqlite_wal_snapshot(installation, tmp_path):
    archive = tmp_path / "backup.zip"
    database = sqlite3.connect(installation / "data/accounts.db")
    try:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("INSERT INTO accounts VALUES ('committed')")
        database.commit()
        database.execute("INSERT INTO accounts VALUES ('uncommitted')")
        LocalBackup(installation).export(archive)
    finally:
        database.close()
    with zipfile.ZipFile(archive) as contents:
        assert not any(name.endswith(("-wal", "-shm")) for name in contents.namelist())
        contents.extract("data/accounts.db", tmp_path / "snapshot")
    with sqlite3.connect(tmp_path / "snapshot/data/accounts.db") as snapshot:
        assert snapshot.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 2


@pytest.mark.parametrize(
    "name",
    [
        "data/../escaped",
        "data/CON",
        "data/name:stream",
        "data/name.",
        "data/C:\\escape",
        "/data/outside",
    ],
)
def test_invalid_paths_leave_service_and_data_untouched(installation, tmp_path, name):
    archive = tmp_path / "backup.zip"
    backup = LocalBackup(installation)
    backup.export(archive)

    def change(content):
        manifest = json.loads(content["manifest.json"])
        old = manifest["files"][0]["path"]
        manifest["files"][0]["path"] = name
        content[name] = content.pop(old)
        content["manifest.json"] = json.dumps(manifest)

    rewrite_archive(archive, change)
    stop = Mock()
    with pytest.raises(ValueError):
        backup.restore(archive, before_apply=stop)
    stop.assert_not_called()
    assert (backup.data / "config.json").is_file()


def test_checksum_rejected_before_stopping(installation, tmp_path):
    archive = tmp_path / "backup.zip"
    backup = LocalBackup(installation)
    backup.export(archive)
    rewrite_archive(
        archive,
        lambda content: content.update(
            {"data/config.json": b"x" * len(content["data/config.json"])}
        ),
    )
    stop = Mock()
    with pytest.raises(ValueError, match="校验失败"):
        backup.restore(archive, before_apply=stop)
    stop.assert_not_called()


def test_failed_swap_rolls_back_all_scopes(installation, tmp_path):
    backup = LocalBackup(installation)
    archive = tmp_path / "backup.zip"
    backup.export(archive)
    (backup.data / "config.json").write_text("current")
    rename = Path.rename

    def fail_rules(path, target):
        if ".dbpulse-rule-restore-" in str(path) and path.name == "custom":
            raise OSError("injected failure")
        return rename(path, target)

    with patch.object(Path, "rename", fail_rules), pytest.raises(
        OSError, match="injected"
    ):
        backup.restore(archive)
    assert (backup.data / "config.json").read_text() == "current"
    assert (backup.scopes["custom_rules"] / "test.yaml").is_file()


def test_restore_can_replace_current_corrupt_database(installation, tmp_path):
    backup = LocalBackup(installation)
    archive = tmp_path / "backup.zip"
    backup.export(archive)
    (backup.data / "accounts.db").write_bytes(b"broken")
    result = backup.restore(archive)
    assert (
        Path(result["previous_data"]) / "data/accounts.db"
    ).read_bytes() == b"broken"
    with sqlite3.connect(backup.data / "accounts.db") as database:
        assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_export_preserves_existing_destination_on_failure(installation, tmp_path):
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"previous backup")
    with patch(
        "modules.desktop.local_backup._hash", side_effect=OSError("failure")
    ), pytest.raises(OSError):
        LocalBackup(installation).export(destination)
    assert destination.read_bytes() == b"previous backup"


@pytest.mark.parametrize("running", [False, True])
def test_import_restores_previous_running_state(installation, running):
    backend = BackendProcess(installation)
    backend.owned_running = Mock(return_value=running)
    backend.healthy = Mock(return_value=False)
    backend.stop = Mock()
    backend.start = Mock()

    def restore(source, progress, before_apply):
        before_apply()
        return {"files": 1}

    with patch.object(LocalBackup, "restore", side_effect=restore):
        assert backend.import_backup("backup.zip", Mock()) == {"files": 1}
    assert backend.stop.call_count == int(running)
    assert backend.start.call_count == int(running)


def test_external_service_is_not_stopped(installation):
    backend = BackendProcess(installation)
    backend.owned_running = Mock(return_value=False)
    backend.healthy = Mock(return_value=True)
    backend.stop = Mock()

    def restore(source, progress, before_apply):
        before_apply()

    with patch.object(LocalBackup, "restore", side_effect=restore), pytest.raises(
        RuntimeError, match="其他启动方式"
    ):
        backend.import_backup("backup.zip", Mock())
    backend.stop.assert_not_called()


def test_incomplete_rollback_does_not_restart(installation):
    backend = BackendProcess(installation)
    backend.owned_running = Mock(return_value=True)
    backend.stop = Mock()
    backend.start = Mock()

    def restore(source, progress, before_apply):
        before_apply()
        raise RecoveryRequired("failure")

    with patch.object(LocalBackup, "restore", side_effect=restore), pytest.raises(
        RecoveryRequired
    ):
        backend.import_backup("backup.zip", Mock())
    backend.start.assert_not_called()


def test_logs_append_utf8_rotation_and_missing(tmp_path):
    path = tmp_path / "server.log"
    reader = LogOutput(path)
    assert reader.read() == ""
    text = "中文启动日志\n".encode()
    path.write_bytes(text[:2])
    assert reader.read() == ""
    with path.open("ab") as output:
        output.write(text[2:])
    assert reader.read() == "中文启动日志\n"
    assert reader.read() == ""
    path.rename(tmp_path / "old.log")
    path.write_bytes(b"\x1b[32mready\x1b[0m\r\n")
    assert reader.read() == "[日志已切换]\nready\n"
    path.write_bytes(b"x")
    assert reader.read() == "[日志已切换]\nx"


def test_logs_start_with_bounded_recent_output(tmp_path):
    path = tmp_path / "server.log"
    path.write_bytes(b"old line\n" * 100 + b"last line\n")
    reader = LogOutput(path, chunk_size=40)
    output = reader.read()
    assert len(output) <= 40
    assert output.endswith("last line\n")


def test_encrypted_datasource_password_survives_restore(installation, tmp_path):
    from cryptography.fernet import Fernet

    backup = LocalBackup(installation)
    key = (backup.data / ".db_key").read_bytes()
    secret = b"fixture-database-password"
    with sqlite3.connect(backup.data / "instances.db") as database:
        database.execute("CREATE TABLE instances (password TEXT)")
        database.execute(
            "INSERT INTO instances VALUES (?)", (Fernet(key).encrypt(secret).decode(),)
        )
    archive = tmp_path / "backup.zip"
    backup.export(archive)
    (backup.data / ".db_key").write_bytes(Fernet.generate_key())
    backup.restore(archive)
    with sqlite3.connect(backup.data / "instances.db") as database:
        token = database.execute("SELECT password FROM instances").fetchone()[0]
    assert (
        Fernet((backup.data / ".db_key").read_bytes()).decrypt(token.encode()) == secret
    )


def test_bad_key_is_rejected_before_service_stop(installation, tmp_path):
    backup = LocalBackup(installation)
    (backup.data / ".db_key").write_bytes(b"invalid")
    archive = tmp_path / "backup.zip"
    backup.export(archive)
    stop = Mock()
    with pytest.raises(ValueError, match="加密密钥"):
        backup.restore(archive, before_apply=stop)
    stop.assert_not_called()


def test_rollback_failure_preserves_previous_data_for_recovery(installation, tmp_path):
    backup = LocalBackup(installation)
    archive = tmp_path / "backup.zip"
    backup.export(archive)
    (backup.data / "config.json").write_text("current")
    rename = Path.rename

    def fail_swap_and_rollback(path, target):
        if (".dbpulse-rule-restore-" in str(path) and path.name == "custom") or (
            path.name == "data" and path.parent.name.startswith("before-import-")
        ):
            raise OSError("injected failure")
        return rename(path, target)

    with patch.object(Path, "rename", fail_swap_and_rollback), pytest.raises(
        RecoveryRequired
    ):
        backup.restore(archive)
    copies = list((installation / "backups").glob("before-import-*/data/config.json"))
    assert len(copies) == 1
    assert copies[0].read_text() == "current"


def test_backup_verification_checks_encrypted_key_match(installation, tmp_path):
    from cryptography.fernet import Fernet
    from modules.desktop.maintenance import Maintenance

    key = (installation / "data/.db_key").read_bytes()
    with sqlite3.connect(installation / "data/instances.db") as database:
        database.execute("CREATE TABLE instances (password TEXT)")
        database.execute(
            "INSERT INTO instances VALUES (?)",
            (base64.b64encode(Fernet(key).encrypt(b"fixture-password")).decode(),),
        )
    archive = tmp_path / "backup.zip"
    LocalBackup(installation).export(archive)
    maintenance = Maintenance(installation)
    assert maintenance.verification(archive)["verified"]
    assert maintenance.state["last_verified"]
    (installation / "data/.db_key").write_bytes(Fernet.generate_key())
    LocalBackup(installation).export(archive)
    with pytest.raises(ValueError, match="密钥不匹配"):
        LocalBackup(installation).verify(archive)


def test_automatic_backup_retention_and_persisted_schedule(installation):
    from datetime import datetime, timezone
    from modules.desktop.maintenance import Maintenance

    maintenance = Maintenance(installation)
    maintenance.save(
        {
            "automatic_backup": True,
            "retention": 1,
            "backup_frequency": "daily",
            "backup_time": "02:00",
        }
    )
    assert maintenance.due(datetime(2026, 11, 12, 3, tzinfo=timezone.utc))
    first = Path(maintenance.automatic_backup())
    second = Path(maintenance.automatic_backup())
    assert second.is_file()
    assert not first.exists()
    restored = Maintenance(installation)
    assert restored.settings["retention"] == 1
    assert restored.state["last_success"]
    assert restored.state["history"][0]["verified"]
    assert not restored.due(datetime.fromisoformat(restored.state["last_success"]))


def test_automatic_backup_failure_preserves_previous_copy(installation):
    from modules.desktop.maintenance import Maintenance

    maintenance = Maintenance(installation)
    previous = Path(maintenance.automatic_backup())
    with patch.object(
        LocalBackup, "verify", side_effect=ValueError("fixture validation failure")
    ), pytest.raises(ValueError):
        maintenance.automatic_backup()
    assert previous.is_file()
    assert maintenance.state["last_error"] == "fixture validation failure"


def test_weekly_backup_only_due_after_scheduled_monday(installation):
    from datetime import datetime, timezone
    from modules.desktop.maintenance import Maintenance

    maintenance = Maintenance(installation)
    maintenance.save(
        {"automatic_backup": True, "backup_frequency": "weekly", "backup_time": "02:00"}
    )
    maintenance.state["last_success"] = "2026-11-09T03:00:00+00:00"
    assert not maintenance.due(datetime(2026, 11, 15, 23, tzinfo=timezone.utc))
    assert not maintenance.due(datetime(2026, 11, 16, 1, tzinfo=timezone.utc))
    assert maintenance.due(datetime(2026, 11, 16, 3, tzinfo=timezone.utc))


def test_crash_recovery_stops_at_limit_and_respects_user_stop():
    from modules.desktop.maintenance import CrashRecovery, DEFAULTS

    settings = {
        **DEFAULTS,
        "restart_enabled": True,
        "restart_limit": 2,
        "restart_delay": 5,
    }
    recovery = CrashRecovery()
    recovery.intentional(True)
    assert "异常停止" in recovery.observe(False, False, settings, now=10)
    assert recovery.observe(False, False, settings, now=14) == ""
    assert recovery.observe(False, False, settings, now=15) == "restart"
    assert recovery.observe(False, False, settings, now=20) == "restart"
    assert "次数限制" in recovery.observe(False, False, settings, now=25)
    assert recovery.observe(False, False, settings, now=30) == ""
    recovery.intentional(False)
    assert recovery.observe(False, False, settings, now=40) == ""


def test_crash_recovery_can_reset_after_stable_running():
    from modules.desktop.maintenance import CrashRecovery, DEFAULTS

    recovery = CrashRecovery()
    recovery.intentional(True)
    recovery.attempts = 2
    recovery.observe(True, True, DEFAULTS, now=10)
    recovery.observe(True, True, DEFAULTS, now=311)
    assert recovery.attempts == 0


def test_crash_recovery_waits_again_after_successful_restart():
    from modules.desktop.maintenance import CrashRecovery, DEFAULTS

    recovery = CrashRecovery()
    recovery.intentional(True)
    settings = {**DEFAULTS, "restart_enabled": True, "restart_delay": 5}
    recovery.observe(False, False, settings, now=10)
    assert recovery.observe(False, False, settings, now=15) == "restart"
    recovery.observe(True, True, settings, now=20)
    assert "准备自动重启" in recovery.observe(False, False, settings, now=30)
    assert recovery.observe(False, False, settings, now=34) == ""
    assert recovery.observe(False, False, settings, now=35) == "restart"


def test_business_groups_backup_validates_its_encryption_key(installation, tmp_path):
    from cryptography.fernet import Fernet

    key = (installation / "data/.db_key").read_bytes()
    with sqlite3.connect(installation / "data/operations.db") as database:
        database.execute("CREATE TABLE operations_groups (payload BLOB)")
        database.execute(
            "INSERT INTO operations_groups VALUES (?)",
            (Fernet(key).encrypt(b'{"name":"fixture business group"}'),),
        )
    backup = LocalBackup(installation)
    archive = tmp_path / "groups.zip"
    backup.export(archive)
    assert backup.verify(archive)["verified"]
    (installation / "data/.db_key").write_bytes(Fernet.generate_key())
    backup.export(archive)
    with pytest.raises(ValueError, match="密钥不匹配"):
        backup.verify(archive)
