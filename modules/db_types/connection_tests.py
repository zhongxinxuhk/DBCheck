# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Connection probes for Web and background inspection."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import json
import os
import re
import sys
import time

from modules.core import paths
from modules.core.paths import PROJECT_ROOT
from modules.core.proc_registry import kill_process_tree
from modules.oracle_conn_test import _find_oracle_client_lib_dir

JDBC_TEST_TIMEOUT = 50


def test_mysql_connection(host, port, user, password, database=None, driver_version=''):
    """测试 MySQL 连接（统一 JDBC 子进程优先，回退 pymysql）。"""
    return run_jdbc_test_subprocess('mysql', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version})


def test_mariadb_connection(host, port, user, password, database=None, driver_version=''):
    """测试 MariaDB 连接（统一 JDBC 子进程优先，回退 pymysql）。

    独立于 test_mysql_connection：MariaDB 走 jdbc:mariadb:// + org.mariadb.jdbc.Driver，
    复用 mysql 会把测试路由到 MySQL 链路（catalog=mysql），登记了 MariaDB 专用驱动的
    用户会误报「mysql 驱动未找到」。
    """
    return run_jdbc_test_subprocess('mariadb', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version})


def test_oceanbase_connection(host, port, user, password, database=None, driver_version=''):
    """测试 OceanBase 连接（统一 JDBC 子进程优先，回退 pymysql）。

    独立于 test_mysql_connection：OceanBase 走 jdbc:oceanbase:// + com.oceanbase.jdbc.Driver；
    user 需含租户拼接（root@tenant），由调用方（_ct_oceanbase）负责拼好后传入。
    """
    return run_jdbc_test_subprocess('oceanbase', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version, 'database': database})

def test_tidb_connection(host, port, user, password, database=None, driver_version=''):
    """测试 TiDB 连接（统一 JDBC 子进程优先，回退 pymysql）。"""
    return run_jdbc_test_subprocess('tidb', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version})

def test_pg_connection(host, port, user, password, database='postgres', driver_version=''):
    """测试 PostgreSQL 连接（统一 JDBC 子进程隔离；JDBC 不可用回退 psycopg2）。

    JVM 在独立子进程内执行，避免主进程 gevent hub 被钉死；与巡检引擎
    同一套统一连接层（modules.jdbc_connector.open_jdbc_connection）。
    """
    return run_jdbc_test_subprocess('pg', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version})

def test_kingbase_connection(host, port, user, password, database='kingbase', driver_version=''):
    """测试 KingbaseES 连接（统一 JDBC 子进程隔离；JDBC 不可用回退 psycopg2）。

    KingbaseES V8 官方驱动 com.kingbase8.jdbc.Driver / jdbc:kingbase8://；
    与巡检引擎同一套统一连接层。
    """
    return run_jdbc_test_subprocess('kingbase', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version})


def test_ivorysql_connection(host, port, user, password, database='ivorysql', driver_version=''):
    """测试 IvorySQL 连接（统一 JDBC：兼容 PG 协议，复用 PostgreSQL 驱动）。

    与其它 JDBC 类型一致，在独立子进程内建连（JVM 不进入主进程，
    避免钉死 gevent hub）。驱动 jar 优先驱动管理登记的版本，
    否则回退 drivers/postgresql/ 自动发现。
    """
    return run_jdbc_test_subprocess('ivorysql', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version})


def test_halodb_connection(host, port, user, password, database='halo', driver_version=''):
    """测试 HaloDB（羲和）连接（统一 JDBC：兼容 PG 协议，复用 PostgreSQL 驱动）。

    与 IvorySQL 同套路：独立子进程内建连（JVM 不进主进程，避免钉死
    gevent hub）；JDBC 不可用时 jdbc_test_cli 内回退 psycopg2。
    """
    return run_jdbc_test_subprocess('halodb', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version})

# (函数已迁移至 modules.oracle_conn_test：def _find_oracle_client_lib_dir)
def test_oracle_connection(host, port, user, password, service_name='ORCL', sysdba=False, jdbc_url=None):
    if paths.PORTABLE_ROOT is not None:
        return run_jdbc_test_subprocess('oracle_jdbc',{'host':host,'port':port,'user':user,'password':password,
            'service_name':service_name,'sysdba':sysdba,'jdbc_url':jdbc_url})
    try:
        import oracledb
        # 解析 "user as sysdba" 语法
        _user = user.strip()
        _mode = oracledb.SYSDBA if (sysdba or re.search(r'\bas\s+sysdba\b', _user, re.IGNORECASE)) else None
        _user = re.sub(r'\s+as\s+sysdba\b', '', _user, flags=re.IGNORECASE).strip()
        _jdbc = (jdbc_url or '').strip()
        if _jdbc and _jdbc.lstrip().upper().startswith('(DESCRIPTION'):
            # 纯 TNS 描述符：oracledb 可直接作为 dsn 使用
            kw = dict(user=_user, password=password, dsn=_jdbc)
        else:
            kw = dict(user=_user, password=password, host=host, port=int(port),
                      service_name=service_name, tcp_connect_timeout=15)
        if _mode is not None:
            kw['mode'] = _mode
        try:
            try:
                conn = oracledb.connect(**kw)
            except TypeError as te:
                if 'tcp_connect_timeout' in str(te):
                    kw.pop('tcp_connect_timeout', None)
                    conn = oracledb.connect(**kw)
                else:
                    raise
        except Exception as e:
            err_str = str(e)
            if 'DPY-3010' in err_str or 'DPY-3015' in err_str:
                # thin mode 不支持 11g / 老密码验证器(0x939)，尝试 thick mode
                _ok = False
                try:
                    oracledb.init_oracle_client()
                    _ok = True
                except Exception:
                    pass
                if not _ok:
                    # 使用辅助函数查找 Oracle Client 目录（支持 lib/ 子目录）
                    _lib_dir = _find_oracle_client_lib_dir()
                    if _lib_dir:
                        try:
                            oracledb.init_oracle_client(lib_dir=_lib_dir)
                            _ok = True
                        except Exception:
                            pass
                if not _ok:
                    return False, 'Oracle 11g 需要 Instant Client，请将包解压到 drivers/oracle_client/ 对应平台目录（支持根目录或 lib/ 子目录）'
                conn = oracledb.connect(**kw)
            else:
                raise
        cur = conn.cursor()
        cur.execute("SELECT BANNER FROM V$VERSION WHERE ROWNUM=1")
        ver = cur.fetchone()[0]
        cur.close()
        conn.close()
        return True, ver
    except Exception as e:
        return False, str(e)

def test_dm_connection(host, port, user, password, driver_version=''):
    # 优先使用 JDBC 驱动（纯 Java，无需达梦原生客户端 libdmcrypt），
    # 驱动 jar 优先「数据库驱动管理」中登记的指定版本，否则
    # 置于 <项目根>/drivers/dm8/DmJdbcDriver*.jar。无原生客户端依赖，
    # 从根本上规避 -70089 Encryption module failed to load。
    try:
        from modules.jdbc_test_cli import _find_dm_jdbc_jar
        _jar = _find_dm_jdbc_jar()
    except Exception:  # noqa: BLE001 - 导入失败则按无 JDBC 处理，走 dmPython 回退
        _jar = None
    if _jar:
        return run_jdbc_test_subprocess('dm', {
            'host': host, 'port': port, 'user': user, 'password': password,
        }, extra_kwargs={'driver_version': driver_version})

    # 回退：dmPython（需要本机安装达梦客户端原生库 libdmcrypt.so）
    try:
        import dmPython
        conn = dmPython.connect(user=user, password=password, server=host, port=int(port))
        cur = conn.cursor()
        cur.execute("SELECT STATUS$ FROM V$INSTANCE")
        ver = cur.fetchone()[0]
        cur.close()
        conn.close()
        return True, ver
    except Exception as e:
        err = str(e)
        if 'No module named' in err and 'dmPython' in err:
            return False, ('未安装 dmPython 驱动，且 drivers/dm8/ 下未找到 JDBC 驱动'
                           '（DmJdbcDriver*.jar），无法测试达梦连接。请将达梦 JDBC 驱动'
                           '放入 drivers/dm8/（推荐，免装客户端）。')
        if '-70089' in err or 'Encryption module' in err or 'dmCrypt' in err:
            return False, ('达梦客户端原生库(dmCrypt)未加载，无法用 dmPython 连接。推荐将达梦 JDBC '
                           '驱动 DmJdbcDriver*.jar 放入 drivers/dm8/（免装客户端即可测试）；'
                           '或在运行本服务的机器安装 DM8 客户端（与数据库同大版本），并将其 bin 目录加入 '
                           'LD_LIBRARY_PATH(Linux)/PATH(Windows)后重启服务。'
                           f'（原始错误：{err}）')
        return False, err


def test_yashandb_connection(host, port, user, password, database='yashandb', driver_version=''):
    """测试 YashanDB 连接（统一 JDBC 子进程隔离；JDBC 不可用回退 yasdb）。

    YashanDB 官方驱动 com.yashandb.jdbc.Driver / jdbc:yashandb://；
    与巡检引擎同一套统一连接层。
    """
    return run_jdbc_test_subprocess('yashandb', {
        'host': host, 'port': port, 'user': user, 'password': password,
        'database': database,
    }, extra_kwargs={'driver_version': driver_version})


def test_gbase_connection(host, port, user, password, database='testdb', gbase_server_name='gbase01', driver_version=''):
    """测试 GBase 8s 连接（JDBC 模式，使用 jaydebeapi）"""
    try:
        from modules.entrypoints.main_gbase import test_gbase_jdbc_connection
        return test_gbase_jdbc_connection(host, port, user, password, database, gbase_server_name, driver_version)
    except Exception as e:
        return False, f"导入 main_gbase 失败：{e}"

def _get_sqlserver_driver():
    """获取系统已安装的 SQL Server ODBC 驱动（按优先级排序）"""
    try:
        import pyodbc
        installed = [d for d in pyodbc.drivers() if d.strip()]
        preferred = ['ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server',
                     'ODBC Driver 13 for SQL Server', 'SQL Server']
        for d in preferred:
            if d in installed:
                return d
        if installed:
            return installed[0]
    except Exception:
        pass
    return 'ODBC Driver 17 for SQL Server'


def _build_sqlserver_conn_str(host, port, user, password, database=None, timeout=10):
    """
    构建 SQL Server ODBC 连接字符串（动态检测驱动）
    密码为 None/空串时省略 PWD（Windows 认证）；
    密码中含分号时用单引号包裹，避免断串。
    """
    driver = _get_sqlserver_driver()
    # 详细调试
    print(f"[SQL Server] password 类型: {type(password)}, 值长度: {len(str(password)) if password else 0}, repr: {repr(password)}")
    # 拼 UID（用户名）
    uid_part = ";UID=" + str(user) if user else ""
    # 拼 PWD（密码为 None 或空字符串时完全省略，触发 Windows 认证）
    pwd_part = ""
    if password and str(password):
        _pwd = str(password)
        if ';' in _pwd:
            pwd_part = ";PWD='" + _pwd + "'"
        else:
            pwd_part = ";PWD=" + _pwd
    conn_str = ("DRIVER={" + driver + "};"
                "SERVER=" + str(host) + "," + str(port) +
                uid_part + pwd_part +
                ";Encrypt=yes;TrustServerCertificate=no;Connection Timeout=" + str(timeout))
    if database:
        conn_str += ";DATABASE=" + str(database)
    # 调试输出（密码打码）
    _debug_pwd = "***" if password else "(empty/None)"
    print(f"[SQL Server] 连接字符串调试: DRIVER={{{driver}}};SERVER={host},{port};UID={user};PWD={_debug_pwd};DB={database}")
    return conn_str


def test_sqlserver_connection(host,port,user,password,database='master',options=None):
    from modules.entrypoints.main_sqlserver import SQLServerInspector
    inspector=SQLServerInspector(host,port,user,password,database,options or {})
    ok,version=inspector.connect()
    if inspector.conn:inspector.conn.close()
    return ok,version


def test_gbase8a_connection(data):
    from modules.entrypoints.main_gbase8a import GBase8aInspector
    inspector=GBase8aInspector(data.get('host') or data.get('ip'),data['port'],data['user'],data['password'],data.get('database'),data)
    ok,version=inspector.connect()
    if inspector.conn:inspector.conn.close()
    return ok,version


def test_sqlserver_jdbc_connection(host, port, user, password, database='master',
                                   jdbc_opts=None, **kwargs):
    """测试 SQL Server JDBC 连接（双轨：odbc / jdbc / auto）。

    Args:
        host / port / user / password / database: 标准连接参数
        jdbc_opts: 可选 dict，JDBC 选项包。task_configs['sqlserver_jdbc'] 的
            connect_test_args 以**第 6 个位置参数**传入该 dict（**kwargs 只能吸收
            关键字参数，无法吸收位置参数，故需显式形参）。
        connection_mode: 'odbc' | 'jdbc' | 'auto'（缺省/空串 → 'jdbc'）
            - 'jdbc'：强制 JDBC。环境不可用时**直接返回错误，绝不回退 ODBC**。
            - 'auto'：按 JDBC 环境可用性选择驱动。
            - 'odbc'：直接走 pyodbc。
        jdbc_url / instance_name / encrypt / trust_server_certificate: JDBC 专用

    Returns:
        (ok, msg)：ok=True 时 msg 是版本可读串（如 'Microsoft SQL Server 2019'）
    """
    # 合并两种传参风格：关键字参数（老调用/测试）与位置 dict（task_configs 调用）。
    # jdbc_opts 优先级更高，因为它是任务配置显式构造的完整选项包。
    opts = {**kwargs, **(jdbc_opts or {})}
    # 本函数只服务 sqlserver_jdbc 类型：缺省 / 空串一律按 'jdbc' 处理（向后兼容旧实例）。
    # 需要 ODBC 的场景由 'sqlserver' 类型或显式 connection_mode='odbc' 覆盖。
    from modules.db_types.connection_profile import resolve_sqlserver_mode

    connection_mode = resolve_sqlserver_mode(dict(opts, db_type='sqlserver_jdbc'))
    try:
        # 走双轨入口（main_sqlserver_dual 自动按 mode 路由 JDBC/ODBC）
        from modules.entrypoints.main_sqlserver_dual import jdbc_unavailable_reason

        # 显式 'jdbc'：环境不满足必须直接报错，**绝不静默回退 ODBC**，
        # 否则用户会看到与所选模式无关的 unixODBC 'SQL Server' 报错（误导性极强）。
        if connection_mode == 'jdbc':
            _reason = jdbc_unavailable_reason()
            if _reason:
                return False, f'JDBC 驱动/JVM 环境不可用: {_reason}'

        # 'auto' 时探测，不可用则回退 ODBC；'jdbc' 走到此处说明环境已就绪
        if connection_mode == 'jdbc':
            from modules.entrypoints.main_sqlserver_dual import SQLServerDualInspector

            inspector = SQLServerDualInspector(
                host=host, port=int(port), user=user, password=password,
                database=database, connection_mode=connection_mode, ssh_info=opts,
            )
            try:
                return inspector.connect()
            finally:
                inspector.disconnect()
        return test_sqlserver_connection(host, port, user, password, database, opts)
    except Exception as e:
        return False, f'test_sqlserver_jdbc_connection 异常: {e}'


def _jdbc_cli_command():
    """返回启动隔离 CLI 的命令行。

    冻结态：``<dbcheck.exe> --jdbc-test-cli``（web_ui.py 在单实例锁之前拦截）；
    开发态：``<python> modules/jdbc_test_cli.py``。
    """
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--jdbc-test-cli']
    return [sys.executable, os.path.join(str(PROJECT_ROOT), 'modules', 'jdbc_test_cli.py')]


def run_jdbc_test_subprocess(db_type, data, extra_kwargs=None, timeout=JDBC_TEST_TIMEOUT):
    """在独立进程中执行一次 JDBC 连接测试。

    Args:
        db_type: 'hgdb' | 'db2' | 'sqlserver_jdbc'
        data: 前端提交的连接参数 dict（host/port/user/password ...）
        extra_kwargs: 透传给插件 test_connection 的关键字参数
        timeout: 硬超时（秒），超时即杀进程树

    Returns:
        (ok: bool, msg: str)
    """
    import subprocess as _sp
    import tempfile as _tf
    from modules.jdbc_test_cli import RESULT_PREFIX

    payload = {
        'db_type': db_type,
        'host': data.get('host'),
        'port': data.get('port'),
        'user': data.get('user'),
        'password': data.get('password'),
        'kwargs': extra_kwargs or {},
    }

    # 参数走 stdin 临时文件而非命令行：避免密码出现在进程列表/任务管理器里；
    # 输出走临时文件而非管道：管道满会阻塞子进程，且读管道会阻塞 gevent hub。
    _in_fd, _in_path = _tf.mkstemp(prefix='dbc_jdbc_in_', suffix='.json')
    _out_fd, _out_path = _tf.mkstemp(prefix='dbc_jdbc_out_', suffix='.log')
    os.close(_out_fd)
    proc = None
    try:
        with os.fdopen(_in_fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=True)

        env = os.environ.copy()
        env['DBCheck_NO_GEVENT_PATCH'] = '1'   # 子进程绝不能被 monkey-patch
        env['PYTHONIOENCODING'] = 'utf-8'

        _kw = {}
        if os.name == 'nt':
            # CREATE_NO_WINDOW：冻结版是控制台程序，不加会闪黑框
            _kw['creationflags'] = (getattr(_sp, 'CREATE_NO_WINDOW', 0x08000000)
                                    | getattr(_sp, 'CREATE_NEW_PROCESS_GROUP', 0x00000200))
        else:
            _kw['start_new_session'] = True  # 独立进程组，便于整树 kill

        with open(_in_path, 'r', encoding='utf-8') as fin, \
                open(_out_path, 'w', encoding='utf-8', errors='replace') as fout:
            proc = _sp.Popen(_jdbc_cli_command(), stdin=fin, stdout=fout,
                             stderr=_sp.STDOUT, env=env, cwd=str(PROJECT_ROOT), **_kw)
            # 注册到活跃子进程表：主进程退出时整树清理（同步等待中遇 Ctrl+C 同样要杀）
            try:
                from modules.core.proc_registry import register_proc
                register_proc(proc)
            except Exception:
                pass

            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    kill_process_tree(proc)
                    # 附带子进程日志尾段：里程碑打印（如 oracle thin/thick 阶段）
                    # 能直接暴露卡在哪一步，而非只有一句笼统超时
                    _tail = ''
                    try:
                        with open(_out_path, 'r', encoding='utf-8', errors='replace') as _f:
                            _tail = (_f.read() or '')[-500:].strip()
                    except OSError:
                        pass
                    return False, (f'连接测试超时（已超过 {timeout} 秒）。'
                                   f'请检查主机地址、端口与网络连通性，'
                                   f'并确认 JDBC 驱动 jar 与 Java 运行环境已就绪。'
                                   + (f'\n\n子进程日志尾段：\n{_tail}' if _tail else ''))
                time.sleep(0.05)

        # 子进程可能在写完结果后才退出，输出文件此时已完整
        try:
            with open(_out_path, 'r', encoding='utf-8', errors='replace') as f:
                out = f.read()
        except Exception:
            out = ''

        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith(RESULT_PREFIX):
                try:
                    r = json.loads(line[len(RESULT_PREFIX):])
                    return bool(r.get('ok')), str(r.get('msg') or '')
                except Exception as e:  # noqa: BLE001
                    return False, f'连接测试结果解析失败: {e}'

        tail = (out or '').strip().splitlines()[-5:]
        return False, ('连接测试子进程未返回结果'
                       + (f'：{" | ".join(tail)}' if tail else f'（退出码 {proc.returncode}）'))
    except Exception as e:  # noqa: BLE001
        if proc is not None and proc.poll() is None:
            kill_process_tree(proc)
        return False, f'连接测试子进程启动失败: {e}'
    finally:
        try:
            from modules.core.proc_registry import unregister_proc
            unregister_proc(proc)
        except Exception:
            pass
        for _p in (_in_path, _out_path):
            try:
                os.remove(_p)
            except Exception:
                pass
