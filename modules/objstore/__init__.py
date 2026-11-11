# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""对象存储（S3 兼容）备份能力：编目 / 打包 / 上传 / 还原 / 周期调度。

模块划分
--------
``s3_client``  SigV4 签名的极简 S3 客户端（AWS S3、MinIO、OSCA fgws3 等通用）
``store``      配置读写（``dbc_config.json`` → ``object_storage``）与凭据加密
``crypto``     备份包口令加密（AES-256-GCM 分块流式，自带命令行）
``engine``     内容编目、tar.gz 打包、上传、保留份数、还原、历史
``scheduler``  进程内守护线程，按间隔分钟自动触发
``routes``     REST 蓝图 ``/api/objstore``，供 Web UI 的「数据管理」页调用
"""

from modules.objstore import engine, store  # noqa: F401

__all__ = ["engine", "store"]
