# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Owner-facing multi-sheet exports of one exact historical inspection report."""

from datetime import datetime
import io
import json
from pathlib import Path
import re
import sqlite3
import ipaddress

from docx import Document
from docx.oxml.ns import qn
from docx.opc.exceptions import PackageNotFoundError
from lxml.etree import XMLSyntaxError

from modules.core.data_security import redact
from .delivery_data import build_snapshot, sources_from_context, TZ
from .delivery_render import bounded_zip
from .delivery_store import digest
from .report_sources import report_catalog
from .service import period_bounds, timestamp
from .store import canonical, utcnow
from .owner_field_adaptation import missing, field_key, adapt_tables, classify_missing, redact_owner_tables

SCHEMA = 'owner-single-inspection-v2'
SENSITIVE = re.compile(r'密码|口令|私钥|密钥|password|passwd|pwd|secret|token|credential|authentication_string',re.I)
TABLES = {
    '全品类数据库资产底账清单': ['资产底账'],
    '集群参数表': ['集群基本信息', '集群节点', '集群参数'],
    '数据库参数表': ['数据库参数'],
    '数据库基本信息表': ['数据库基本信息'],
    '逻辑存储空间属性表': ['逻辑存储空间', '存储文件'],
    '用户及权限表': ['用户账号', '角色成员', '权限明细'],
    '备份策略表': ['备份策略'],
}
HEADERS = {
    '全品类数据库资产底账清单': ['配置项编号','数据库名称','实例名称','数据库类型','数据库版本及补丁号','数据库地址','端口',
        '部署方式','硬件配置','系统归属','业务用途','运维责任人','上线时间','运行状态','集群属性'],
    '集群参数表': ['配置项编号','集群编号','集群类型','数据库版本','节点名称','节点角色','节点数量','集群网络配置','参数名称','参数值'],
    '数据库参数表': ['配置项编号','数据库名称','实例名称','参数名称','配置值','运行值','单位','参数版本','变更引用','观测时间'],
    '数据库基本信息表': ['配置项编号','数据库名称','实例名称','字符集','数据库文件大小','归档路径','归档模式','主机型号','设备位置',
        'OS版本','存储平台','数据库版本及补丁号','存储模式','备份策略','容灾状态','集群属性'],
    '逻辑存储空间属性表': ['配置项编号','数据库名称','逻辑存储空间名称','类型','存储大小','文件路径','自动扩展模式','使用率阈值','当前使用率'],
    '用户及权限表': ['配置项编号','用户名','业务用途','权限','角色','授权对象','责任归属人','权限变更引用'],
    '备份策略表': ['配置项编号','备份策略编号','备份数据类型','备份内容','备份窗口','备份方式','执行频率','保存周期','运维责任人','策略版本','变更引用'],
    '巡检检查结果': ['检查项','检查时间','检查结果','实测值','判定依据','风险等级'],
    '异常及整改记录': ['问题描述','风险等级','问题根因','整改方案','整改时限','责任人','验证记录','闭环状态'],
    '备份执行记录': ['备份任务编号','数据库名称','备份类型','开始时间','结束时间','执行状态','备份大小','校验结果'],
}
ALIASES = {
    '数据库名称': ['database','db_name','数据库名'], '实例名称': ['instance_name','实例名'],
    '数据库类型': ['db_type'], '数据库版本及补丁号': ['version_patch','补丁版本','数据库版本'],
    '数据库地址': ['host','IP地址','数据库IP地址'], '运行状态': ['lifecycle','实例状态'],
    '节点名称': ['node_name','节点','instance_id'], '节点角色': ['expected_role','role'],
    '参数名称': ['parameter_name','variable_name','name','名称','参数名'],
    '参数值': ['value','配置值'], '配置值': ['value','参数值','variable_value'],
    '运行值': ['value_in_use','当前值','实际运行值'], '观测时间': ['collected_at','采集时间'],
    '逻辑存储空间名称': ['tablespace_name','space_name'], '存储大小': ['size','bytes','总容量','总大小'],
    '文件路径': ['file_name','physical_name','数据文件路径'], '自动扩展模式': ['autoextensible','autoextend','自动扩展'],
    '当前使用率': ['used_percent','使用率（%）','已用百分比','使用率'],
    '用户名': ['username','user_name','grantee','授权主体'], '权限': ['privilege','permission_name','具体权限'],
    '角色': ['granted_role','授权角色'], '授权对象': ['table_name','对象名称'], '责任归属人': ['account_owners','账号责任人'],
    'OS版本': ['操作系统版本','os_version'], '集群属性': ['cluster_attrs','所属集群','集群编号'],
    '备份窗口': ['backup_window','执行时间窗口'], '执行频率': ['backup_frequency','备份频率'],
    '保存周期': ['backup_retention','备份保存周期','数据保存周期'],
    '策略版本': ['backup_policy_version','备份策略版本'], '备份策略': ['备份策略编号','backup_policy'],
    '检查项': ['检查项名称','检查名称','check_name'], '检查结果': ['结论'],
    '问题描述': ['问题','描述','风险描述','description','col1'], '风险等级': ['风险级别','严重程度','level','col2'],
    '整改方案': ['整改建议','处理建议','建议','suggestion','col3'], '整改时限': ['deadline'],
    '备份任务编号': ['session_key','backup_set_id'], '备份类型': ['input_type','type'],
    '开始时间': ['start_time','backup_start_date'], '结束时间': ['end_time','backup_finish_date'],
    '执行状态': ['status','备份状态'], '备份大小': ['backup_size','output_bytes'],
}
ALIASES.update({
    '配置项编号':['configuration_id','配置编号','CI编号'],
    '数据库名称':['database','database_name','db_name','table_catalog','table_schema','数据库名'],
    '实例名称':['instance_name','server_name','实例名'],
    '数据库版本':['version','product_version','数据库版本及补丁号'],
    '数据库版本及补丁号':['version_patch','product_version','版本及补丁号','补丁版本','数据库版本'],
    '数据库地址':['host','ip','ip_address','server_address','IP地址','数据库IP地址'],
    '端口':['port','port_number'], '上线时间':['online_date','上线日期'],
    '运行状态':['lifecycle','instance_status','database_status','open_mode','运行模式','实例状态'],
    '参数名称':['parameter_name','para_name','variable_name','name','名称','参数名'],
    '参数值':['para_value','variable_value','value','配置值'],
    '配置值':['para_value','variable_value','config_value','value','参数值'],
    '运行值':['value_in_use','runtime_value','setting','实际值','当前值','实际运行值'],
    '单位':['unit','units','参数单位'], '参数版本':['parameter_version','快照版本'],
    '观测时间':['observed_at','collected_at','采集时间','记录时间'],
    '逻辑存储空间名称':['tablespace_name','space_name','dbspace','dbspace_name','表空间','表空间名称'],
    '类型':['type_desc','contents','segment_type','storage_type','空间类型'],
    '文件路径':['file_path','file_name','physical_name','file_paths','文件名称','物理名称','数据文件路径'],
    '自动扩展模式':['autoextensible','autoextend','自动扩展'],
    '当前使用率':['used_pct','used_percent','usage_pct','used_percentage','使用率（%）','已用百分比','使用率'],
    '使用率阈值':['usage_threshold','使用率告警阈值'],
    '用户名':['username','user_name','login_name','login_user','member_name','grantee','usename','授权主体'],
    '角色':['granted_role','role_name','rolname','授权角色'],
    '权限':['privilege','privilege_type','permission_name','permission_type','具体权限'],
    '授权对象':['object_name','table_name','对象名称'],
    '业务用途':['purpose','purposes','business','用途'],
    '责任归属人':['account_owner','account_owners','账号责任人'],
    '备份任务编号':['session_key','backup_set_id','backup_id','备份编号'],
    '备份类型':['input_type','backup_type','type'],
    '开始时间':['start_time','backup_start_date','backup_start_time','start_date'],
    '结束时间':['end_time','backup_finish_date','backup_finish_time','end_date'],
    '备份大小':['backup_size','output_bytes','backup_bytes'],
    '执行频率':['repeat_interval','backup_frequency','备份频率'],
    '运维责任人':['backup_owner','备份责任人','owner'],
    '检查项':['检查项名称','检查名称','parameter_name','参数名称','参数','check_name'],
    '检查结果':['结论','status','状态'], '实测值':['当前值','current_value','value_in_use','实测内容'],
    '判定依据':['期望值','expected_value','expected','原因'],
    '问题根因':['root_cause'], '验证记录':['verification_reference','验证证据'],
})


def owner_rows(title, rows):
    results=[]
    for row in rows:
        lookup={field_key(key):value for key,value in row.items()}
        normalized={}
        for header in HEADERS[title]:
            candidates=[header,*ALIASES.get(header,[])]
            if header=='数据库名称' and title=='数据库基本信息表':
                candidates+=['name','名称']
            if header=='用户名' and title=='用户及权限表' and row.get('记录类型')=='用户账号':
                candidates+=['name','名称']
            if header=='节点名称' and title=='集群参数表':
                candidates+=['instance_name','host_name','实例名称','主机名']
            if title=='集群参数表' and header=='参数名称' and row.get('记录类型')!='集群参数' and \
                    not any(word in str(row.get('原报告章节','')).casefold() for word in ('参数','parameter')):
                candidates=['参数名称','parameter_name','variable_name']
            origin=next((key for key in candidates if not missing(lookup.get(field_key(key)))),None)
            normalized[header]=lookup[field_key(origin)] if origin is not None else None
            if origin is not None and header not in row.get('字段来源',{}):
                normalized.setdefault('_原字段来源',{})[header]='原字段 '+str(origin)
            if normalized[header] is None and any(str(lookup.get(field_key(key))).casefold() in
                    {'[masked]','[email]','[phone]','[identity]'} for key in candidates):
                normalized.setdefault('字段缺项原因',{})[header]='原资料标识已脱敏，需要该次原始观测记录'
        normalized.update({key:value for key,value in row.items() if key not in normalized})
        normalized['字段来源']={**normalized.pop('_原字段来源',{}),**row.get('字段来源',{})}
        classify_missing(title,normalized,HEADERS[title])
        results.append(normalized)
    return results


def report_file(root, filename):
    if not isinstance(filename, str) or not filename or '/' in filename or '\\' in filename or Path(filename).suffix.lower() != '.docx':
        raise ValueError('请选择巡检历史中的 Word 报告')
    base = Path(root).resolve()
    path = base / filename
    if path.is_symlink() or path.resolve().parent != base:
        raise ValueError('报告路径无效')
    if not path.is_file():
        raise ValueError('巡检报告文件不存在')
    return path


def topic(title, headers):
    text = (title + ' ' + ' '.join(headers)).casefold()
    columns={re.split(r'[（(]',header.casefold())[0].strip() for header in headers}
    if columns & {'para_name','parameter_name'} and columns & {'para_value','parameter_value','value'}:
        return '数据库参数表'
    if columns & {'table_schema','tablespace_name'} and columns & {'data_mb','total_mb','size_gb','used_mb'}:
        return '逻辑存储空间属性表'
    if any(word in title.casefold() for word in ('数据库基本信息','数据库基础信息','核心属性','database basic','database information')):
        return '数据库基本信息表'
    if ('基线' in title or 'baseline' in title.casefold()) and columns & {'期望值','运算符','expected','operator'}:
        return '巡检检查结果'
    if ('备份' in text or 'backup' in text) and columns & {
            'backup_start_date','backup_finish_date','backup_start_time','backup_finish_time','start_time','input_type','is_damaged'}:
        return '备份执行记录'
    if columns & {'value_in_use','is_dynamic','is_advanced','isdefault','reset_val','boot_val'} or any(
            value in title for value in ('数据库参数','实例配置项','初始化参数')):
        return '数据库参数表'
    if any(word in title.casefold() for word in ('会话','阻塞','慢sql','session','blocking','slow sql')) and not any(
            word in title.casefold() for word in ('权限','账号','账户','privilege','account')):
        return '巡检检查结果'
    if any(word in text for word in ('备份策略', '备份计划', 'backup policy', 'backup strategy')):
        return '备份策略表'
    if any(word in text for word in ('备份', '恢复记录', 'backup', 'restore')):
        return '备份执行记录'
    if any(word in text for word in ('用户名', '账号', '用户与', '用户及', '用户信息', '权限', '角色', 'username', 'privilege', 'grantee', 'grant', 'role member', 'account', 'db users')):
        return '用户及权限表'
    if any(word in text for word in ('表空间', '逻辑存储', '数据文件', '文件组', 'tablespace', 'datafile', 'data file', 'filegroup')):
        return '逻辑存储空间属性表'
    if ('集群' in text or 'cluster' in text or 'rac ' in text) and any(word in text for word in ('参数', '配置', '节点', '成员', 'parameter', 'node', 'member', 'configuration')):
        return '集群参数表'
    if any(word in text for word in ('参数', '配置基线', 'parameter', 'global variables', 'settings', 'variable_name')):
        return '数据库参数表'
    if any(word in text for word in ('资产底账', '资产台账', 'asset register', 'asset inventory')):
        return '全品类数据库资产底账清单'
    if any(word in text for word in ('基本信息', '基础信息', '核心属性', '基本属性', 'basic information', 'database information', 'basic info')):
        return '数据库基本信息表'
    if any(word in text for word in ('风险', '整改', '异常分析', '智能分析', 'risk', 'remediation', 'finding')):
        return '异常及整改记录'
    return '巡检检查结果'


def extract_tables(content):
    with bounded_zip(content):
        pass
    try:
        document = Document(io.BytesIO(content))
    except (KeyError, ValueError, PackageNotFoundError, XMLSyntaxError) as error:
        raise ValueError('原巡检报告格式异常，请核对 Word 文件完整性') from error
    from docx.table import Table
    groups = {name: [] for name in [*TABLES, '巡检检查结果', '异常及整改记录', '备份执行记录', '原始表格明细']}
    family=None
    masthead=' '.join(paragraph.text for paragraph in document.paragraphs[:5]).upper()
    for marker,code in [('SQL SERVER','sqlserver'),('ORACLE','oracle'),('DM8','dm'),('GBASE8A','gbase8a'),('POSTGRESQL','pg'),('MYSQL','mysql')]:
        if marker in masthead:
            family=code
            break
    groups['_report_identity']=[{'db_type':family}] if family else []
    title = '原报告'
    table_number = 0
    for element in document.element.body:
        if element.tag == qn('w:p'):
            style = element.find('./' + qn('w:pPr') + '/' + qn('w:pStyle'))
            if style is not None and ('heading' in str(style.get(qn('w:val'), '')).lower() or
                                      str(style.get(qn('w:val'), '')).isdigit()):
                title = ''.join(node.text or '' for node in element.iter(qn('w:t'))).strip() or title
            continue
        if element.tag != qn('w:tbl'):
            continue
        table_number += 1
        table = Table(element, document._body)
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        if not rows:
            continue
        cover= len(rows[0])==2 and rows[0][0].rstrip('：:') in {'服务器地址','数据库地址','巡检人员','报告生成时间'}
        headers = []
        for index, text in enumerate(['配置项','内容'] if cover else rows[0], 1):
            header = text or '列' + str(index)
            while header in headers:
                header += '_' + str(index)
            headers.append(header)
        destination = '数据库基本信息表' if cover else topic(title, headers)
        section=TABLES.get(destination,[destination])[0]
        joined=(title+' '+' '.join(headers)).casefold()
        if destination=='用户及权限表':
            names={re.split(r'[（(]',header.casefold())[0].strip() for header in headers}
            section='角色成员' if names & {'granted_role','role_name','member_name','role_principal_id'} or '角色成员' in title else \
                '权限明细' if names & {'privilege','privilege_type','permission_name','grantee','权限','具体权限'} else '用户账号'
        elif destination=='逻辑存储空间属性表':
            section='存储文件' if any(word in joined for word in ('数据文件','file_name','physical_name','datafile','data file')) else '逻辑存储空间'
        elif destination=='集群参数表':
            section='集群参数' if '参数' in title or 'parameter' in title.casefold() else '集群节点' if any(
                word in joined for word in ('节点','成员','node','inst_id')) else '集群基本信息'
        extracted = []
        for number, values in enumerate(rows if cover else rows[1:], 1 if cover else 2):
            row = {'来源': '原巡检报告表格', '记录类型':section, '原报告章节': title, '原表号': table_number, '原表行号': number,
                   **dict(zip(headers, values))}
            for header in headers:
                if SENSITIVE.search(header):
                    row[header]='[REDACTED]'
            parameter=next((row.get(header) for header in headers if field_key(header) in
                            {field_key(key) for key in ('参数名称','参数名','名称','name','variable_name','para_name','parameter_name',
                                                       'policy_parameter','配置项','配置项名称','属性','属性名称')}),None)
            if parameter and SENSITIVE.search(str(parameter)):
                for header in headers:
                    if field_key(header) in {field_key(key) for key in ('参数值','配置值','运行值','当前值','value','variable_value',
                        'para_value','display_value','policy_value','value_in_use','值','内容','登记值')}:
                        row[header]='[REDACTED]'
            if destination=='用户及权限表':
                identifier_keys={'username':'用户名','user_name':'用户名','login_name':'用户名','member_name':'用户名',
                                 'grantee':'授权主体','role_name':'角色','granted_role':'角色'}
                for header,value in zip(headers,values):
                    native=re.split(r'[（(]',header.casefold())[0].strip()
                    if native in identifier_keys:
                        row[identifier_keys[native]]=value
            extracted.append(row)
            groups['原始表格明细'].append(row)
        if destination == '数据库基本信息表' and len(headers) == 2 and extracted:
            values = {str(row.get(headers[0])): row.get(headers[1]) for row in extracted if row.get(headers[0])}
            groups[destination].append({'来源': '原巡检报告表格','记录类型':section,'原报告章节': title, '原表号': table_number, **values})
        else:
            groups[destination].extend(extracted)
    return redact_owner_tables(groups)


def exact_history_metadata(filename, history_file):
    path = Path(history_file)
    if not path.is_file():
        return {}
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='inspection_history'").fetchone():
            return {}
        db.row_factory = sqlite3.Row
        matches = [dict(row) for row in db.execute('SELECT * FROM inspection_history')
                   if str(row['report_path'] or '').replace('\\', '/').split('/')[-1] == filename]
    if len(matches) != 1:
        return {}
    allowed = ('id', 'instance_id', 'instance_name', 'db_type', 'host', 'inspect_time', 'health_score', 'risk_count', 'risk_level')
    result={key:matches[0].get(key) for key in allowed}
    if missing(result.get('host')):
        encoded=re.search(r'巡检报告_((?:\d{1,3}\.){3}\d{1,3}|localhost)_',filename)
        if encoded:
            try:
                if encoded[1]!='localhost':ipaddress.ip_address(encoded[1])
                result['host']=encoded[1]
                result['host_source']='与该原巡检记录精确匹配的系统文件名'
            except ValueError:
                pass
    return result


def exact_legacy_context(filename, history_file):
    path = Path(history_file)
    if not path.is_file():
        return None
    match = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.docx$', filename)
    task_id = match.group(1) if match else None
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='snapshots'").fetchone():
            return None
        found = []
        for identity, stamp, encoded in db.execute('SELECT id,ts,context_json FROM snapshots WHERE instr(context_json,?)>0',
                                                   (task_id or filename,)):
            try:
                context = json.loads(encoded)
            except (ValueError, TypeError):
                continue
            if context.get('_report_file_name') == filename or (task_id and context.get('_report_task_id') == task_id):
                found.append((identity, stamp, context))
    return found[0] if len(found) == 1 else None


class OneInspection:
    include_legacy_history = False
    def __init__(self, asset, run):
        self.asset, self.run = asset, run

    def reporting_records(self, actor, cutoff):
        return [self.asset, self.run]

    def asset_history(self, actor, identity):
        return []


class NoAdditionalObservations:
    def observations(self, actor, identities):
        return []


def historical_asset(archive,actor,source_id,moment,label=None):
    asset={'id':source_id,'asset_id':'','kind':'asset','tenant':actor['tenant_id'],'owner':actor['id'],'version':0,
           'payload':{'instance_id':source_id,'asset_name':label or source_id},
           'created_at':moment.isoformat(),'updated_at':moment.isoformat()}
    versions=[row for row in archive.services.inventory.history(archive.services.principal,{source_id})
              if timestamp(row['created'])<=moment and row['details'].get('after')]
    if versions:
        prior=max(versions,key=lambda row:timestamp(row['created']))
        previous=prior['details']['after']
        asset['payload'].update(previous.get('metadata',{}))
        for key in ('host','port','database','db_type'):
            asset['payload'][key]=previous.get('source',{}).get(key)
        asset['version']=(prior['details'].get('before') or {}).get('version',0)+1
    return asset


def single_snapshot(actor, asset, observation):
    day=timestamp(observation['observed_at']).astimezone(TZ).date().isoformat()
    start,end=period_bounds('month',day)
    options={'source_ids':[asset['id']],'asset_ids':[asset['id']],
             'deliverables':['D01','D02','D03','D04','D05','D06','D07','RUN'],
             'run_id':observation['id'],'date':day,'cutoff':observation['observed_at'],
             'start':start.isoformat(),'end':end.isoformat(),'period':'month',
             'formats':['xlsx'],'layout':'combined','sheets':[],'workflow_ids':[]}
    run = {'id': observation['id'], 'kind': 'inspection', 'item_id': 'run-evidence', 'asset_id': asset['id'], 'version': 1,
           'created_at': observation['observed_at'], 'updated_at': observation['observed_at'],
           'payload': {'run_id': observation['id'], 'collected_at': observation['observed_at'],
                       'source_results': observation['source_results'], 'provenance': 'exact_inspection_report'}}
    snapshot = build_snapshot(OneInspection(asset, run), NoAdditionalObservations(), actor, report_catalog(actor), options)
    return snapshot


def historical_export(actor, archive, path, *, legacy_context_db, pro_history_db):
    filename, content = path.name, path.read_bytes()
    checksum = digest(content)
    document_groups = extract_tables(content)
    linked = archive.inspection_report(actor, filename)
    try:
        metadata = exact_history_metadata(filename, pro_history_db)
    except sqlite3.Error:
        metadata = {}
    if missing(metadata.get('host')):
        encoded=re.search(r'巡检报告_((?:\d{1,3}\.){3}\d{1,3}|localhost)_',filename)
        if encoded:
            try:
                if encoded[1]!='localhost':ipaddress.ip_address(encoded[1])
                metadata['host']=encoded[1]
                metadata['host_source']='原报告文件名所载连接地址（待核对）'
            except ValueError:
                pass
    source_ids = []
    snapshot = None
    note = '按指定原报告提取，未关联可验证的结构化采集批次。缺失字段以未记录展示。'
    inspected_at = metadata.get('inspect_time') or '原报告未关联可验证巡检时间'
    frozen_asset=None
    inventory_observation=None
    inventory_observation_used=False
    if hasattr(archive.services,'inventory') and hasattr(archive.services.inventory,'observation_for_report'):
        inventory_observation=archive.services.inventory.observation_for_report(archive.services.principal,filename)
    if linked:
        reference, observation = linked
        source_ids = [reference['source_id']]
        frozen_asset=reference['source_asset']
        snapshot = single_snapshot(actor, reference['source_asset'], observation)
        inspected_at = observation['observed_at']
        note = '已关联该原报告的固定采集批次。'
        if reference['file_sha256'] != checksum:
            document_groups = {name: [] for name in document_groups}
            note = '现存 Word 文件与生成时摘要不一致，本工作簿使用系统保存的原始巡检批次。'
    else:
        try:
            legacy = exact_legacy_context(filename, legacy_context_db)
        except sqlite3.Error:
            legacy = None
        if legacy:
            identity, stamp, context = legacy
            if context.get('_instance_id') in archive.services.visible:
                source_id = context['_instance_id']
                # Historical values come from the original context, with identity-only registration fields.
                source_ids = [source_id]
                moment = datetime.fromisoformat(stamp)
                if moment.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    moment = moment.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                observation = {'id': 'history-' + str(identity), 'observed_at': moment.isoformat(),
                               'source_results': sources_from_context(context)}
                asset=historical_asset(archive,actor,source_id,moment,metadata.get('instance_name'))
                frozen_asset=asset
                snapshot = single_snapshot(actor, asset, observation)
                inspected_at = observation['observed_at']
                note = '按原报告文件名/任务编号精确关联历史快照，台账登记字段以原报告为依据。'
    sheets = {}
    observed_sections={}
    if inventory_observation and (not linked or linked[0]['file_sha256']==checksum):
        inventory_observation_used=True
        if frozen_asset is None:
            moment=timestamp(inventory_observation['observed_at'])
            frozen_asset=historical_asset(archive,actor,inventory_observation['instance_id'],moment,metadata.get('instance_name'))
            frozen_asset['payload'].update(inventory_observation['data'].get('auto_fields',{}))
            for key,value in inventory_observation['data'].get('basic_info',{}).items():
                if not missing(value):frozen_asset['payload'].setdefault(key,value)
            source_ids=[inventory_observation['instance_id']]
            inspected_at=inventory_observation['observed_at']
            note='已按原报告精确关联同次加密台账观测，已使用该时点的基础属性及专项明细。'
        for key,section,title in [('accounts','用户账号','用户及权限表'),('tablespaces','逻辑存储空间','逻辑存储空间属性表')]:
            block=inventory_observation['data'].get('specialized',{}).get(key,{})
            if block.get('status')=='collected':
                observed_sections[section]=[{'来源':'同次加密台账观测','记录类型':section,
                    '观测时间':inventory_observation['observed_at'],'采集批次':inventory_observation['task_id'],
                    **{({'username':'用户名','size':'存储大小','used_pct':'当前使用率','name':'逻辑存储空间名称'}.get(field,field)):value
                       for field,value in row.items()}} for row in block.get('rows',[])]
    for title, sections in TABLES.items():
        rows = []
        for section in sections:
            collected=[{**row,'记录类型':section} for row in snapshot['sheets'].get(section,[])
                       if row.get('状态')!='未采集'] if snapshot else []
            if section in observed_sections:
                if section=='用户账号':
                    collected=[row for row in collected if not missing(owner_rows(title,[row])[0].get('用户名'))]
                    collected+=observed_sections[section]
                elif not collected:
                    collected=observed_sections[section]
            original=[row for row in document_groups[title] if row.get('记录类型')==section]
            # Registered summaries use the released document. Query detail keeps every captured row.
            if section in {'资产底账','数据库基本信息','集群基本信息','备份策略'} and original:
                rows.extend(original)
            else:
                rows.extend(collected or original)
        sheets[title] = rows or [{'状态': '原巡检未记录', '说明': '该次巡检缺少此类数据，需以该次采集范围核对'}]
    if sheets['全品类数据库资产底账清单'][0].get('状态')=='原巡检未记录' and document_groups['数据库基本信息表']:
        basic=next((row for row in document_groups['数据库基本信息表'] if '配置项编号' in row or '字符集' in row),document_groups['数据库基本信息表'][0])
        fields={key:value for key,value in basic.items() if key in HEADERS['全品类数据库资产底账清单'] or key in ('实例名','数据库名')}
        if fields:
            sheets['全品类数据库资产底账清单']=[{'来源':'原报告基本信息映射',**fields}]
    for title in TABLES:
        sheets[title]=owner_rows(title,sheets[title])
    for title, section in [('巡检检查结果', '检查明细'), ('异常及整改记录', '风险清单'), ('备份执行记录', '备份执行')]:
        sheets[title] = snapshot['sheets'].get(section, []) if snapshot else []
        if not sheets[title]:
            sheets[title] = document_groups[title]
        sheets[title]=owner_rows(title,sheets[title] or [{'状态':'原巡检未记录','说明':'该次报告未提供对应检查或处置记录'}])
    sheets['原始表格明细'] = document_groups['原始表格明细']
    if snapshot and snapshot['sheets'].get('监控采集明细'):
        sheets['原始采集明细']=snapshot['sheets']['监控采集明细']
    if missing(metadata.get('db_type')) and document_groups.get('_report_identity'):
        metadata['db_type']=document_groups['_report_identity'][0].get('db_type')
    # Aggregate identity only from this file, its exact history record and its frozen source state.
    adapt_tables(sheets,metadata,frozen_asset,inspected_at if snapshot or inventory_observation_used else metadata.get('inspect_time'))
    for title in HEADERS:
        sheets[title]=owner_rows(title,sheets[title])
    if snapshot is None:
        snapshot = {'assets': [frozen_asset] if frozen_asset else [], 'sources': [], 'coverage': {'assets': 0, 'runs': 1, 'checks': len(sheets['巡检检查结果']),
                    'uncollected_days': 0, 'workflows': 0, 'open_issues': len(sheets['异常及整改记录'])}}
    now = utcnow()
    options = {'source_ids': source_ids, 'asset_ids': source_ids, 'deliverables': ['RUN'], 'formats': ['xlsx'],
               'period': 'month', 'date': now[:10], 'start': now, 'end': now, 'cutoff': now,
               'run_id': '', 'sheets': [], 'layout': 'combined', 'workflow_ids': [],
               'history_export': True, 'original_report': filename, 'original_sha256': checksum,
               'inspection_time': inspected_at, 'history_export_schema': SCHEMA}
    sources = [*snapshot['sources'], {'report_name': filename, 'sha256': checksum}]
    if inventory_observation_used:
        sources.append({'inventory_observation_id':inventory_observation['id'],'task_id':inventory_observation['task_id'],
                        'payload_sha256':digest(canonical(inventory_observation['data']).encode())})
    snapshot.update(sheets=redact_owner_tables(sheets), start=now, end=now,
                    cutoff=now, sources=sources, source_manifest_sha256=digest(canonical(sources).encode()),
                    history_sheet_names=list(sheets), history_notes={'export_note': note, 'original_record': metadata})
    snapshot['coverage']={'原报告数':1,'已关联数据源数':len(source_ids),'台账Sheet数':len(TABLES),
                          '检查结果行数':sum(row.get('状态')!='原巡检未记录' for row in sheets['巡检检查结果']),
                          '异常及整改记录行数':sum(row.get('状态')!='原巡检未记录' for row in sheets['异常及整改记录'])}
    cache_key = digest(canonical([SCHEMA, filename, checksum, sources, snapshot['sheets']]).encode())
    return options, snapshot, cache_key
