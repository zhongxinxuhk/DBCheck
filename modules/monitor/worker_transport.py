# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Hard deadline and response limits for monitor driver workers."""

import json
import os
import subprocess
import sys
from tempfile import TemporaryFile

from modules.core.paths import PROJECT_ROOT
from modules.core.proc_registry import kill_process_tree, register_proc, unregister_proc

MAX_WORKER_OUTPUT = 24 * 1024 * 1024


def _worker_command():
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--jdbc-collect-cli']
    return [sys.executable, '-m', 'modules.monitor.jdbc_collect_cli']


def run_monitor_worker(payload, timeout):
    options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == 'nt' else {'start_new_session': True})
    env = {**os.environ, 'DBCheck_NO_GEVENT_PATCH': '1', 'PYTHONIOENCODING': 'utf-8'}
    with TemporaryFile() as stdin, TemporaryFile() as stdout:
        stdin.write(json.dumps(payload, ensure_ascii=True).encode('utf-8'))
        stdin.seek(0)
        proc = subprocess.Popen(_worker_command(), stdin=stdin, stdout=stdout,
                                stderr=subprocess.STDOUT, env=env,
                                cwd=PROJECT_ROOT, **options)
        register_proc(proc)
        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                kill_process_tree(proc)
                proc.wait(timeout=5)
                raise TimeoutError(f'监控采集子进程超时（{timeout:g}s）') from error
            if os.fstat(stdout.fileno()).st_size > MAX_WORKER_OUTPUT:
                raise ValueError('监控采集输出超过容量上限')
            stdout.seek(0)
            for line in reversed(stdout.read(MAX_WORKER_OUTPUT).splitlines()):
                if line.lstrip().startswith(b'{'):
                    return json.loads(line)
            raise RuntimeError(f'监控采集子进程无结果（退出码 {proc.returncode}）')
        finally:
            if proc.poll() is None:
                kill_process_tree(proc)
            unregister_proc(proc)
