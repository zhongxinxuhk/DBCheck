# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Read-only owner-ledger metadata collected with the existing inspection cursor."""

from datetime import datetime, timezone
import hashlib

from modules.core.data_security import assert_readonly_sql, redact

BACKUP_QUERIES = {
    'oracle': {
        'backup_configuration': "SELECT NAME AS POLICY_PARAMETER,VALUE AS POLICY_VALUE FROM V$RMAN_CONFIGURATION "
            "WHERE NAME IN ('RETENTION POLICY','BACKUP OPTIMIZATION','DEFAULT DEVICE TYPE','CONTROLFILE AUTOBACKUP')",
        'backup_schedules': "SELECT OWNER,JOB_NAME,ENABLED,REPEAT_INTERVAL,TO_CHAR(START_DATE,'YYYY-MM-DD HH24:MI:SS TZH:TZM') AS START_DATE,JOB_ACTION AS SQL_TEXT "
                            "FROM DBA_SCHEDULER_JOBS WHERE UPPER(JOB_ACTION) LIKE '%BACKUP%' "
                            "OR UPPER(JOB_ACTION) LIKE '%RMAN%'",
    },
    'sqlserver': {
        'backup_schedules': 'SELECT J.JOB_ID,J.NAME AS POLICY_NAME,J.ENABLED,ST.STEP_ID,ST.COMMAND AS SQL_TEXT,'
            'ST.DATABASE_NAME AS JOB_DATABASE_CONTEXT,SC.NAME AS SCHEDULE_NAME,SC.FREQ_TYPE,SC.FREQ_INTERVAL,'
            'SC.SCHEDULE_ID,SC.FREQ_RELATIVE_INTERVAL,SC.FREQ_SUBDAY_TYPE,SC.FREQ_SUBDAY_INTERVAL,SC.FREQ_RECURRENCE_FACTOR,SC.ACTIVE_START_TIME,'
            'SC.ACTIVE_END_TIME FROM MSDB.DBO.SYSJOBS J JOIN MSDB.DBO.SYSJOBSTEPS ST ON J.JOB_ID=ST.JOB_ID '
            'LEFT JOIN MSDB.DBO.SYSJOBSCHEDULES JS ON J.JOB_ID=JS.JOB_ID '
            "LEFT JOIN MSDB.DBO.SYSSCHEDULES SC ON JS.SCHEDULE_ID=SC.SCHEDULE_ID WHERE UPPER(ST.COMMAND) LIKE '%BACKUP%'",
    },
    'dm': {
        'backup_configuration': "SELECT PARA_NAME AS POLICY_PARAMETER,PARA_VALUE AS POLICY_VALUE FROM V$DM_INI "
                                "WHERE PARA_NAME LIKE '%BAK%' OR PARA_NAME LIKE '%BACKUP%'",
    },
}


def collect_owner_details(cursor, db_type):
    # Reuse the fixed inventories already used by the full-ledger collector.
    from modules.service_management.inspection import INVENTORY_QUERIES
    family={'oracle_jdbc':'oracle','sqlserver_jdbc':'sqlserver','dm8':'dm','gbase':'gbase8s'}.get(db_type,db_type)
    queries=dict(INVENTORY_QUERIES.get(family,{}))
    queries.update(BACKUP_QUERIES.get(family,{}))
    parameter_queries={
        'oracle':'SELECT NAME,VALUE,DISPLAY_VALUE,DESCRIPTION FROM V$PARAMETER',
        'dm':'SELECT NAME,VALUE FROM V$PARAMETER',
        'sqlserver':'SELECT NAME,CAST(VALUE AS NVARCHAR(4000)) AS VALUE,CAST(VALUE_IN_USE AS NVARCHAR(4000)) AS VALUE_IN_USE,DESCRIPTION,IS_DYNAMIC FROM SYS.CONFIGURATIONS',
        'pg':'SELECT NAME,SETTING,UNIT,CONTEXT,SOURCE FROM PG_SETTINGS',
        'postgresql':'SELECT NAME,SETTING,UNIT,CONTEXT,SOURCE FROM PG_SETTINGS',
        'mysql':'SHOW GLOBAL VARIABLES', 'mariadb':'SHOW GLOBAL VARIABLES', 'gbase8a':'SHOW GLOBAL VARIABLES',
    }
    if family in parameter_queries:
        queries['parameter_metadata']=parameter_queries[family]
    results={}
    for key,sql in queries.items():
        try:
            assert_readonly_sql(sql)
            cursor.execute(sql)
            columns=[str(column[0]).lower() for column in cursor.description or []]
            values=cursor.fetchmany(1001)
            truncated=len(values)>1000 or getattr(cursor,'truncated',False) is True or getattr(cursor,'remaining',None)==0
            rows=[dict(zip(columns,[value if isinstance(value,(int,float,bool,type(None))) else str(value) for value in row]))
                  for row in values[:1000]]
            if family == 'sqlserver' and key in {
                'accounts', 'role_members', 'permissions', 'server_accounts',
                'server_role_members', 'server_permissions',
            }:
                for row in rows:
                    row['principal_scope'] = 'server' if key.startswith('server_') else 'database'
            results[key]={'status':'collected','rows':redact(rows),'truncated':truncated,
                          'scope':'current_connection_metadata_visibility','completeness':'bounded_metadata',
                          'query_sha256':hashlib.sha256(sql.encode()).hexdigest(),
                          'collected_at':datetime.now(timezone.utc).isoformat()}
        except Exception as error:
            results[key]={'status':'error','reason':redact(str(error),mask_identity=True),
                          'query_sha256':hashlib.sha256(sql.encode()).hexdigest()}
    return results
