#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""DM8 inspection entry point and driver compatibility helpers."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

from modules.config.version import __version__ as VER
from i18n import get_lang, t as _t
from modules.inspection.engine import BaseInspectionEngine
from modules.core import entry
from modules.core.paths import PROJECT_ROOT
from modules.driver_registry import resolve_jdbc_driver_jars
import os
import glob as _glob

"""
达梦 DM8 数据库自动化健康巡检工具 {VER}
支持 DM8 及以上版本
依赖: dmpython (pip install dmpython), python-docx, docxtpl, openpyxl, psutil, paramiko>=2.8,<2.10
注意: dmpython 需要达梦数据库自带的 dpi 动态库支持
"""

import warnings
warnings.filterwarnings("ignore")
import sys
import datetime
import argparse
import getpass

# 兼容函数 — 供 web_ui.py 旧代码调用
# (DmInspector 在下方定义，兼容函数仅在调用时才引用，不会提前报错)

def getData(ip, port, user, password, ssh_info=None, db_name=None, template_id=None, driver_version=''):
    """
    原有 API - 创建 DmInspector 实例

    注意：这个函数在重构过程中保留，用于兼容 web_ui.py 中的旧代码。
    新代码应该直接使用 DmInspector 类。
    """
    inspector = DmInspector(ip, port, user, password, db_name, ssh_info, template_id, driver_version=driver_version)
    ok, ver = inspector.connect()
    if not ok:
        return None

    class CompatWrapper:
        def __init__(self, inspector):
            self.inspector = inspector
            self.conn_db = inspector.conn

        def checkdb(self, sqlfile=''):
            self.inspector.collect_data()
            return self.inspector.context

        def generate_report(self, output_file, inspector_name="Jack"):
            return self.inspector.generate_report(output_file, inspector_name)

    return CompatWrapper(inspector)


def create_word_template(inspector_name):
    """原有 API - 创建 Word 模板（DM8 由 saveDoc 内部通过 inspector.generate_report 生成）"""
    return '_dm_inspector_generated_'


def saveDoc(context, ofile, ifile, inspector_name, **kwargs):
    """原有 API - 保存 Word 报告（委托给 DmInspector.generate_report）"""
    inspector = kwargs.get('H')  # web_ui.py 传 H=data.H，data 是 CompatWrapper
    class CompatWrapper:
        def __init__(self, context, ofile, inspector, inspector_name):
            self.context = context
            self.ofile = ofile
            self._inspector = inspector
            self._inspector_name = inspector_name

        def contextsave(self):
            if self._inspector and hasattr(self._inspector, 'generate_report'):
                return self._inspector.generate_report(self.ofile, self._inspector_name) is not None
            # 兜底：保存空文档
            from docx import Document
            doc = Document()
            doc.save(self.ofile)
            return True
    return CompatWrapper(context, ofile, inspector, inspector_name)


# 达梦 DM8 驱动
try:
    import dmPython as dm_driver
    DM_DRIVER = 'dmPython'
except ImportError:
    print(_t("dm8_driver_missing"))
    print("  pip install dmpython")
    print("  " + _t("dm8_driver_path_note"))
    dm_driver = None
    DM_DRIVER = None


# ── JDBC 支持（优先 JDBC，回退 dmPython）────────────────────────────
# 与「测试连接」(/api/test_db、/api/pro/datasources/test-connection) 策略一致：
# 优先用 drivers/dm8/DmJdbcDriver*.jar 走纯 Java JDBC，规避 -70089。
def _detect_java_home():
    """自动探测 JAVA_HOME（按常见安装路径），与 gbase 巡检保持一致。"""
    candidates = [
        os.environ.get('JAVA_HOME', ''),
        # Windows
        'C:\\Program Files\\Java\\jdk-11',
        'C:\\Program Files\\Java\\jdk-17',
        'C:\\Program Files\\Java\\jdk-1.8',
        'C:\\Program Files\\Eclipse Adoptium\\jdk-11',
        'C:\\Program Files\\Eclipse Adoptium\\jdk-17',
        # Linux (Debian/Ubuntu Docker 镜像)
        '/usr/lib/jvm/java-17-openjdk-amd64',
        '/usr/lib/jvm/java-11-openjdk-amd64',
        '/usr/lib/jvm/default-java',
        '/usr/lib/jvm/java-1.17.0-openjdk-amd64',
        '/usr/lib/jvm/java-1.11.0-openjdk-amd64',
    ]
    for path in candidates:
        if path and os.path.isdir(path):
            return path
    try:
        _jvm_dirs = sorted(_glob.glob('/usr/lib/jvm/java-*'))
        for d in _jvm_dirs:
            if os.path.isdir(d):
                return d
    except Exception:
        pass
    return None


def _setup_jvm_for_dm(jar_path):
    """确保 JVM 已启动，并把达梦 JDBC 驱动 jar 加入 classpath。"""
    import jpype
    _java_home = _detect_java_home()
    if _java_home:
        os.environ['JAVA_HOME'] = _java_home
        _jvm_dir = os.path.join(_java_home, 'bin', 'server')
        if os.path.isdir(_jvm_dir):
            os.environ['PATH'] = _jvm_dir + os.pathsep + os.environ.get('PATH', '')
    if not jpype.isJVMStarted():
        try:
            jpype.addClassPath(jar_path)
            jpype.startJVM()
        except Exception:
            pass
    else:
        # JVM 已被其它巡检类型启动，尝试补加 classpath（兼容多库同进程）
        try:
            jpype.addClassPath(jar_path)
        except Exception:
            pass


def _find_dm_jdbc_jar():
    """Use registered and packaged DM drivers, including drivers/dm/<version>."""
    from modules.jdbc_connector import resolve_driver_jars
    jars = resolve_driver_jars('dm', fallback_dirs=[
        os.path.join(str(PROJECT_ROOT), 'drivers', 'dm'),
        os.path.join(str(PROJECT_ROOT), 'drivers', 'dm8')], recursive=True)
    return jars[0] if jars else None


class DmInspector(BaseInspectionEngine):
    """
    达梦 DM8 巡检引擎
    继承 BaseInspectionEngine，只需实现 connect() 和 get_template_id()
    """
    
    def __init__(self, host, port, user, password, database=None, ssh_info=None, template_id=None,
                 driver_version=''):
        super().__init__(host, port, user, password, database, ssh_info, template_id)
        self.db_type = 'dm8'  # 设置数据库类型
        self.driver_version = driver_version   # 来自数据源/手动表单；空串=用激活驱动
        self._lang = get_lang()
        
    def connect(self):
        """
        连接达梦 DM8 数据库。

        优先使用 JDBC 驱动（纯 Java，无需达梦原生客户端 libdmcrypt.so），
        驱动 jar 置于 <项目根>/drivers/dm8/DmJdbcDriver*.jar；JDBC 不可用时
        回退 dmPython（需本机安装达梦客户端原生库）。与「测试连接」策略一致，
        从根本上规避 -70089 Encryption module failed to load。
        """
        # ── 优先 JDBC ──────────────────────────────────────────────
        # 统一连接层（modules/jdbc_connector）：驱动管理优先（driver_version），
        # 回退项目根 drivers/dm8/ 自动发现；连接失败走 dmPython 回退。
        _conn = None
        _meta = {}
        try:
            from modules.jdbc_connector import open_jdbc_connection
            _conn, _meta = open_jdbc_connection(
                'dm', self.host, self.port, self.user, self.password,
                driver_version=self.driver_version,
                fallback_dirs=[os.path.join(str(PROJECT_ROOT), 'drivers', 'dm8'),
                               os.path.join(str(PROJECT_ROOT), 'drivers', 'dm')],
                recursive=True,
            )
        except Exception as e:  # noqa: BLE001
            print("DM8 统一连接层调用失败，回退 dmPython:", e)

        if _conn is not None:
            self.conn = _conn
            self.cursor = self.conn.cursor()
            try:
                self.cursor.execute("SELECT VERSION$ FROM V$VERSION")
                version = self.cursor.fetchone()[0]
            except Exception:
                try:
                    self.cursor.execute("SELECT BANNER FROM V$VERSION WHERE ROWNUM=1")
                    version = self.cursor.fetchone()[0]
                except Exception:
                    version = 'Unknown'
            self.context['version'] = [{'VERSION': version}]
            print(_t("dm8_connect_success").format(host=self.host, port=self.port))
            return True, version
        else:
            print("DM8 JDBC 连接失败，回退 dmPython:", (_meta or {}).get('error', ''))

        # ── 回退 dmPython ──────────────────────────────────────────
        try:
            if dm_driver is None:
                return False, "达梦数据库驱动未安装，请执行: pip install dmpython（或将 JDBC 驱动放入 drivers/dm8/）"

            # 正确写法：server='host:port'（参考 instance_manager.py 第 448-449 行）
            dsn = '%s:%d' % (self.host, int(self.port))
            self.conn = dm_driver.connect(
                user=self.user,
                password=self.password,
                server=dsn  # 正确：'host:port' 格式
            )
            self.cursor = self.conn.cursor()

            # 获取达梦版本号
            try:
                self.cursor.execute("SELECT VERSION$ FROM V$VERSION")
                version = self.cursor.fetchone()[0]
            except Exception:
                try:
                    self.cursor.execute("SELECT BANNER FROM V$VERSION WHERE ROWNUM=1")
                    version = self.cursor.fetchone()[0]
                except Exception:
                    version = 'Unknown'
            self.context['version'] = [{'VERSION': version}]

            print(_t("dm8_connect_success").format(host=self.host, port=self.port))
            return True, version

        except Exception as e:
            err_msg = str(e)
            print(_t("dm8_connect_fail").format(error=err_msg))
            import traceback
            traceback.print_exc()
            return False, err_msg
    
# ============================================================
# CLI 入口
# ============================================================
def main_cli():
    """独立运行时的 argparse 入口"""
    entry.ensure_bootstrapped('dm8')
    parser = argparse.ArgumentParser(description=_t("dm8_cli_desc"))
    parser.add_argument('-H', '--host', required=True, help=_t("cli_host"))
    parser.add_argument('-P', '--port', type=int, default=5236, help=_t("dm8_cli_port"))
    parser.add_argument('-u', '--user', required=True, help=_t("cli_user"))
    parser.add_argument('-p', '--password', help=_t("cli_password"))
    parser.add_argument('-d', '--database', help=_t("cli_database"))
    parser.add_argument('-o', '--output', help=_t("cli_output"))
    parser.add_argument('--ssh-host', help=_t("cli_ssh_host"))
    parser.add_argument('--ssh-port', type=int, default=22, help=_t("cli_ssh_port"))
    parser.add_argument('--ssh-user', default='root', help=_t("cli_ssh_user"))
    parser.add_argument('--ssh-key', help=_t("cli_ssh_key_file"))
    parser.add_argument('--driver-version', default='', help=_t("cli_driver_version"))
    args = parser.parse_args()

    password = args.password
    if not password:
        password = getpass.getpass(_t("cli_pwd_prompt"))

    ssh_info = None
    if args.ssh_host:
        ssh_info = {
            'ssh_host': args.ssh_host,
            'ssh_port': args.ssh_port,
            'ssh_user': args.ssh_user,
            'ssh_password': args.ssh_password
        }

    inspector = DmInspector(
        host=args.host,
        port=args.port,
        user=args.user,
        password=password,
        database=args.database,
        ssh_info=ssh_info,
        driver_version=args.driver_version
    )

    ok, version = inspector.connect()
    if not ok:
        print(_t("dm8_conn_fail_exit"))
        sys.exit(1)

    inspector.collect_data()
    output_file = args.output or "DM8_Inspection_Report_{}.docx".format(datetime.now().strftime('%Y%m%d_%H%M%S'))
    inspector.generate_report(output_file)
    print(_t("dm8_report_generated").format(file=output_file))


def main(host=None, port=None, user=None, password=None, database=None, output=None, ssh_info=None):
    """DM8 巡检 CLI 入口 - 支持交互模式和参数模式"""
    entry.ensure_bootstrapped('dm8')
    if host is None:
        # 交互模式（从 main.py 调用时）
        print(u"DM8 达梦数据库巡检")
        print(u"=" * 50)
        host = input(u"主机地址 [localhost]: ") or "localhost"
        port = int(input(u"端口 [5236]: ") or 5236)
        user = input(u"用户名: ")
        if not user:
            print(u"用户名不能为空")
            return
        if password is None:
            password = getpass.getpass(u"密码: ")
        database = input(u"数据库名: ") or ""
        output = input(u"输出文件 [DM8_Inspection_Report.docx]: ") or "DM8_Inspection_Report.docx"

    inspector = DmInspector(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        ssh_info=ssh_info
    )

    ok, version = inspector.connect()
    if not ok:
        print(u"连接失败: {}".format(version))
        return

    inspector.collect_data()
    output_file = output or "DM8_Inspection_Report_{}.docx".format(datetime.now().strftime('%Y%m%d_%H%M%S'))
    inspector.generate_report(output_file)
    print(u"报告已生成: {}".format(output_file))


if __name__ == '__main__':
    main_cli()
