# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Isolate native/JVM metadata collectors; credentials travel on stdin, never argv."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import contextlib
import io
import json
import subprocess
import sys
from modules.core.paths import PROJECT_ROOT


def isolated_collect(instance,asset):
    try:
        command = [sys.executable,'--service-collect-cli'] if getattr(sys,'frozen',False) else [sys.executable,'-m','modules.service_management.driver_worker']
        result = subprocess.run(command,
                                input=json.dumps({'instance':instance,'asset':asset}),text=True,
                                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=100,
                                cwd=str(PROJECT_ROOT),check=False)
        if result.returncode or len(result.stdout)>2_000_000:
            raise ValueError('采集进程异常退出或结果超限')
        value=json.loads(result.stdout)
        if not isinstance(value,dict):
            raise ValueError('采集进程未返回合法结果')
        return value
    except (subprocess.TimeoutExpired,ValueError,OSError):
        return {'connection':{'status':'blocked','reason':'隔离采集未完成：请检查驱动、传输配置和权限，最长100秒'}}


def main():
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
