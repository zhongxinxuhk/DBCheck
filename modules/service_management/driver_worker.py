# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Isolate native/JVM metadata collectors; credentials travel on stdin, never argv."""


import contextlib
import io
import json
import os
import subprocess
import sys
from modules.core.paths import PROJECT_ROOT


def isolated_collect(instance,asset):
    try:
        command = [sys.executable,'--service-collect-cli'] if getattr(sys,'frozen',False) else [sys.executable,'-m','modules.service_management.driver_worker']
        # 子进程与父进程统一用 UTF-8：Windows 中文环境下父进程默认 GBK，
        # 子进程若按 UTF-8（或 PYTHONUTF8=1）输出含中文的 JSON，
        # subprocess 的 reader 线程会抛 UnicodeDecodeError 直接打断采集。
        environment = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
        result = subprocess.run(command,
                                input=json.dumps({'instance':instance,'asset':asset}),text=True,
                                encoding='utf-8',errors='replace',env=environment,
                                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=300 if asset.get('_delivery_full_inventory') else 100,
                                cwd=str(PROJECT_ROOT),check=False)
        if result.returncode or len(result.stdout)>(64_000_000 if asset.get('_delivery_full_inventory') else 2_000_000):
            raise ValueError('采集进程异常退出或结果超限')
        value=json.loads(result.stdout)
        if not isinstance(value,dict):
            raise ValueError('采集进程未返回合法结果')
        return value
    except (subprocess.TimeoutExpired,ValueError,OSError):
        return {'connection':{'status':'blocked','reason':'隔离采集未完成：请检查驱动、传输配置和权限，最长100秒'}}


def main():
    # 冻结/开发两种入口都显式固定 UTF-8，避免中文资产名在 GBK 控制台乱码
    # 或写回时触发编码异常。reconfigure 失败（如测试用的 StringIO）则忽略。
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass
    try:
        payload=json.loads(sys.stdin.read(128_001))
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            from .inspection import collect
            result=collect(payload['instance'],payload['asset'],_worker=True)
    except Exception:
        result={'connection':{'status':'blocked','reason':'隔离采集输入或驱动初始化失败'}}
    sys.stdout.write(json.dumps(result,ensure_ascii=False,default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
