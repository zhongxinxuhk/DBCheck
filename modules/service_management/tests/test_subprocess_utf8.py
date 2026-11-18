# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""隔离采集子进程的 UTF-8 传输回归测试（不真正 spawn 子进程）。"""

from modules.service_management import driver_worker


def test_isolated_collect_forces_utf8_on_parent_and_child(monkeypatch):
    captured = {}

    class Result:
        returncode = 0
        stdout = '{"connection": {"status": "ok"}}'

    def fake_run(command, **kwargs):
        captured['command'] = command
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(driver_worker.subprocess, 'run', fake_run)

    result = driver_worker.isolated_collect(
        {'host': 'db-1', 'port': 5258, 'user': 'reader'},
        {'name': '中文资产名'},
    )

    assert result == {'connection': {'status': 'ok'}}
    assert captured['encoding'] == 'utf-8'
    assert captured['errors'] == 'replace'
    assert captured['env']['PYTHONIOENCODING'] == 'utf-8'
    assert captured['env']['PYTHONUTF8'] == '1'
    assert captured['text'] is True
