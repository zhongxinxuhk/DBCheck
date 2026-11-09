# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Non-default initial credentials for the installation administrator.

强制默认口令轮换（/security/rotate-password + 激活码）已按产品要求整条移除：
它只拦住了自己人，没有增加实际安全性。这里仅保留安装期生成随机初始口令的能力。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


import os
import secrets
from modules.core import paths


def initial_admin_password():
    configured = os.environ.get('DBPULSE_INITIAL_ADMIN_PASSWORD')
    if configured:
        if len(configured)<16:
            raise ValueError('初始管理员口令至少16位')
        return configured
    target = paths.DATA_DIR/'bootstrap_admin_password'
    target.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd = os.open(target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        existing = target.read_text().strip()
        if len(existing)<16:
            raise ValueError('初始化口令文件不完整，请由安装管理员检查')
        return existing
    password = secrets.token_urlsafe(24)
    with os.fdopen(fd,'w') as stream:
        stream.write(password+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    return password
