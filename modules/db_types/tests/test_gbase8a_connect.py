# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""GBase 8a 直连 TLS 策略回归测试。

默认保持「直连必须提供可信 CA」；只有显式开启 ``gbase8a_allow_plaintext``
才允许与 Navicat 默认一致的明文直连，且必须能持久化到数据源库。
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from modules.core import paths
from modules.db_types.compatibility import connect_gbase8a, plaintext_allowed
from modules.pro.instance_manager import DatabaseInstance, InstanceManager


def _data(**extra):
    return {
        'host': '10.60.25.101',
        'port': 5258,
        'user': 'reader',
        'password': 'secret',
        **extra,
    }


@pytest.fixture()
def fake_pymysql(monkeypatch):
    """pymysql 未必装在本地环境，注入替身避免依赖原生驱动。"""
    module = MagicMock()
    module.connect.return_value = MagicMock()
    module.Connection.return_value = MagicMock()
    monkeypatch.setitem(sys.modules, 'pymysql', module)
    return module


@pytest.mark.parametrize('value,expected', [
    (1, True), ('1', True), ('true', True), ('TRUE', True), ('yes', True), ('on', True),
    (0, False), ('0', False), ('', False), (None, False), ('false', False),
])
def test_plaintext_allowed_only_accepts_explicit_truthy(value, expected):
    assert plaintext_allowed(value) is expected


def test_direct_connection_without_ca_is_still_blocked(fake_pymysql):
    """默认配置绝不能静默降级为明文。"""
    with pytest.raises(ValueError, match='可信CA'):
        connect_gbase8a(_data())
    fake_pymysql.connect.assert_not_called()


def test_direct_connection_with_explicit_plaintext_flag(fake_pymysql):
    connection = connect_gbase8a(_data(gbase8a_allow_plaintext=1))

    assert connection is fake_pymysql.connect.return_value
    kwargs = fake_pymysql.connect.call_args.kwargs
    assert kwargs['host'] == '10.60.25.101'
    assert kwargs['port'] == 5258
    # 显式明文分支不得再叠加 TLS，否则服务端未启用 SSL 时仍会握手失败
    assert 'ssl' not in kwargs
    assert 'defer_connect' not in kwargs


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    pro = data / 'pro_data'
    pro.mkdir(parents=True)
    monkeypatch.setattr(paths, 'DATA_DIR', data)
    monkeypatch.setattr(paths, 'PRO_DATA_DIR', pro)
    monkeypatch.setattr(paths, 'DB_KEY_PATH', data / '.db_key')
    monkeypatch.setattr(paths, 'DB_KEY_PATH_LEGACY', data / '.db_key.legacy')
    return pro


def test_instance_manager_persists_plaintext_flag(isolated_paths):
    manager = InstanceManager(str(isolated_paths))
    result = manager.add_instance(DatabaseInstance(
        id='gbase8a-plain',
        name='gbase8a-plain',
        db_type='gbase8a',
        host='10.60.25.101',
        port=5258,
        user='reader',
        password='secret',
        gbase8a_allow_plaintext=1,
    ))
    assert result['ok'] is True

    # 重新打开连接，确认列已迁移且值可读（旧库升级路径同样走 _ensure_instance_columns）
    reopened = InstanceManager(str(isolated_paths))
    instance = reopened.get_instance_decrypted('gbase8a-plain')
    assert instance is not None
    assert int(instance['gbase8a_allow_plaintext']) == 1
