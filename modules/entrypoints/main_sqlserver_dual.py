#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
SQL Server 双轨巡检入口 —— 根据 connection_mode 路由 JDBC 或 ODBC 巡检器。

设计原则：
  1. main_sqlserver.py（pyodbc 实现）零修改，向后兼容现有用户。
  2. 本文件暴露 SQLServerDualInspector 复合类 + getData 工厂。
  3. connection_mode 取值：
       'odbc'  → 走 main_sqlserver.SQLServerInspector（pyodbc，默认）
       'jdbc'  → 走 plugins.available.sqlserver_jdbc.MssqlJdbcInspector
       'auto'  → 优先 JDBC（检测 jar + JPype1），失败回退 ODBC

调用方约定：
  - web_ui.py 在实例化前从 instance['connection_mode'] 读取 mode
  - 若字段缺失，默认 'odbc'（向后兼容铁律）
  - 'auto' 模式下只回退一次：JDBC 失败 → ODBC；ODBC 失败 → 直接报错

安全边界：
  - 不在模块级 import 任何 JDBC 相关模块（避免污染无 JDBC 环境的 import）
  - JPype1 / jar 缺失时才打印清晰提示，不抛栈到 web_ui
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import os
import sys
import glob
from typing import Any, Dict, List, Optional, Tuple

# 项目根统一取自 modules/core/paths.py（禁止 __file__ 上溯推导项目根）。
# 仅在“独立运行本文件”导致 modules 包尚不可导入时，才用本文件位置做一次
# **最小 sys.path 引导**；引导后仍以 paths.PROJECT_ROOT 为唯一权威值，
# 从而保证 frozen（PyInstaller one-folder/_internal、one-file/_MEIPASS）下路径正确。
try:
    from modules.core.paths import PROJECT_ROOT as _PATHS_PROJECT_ROOT
except ImportError:  # pragma: no cover - 仅独立运行脚本时触发
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
    from modules.core.paths import PROJECT_ROOT as _PATHS_PROJECT_ROOT

_PROJECT_ROOT = str(_PATHS_PROJECT_ROOT)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 复用现有 pyodbc 入口（保持完全不变）
from modules.entrypoints import main_sqlserver  # noqa: E402

# JDBC 入口延后导入：避免无 JPype1 环境时启动崩溃
_MssqlJdbcInspector = None
_jdbc_module = None


def _get_jdbc_inspector_class():
    """按绝对路径加载 sqlserver_jdbc/main_plugin.py，避免与 db2/oracle 同名模块污染。

    Returns:
        MssqlJdbcInspector 类

    Raises:
        ImportError: 当 JPype1 缺失或 main_plugin 加载失败时
    """
    global _MssqlJdbcInspector, _jdbc_module
    if _MssqlJdbcInspector is not None:
        return _MssqlJdbcInspector

    plugin_dir = os.path.join(_PROJECT_ROOT, "plugins", "available", "sqlserver_jdbc")
    main_plugin_path = os.path.join(plugin_dir, "main_plugin.py")
    if not os.path.isfile(main_plugin_path):
        raise ImportError(f"未找到 SQL Server JDBC 插件入口: {main_plugin_path}")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "main_sqlserver_dual_jdbc_module",
        main_plugin_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 SQL Server JDBC 插件入口: {main_plugin_path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, plugin_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        # 不在 sys.path 中保留插件目录（避免污染）
        try:
            if plugin_dir in sys.path:
                sys.path.remove(plugin_dir)
        except ValueError:
            pass
    _jdbc_module = module
    _MssqlJdbcInspector = module.MssqlJdbcInspector
    return _MssqlJdbcInspector


def jdbc_unavailable_reason() -> Optional[str]:
    """探测 JDBC 环境，返回**不可用的具体原因**；可用时返回 None。

    与 :func:`_jdbc_available` 共用同一套判定条件，但保留失败细节，
    供 connection_mode='jdbc' 显式模式下向用户报出可诊断的错误信息
    （而不是静默回退 ODBC 后抛出无关的 unixODBC 报错）。

    Returns:
        Optional[str]: 不可用原因描述；环境完备时为 None。
    """
    # 1. JPype1 可导入
    try:
        import jpype  # noqa: F401
    except Exception as e:
        return f"JPype1 未安装或导入失败（pip install JPype1）: {e}"

    # 2. mssql-jdbc-*.jar 存在
    drivers_dir = os.path.join(_PROJECT_ROOT, "drivers", "sqlserver")
    if not os.path.isdir(drivers_dir):
        return f"驱动目录不存在: {drivers_dir}"
    jars = glob.glob(os.path.join(drivers_dir, "**", "mssql-jdbc-*.jar"), recursive=True)
    if not jars:
        return f"未找到 mssql-jdbc-*.jar，请放置到: {drivers_dir}"

    # 3. 插件 main_plugin.py 可加载（含 JVM 启动依赖的 Java 运行时）
    try:
        _get_jdbc_inspector_class()
    except Exception as e:
        return f"SQL Server JDBC 插件加载失败（请确认已安装 Java 运行时）: {e}"

    return None


def _jdbc_available() -> bool:
    """探测 JDBC 是否可用（jar 存在 + JPype1 可导入 + 插件可加载）。

    Returns:
        bool
    """
    return jdbc_unavailable_reason() is None


class SQLServerDualInspector:
    """SQL Server 双轨巡检复合类。

    根据 connection_mode 路由到 JDBC（MssqlJdbcInspector）或 ODBC
    （SQLServerInspector）实现。保持与单轨 Inspector 相同的接口签名
    （connect / collect_data / generate_report / disconnect），对调用方透明。

    Args:
        host: SQL Server 地址
        port: 端口
        user: 用户名
        password: 密码
        database: 数据库名
        ssh_info: SSH / 额外参数（含 connection_mode / jdbc_url / instance_name /
                  encrypt / trust_server_certificate 等）
        template_id: 模板 ID
        connection_mode: 'odbc' | 'jdbc' | 'auto'（默认 'odbc'）
    """

    def __init__(self, host, port, user, password, database=None, ssh_info=None,
                 template_id=None, connection_mode: str = 'odbc', **kwargs):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.database = database
        self.ssh_info = ssh_info or {}
        self._template_id = template_id
        self.mode = (connection_mode or 'odbc').lower()

        # 'auto' 模式立刻判定实际 mode
        self._resolved_mode = self.mode
        if self.mode == 'auto':
            self._resolved_mode = 'jdbc' if _jdbc_available() else 'odbc'

        self._inner = None
        self._init_inner()

    def _init_inner(self):
        """根据 mode 实例化内层 Inspector。"""
        if self._resolved_mode == 'jdbc':
            cls = _get_jdbc_inspector_class()
            jdbc_url = self.ssh_info.get('jdbc_url')
            instance_name = self.ssh_info.get('instance_name', '')
            encrypt = bool(self.ssh_info.get('encrypt', False))
            trust_sc = bool(self.ssh_info.get('trust_server_certificate', True))
            self._inner = cls(
                self.host, self.port, self.user, self.password,
                database=self.database,
                ssh_info=self.ssh_info,
                template_id=self._template_id,
                jdbc_url=jdbc_url,
                instance_name=instance_name,
                encrypt=encrypt,
                trust_server_certificate=trust_sc,
            )
        else:
            # ODBC（默认 / 兜底）
            self._inner = main_sqlserver.SQLServerInspector(
                self.host, self.port, self.user, self.password,
                database=self.database, ssh_info=self.ssh_info,
                template_id=self._template_id,
            )

    @property
    def db_type(self) -> str:
        return 'sqlserver_jdbc' if self._resolved_mode == 'jdbc' else 'sqlserver'

    @property
    def context(self) -> Dict[str, Any]:
        return getattr(self._inner, 'context', {})

    @context.setter
    def context(self, value: Dict[str, Any]):
        self._inner.context = value

    @property
    def conn(self):
        return getattr(self._inner, 'conn', None)

    @property
    def cursor(self):
        return getattr(self._inner, 'cursor', None)

    def connect(self) -> Tuple[bool, str]:
        """连接数据库。

        'auto' 模式且 JDBC 初始化失败 → 静默回退到 ODBC（仅一次）。
        'jdbc' / 'odbc' 模式下，错误直接透传，不回退。
        """
        try:
            return self._inner.connect()
        except Exception as e:
            if self.mode == 'auto' and self._resolved_mode == 'jdbc':
                print(f"[MSSQL-DUAL] auto 模式下 JDBC 失败，回退 ODBC: {e}")
                self._resolved_mode = 'odbc'
                self._inner = main_sqlserver.SQLServerInspector(
                    self.host, self.port, self.user, self.password,
                    database=self.database, ssh_info=self.ssh_info,
                    template_id=self._template_id,
                )
                return self._inner.connect()
            raise

    def disconnect(self) -> None:
        if self._inner is not None:
            try:
                self._inner.disconnect()
            except Exception:
                pass

    def collect_data(self, sql_templates: str = ''):
        """采集数据（透传到内层 Inspector）。"""
        return self._inner.collect_data(sql_templates)

    def generate_report(self, output_file: str, inspector_name: str = "Jack") -> str:
        """生成报告（透传）。"""
        if hasattr(self._inner, 'generate_report'):
            return self._inner.generate_report(output_file, inspector_name)
        raise NotImplementedError("内层 Inspector 不支持 generate_report")

    def get_template_id(self):
        """返回模板 ID（透传）。"""
        if hasattr(self._inner, 'get_template_id'):
            return self._inner.get_template_id()
        return None


# ── 数据源获取函数（供 web_ui.py 替换原 main_sqlserver.getData）────
def getData(ip, port, user, password, ssh_info=None, template_id=None,
            connection_mode: str = 'odbc', label=None):
    """获取 SQL Server 数据源（双轨）。

    Args:
        ip: 主机地址
        port: 端口
        user: 用户名
        password: 密码
        ssh_info: 额外参数（与 main_sqlserver 兼容；含 connection_mode 时自动路由）
        template_id: 模板 ID
        connection_mode: 'odbc' / 'jdbc' / 'auto'（优先于 ssh_info.connection_mode）
        label: 实例显示名。仅作兼容占位——web 层（modules/web/app.py 的
               getdata_args）对所有 SQL Server 类型统一透传该关键字参数。
               本函数不用它参与连接或路由（路由键始终是 connection_mode），
               仅挂到返回的 CompatWrapper 上供下游可选读取。行为与
               main_sqlserver.getData 的 label 占位忽略保持一致。

    Returns:
        CompatWrapper 对象（与 main_sqlserver.getData 兼容）；失败返回 None。
    """
    ssh_info = ssh_info or {}
    # 优先取显式 connection_mode；否则取 ssh_info['connection_mode']
    mode = connection_mode or ssh_info.get('connection_mode') or 'odbc'
    database = ssh_info.get('database', '')

    try:
        inspector = SQLServerDualInspector(
            ip, int(port), user, password,
            database=database, ssh_info=ssh_info,
            template_id=template_id, connection_mode=mode,
        )
    except Exception as e:
        print(f"[MSSQL-DUAL] 初始化失败: {e}")
        return None

    ok, msg = inspector.connect()
    if not ok:
        print(f"[MSSQL-DUAL] 连接失败: {msg}")
        return None

    class CompatWrapper:
        """兼容 web_ui.py 调用约定的包装对象（与 main_sqlserver.getData 一致）。"""

        def __init__(self, inner_inspector):
            self.inspector = inner_inspector
            self.conn = getattr(inner_inspector, 'conn', None)
            # 兼容老代码：保留 conn_db2 字段（历史命名，无实际意义）
            self.conn_db2 = self.conn
            # 透传实例显示名（闭包捕获外层 getData 的 label 形参）。
            # 仅供下游 web 层可选读取，不参与任何连接/路由逻辑。
            self.label = label

        def checkdb(self, sqlfile=''):
            self.inspector.collect_data()
            return self.inspector.context

        def generate_report(self, output_file, inspector_name="Jack"):
            return self.inspector.generate_report(output_file, inspector_name)

    return CompatWrapper(inspector)


if __name__ == '__main__':
    # 简单自测
    print("SQL Server Dual Inspector")
    print(f"  _jdbc_available() = {_jdbc_available()}")
    print(f"  现有 main_sqlserver.SQLServerInspector = {main_sqlserver.SQLServerInspector.__name__}")
