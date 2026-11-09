# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Offline driver readiness and explicit legacy compatibility, without target probes."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import importlib
import os
import re
import zipfile
from pathlib import Path
from modules.core.paths import PROJECT_ROOT


def dependency(name):
    try:
        module = importlib.import_module(name)
        return module, None
    except (ImportError, OSError):
        return None, '组件未安装或原生动态库不可加载'


def jar_valid(path, class_name):
    try:
        with zipfile.ZipFile(path) as archive:
            return class_name.replace('.', '/') + '.class' in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def oracle11g_client(driver, asset):
    """Choose Thick before the first connection; never silently retry plaintext."""
    version = str(asset.get('version', '')).lower()
    if not (version.startswith(('11.', '11g')) or 'oracle 11' in version):
        return
    if driver.is_thin_mode():
        options = {}
        if asset.get('oracle_client_dir'):
            options['lib_dir'] = str(Path(asset['oracle_client_dir']).resolve(strict=True))
        if asset.get('oracle_config_dir'):
            options['config_dir'] = str(Path(asset['oracle_config_dir']).resolve(strict=True))
        try:
            driver.init_oracle_client(**options)
        except Exception as error:
            raise ValueError('Oracle 11g 需要兼容的 Thick 客户端；11.2 建议使用 19c 客户端，配置后重启采集进程') from error
    # 23c clients cannot be assumed compatible with 11g. Do not change a global
    # client initialized by other modules in this process.
    major = int(driver.clientversion()[0])
    if major not in {11, 12, 18, 19, 21}:
        raise ValueError('当前 Oracle Client 与 11g 未建立兼容范围，请使用经验证的 19c 客户端')
    if version.startswith('11.1') and major >= 19:
        raise ValueError('Oracle 11.1 需独立旧版客户端兼容验证，不能按11.2处理')


def readiness():
    """An import/file check is explicitly distinct from a database connection."""
    pymysql, _ = dependency('pymysql')
    oracle, _ = dependency('oracledb')
    dm, _ = dependency('dmPython')
    odbc, _ = dependency('pyodbc')
    tds, _ = dependency('pytds')
    jdbc, _ = dependency('jaydebeapi')
    jpype, _ = dependency('jpype')
    dm_jar = PROJECT_ROOT / 'drivers/dm/8/DmJdbcDriver8.jar'
    dm_jdbc = bool(jdbc and jpype and jar_valid(dm_jar, 'dm.jdbc.driver.DmDriver'))
    jvm = False
    if jpype:
        try:
            jvm = Path(jpype.getDefaultJVMPath()).is_file()
        except Exception:
            pass
    installed = []
    if odbc:
        try:
            installed = list(odbc.drivers())
        except Exception:
            pass
    oracle_mode = '未安装'
    if oracle:
        oracle_mode = 'Thin（11g需要另配Thick客户端）' if oracle.is_thin_mode() else 'Thick '+'.'.join(map(str,oracle.clientversion()))
    mismatch = []
    for path in sorted((PROJECT_ROOT/'drivers/gbase8a').glob('**/*.jar')):
        if not jar_valid(path, 'com.gbase.jdbc.Driver'):
            mismatch.append(path.name+' 不含8a驱动类，禁止作为8a驱动使用')
    rows = [
        dict(db_type='gbase8a',version='GBase 8a MPP Cluster',backend='PyMySQL（MySQL协议）',
             available=bool(pymysql),requirement='TLS证书校验，或数据库主机已验证SSH隧道；集群节点逐项采集',notes=mismatch),
        dict(db_type='dm',version='DM8',backend='dmPython / 独立进程JDBC',
             available=bool(dm or dm_jdbc and jvm),requirement='原生SSL证书目录；JDBC通过已验证SSH隧道',notes=['原生组件可加载' if dm else '原生组件不可用，使用JDBC隔离采集', 'JDBC及JVM就绪' if dm_jdbc and jvm else 'JDBC或JVM缺失']),
        dict(db_type='oracle',version='Oracle 11g',backend='python-oracledb Thick',
             available=bool(oracle and not oracle.is_thin_mode() and int(oracle.clientversion()[0]) in {11,12,18,19,21}),requirement='兼容的Oracle Client、11.1/11.2精确版本、TCPS钱包及服务端证书DN',notes=[oracle_mode]),
    ]
    for version in ('2005','2008','2014'):
        rows.append(dict(db_type='sqlserver',version='SQL Server '+version,backend='python-tds（隔离隧道） / ODBC 18（TLS）',
                         available=bool(tds or odbc and 'ODBC Driver 18 for SQL Server' in installed),
                         requirement='2005使用已验证SSH隧道；2008/2014可选验证证书的TLS或显式SSH隧道',notes=['ODBC驱动：'+(', '.join(installed) or '未安装'), 'TDS组件可加载' if tds else 'TDS组件缺失']))
    for row in rows:
        row['status'] = 'component_ready' if row['available'] else 'dependency_missing'
        row['connection_verified'] = False
    return {'scope':'offline_local_environment','drivers':rows}


def jdbc_dm(instance):
    """Called only inside the bounded worker and only over a verified tunnel."""
    import jaydebeapi
    import jpype
    path = PROJECT_ROOT/'drivers/dm/8/DmJdbcDriver8.jar'
    if not jar_valid(path,'dm.jdbc.driver.DmDriver'):
        raise ValueError('DM8 JDBC 驱动缺失或驱动类不匹配')
    if not jpype.isJVMStarted():
        jpype.startJVM(classpath=[str(path)])
    connection = jaydebeapi.connect('dm.jdbc.driver.DmDriver',
                                   f"jdbc:dm://127.0.0.1:{int(instance['port'])}",
                                   {'user':instance['user'],'password':instance['password'],
                                    'loginTimeout':'5','socketTimeout':'15000'},str(path))
    connection.jconn.setAutoCommit(False)
    connection.jconn.setReadOnly(True)
    return connection


class TunnelConnection:
    def __init__(self, connection, tunnel):
        self.connection,self.tunnel=connection,tunnel
    def __getattr__(self,name):
        return getattr(self.connection,name)
    def close(self):
        try:
            self.connection.close()
        finally:
            self.tunnel.close()


def verified_tunnel(data):
    host=data.get('host') or data.get('ip')
    if not data.get('ssh_host') or data['ssh_host'] != host:
        raise ValueError('加密隧道须连接数据库主机自身的已验证SSH端点')
    from modules.ssh import SSHTunnel
    tunnel=SSHTunnel(ssh_host=data['ssh_host'],ssh_port=int(data.get('ssh_port') or 22),
                     ssh_user=data.get('ssh_user'),ssh_password=data.get('ssh_password'),
                     ssh_key=data.get('ssh_key_file'),remote_host='127.0.0.1',remote_port=int(data['port']))
    tunnel.__enter__()
    return tunnel


def connect_gbase8a(data):
    import pymysql
    import ssl
    tunnel=None
    try:
        options=dict(user=data['user'],password=data['password'],database=data.get('database') or 'information_schema',
                     connect_timeout=5,read_timeout=15,write_timeout=15,autocommit=False,local_infile=False)
        if data.get('ssh_host') and data.get('ssh_enabled',True):
            tunnel=verified_tunnel(data)
            options.update(host='127.0.0.1',port=tunnel.local_port)
            connection=pymysql.connect(**options)
        else:
            ca=data.get('tls_ca_file')
            if not ca or not Path(ca).is_file():
                raise ValueError('GBase 8a直连需要可信CA；也可配置数据库主机的SSH加密隧道')
            context=ssl.create_default_context(cafile=ca)
            context.minimum_version=ssl.TLSVersion.TLSv1_2
            options.update(host=data.get('host') or data.get('ip'),port=int(data['port']),ssl=context,defer_connect=True)
            connection=pymysql.Connection(**options)
            if not getattr(connection,'_ssl_required',False):
                connection.close()
                raise ValueError('当前驱动无法强制TLS，已在认证前阻断')
            connection.connect()
        return TunnelConnection(connection,tunnel) if tunnel else connection
    except Exception:
        if tunnel:tunnel.close()
        raise


def connect_sqlserver(data):
    tunnel=None
    try:
        if data.get('ssh_host') and data.get('ssh_enabled',True):
            import pytds
            tunnel=verified_tunnel(data)
            # TDS 7.2 is accepted by 2005, 2008 and 2014. Queries are selected
            # after the authenticated server reports its actual product version.
            connection=pytds.connect(server='127.0.0.1',port=tunnel.local_port,user=data['user'],
                                     password=data['password'],database=data.get('database') or 'master',
                                     tds_version=pytds.tds_base.TDS72,login_timeout=5,timeout=15,
                                     autocommit=False,use_mars=False,appname='DBPulse')
            return TunnelConnection(connection,tunnel)
        import pyodbc
        if 'ODBC Driver 18 for SQL Server' not in pyodbc.drivers():
            raise ValueError('请安装ODBC Driver 18，或配置数据库主机的SSH加密隧道')
        quote=lambda value:'{'+str(value).replace('}','}}')+'}'
        connection=pyodbc.connect('DRIVER={ODBC Driver 18 for SQL Server};SERVER='+quote(str(data.get('host') or data.get('ip'))+','+str(int(data['port'])))+
                                  ';UID='+quote(data['user'])+';PWD='+quote(data['password'])+
                                  ';DATABASE='+quote(data.get('database') or 'master')+
                                  ';Encrypt=yes;TrustServerCertificate=no;Connection Timeout=5;APP=DBPulse;',timeout=5,autocommit=False)
        connection.timeout=15
        return connection
    except Exception:
        if tunnel:tunnel.close()
        raise
