#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

from modules.config.version import __version__ as VER
from i18n import get_lang, t as _t
from modules.inspection.engine import BaseInspectionEngine
from modules.inspection import kingbase_compat
from modules.core import entry

"""
KingbaseES 数据库自动化健康巡检工具 {VER}
支持 KingbaseES V8/V9 的 PostgreSQL 兼容内核（V8R2–V9R4）
依赖: psycopg2-binary, python-docx, docxtpl, openpyxl, psutil, paramiko>=2.8,<2.10
注意: KingbaseES 是 PostgreSQL 兼容的国产数据库，使用 psycopg2 驱动；
V7 及更早版本采用非 PostgreSQL 兼容内核，不在本入口的模板范围内。
"""

import warnings
warnings.filterwarnings("ignore")

# KingbaseES 驱动 (使用 psycopg2，因为 KingbaseES 基于 PostgreSQL 兼容)
try:
    import psycopg2 as kingbase_driver
    KINGBASE_DRIVER = 'psycopg2'
except ImportError:
    print("KingbaseES 驱动缺失: 请安装 psycopg2-binary")
    print("  pip install psycopg2-binary")
    import sys
    sys.exit(1)

import sys
import datetime
import argparse
import subprocess
import logging
import logging.handlers
import socket
import re
import time
from pathlib import Path
import getpass
import docx
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.shared import Cm
from docxtpl import DocxTemplate
import modules.config as configparser
import importlib
import json
import hashlib
import base64
from datetime import datetime, timedelta
import platform
import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
import tempfile
import io
import psutil
import shutil
import paramiko


class KingbaseESInspector(BaseInspectionEngine):
    """
    KingbaseES 巡检引擎
    继承 BaseInspectionEngine，只需实现 connect() 和 get_template_id()
    """
    
    def __init__(self, host, port, user, password, database='kingbase', ssh_info=None, template_id=None, driver_version=''):
        super().__init__(host, port, user, password, database, ssh_info, template_id)
        self.db_type = 'kingbase'  # 设置数据库类型
        self._lang = get_lang()
        self.driver_version = driver_version or ''

    def connect(self):
        """
        连接 KingbaseES 数据库（统一 JDBC 优先，回退 psycopg2）。

        KingbaseES V8 官方驱动 com.kingbase8.jdbc.Driver / jdbc:kingbase8://；
        JDBC 驱动缺失或 JVM 异常时回退 psycopg2（子进程未 patch，安全）。
        """
        try:
            import os as _os
            from modules.jdbc_connector import open_jdbc_connection
            from modules.core.paths import PROJECT_ROOT
            _conn, _meta = open_jdbc_connection(
                'kingbase', self.host, int(self.port),
                user=self.user, password=self.password,
                database=self.database or 'kingbase',
                driver_version=self.driver_version,
                fallback_dirs=[_os.path.join(str(PROJECT_ROOT), 'drivers', 'kingbase')],
            )
            if _conn is None:
                return self._connect_native((_meta or {}).get('error') or 'JDBC 连接失败')
            self.conn = _conn
            self.cursor = self.conn.cursor()

            # 获取 KingbaseES 版本与 PostgreSQL 兼容内核版本
            version = self._read_version()
            self.context['version'] = [{'version': version}]

            print("KingbaseES 连接成功: {}:{}".format(self.host, self.port))
            return True, version

        except Exception as e:
            return self._connect_native(str(e))

    def _connect_native(self, reason=''):
        """回退：psycopg2（KingbaseES 兼容 PG 协议）。"""
        try:
            self.conn = kingbase_driver.connect(
                host=self.host,
                port=int(self.port),
                user=self.user,
                password=self.password,
                database=self.database or 'kingbase',
                connect_timeout=10
            )
            self.cursor = self.conn.cursor()

            # 获取 KingbaseES 版本与 PostgreSQL 兼容内核版本
            version = self._read_version()
            self.context['version'] = [{'version': version}]

            print(f"KingbaseES psycopg2 回退连接成功: {self.host}:{self.port}（JDBC: {reason[:80]}...）" if reason else f"KingbaseES 连接成功: {self.host}:{self.port}")
            return True, version
        except Exception as e2:
            err_msg = str(e2)
            print("KingbaseES 连接失败: {}".format(err_msg))
            return False, (f'{reason}；psycopg2 回退也失败: {err_msg}' if reason else err_msg)

    def _read_version(self):
        """读取 banner 和 PG 兼容内核；探测失败时仍保留原始 version()。"""
        try:
            version, _parsed = kingbase_compat.fetch_kingbase_version(
                self.cursor)
            if version:
                return version
        except Exception:
            pass
        self.cursor.execute("SELECT version()")
        row = self.cursor.fetchone()
        return row[0] if row else ''

# ── 保留原有 API 兼容性（供 web_ui.py 旧代码调用）────────────────────
def getData(ip, port, user, password, database='kingbase', ssh_info=None, label=None, template_id=None, driver_version=''):
    inspector = KingbaseESInspector(ip, port, user, password, database, ssh_info, template_id, driver_version)
    ok, ver = inspector.connect()
    if not ok:
        return None
    class CompatWrapper:
        def __init__(self, inspector):
            self.inspector = inspector
            self.conn_db2 = inspector.conn
        def checkdb(self, sqlfile=''):
            self.inspector.collect_data()
            return self.inspector.context
        def generate_report(self, output_file, inspector_name="Jack"):
            return self.inspector.generate_report(output_file, inspector_name)
    return CompatWrapper(inspector)

# ============================================================
# CLI 入口
# ============================================================
def main_cli():
    """独立运行时的 argparse 入口"""
    entry.ensure_bootstrapped('kingbase')
    parser = argparse.ArgumentParser(description="KingbaseES Database Inspection Tool")
    parser.add_argument('-H', '--host', required=True, help='Host address')
    parser.add_argument('-P', '--port', type=int, default=54321, help='Port (default: 54321)')
    parser.add_argument('-u', '--user', required=True, help='Username')
    parser.add_argument('-p', '--password', help='Password')
    parser.add_argument('-d', '--database', default='kingbase', help='Database name (default: kingbase)')
    parser.add_argument('-o', '--output', help='Output file path')
    parser.add_argument('--ssh-host', help='SSH host for remote inspection')
    parser.add_argument('--ssh-port', type=int, default=22, help='SSH port (default: 22)')
    parser.add_argument('--ssh-user', default='root', help='SSH user (default: root)')
    parser.add_argument('--ssh-password', help='SSH password')
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass("Password: ")

    ssh_info = None
    if args.ssh_host:
        ssh_info = {
            'ssh_host': args.ssh_host,
            'ssh_port': args.ssh_port,
            'ssh_user': args.ssh_user,
            'ssh_password': args.ssh_password
        }

    inspector = KingbaseESInspector(
        host=args.host,
        port=args.port,
        user=args.user,
        password=password,
        database=args.database,
        ssh_info=ssh_info
    )

    ok, version = inspector.connect()
    if not ok:
        print("Connection failed: {}".format(version))
        sys.exit(1)

    inspector.collect_data()
    output_file = args.output or "KingbaseES_Inspection_Report_{}.docx".format(datetime.now().strftime('%Y%m%d_%H%M%S'))
    inspector.generate_report(output_file)
    print("Report generated: {}".format(output_file))


def main(host=None, port=None, user=None, password=None, database=None, output=None, ssh_info=None):
    """KingbaseES 巡检 CLI 入口 - 支持交互模式和参数模式"""
    entry.ensure_bootstrapped('kingbase')
    if host is None:
        # 交互模式（从 main.py 调用时）
        print("KingbaseES 数据库巡检")
        print("=" * 50)
        host = input("主机地址 [localhost]: ") or "localhost"
        port = int(input("端口 [54321]: ") or 54321)
        user = input("用户名: ")
        if not user:
            print("用户名不能为空")
            return
        if password is None:
            password = getpass.getpass("密码: ")
        database = input("数据库名 [kingbase]: ") or "kingbase"
        output = input("输出文件 [KingbaseES_Inspection_Report.docx]: ") or "KingbaseES_Inspection_Report.docx"

    inspector = KingbaseESInspector(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        ssh_info=ssh_info
    )

    ok, version = inspector.connect()
    if not ok:
        print("连接失败: {}".format(version))
        return

    inspector.collect_data()
    output_file = output or "KingbaseES_Inspection_Report_{}.docx".format(datetime.now().strftime('%Y%m%d_%H%M%S'))
    inspector.generate_report(output_file)
    print("报告已生成: {}".format(output_file))


if __name__ == '__main__':
    main_cli()
