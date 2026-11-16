# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""本地存储加密与权限收紧的回归测试。"""

from __future__ import annotations

import json
import os
import sqlite3
import stat

import pytest

from modules.core import local_secrets, paths
from modules.pro.instance_manager import DatabaseInstance, InstanceManager


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    data = tmp_path / "data"
    pro = data / "pro_data"
    pro.mkdir(parents=True)
    monkeypatch.setattr(paths, "DATA_DIR", data)
    monkeypatch.setattr(paths, "PRO_DATA_DIR", pro)
    monkeypatch.setattr(paths, "DB_KEY_PATH", data / ".db_key")
    monkeypatch.setattr(paths, "DB_KEY_PATH_LEGACY", data / ".db_key.legacy")
    local_secrets._WARNED.clear()
    return data


def _sample_config():
    return {
        "ai": {
            "backend": "openai",
            "api_key": "sk-secret-value",
            "online_api_key": "sk-online-secret",
            "model": "gpt-4o-mini",
        },
        "bicqa": {"api_key": "bicqa-secret", "api_base": "https://api.bic-qa.com"},
        "mcp_server": {"token": "mcp-token", "pool_min": 1},
        "notification": {
            "webhook": {"url": "https://example.invalid/hook?token=webhook-secret"},
            "email": {"password": "_enc_keep-me"},
            "notification_encryption_key": "keep-plain-key",
        },
        "object_storage": {"_fernet_key": "keep-objstore-key", "secret_key": "_enc_keep-me"},
        "language": "zh",
    }


def test_config_secrets_are_encrypted_on_disk_but_plain_in_memory(isolated_paths):
    config_path = isolated_paths / "dbc_config.json"
    local_secrets.save_config(config_path, _sample_config())

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["ai"]["api_key"].startswith(local_secrets.PREFIX)
    assert raw["ai"]["online_api_key"].startswith(local_secrets.PREFIX)
    assert raw["bicqa"]["api_key"].startswith(local_secrets.PREFIX)
    assert raw["mcp_server"]["token"].startswith(local_secrets.PREFIX)
    assert raw["notification"]["webhook"]["url"].startswith(local_secrets.PREFIX)
    assert "sk-secret-value" not in config_path.read_text(encoding="utf-8")
    # 普通配置、既有加密格式与对象存储自有密钥不得被二次包装
    assert raw["ai"]["model"] == "gpt-4o-mini"
    assert raw["notification"]["email"]["password"] == "_enc_keep-me"
    assert raw["notification"]["notification_encryption_key"] == "keep-plain-key"
    assert raw["object_storage"]["_fernet_key"] == "keep-objstore-key"
    assert raw["object_storage"]["secret_key"] == "_enc_keep-me"

    loaded = local_secrets.load_config(config_path)
    assert loaded["ai"]["api_key"] == "sk-secret-value"
    assert loaded["ai"]["online_api_key"] == "sk-online-secret"
    assert loaded["bicqa"]["api_key"] == "bicqa-secret"
    assert loaded["mcp_server"]["token"] == "mcp-token"
    assert loaded["notification"]["webhook"]["url"].endswith("webhook-secret")


def test_migrate_config_is_idempotent(isolated_paths):
    config_path = isolated_paths / "dbc_config.json"
    config_path.write_text(json.dumps(_sample_config()), encoding="utf-8")

    assert local_secrets.migrate_config(config_path) is True
    first = json.loads(config_path.read_text(encoding="utf-8"))
    assert first["ai"]["api_key"].startswith(local_secrets.PREFIX)

    assert local_secrets.migrate_config(config_path) is False
    second = json.loads(config_path.read_text(encoding="utf-8"))
    assert second["ai"]["api_key"] == first["ai"]["api_key"]


def test_config_file_mode_is_restricted(isolated_paths):
    if os.name == "nt":
        pytest.skip("POSIX permissions only")
    config_path = isolated_paths / "dbc_config.json"
    local_secrets.save_config(config_path, _sample_config())
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_jdbc_url_is_encrypted_at_rest_but_plain_for_inspection(isolated_paths):
    manager = InstanceManager(str(isolated_paths / "pro_data"))
    jdbc_url = "jdbc:oracle:thin:scott/tiger@db.example:1521/ORCL"
    result = manager.add_instance(
        DatabaseInstance(
            id="inst-jdbc",
            name="jdbc",
            db_type="oracle_jdbc",
            host="db.example",
            port=1521,
            user="scott",
            password="tiger-password",
            jdbc_url=jdbc_url,
        )
    )
    assert result["ok"] is True

    with sqlite3.connect(manager.instances_db) as database:
        stored_url = database.execute(
            "SELECT jdbc_url FROM instances WHERE id=?", ("inst-jdbc",)
        ).fetchone()[0]
    assert stored_url.startswith("jdbc:enc:v1:")
    assert jdbc_url not in stored_url
    raw = (isolated_paths / "pro_data" / "instances.db").read_bytes()
    assert b"scott/tiger" not in raw

    # 内存/巡检消费方看到的仍是可直接建连的明文 URL
    loaded = manager.get_instance_decrypted("inst-jdbc")
    assert loaded["jdbc_url"] == jdbc_url

    # 历史明文行读入内存时兼容，下一次保存自动转为密文
    with sqlite3.connect(manager.instances_db) as database:
        database.execute(
            "UPDATE instances SET jdbc_url=? WHERE id=?", (jdbc_url, "inst-jdbc")
        )
        database.commit()
    manager._load_data()
    assert manager._instances["inst-jdbc"].jdbc_url == jdbc_url

    exported = manager.export_json()
    assert "jdbc_url" not in exported["instances"][0]


def test_harden_runtime_permissions(isolated_paths):
    if os.name == "nt":
        pytest.skip("POSIX permissions only")
    data = isolated_paths
    (data / "reports").mkdir()
    (data / ".db_key").write_text("key", encoding="utf-8")
    (data / "dbc_config.json").write_text("{}", encoding="utf-8")
    (data / "pro_data" / "instances.db").write_bytes(b"db")
    (data / "reports" / "r.html").write_text("report", encoding="utf-8")

    assert paths.harden_runtime_permissions(data) > 0
    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    assert stat.S_IMODE((data / "reports").stat().st_mode) == 0o700
    assert stat.S_IMODE((data / ".db_key").stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "dbc_config.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "pro_data" / "instances.db").stat().st_mode) == 0o600


def test_read_with_missing_key_does_not_create_new_key(isolated_paths):
    config_path = isolated_paths / "dbc_config.json"
    local_secrets.save_config(config_path, _sample_config())
    (isolated_paths / ".db_key").unlink()

    loaded = local_secrets.load_config(config_path)
    assert loaded["ai"]["api_key"].startswith(local_secrets.PREFIX)
    assert not (isolated_paths / ".db_key").exists()


def test_startup_migrates_legacy_plaintext_jdbc_url(isolated_paths):
    manager = InstanceManager(str(isolated_paths / "pro_data"))
    manager.add_instance(
        DatabaseInstance(
            id="legacy-jdbc",
            name="legacy",
            db_type="oracle_jdbc",
            host="db.example",
            port=1521,
            user="scott",
            password="tiger-password",
            jdbc_url="jdbc:oracle:thin:scott/tiger@db.example:1521/ORCL",
        )
    )
    with sqlite3.connect(manager.instances_db) as database:
        database.execute(
            "UPDATE instances SET jdbc_url=? WHERE id=?",
            ("jdbc:oracle:thin:scott/tiger@db.example:1521/ORCL", "legacy-jdbc"),
        )
        database.commit()

    migrated = InstanceManager(str(isolated_paths / "pro_data"))
    with sqlite3.connect(migrated.instances_db) as database:
        stored = database.execute(
            "SELECT jdbc_url FROM instances WHERE id=?", ("legacy-jdbc",)
        ).fetchone()[0]
    assert stored.startswith("jdbc:enc:v1:")
    assert migrated.get_instance_decrypted("legacy-jdbc")["jdbc_url"].endswith("/ORCL")


def test_scheduler_jobs_sensitive_fields_are_encrypted(isolated_paths, monkeypatch):
    from modules.web import scheduler

    jobs_file = isolated_paths / "scheduler_jobs.json"
    monkeypatch.setattr(scheduler, "CONFIG_FILE", str(jobs_file))
    jobs = [{
        "id": "job-1",
        "name": "nightly",
        "db_info": {
            "host": "db.example",
            "password": "db-password",
            "ssh_password": "ssh-password",
            "jdbc_url": "jdbc:oracle:thin:scott/tiger@db.example:1521/ORCL",
        },
    }]
    scheduler._save_jobs(jobs)

    raw_text = jobs_file.read_text(encoding="utf-8")
    assert "db-password" not in raw_text
    assert "ssh-password" not in raw_text
    assert "scott/tiger" not in raw_text
    loaded = scheduler._load_jobs()
    assert loaded[0]["db_info"]["password"] == "db-password"
    assert loaded[0]["db_info"]["jdbc_url"].endswith("/ORCL")


def test_legacy_instances_json_jdbc_url_is_migrated(isolated_paths):
    pro = isolated_paths / "pro_data"
    legacy = {
        "instances": [{
            "id": "legacy-1",
            "name": "legacy",
            "db_type": "oracle",
            "host": "db.example",
            "port": 1521,
            "user": "scott",
            "jdbc_url": "jdbc:oracle:thin:scott/tiger@db.example:1521/ORCL",
        }]
    }
    path = pro / "instances.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    manager = InstanceManager(str(pro))

    raw = path.read_text(encoding="utf-8")
    assert "scott/tiger" not in raw
    assert "jdbc:enc:v1:" in raw
    instance = manager.get_instance("legacy-1")
    assert instance is not None
    assert instance["jdbc_url"].endswith("/ORCL")


def test_jdbc_url_decrypt_failure_keeps_ciphertext(isolated_paths):
    from modules.pro.instance_manager import (
        _decrypt_jdbc_url,
        _encrypt_jdbc_url,
    )

    # 先生成密钥并得到密文，然后模拟密钥丢失。
    InstanceManager(str(isolated_paths / "pro_data"))
    cipher = _encrypt_jdbc_url("jdbc:oracle:thin:scott/tiger@db.example:1521/ORCL")
    for name in (".db_key", ".db_key.legacy"):
        try:
            (isolated_paths / name).unlink()
        except FileNotFoundError:
            pass
    assert _decrypt_jdbc_url(cipher) == cipher
