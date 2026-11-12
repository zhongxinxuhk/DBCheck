# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""DBCheck 共享启动引导模块（阶段4 · 方案 B）。

抽出 12 个 main_*.py 入口的公共 bootstrap，统一其启动约定：

* ``ensure_bootstrapped(db_type)`` —— 幂等地完成一次进程级启动引导
  （触发旧路径迁移 + 一次性日志配置）。这是 12 入口统一调用的起点。
* ``build_parser(description)`` —— 返回带公共参数的基础 ``argparse.ArgumentParser``，
  供各入口按需扩展自己的专有参数。

本模块不引入任何新的第三方依赖，仅依赖标准库与项目内的 ``core.paths``。

设计原则（与阶段4 一致）：
* 最小改动、100% 向后兼容；
* 不重写任何巡检 / 业务 / Web 逻辑；
* ``ensure_migrated`` 的覆盖由各入口在启动最早期调用 ``ensure_bootstrapped`` 保证。
"""

import argparse
import logging

from modules.core import paths


# 进程级哨兵：确保迁移与日志配置在整进程内只执行一次，
# 满足 ensure_bootstrapped 的「幂等」约定（第二次及以后调用直接返回）。
_BOOTSTRAPPED: bool = False


def ensure_bootstrapped(db_type: str) -> None:
    """幂等地完成一次进程级启动引导。

    职责：
      1. 调用 ``core.paths.ensure_migrated()`` 触发（幂等）旧路径自动迁移；
      2. 若 root logger 尚未配置 handler，则做一次性基础日志配置。

    多次调用安全：第二次及以后调用直接返回，不重复执行迁移 / 日志配置。

    :param db_type: 当前入口对应的数据库类型标识（如 ``'mysql'`` / ``'oracle'``），
                    仅用于日志诊断，不影响迁移行为。
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    # 1) 幂等自动迁移（异常降级由 paths.ensure_migrated 内部保证不阻断启动）
    paths.ensure_migrated()

    # 2) 一次性日志配置：仅在 root logger 尚未挂载任何 handler 时补全
    _configure_logging_once(db_type)


def _configure_logging_once(db_type: str) -> None:
    """若 root logger 尚未配置 handler，则挂载一个基础 StreamHandler。

    采用 conservative 默认配置（WARNING 以上输出到 stderr），避免与现有入口的
    print 风格冲突。``logging.basicConfig`` 自带「无 handler 才配置」的幂等语义，
    因此即便被多个入口重复调用也只生效一次。
    """
    tag = db_type or 'app'
    try:
        logging.basicConfig(
            level=logging.WARNING,
            format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
        )
    except Exception as exc:  # 日志配置失败不应阻断启动
        print('[boot:%s] 日志配置失败，已忽略: %s' % (tag, exc), flush=True)


def build_parser(description: str) -> argparse.ArgumentParser:
    """返回一个带公共参数的基础 ``argparse.ArgumentParser``。

    各入口可在返回对象上继续 ``add_argument`` 扩展自己的专有参数。
    公共参数覆盖 12 入口最常见的通用项：``--host`` / ``--port`` / ``--user`` / ``--db``。

    :param description: ArgumentParser 的描述文案。
    :return: 已添加公共参数的 argparse.ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--host', default='localhost', help='数据库主机地址')
    parser.add_argument('--port', type=int, default=None, help='数据库端口')
    parser.add_argument('--user', default=None, help='数据库用户名')
    parser.add_argument('--db', default=None, help='数据库名 / 租户名')
    return parser
