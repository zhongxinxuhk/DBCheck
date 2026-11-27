# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Inspection execution and result recording outside the Web application."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import copy
import datetime
import logging
import os
import re
import sys

from i18n import t as _t
from modules.core import paths
from modules.db_types.connection_tests import (
    test_dm_connection,
    test_gbase8a_connection,
    test_gbase_connection,
    test_halodb_connection,
    test_ivorysql_connection,
    test_kingbase_connection,
    test_mysql_connection,
    test_oracle_connection,
    test_pg_connection,
    test_sqlserver_connection,
    test_sqlserver_jdbc_connection,
    test_tidb_connection,
    test_yashandb_connection,
)

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else str(paths.PROJECT_ROOT)


def _ts():
    return datetime.datetime.now().strftime('%H:%M:%S')


_I18N_MAP = {
    'report.risk_high': '高风险', 'report.risk_mid': '中风险', 'report.risk_low': '低风险',
    'report.risk_suggest': '建议', 'report.risk_suggestion': '建议',
    'report.risk_dba': 'DBA',
    'report.risk_tablespace': '表空间',
    'report.risk_ts_high': '表空间使用率过高', 'report.risk_ts_mid': '表空间使用率偏高',
    'report.risk_invalid_obj': '无效对象', 'report.risk_invalid_desc': '存在无效对象',
    'report.risk_locked': '账户锁定', 'report.risk_locked_desc': '存在锁定账户',
    'report.risk_alert': 'Alert日志错误', 'report.risk_alert_desc': '近7天存在错误日志',
    'report.risk_fix_ts': '查询表空间使用情况',
    'report.risk_fix_alert': '检查Alert日志具体内容',
    'report.risk_fix_locked': '查看锁定账户',
    'report.risk_fix_sql': '修复 SQL',
    'report.severity_high': '高', 'report.severity_mid': '中', 'report.severity_low': '低',
    'report.health_excellent': '优秀', 'report.health_good': '良好',
    'report.health_fair': '一般', 'report.health_attention': '需关注',
}
def _tr(s):
    """翻译 i18n key（如 report.risk_high → 高风险），非 key 原样返回"""
    if not s: return ''
    return _I18N_MAP.get(s, s)

def _web_log_is_console_only(msg):
    """判断一条日志是否仅输出到后端控制台、不推送到前端巡检日志。

    这些内部输出不应出现在 Web UI 的前端巡检日志中，仅留在后端控制台便于排查：
    - 后台采集内部日志 [metrics]
    - 连接状态类日志（连接成功/连接失败，既有规则，本次未改动）
    - 内部调试日志 [DEBUG] 开头（如 AI 诊断前 version 信息、基线检查、章节追加等）
    - AI 诊断框架日志 [AIAdvisor] 开头（RAG 知识库启用/初始化等）
    - 插件加载/注册日志 [Plugin] / [插件] 开头
    - 各 JDBC 插件注册成功提示（含「插件注册成功」，如
      [ClickHouse]/[DB2]/[HGDB]/[Oracle JDBC]/[UXDB]）
    - AI 诊断调用提示（含「正在调用 AI 诊断」，如
      「🤖 正在调用 AI 诊断（ollama / qwen3:8b）...」）
    - 报告生成内部内存监控日志 [Report] 开头（防御性过滤；当前其在拦截器恢复后执行，
      本身不会混入前端，此处一并屏蔽以防未来路径变化）

    注意：正常的巡检进度信息（如 [MySQL] 开始巡检...、📊 开始执行巡检 SQL...、
    🔍 正在进行慢查询深度分析...、✅ 慢查询深度分析完成...、[WARN]/[ERROR]/[INFO] 等）
    不会被上述规则过滤，仍会推送到前端。
    """
    _m = msg.lstrip()
    # 后台采集内部日志
    if _m.startswith('[metrics]'):
        return True
    # 连接成功类日志仅输出到后端控制台（避免刷屏）；连接失败属错误信息，
    # 必须推送到前端巡检日志——巡检失败时用户排查的第一手依据（原规则把
    # 连接失败也过滤，导致「巡检失败却没有任何错误日志」）。
    if '连接成功' in msg:
        return True
    # 内部调试日志
    if _m.startswith('[DEBUG]'):
        return True
    # AI 诊断框架日志
    if _m.startswith('[AIAdvisor]'):
        return True
    # 插件加载/注册日志
    if _m.startswith('[Plugin]') or _m.startswith('[插件]'):
        return True
    # 各 JDBC 插件注册成功提示（[X] 插件注册成功）
    if '插件注册成功' in msg:
        return True
    # AI 诊断调用提示
    if '正在调用 AI 诊断' in msg:
        return True
    # 报告生成内部内存监控日志（防御性）
    if _m.startswith('[Report]'):
        return True
    return False


# ── 中央日志中枢：把巡检 / 诊断等运行日志按分类写入 loghub ──
def _hub_log(msg, category='inspection'):
    """把一条运行日志写入 loghub（带 category 标签）。

    智能分析 / AI 诊断类消息自动归到 ``diagnosis`` 分类，便于「运行日志」页按
    巡检 / 定时巡检 / 智能诊断 / 监控 过滤查看。日志同时仍走原有 task['log'] 与
    socketio 推送（保持巡检面板不变），此处只是额外汇聚到中枢。
    """
    if not msg:
        return
    _cat = category
    if _cat == 'inspection' and ('智能分析' in msg or 'AI 诊断' in msg or msg.startswith('[诊断]')):
        _cat = 'diagnosis'
    try:
        logging.getLogger('dbcheck.' + _cat).info(msg)
    except Exception:
        pass


def _task_log_path(task_id):
    """巡检任务日志文件路径：data/logs/inspection-YYYYMMDD.log（按天滚动）。"""
    try:
        log_dir = os.path.join(str(paths.DATA_DIR), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir,
                            'inspection-%s.log' % datetime.datetime.now().strftime('%Y%m%d'))
    except Exception:
        return None


def _append_task_log(task_id, msg):
    """把巡检日志落盘。

    此前巡检日志只存在于内存 task['log'] 与 socket 推送中：进程重启 / 前端未订阅
    socket / 任务对象被回收后，失败现场全部丢失，用户只能看到一句「巡检失败」而
    拿不到任何可排查的日志。这里按天追加写入 data/logs/inspection-YYYYMMDD.log。
    """
    if not msg:
        return
    path = _task_log_path(task_id)
    if not path:
        return
    line = '%s [%s] %s' % (
        datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        (task_id or '-')[:8],
        str(msg).replace('\n', '\n    '),
    )
    try:
        # 写前按阈值轮转（单文件默认 50MB），避免连续巡检把当天文件写成巨型文件
        from modules.core.log_manager import append_line
        append_line(path, line, family='inspection')
    except Exception:
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass


def execute_inspection(task_id, task, db_info, inspector_name, template_id=None,
                       chapter_ids=None, *, emit):
    from modules.inspection.sql_safety import inspection_lease
    kind = db_info.get('_db_type') or db_info.get('db_type') or 'dm'
    try:
        with inspection_lease(db_info, kind):
            return _run_inspection_task(task_id, task, db_info, inspector_name, template_id, chapter_ids, emit)
    except TimeoutError as error:
        task.update(status='error', error_msg=str(error))
        emit('error', {'msg': str(error)})
        emit('done', {'msg': str(error), 'task_id': task_id})


def _run_inspection_task(task_id, task, db_info, inspector_name, template_id, chapter_ids, emit):
    """
    通用数据库巡检任务函数
    支持: MySQL, PostgreSQL, TiDB, SQL Server, IvorySQL, DM8
    通过 task_configs 字典配置各数据库类型的差异参数
    """
    db_type = db_info.pop('_db_type', None)
    if not db_type:
        # 兜底：无论是否走 socketio，都必须把任务状态置为 error，
        # 否则前端 pollChatTaskStatus 会无限「正在巡检」。
        task.update(status='error', error_msg='缺少数据库类型')
        emit('error', {'msg': '缺少数据库类型'})
        return

    if db_type == 'sqlserver_jdbc':
        from modules.db_types.connection_profile import resolve_sqlserver_mode

        db_info['connection_mode'] = resolve_sqlserver_mode(
            dict(db_info, db_type=db_type))

    # KingbaseES 数据库名修正：若未设置或为 postgres（从 PG 复制而来），则改为 kingbase
    if db_type == 'kingbase':
        _db = db_info.get('database') or ''
        if _db in ('', 'postgres'):
            db_info['database'] = 'kingbase'

    # UXDB 数据库名修正：若未设置或为 postgres（从 PG 复制而来），则改为 uxdb
    if db_type == 'uxdb':
        _db = db_info.get('database') or ''
        if _db in ('', 'postgres'):
            db_info['database'] = 'uxdb'

    # HaloDB 数据库名修正：若未设置或为 postgres（从 PG 复制而来），则改为 halo
    if db_type == 'halodb':
        _db = db_info.get('database') or ''
        if _db in ('', 'postgres'):
            db_info['database'] = 'halo'

    _insp_name = db_info.get('name') or db_info.get('ip') or '?'
    _insp_host = f"{db_info.get('ip')}:{db_info.get('port')}"
    print(f"[巡检] ===== 巡检任务开始：目标 {_insp_name} ({_insp_host}) db_type={db_type} =====\n"
          f"[巡检] 说明：[Monitor] 开头的日志为本系统后台监控独立任务（周期性遍历全部已保存实例），"
          f"并非本次巡检建立连接，请勿误判为串库。", flush=True)

    task_configs = {
        'mysql': dict(
            module_name='main_mysql',
            connect_test=test_mysql_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_mysql',
            filename_key='webui.mysql_report_filename',
            history_db_type='mysql',
            instance_prefix='mysql',
            error_task_name='MySQL',
            log_start_key='webui.log_mysql_start',
            err_module_key='webui.err_mysql_module',
            label_default='unknown',
            db_name_default='mysql',
        ),
        'mariadb': dict(
            module_name='main_mariadb',
            connect_test=test_mysql_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_mariadb',
            filename_key='webui.mariadb_report_filename',
            history_db_type='mariadb',
            instance_prefix='mariadb',
            error_task_name='MariaDB',
            log_start_key='webui.log_mariadb_start',
            err_module_key='webui.err_mariadb_module',
            label_default='MariaDB',
            db_name_default='mysql',
        ),
        'oceanbase': dict(
            module_name='main_oceanbase',
            connect_test=test_mysql_connection,
            connect_test_args=lambda info: [info['ip'], info['port'],
                                            (info['user'] + '@' + info['tenant']) if info.get('tenant') else info['user'],
                                            info['password'], info.get('database', 'sys'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'],
                                        (info['user'] + '@' + info['tenant']) if info.get('tenant') else info['user'],
                                        info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database', 'sys'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_mysql',
            filename_key='webui.oceanbase_report_filename',
            history_db_type='oceanbase',
            instance_prefix='oceanbase',
            error_task_name='OceanBase',
            log_start_key='webui.log_oceanbase_start',
            err_module_key='webui.err_oceanbase_module',
            label_default='OceanBase',
            db_name_default='sys',
        ),
        'pg': dict(
            module_name='main_pg',
            connect_test=test_pg_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database', 'postgres'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database', 'postgres'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_pg',
            filename_key='webui.pg_report_filename',
            history_db_type='pg',
            instance_prefix='pg',
            error_task_name='PostgreSQL',
            log_start_key='webui.log_pg_start',
            err_module_key='webui.err_pg_module',
            label_default='unknown',
            db_name_default='postgres',
        ),
        'tidb': dict(
            module_name='main_tidb',
            connect_test=test_tidb_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database', 'mysql'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_tidb',
            filename_key='webui.tidb_report_filename',
            history_db_type='tidb',
            instance_prefix='tidb',
            error_task_name='TiDB',
            log_start_key='webui.log_tidb_start',
            err_module_key='webui.err_tidb_module',
            label_default='unknown',
            db_name_default='mysql',
        ),
        'sqlserver': dict(
            module_name='main_sqlserver',
            connect_test=test_sqlserver_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database', 'master'), info],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database'), 'label': info.get('name')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_sqlserver',
            filename_key='webui.sqlserver_report_filename',
            history_db_type='sqlserver',
            instance_prefix='sqlserver',
            error_task_name='SQL Server',
            log_start_key='webui.log_sqlserver_start',
            err_module_key='webui.err_sqlserver_module',
            label_default='SQLServer',
            db_name_default='master',
        ),
        # ── SQL Server (JDBC 双轨) —— P1/T-014 ──
        #   与 'sqlserver' 共用 main_sqlserver_dual.getData，按 connection_mode 路由：
        #     - 'odbc'  → main_sqlserver.SQLServerInspector（默认，向后兼容）
        #     - 'jdbc'  → plugins/available/sqlserver_jdbc.MssqlJdbcInspector
        #     - 'auto'  → 优先 JDBC（探测 jar + JPype1），失败回退 ODBC
        #   UI 前端可在「新增 SQL Server 实例」表单中通过 connection_mode 下拉框选择；
        #   后端通过 ssh_info['connection_mode'] 字段透传。
        #   注意：本类型缺省 / 空串一律按 'jdbc' 处理（类型语义即 JDBC，兼容旧实例）；
        #   显式 'jdbc' 时环境不可用会直接报错，不会静默回退 ODBC。
        'sqlserver_jdbc': dict(
            module_name='main_sqlserver_dual',
            connect_test=test_sqlserver_jdbc_connection,
            connect_test_args=lambda info: [
                info['ip'], int(info['port']), info['user'], info['password'],
                info.get('database', 'master'),
                {
                    'connection_mode': info.get('connection_mode') or 'jdbc',
                    'jdbc_url': info.get('jdbc_url', '') or '',
                    'instance_name': info.get('instance_name', '') or '',
                    'encrypt': bool(info.get('encrypt', False)),
                    'trust_server_certificate': bool(info.get('trust_server_certificate', True)),
                    'driver_version': info.get('driver_version', ''),
                },
            ],
            getdata_args=lambda info: ([info['ip'], int(info['port']), info['user'], info['password']],
                                       {
                                           'ssh_info': {
                                               'database': info.get('database', 'master'),
                                               'connection_mode': info.get('connection_mode') or 'jdbc',
                                               'jdbc_url': info.get('jdbc_url', '') or '',
                                               'instance_name': info.get('instance_name', '') or '',
                                               'encrypt': bool(info.get('encrypt', False)),
                                               'trust_server_certificate': bool(info.get('trust_server_certificate', True)),
                                               'driver_version': info.get('driver_version', ''),
                                           },
                                           'template_id': template_id,
                                           'connection_mode': info.get('connection_mode') or 'jdbc',
                                           'label': info.get('name'),
                                       }),
            conn_attr='conn',
            smart_analyze='smart_analyze_sqlserver',
            filename_key='webui.sqlserver_jdbc_report_filename',
            history_db_type='sqlserver_jdbc',
            instance_prefix='sqlserver_jdbc',
            error_task_name='SQL Server (JDBC)',
            log_start_key='webui.log_sqlserver_jdbc_start',
            err_module_key='webui.err_sqlserver_jdbc_module',
            label_default='SQLServer-JDBC',
            db_name_default='master',
        ),
        'ivorysql': dict(
            module_name='main_ivorysql',
            connect_test=test_ivorysql_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database', 'ivorysql'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database', 'ivorysql'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_ivorysql',
            filename_key='webui.ivorysql_report_filename',
            history_db_type='ivorysql',
            instance_prefix='ivorysql',
            error_task_name='IvorySQL',
            log_start_key='webui.log_ivorysql_start',
            err_module_key='webui.err_ivorysql_module',
            label_default='unknown',
            db_name_default='ivorysql',
        ),
        'halodb': dict(
            module_name='main_halodb',
            connect_test=test_halodb_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database', 'halo'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database', 'halo'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_pg',
            filename_key='webui.halodb_report_filename',
            history_db_type='halodb',
            instance_prefix='halodb',
            error_task_name='HaloDB',
            log_start_key='webui.log_halodb_start',
            err_module_key='webui.err_halodb_module',
            label_default='unknown',
            db_name_default='halo',
        ),
        'kingbase': dict(
            module_name='main_kingbase',
            connect_test=test_kingbase_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database', 'kingbase'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database', 'kingbase'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_kingbase',
            filename_key='webui.kingbase_report_filename',
            history_db_type='kingbase',
            instance_prefix='kingbase',
            error_task_name='KingbaseES',
            log_start_key='webui.log_kingbase_start',
            err_module_key='webui.err_kingbase_module',
            label_default='unknown',
            db_name_default='kingbase',
        ),
        'dm': dict(
            module_name='main_dm',
            connect_test=test_dm_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('driver_version','')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'db_name': info.get('database', 'DAMENG'),
                                        'driver_version': info.get('driver_version','')}),
            conn_attr='conn_db',
            smart_analyze='smart_analyze_dm',
            filename_key='webui.dm_report_filename',
            history_db_type='dm',
            instance_prefix='dm',
            error_task_name='DM8',
            log_start_key='webui.log_dm_start',
            err_module_key='webui.err_dm_module',
            label_default='DM8',
            db_name_default='DAMENG',
        ),
        'yashandb': dict(
            module_name='main_yashandb',
            connect_test=test_yashandb_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database', 'yashandb'), info.get('driver_version', '')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                       {'ssh_info': {}, 'template_id': template_id, 'database': info.get('database', 'yashandb'),
                                        'driver_version': info.get('driver_version', '')}),
            conn_attr='conn_db',
            smart_analyze='smart_analyze_yashandb',
            filename_key='webui.yashandb_report_filename',
            history_db_type='yashandb',
            instance_prefix='yashandb',
            error_task_name='YashanDB',
            log_start_key='webui.log_yashandb_start',
            err_module_key='webui.err_yashandb_module',
            label_default='YashanDB',
            db_name_default='YASHANDB',
        ),
        'gbase8a': dict(
            module_name='main_gbase8a',connect_test=test_gbase8a_connection,
            connect_test_args=lambda info:[{**info,'host':info['ip']}],
            getdata_args=lambda info:([info['ip'],info['port'],info['user'],info['password']],
                                     {'database':info.get('database'),'template_id':template_id,
                                      'ssh_info':{'tls_ca_file':info.get('tls_ca_file',''),
                                                  'gbase8a_allow_plaintext':info.get('gbase8a_allow_plaintext',0)}}),
            conn_attr='conn_db2',smart_analyze='smart_analyze_gbase8a',
            filename_key='webui.gbase8a_report_filename',history_db_type='gbase8a',instance_prefix='gbase8a',
            error_task_name='GBase 8a MPP',log_start_key='webui.log_gbase8a_start',err_module_key='webui.err_gbase8a_module',
            label_default='GBase 8a MPP',db_name_default='information_schema'),
        'gbase': dict(
            module_name='main_gbase',
            connect_test=test_gbase_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info['user'], info['password'], info.get('database', 'testdb'), info.get('gbase_server_name', 'gbase01'), info.get('driver_version','')],
            getdata_args=lambda info: ([info['ip'], info['port'], info['user'], info['password']],
                                           {'ssh_info': {}, 'template_id': template_id,
                                            'database': info.get('database', 'testdb'),
                                            'gbase_server_name': info.get('gbase_server_name', 'gbase01'),
                                            'driver_version': info.get('driver_version','')}),
            conn_attr='conn_db2',
            smart_analyze='smart_analyze_gbase',
            filename_key='webui.gbase_report_filename',
            history_db_type='gbase',
            instance_prefix='gbase',
            error_task_name='GBase 8s',
            log_start_key='webui.log_gbase_start',
            err_module_key='webui.err_gbase_module',
            label_default='unknown',
            db_name_default='gbase01',
        ),
        'oracle': dict(
            module_name='main_oracle_full',
            connect_test=test_oracle_connection,
            connect_test_args=lambda info: [info['ip'], info['port'], info.get('user', 'sys'), info['password'],
                                            info.get('service_name') or info.get('sid') or info.get('database', 'orcl'),
                                            bool(info.get('sysdba', info.get('user', 'sys').upper() == 'SYS'))],
            getdata_args=lambda info: ([info['ip'], info['port'], info.get('user', 'sys'), info['password']],
                                       {'ssh_info': {}, 'service_name': info.get('service_name'),
                                        'sid': info.get('sid') or info.get('database', 'orcl'),
                                        'sysdba': bool(info.get('sysdba', info.get('user', 'sys').upper() == 'SYS')),
                                        'inspector_name': None, 'desensitize': info.get('desensitize', False),
                                        'template_id': template_id}),
            conn_attr=None,  # Oracle 连接在 single_inspection 内部管理，无需检查 conn_db
            smart_analyze='smart_analyze_oracle',
            filename_key='webui.oracle_report_filename',
            history_db_type='oracle',
            instance_prefix='oracle',
            error_task_name='Oracle',
            log_start_key='webui.log_oracle_start',
            err_module_key='webui.err_oracle_module',
            label_default='ORACLE',
            db_name_default='orcl',
        ),
    }

    def _emit(event, data):
        msg = data.get('msg', '')
        if msg and task is not None:
            task.setdefault('log', []).append(msg)
        if event == 'log':
            _hub_log(msg, data.get('category', 'inspection'))
            _append_task_log(task_id, msg)   # 同时落盘，失败现场可追溯
        emit(event, data)

    cfg = task_configs.get(db_type)
    if not cfg:
        # 尝试从插件系统加载配置
        try:
            from modules.pluginkit.loader import get_plugin_task_config
            cfg = get_plugin_task_config(db_type)
            if cfg:
                # 插件配置，需要动态导入模块
                import importlib.util
                plugin_path = cfg.get('plugin_path')
                if plugin_path:
                    main_file = cfg.get('main_file', 'main_plugin.py')
                    main_path = os.path.join(str(plugin_path), str(main_file))
                    spec = importlib.util.spec_from_file_location(f"plugin_{db_type}", main_path)
                    if spec:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        cfg['module'] = mod
        except Exception as e:
            print(f"[WebUI] 加载插件配置失败: {e}")

        if not cfg:
            err_msg = f'不支持的数据库类型: {db_type}'
            if task:
                task['status'] = 'error'
                task['error_msg'] = err_msg
            _emit('error', {'msg': err_msg})
            _emit('done', {'msg': err_msg, 'task_id': task_id})
            print(f"[巡检] ===== 巡检任务结束（失败：不支持类型 {db_type}）=====", flush=True)
            return

    _emit('log', {'msg': _t(cfg['log_start_key']).format(ts=_ts())})
    _emit('inspection_step', {'step': 0, 'msg': _t('webui.log_connecting').format(ts=_ts(), host=db_info['ip'], port=db_info['port'])})

    _tunnel = None      # SSH 本地端口转发（若配置了 SSH）
    conn_info: dict = db_info  # 实际用于建连的参数（隧道场景下主机/端口会被改写）
    # 子进程巡检（jdbc_inspection_cli）已提前建好隧道并通过 _conn_ip/_conn_port
    # 传入，此时直接复用，避免在建好的隧道上再套一层隧道。
    if db_info.get('_tunnel_ready') and db_info.get('_conn_ip'):
        conn_info = dict(db_info)
        conn_info['host'] = conn_info['ip'] = db_info['_conn_ip']
        conn_info['port'] = int(db_info.get('_conn_port') or db_info['port'])

    try:
        # 插件类型：使用动态导入的模块
        if cfg.get('module'):
            mod = cfg['module']
        else:
            # 内置类型：常规导入（entrypoints 包下的 main_* 模块，绝对导入）
            mod = __import__('modules.entrypoints.' + cfg['module_name'], fromlist=['_'])

        _emit('log', {'msg': _t('webui.log_connecting').format(ts=_ts(), host=db_info['ip'], port=db_info['port'])})

        # ── SSH 跳板：数据库只监听远端 127.0.0.1 时，必须走端口转发 ──────────
        # 此前 SSH 配置只用于采集主机指标，数据库连接始终直连：
        # 用户按 Navicat 习惯填写 127.0.0.1 时，实际连的是巡检机自己的回环
        # → Connection refused（或连到本机另一套库），且日志里看不出原因。
        if db_info.get('ssh_tunnel') and not db_info.get('_tunnel_ready'):
            from modules.ssh import prepare_db_endpoint

            conn_info, _tunnel = prepare_db_endpoint(
                db_info, log=lambda message: _emit('log', {'msg': message}))
        elif db_info.get('ssh_host') and not db_info.get('ssh_tunnel', False):
            # 隧道开关被关闭：SSH 只采集系统指标，数据库直连
            _ip = (db_info.get('ip') or '').strip().lower()
            _emit('log', {'msg': f'ℹ️ 已关闭 SSH 隧道，数据库将直连 {db_info.get("ip")}:'
                                  f'{db_info.get("port")}（SSH 仅用于系统指标采集）'})
            if _ip in ('127.0.0.1', 'localhost', '::1'):
                _emit('log', {'msg': '⚠️ 数据库主机是回环地址（127.0.0.1），关闭隧道后会连接'
                                      '本机回环而非 SSH 服务器。堡垒机/内网映射场景请开启'
                                      '「通过 SSH 隧道连接数据库」。'})

        ok, ver = cfg['connect_test'](*cfg['connect_test_args'](conn_info))
        if not ok:
            raise RuntimeError(_t('webui.err_db_connect').format(ver=ver))
        _emit('log', {'msg': _t('webui.log_connected').format(ts=_ts(), ver=ver)})
        _emit('inspection_step', {'step': 1, 'msg': _t('webui.log_executing_sql').format(ts=_ts())})

        # SSH
        # 透传 ssh_tunnel 与 _tunnel_ready：插件层（clickhouse_jdbc 等）据此判断
        # 是否需要自建隧道。两者都取自 conn_info —— 隧道是否已经由本函数建好，
        # 只有 conn_info 知道；只看 db_info 会让插件再建一条（隧道套隧道）。
        ssh_info = {}
        if db_info.get('ssh_host'):
            ssh_info = {k: conn_info[k] for k in (
                'ssh_host', 'ssh_port', 'ssh_user', 'ssh_password', 'ssh_key_file',
                'ssh_key_password', 'ssh_ebpf', 'ssh_tunnel', '_tunnel_ready',
            ) if k in conn_info}

        if db_type=='gbase8a':
            ssh_info['tls_ca_file']=db_info.get('tls_ca_file','')
            # 明文直连开关必须随 ssh_info 一起下传；缺省 0 = 保持强制 TLS 校验
            ssh_info['gbase8a_allow_plaintext']=db_info.get('gbase8a_allow_plaintext',0)

        # MongoDB 专用参数透传到 ssh_info（供插件 getData → MongoConnectionConfig 使用）
        if db_type == 'mongodb':
            for _mk in ('database', 'connect_mode', 'auth_source', 'auth_mechanism',
                        'replica_set', 'tls', 'tls_ca_file', 'tls_cert_key_file',
                        'tls_allow_invalid_certs'):
                if _mk in db_info:
                    ssh_info[_mk] = db_info[_mk]
        # Redis / Redis Cluster 专用参数透传到 ssh_info（供插件 getData 使用）
        if db_type in ('redis', 'redis-cluster'):
            for _mk in ('database', 'seed_nodes'):
                if _mk in db_info:
                    ssh_info[_mk] = db_info[_mk]
        # SQL Server (JDBC) 专用参数透传到 ssh_info（P1/T-014）
        #   - connection_mode: 'odbc' / 'jdbc' / 'auto'，控制 main_sqlserver_dual 路由
        #   - jdbc_url / instance_name / encrypt / trust_server_certificate: JDBC 连接参数
        if db_type == 'sqlserver_jdbc':
            for _mk in ('database', 'connection_mode', 'jdbc_url', 'instance_name',
                        'encrypt', 'trust_server_certificate', 'driver_version'):
                if _mk in db_info:
                    ssh_info[_mk] = db_info[_mk]

        # 激活 print 拦截器（需在 getData 之前就激活，因为 Oracle 的日志在 getData 内部产生）
        import builtins as _bi
        _orig_print = _bi.print
        def _web_print(*_a, **_kw):
            _sep = _kw.get('sep', ' ')
            _msg = _sep.join(str(x) for x in _a)
            _msg_clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', _msg)
            # 后台采集/内部日志（如 [metrics]）及连接状态日志（连接成功/连接失败）仅输出到后端控制台，不推送到前端巡检日志
            if _msg_clean.strip() and not _web_log_is_console_only(_msg_clean):
                _emit('log', {'msg': _msg_clean})
            _orig_print(*_a, **_kw)
        _bi.print = _web_print

        try:
            # getData
            _pos, _kw = cfg['getdata_args'](conn_info)
            if ssh_info:
                _kw['ssh_info'] = ssh_info
            data = mod.getData(*_pos, **_kw)

            # 绑定调用者选择的数据源，避免同主机多库时台账和实例基线关联含糊。
            _target = getattr(data, 'inspector', data)
            _target_context = getattr(_target, 'context', None)
            if isinstance(_target_context, dict) and (task or {}).get('datasource_id'):
                _target_context['_instance_id'] = task['datasource_id']

            # 章节可选：将前端传入的 chapter_ids 注入到检查器实例（兼容所有内置/插件入口）。
            # 内置入口的 CompatWrapper.inspector 与插件引擎均暴露 chapter_ids 属性，
            # collect_data / _render_context / _load_chapters_from_db 已消费该字段：
            #   None        -> 全量巡检（不过滤）
            #   [int, ...]  -> 仅巡检选中章节
            if chapter_ids is not None:
                _insp = getattr(data, 'inspector', None)
                if _insp is None and hasattr(data, 'chapter_ids'):
                    _insp = data
                if _insp is not None and hasattr(_insp, 'chapter_ids'):
                    _insp.chapter_ids = chapter_ids

            if data is None or (cfg['conn_attr'] and getattr(data, cfg['conn_attr'], None) is None):
                raise RuntimeError(_t('webui.err_getdata_none'))

            # AI诊断
            _emit('inspection_step', {'step': 2, 'msg': _t('webui.log_analyzing').format(ts=_ts())})

            context = data.checkdb('builtin')

        finally:
            _bi.print = _orig_print

        if not context:
            raise RuntimeError(_t('webui.err_checkdb_false'))

        if context.get('ai_advice'):
            _emit('log', {'msg': _t('webui.log_ai_done').format(ts=_ts())})
        if task:
            task['ai_advice'] = context.get('ai_advice', '')

        # 生成报告
        _emit('inspection_step', {'step': 3, 'msg': _t('webui.log_generating_report').format(ts=_ts())})
        label_name = db_info.get('name', cfg['label_default'])
        db_name = db_info.get('database') or cfg['db_name_default']

        context['co_name'] = [{'CO_NAME': db_name}]
        context['port'] = [{'PORT': db_info['port']}]
        context['ip'] = [{'IP': db_info['ip']}]

        inspector_nm = db_info.get('inspector_name') or inspector_name or 'Jack'

        reports_dir = str(paths.REPORTS_DIR)
        os.makedirs(reports_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        filename_tmpl = _t(cfg['filename_key'])
        # 数据源名称由用户输入，可能含 Windows 非法字符（如 10.60.6.216:1433
        # 里的冒号）或超长；直接拼进文件名会让 Document.save() 抛 OSError，
        # 最终只显示「生成报告文件失败」。这里统一收敛为安全文件名。
        from modules.core.filename_safety import build_report_filename
        file_name = build_report_filename(
            filename_tmpl, ip=db_info['ip'], name=label_name, timestamp=timestamp,
            suffix=task_id, extension='.docx')
        ofile = os.path.join(reports_dir, file_name)

        from modules.core.data_security import redact
        # 台账自动同步只使用采集结果，不读取任何凭据；在脱敏前保留基础属性快照。
        _inventory_basic_info = copy.deepcopy(
            context.get('_basic_info_observed',
                        context.get('basic_info_db', context.get('basic_info') or {})))
        _inventory_system_info = copy.deepcopy(context.get('system_info') or {})
        # 表空间、账号等专项结果只做实测快照，绝不写回 metadata.tables。
        _inventory_specialized = {}
        try:
            from modules.operations.inventory_sync import build_specialized_observation
            _inventory_specialized = build_specialized_observation(context)
        except Exception as _special_err:
            print('[WARN] 专项台账实测快照提取失败: %s' % _special_err)
        _owner_details_snapshot = copy.deepcopy(context.get('_owner_details') or {})
        context = redact(context, mask_identity=True, preserve_network=True)
        context['_owner_details'] = redact(_owner_details_snapshot, preserve_network=True)
        if hasattr(data, 'context'):
            data.context = context
        if getattr(data, 'inspector', None) is not None:
            data.inspector.context = context

        # ── 插件系统：在生成报告前执行，使插件发现并入统一问题清单 ────────
        # 这样 docx 报告、定时通知、巡检历史、Web UI 智能分析才能看到同一批问题。
        try:
            from modules.pluginkit.core import run_plugin_inspections_for_db
            # 构建 SQL 执行器（使用当前检查器的连接）
            def _exec_plugin_sql(sql):
                cur = getattr(data, 'conn', None)
                if cur is None:
                    try:
                        cur = getattr(data, 'cursor', None)
                    except Exception:
                        pass
                if cur is None:
                    return {"headers": [], "rows": [], "_error": "无可用数据库连接"}
                try:
                    from modules.core.data_security import assert_readonly_sql
                    assert_readonly_sql(sql)
                    cur.execute(sql)
                    cols = [d[0] for d in cur.description] if cur.description else []
                    rows = [list(r) for r in cur.fetchall()]
                    return {"headers": cols, "rows": rows}
                except Exception as e:
                    return {"headers": [], "rows": [], "_error": str(e)}

            plugin_issues = run_plugin_inspections_for_db(
                cfg['history_db_type'], context, execute_sql=_exec_plugin_sql)
            if plugin_issues:
                _auto = context.get('auto_analyze')
                if not isinstance(_auto, list):
                    _auto = []
                    context['auto_analyze'] = _auto
                _auto.extend([x for x in plugin_issues if isinstance(x, dict)])
                _emit('log', {'msg': f"[插件] {len(plugin_issues)} 个插件发现附加风险"})
        except Exception as e:
            _emit('log', {'msg': f"[插件] 执行跳过: {e}"})

        ofile_result = data.generate_report(ofile, inspector_nm)
        print(f"[DEBUG] generate_report 返回: {ofile_result}")
        print(f"[DEBUG] ofile 路径: {ofile}")
        if ofile_result and os.path.exists(ofile_result):
            print(f"[DEBUG] 文件确实存在，大小: {os.path.getsize(ofile_result)}")
        if not ofile_result:
            # 报告生成在引擎内部已被 try/except 包裹，真实异常只打到进程 stdout；
            # Windows 打包版通常没有控制台，web 日志里只剩一句「生成报告文件失败」。
            # 这里把引擎捕获的失败原因一并抛出并推送到前端日志，保证可回溯。
            _writable = os.access(reports_dir, os.W_OK)
            _detail = str(
                getattr(data, 'last_report_error', '')
                or getattr(getattr(data, 'inspector', None), 'last_report_error', '')
                or ''
            ).strip()
            print(f"[ERROR] generate_report 返回空 ({ofile_result})；"
                  f"目标路径={ofile}；目录可写={_writable}；"
                  f"文件已存在={os.path.exists(ofile)}；原因={_detail or '未知'}")
            _msg = _t('webui.err_report_generate')
            if _detail:
                _msg = f'{_msg}：{_detail}'
            _emit('log', {'msg': f"[ERROR] {_msg}（报告文件：{file_name}）"})
            raise RuntimeError(_msg)
        _emit('log', {'msg': _t('webui.log_report_ok').format(fname=file_name)})
        print(f"[REPORT] 报告已生成: {ofile_result}")
        print(f"[REPORT] 文件是否存在: {os.path.exists(ofile_result)}")

        # 台账自动同步：只处理有数据源归属的成功巡检；失败仅记录，不影响巡检结果。
        _inventory_sync_result = {}
        if task and task.get('datasource_id'):
            try:
                from modules.operations.inventory_sync import apply_inspection_observation
                _inventory_sync_result = apply_inspection_observation(
                    instance_id=task.get('datasource_id'),
                    task_id=task_id,
                    tenant_id=task.get('owner_tenant_id'),
                    user_id=task.get('owner_user_id'),
                    basic_info=_inventory_basic_info,
                    system_info=_inventory_system_info,
                    report_path=ofile,
                    observed_tables=_inventory_specialized,
                )
                _sync_status = _inventory_sync_result.get('status')
                if _sync_status == 'applied':
                    _emit('log', {
                        'msg': '[台账] 已自动更新 %d 个字段：%s' % (
                            len(_inventory_sync_result.get('changed') or []),
                            '、'.join(_inventory_sync_result.get('changed') or []),
                        )
                    })
                elif _sync_status == 'failed':
                    _emit('log', {
                        'msg': '[台账] 自动同步失败：%s' % _inventory_sync_result.get('reason', '未知错误')
                    })
            except Exception as _sync_error:
                _inventory_sync_result = {
                    'status': 'failed',
                    'reason': str(_sync_error)[:300],
                }
                _emit('log', {'msg': '[台账] 自动同步异常：%s' % str(_sync_error)[:300]})

        # 智能分析：统一问题汇总口径
        # 与 docx 报告（_append_chapters 内部同样调用 collect_issues）、定时巡检通知
        # （scheduler._collect_issues）、巡检历史（run.py/_record_inspection）共用
        # collect_issues 生成的同一份问题清单，保证四处数量完全一致。
        try:
            _analyzer_mod = __import__('modules.inspection.analyzer', fromlist=['_'])
            auto_analyze = _analyzer_mod.collect_issues(
                cfg['history_db_type'], context, analyzer_fn=cfg['smart_analyze'])
            context['auto_analyze'] = auto_analyze
            context['risk_count'] = len(auto_analyze)
            if task:
                task['auto_analyze'] = auto_analyze
            _emit('log', {'msg': f"[智能分析] 完成，发现 {len(auto_analyze)} 个可优化项"})
        except Exception as e:
            # 即使 analyzer 调用失败，也尝试保留 context 中已有的 auto_analyze
            auto_analyze = context.get('auto_analyze') or []
            context['auto_analyze'] = auto_analyze
            context['risk_count'] = len(auto_analyze)
            if task:
                task['auto_analyze'] = auto_analyze
            _emit('log', {'msg': f"[警告] 智能分析失败: {e}；保留已有 {len(auto_analyze)} 项"})

        # 历史快照
        try:
            context['_report_task_id'] = task_id
            context['_report_file_name'] = file_name
            from modules.inspection.analyzer import HistoryManager
            hm = HistoryManager(BASE_DIR)
            hm.save_snapshot(
                db_type=cfg['history_db_type'],
                host=db_info['ip'],
                port=db_info['port'],
                label=db_info.get('name', db_info['ip']),
                context=context
            )
        except Exception as e:
            _emit('log', {'msg': f"[警告] 历史快照保存失败: {e}"})

        # 保存报告中心的同批观测，复用已登记数据源和台账。
        _delivery_run_id = None
        if task and task.get('datasource_id'):
            try:
                from modules.service_management.delivery_capture import capture_inspection
                _delivery_observation = capture_inspection(
                    {**task, 'id': task_id, 'report_path': ofile}, context)
                _delivery_run_id = (_delivery_observation or {}).get('id')
            except Exception as error:
                _emit('log', {'msg': '[报告中心] 观测归档失败：%s' % str(error)[:200]})

        # 计算评分和风险等级（供前端展示使用）
        risk_count = context.get('risk_count', 0)
        if not risk_count:
            issues = context.get('issues', [])
            risk_count = len(issues) if isinstance(issues, list) else 0

        health_status = context.get('health_status', '')
        if risk_count:
            # 有问题项时不能沿用早期“优秀”状态，否则会把真实风险显示成 100 分。
            health_score = max(0, 100 - min(risk_count * 5, 50))
            health_status = ('良好' if health_score >= 80 else
                             '一般' if health_score >= 60 else '需关注')
        elif '优秀' in health_status or 'Excellent' in health_status:
            health_score = 100
        elif '良好' in health_status or 'Good' in health_status:
            health_score = 80
        elif '一般' in health_status or 'Fair' in health_status:
            health_score = 60
        elif '需关注' in health_status or 'Attention' in health_status:
            health_score = 40
        else:
            health_score = 100 - min(risk_count * 5, 50)

        if health_score >= 85:
            risk_level = 'healthy'
        elif health_score >= 70:
            risk_level = 'low'
        elif health_score >= 50:
            risk_level = 'medium'
        elif health_score >= 30:
            risk_level = 'high'
        else:
            risk_level = 'critical'

        # 先设置 task['result']，确保前端一定能拿到结果（不受 Pro 记录保存影响）
        if task:
            _aa = task.get('auto_analyze') or []
            _safe_issues = []
            for item in _aa:
                if isinstance(item, dict):
                    _safe_issues.append({
                        'level': _tr(item.get('col2', '')),
                        'description': _tr(item.get('col1', '')),
                        'suggestion': _tr(item.get('col3', '')),
                    })
            from modules.operations.findings import snapshot_metrics
            task['result'] = {
                'metrics': snapshot_metrics(context),
                'db_type': cfg['history_db_type'],
                'host': db_info['ip'],
                'port': db_info['port'],
                'label': label_name,
                'health_score': health_score,
                'health_status': health_status,
                'risk_count': risk_count,
                'risk_level': risk_level,
                'finished_at': datetime.datetime.now().isoformat(),
                'issues': _safe_issues,
                'report_file': ofile,
                'report_name': file_name,
                'ai_advice': context.get('ai_advice', ''),
                'basic_info': _inventory_basic_info,
                'inventory_sync': _inventory_sync_result,
                'delivery_run_id': _delivery_run_id,
            }
            # status 必须在 result 之后设置，避免前端轮询到 done 但 result 还是空的
            task['status'] = 'done'
            task['report_file'] = ofile
            task['report_name'] = file_name
            # report_path 与 report_file 同源（子进程隔离路径的 DONE 行与主进程
            # /api/task_status 都依赖 task['report_path']；此处与 Pro 记录保持一致）。
            task['report_path'] = ofile

        # Pro巡检记录（失败不影响前端结果展示）
        try:
            from modules.pro import get_instance_manager
            im = get_instance_manager()
            # 优先使用数据源ID，便于后续按数据源做协同诊断与历史查询；
            # 兼容旧版手动输入模式（无 datasource_id）时退回到 hash(ip:port)
            datasource_id = task.get('datasource_id')
            if datasource_id:
                instance_id = datasource_id
            else:
                # 未走数据源入口时，先按端点反查已注册实例，再退回 md5 指纹
                instance_id = None
                try:
                    instance_id = im.resolve_instance_id_by_endpoint(
                        cfg.get('history_db_type') or db_type,
                        db_info.get('ip'), db_info.get('port'))
                except Exception:
                    instance_id = None
                if not instance_id:
                    import hashlib
                    raw = f"{cfg['instance_prefix']}-{db_info['ip']}-{db_info['port']}".encode()
                    instance_id = hashlib.md5(raw).hexdigest()[:12]
            im.record_inspection(
                instance_id=instance_id,
                instance_name=label_name,
                db_type=cfg['history_db_type'],
                health_score=health_score,
                risk_count=risk_count,
                risk_level=risk_level,
                report_path=ofile,
                duration=0,
                host=db_info.get('ip', ''),
                auto_analyze=auto_analyze if auto_analyze else []
            )
        except Exception as e:
            _emit('log', {'msg': f"[警告] Pro 巡检记录保存失败: {e}"})

        print(f"[巡检] ===== 巡检任务结束：{_insp_name} ({_insp_host}) db_type={db_type} =====", flush=True)
        _emit('done', {'msg': _t('webui.log_inspection_done').format(ver=ver), 'task_id': task_id,
                       'ai_advice': context.get('ai_advice', '')})
    except Exception as e:
        import traceback
        _tb = traceback.format_exc()
        # 巡检失败：错误信息必须可见（此前 traceback 走 stdout 非事件行被主进程
        # 忽略、error 事件前端无监听 → 「巡检失败却没有任何错误日志」）。
        # 双通道：控制台保留原始堆栈 + log 事件推送前端日志面板。
        try:
            sys.stdout.write(_tb + '\n')
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        _err_summary = _t('webui.err_inspection').format(task=cfg['error_task_name'], e=str(e))
        _emit('log', {'msg': _err_summary})
        _emit('log', {'msg': f'[{cfg["error_task_name"]}] 巡检失败堆栈:\n{_tb}'})
        _emit('error', {'msg': f'{_err_summary}\n{_tb}'})
        if task:
            task['status'] = 'error'
            task['error_msg'] = str(e)
        print(f"[巡检] ===== 巡检任务结束（异常）：{_insp_name} ({_insp_host}) db_type={db_type} =====", flush=True)
    finally:
        # 关掉 SSH 隧道（含失败路径），避免端口/连接泄漏
        if _tunnel is not None:
            try:
                _tunnel.close()
            except Exception:
                pass
