# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Reused process IDs must not prevent a frozen backend from restarting."""


import ast
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.mark.parametrize('legacy,same_executable,same_start,blocked', [
    (True,False,True,False),
    (True,True,True,True),
    (False,True,False,False),
    (False,True,True,True),
])
def test_reused_pid_lock_is_reclaimed(tmp_path,legacy,same_executable,same_start,blocked):
    source = Path(__file__).resolve().parents[3] / 'web_ui.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_acquire_single_instance')
    namespace = {'os':os,'sys':SimpleNamespace(frozen=True,executable=str(tmp_path/'dbcheck.exe'))}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
    lock = tmp_path / '.dbcheck.lock'
    lock.write_text('12345' if legacy else json.dumps({'pid':12345,'started':10}))
    previous = SimpleNamespace(exe=lambda:str(tmp_path/('dbcheck.exe' if same_executable else 'unrelated.exe')),
        create_time=lambda:10 if same_start else 20,is_running=lambda:True)
    current = SimpleNamespace(create_time=lambda:30)
    fake = SimpleNamespace(Process=lambda pid=None: previous if pid is not None else current)
    with patch.dict(sys.modules,{'psutil':fake}), patch('atexit.register'), patch.object(os,'_exit',side_effect=SystemExit(0)):
        if blocked:
            with pytest.raises(SystemExit):namespace['_acquire_single_instance']()
        else:
            namespace['_acquire_single_instance']()
            assert json.loads(lock.read_text())['pid']==os.getpid()
            assert json.loads(lock.read_text())['started']==30
