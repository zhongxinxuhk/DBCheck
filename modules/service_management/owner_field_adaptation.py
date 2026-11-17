# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Evidence-based joins and unit conversions for owner delivery tables."""

from decimal import Decimal, InvalidOperation
import re

from .labels import FIELD_LABELS
from modules.inspection.basic_info import FIELD_ORDER
from .store import canonical
from .delivery_store import digest

UNKNOWN = {'','未记录','未采集','未登记','未知','unknown','n/a','none','null','-','—','[masked]','[ip]','[email]','[phone]','[identity]'}


def redact_owner_tables(value):
    """Retain explicitly requested ledger identities while masking secrets and SQL literals."""
    from modules.core.data_security import redact
    cleaned=redact(value,mask_identity=True,preserve_network=True)
    identifiers={'用户名','账号责任人','责任归属人','运维责任人','授权主体','角色','授权对象'}
    def restore(original,output):
        if isinstance(original,dict) and isinstance(output,dict):
            for key,item in original.items():
                if key in identifiers and isinstance(item,str):
                    output[key]=item
                elif key in output:
                    restore(item,output[key])
        elif isinstance(original,list) and isinstance(output,list):
            for item,clean in zip(original,output):restore(item,clean)
    restore(value,cleaned)
    return cleaned


def missing(value):
    return value is None or (isinstance(value,str) and (value.strip().casefold() in UNKNOWN or
        value.strip().startswith(('未采集 /','未登记 /','未读取到','原巡检未记录'))))


def field_key(value):
    value=re.sub(r'^([A-Za-z_][A-Za-z0-9_.$]*)\s*[（(].*$',r'\1',str(value))
    return re.sub(r'[\s_（）()]','',value.casefold())


def pick(row,*keys):
    lookup={field_key(key):value for key,value in row.items()}
    return next((lookup[field_key(key)] for key in keys if not missing(lookup.get(field_key(key)))),None)


def number(value):
    if value is None or isinstance(value,bool):
        return None
    try:
        result=Decimal(str(value).replace(',','').rstrip('%').strip())
        return result if result.is_finite() else None
    except (InvalidOperation,ValueError):
        return None


def fill(row,field,value,source):
    if missing(row.get(field)) and not missing(value):
        row[field]=value
        row.setdefault('字段来源',{})[field]=source
        if '待核对' in source:
            row.setdefault('待核对字段',[]).append(field)


def facts_from(sheets,metadata,asset):
    facts={}
    if asset:
        for key,value in asset.get('payload',{}).items():
            if not missing(value):
                facts[FIELD_LABELS.get(key,dict(FIELD_ORDER).get(key,key))]=value
    for row in sheets['数据库基本信息表']:
        if not missing(row.get('字符集')) or row.get('原表号')==2 or not missing(row.get('服务器地址')):
            facts.update({key:value for key,value in row.items() if not missing(value)})
    address=pick(facts,'服务器地址')
    if address:
        if re.match(r'^\[(IP|MASKED|REDACTED)\]',str(address),re.I):
            address=None
    if address:
        endpoint=re.fullmatch(r'\[([^\]]+)\]:(\d{1,5})',str(address))
        if not endpoint and str(address).count(':')==1:
            endpoint=re.fullmatch(r'([^:]+):(\d{1,5})',str(address))
        if endpoint and 0<int(endpoint[2])<65536:
            if missing(facts.get('数据库地址')):
                facts['数据库地址']=endpoint[1]
            if missing(facts.get('端口')):
                facts['端口']=int(endpoint[2])
        elif missing(facts.get('数据库地址')):
            facts['数据库地址']=address
    for target,key in [('数据库类型','db_type'),('数据库地址','host')]:
        if missing(facts.get(target)) and not missing(metadata.get(key)):
            facts[target]=metadata[key]
            facts.setdefault('_字段来源',{})[target]=metadata.get('host_source','原巡检历史记录') if key=='host' else '原巡检历史记录'
    for target,keys in {
        '实例名称':('instance_name','实例名','实例名称'),
        '数据库地址':('server_ip','数据库地址','server_address'), '端口':('port','端口'),
        '数据库版本及补丁号':('version_patch','product_version','版本及补丁号'),
        '运行状态':('database_status','database_state','open_mode'),
        '数据库名称':('database_name','db_name'),
    }.items():
        candidates={str(value):value for row in sheets.get('原始表格明细',[]) for value in [pick(row,*keys)] if not missing(value)}
        if len(candidates)==1 and missing(facts.get(target)):
            facts[target]=next(iter(candidates.values()))
    if missing(facts.get('实例名称')):
        facts['实例名称']=pick(facts,'实例名')
    for target,keys in {'OS版本':('操作系统版本','os_version'),
                        '上线时间':('上线日期','online_date'),
                        '数据库版本及补丁号':('数据库版本','version'),
                        '集群属性':('cluster_attrs',),'数据库名称':('database','db_name')}.items():
        if missing(facts.get(target)):
            facts[target]=pick(facts,*keys)
    if asset:
        for key in ('configuration_id','system','business','owner','deployment','hardware','online_date','location','storage_platform'):
            value=asset.get('payload',{}).get(key)
            if not missing(value):
                facts[FIELD_LABELS.get(key,key)]=value
    return facts


def adapt_space(row,family):
    section=row.get('记录类型')
    label=pick(row,'tablespace_name','逻辑存储空间名称','表空间','表空间名称')
    schema=pick(row,'table_schema','schema_name')
    if missing(label) and schema is not None:
        label=schema
        fill(row,'类型','数据库/Schema','原生 TABLE_SCHEMA 容量汇总')
    if missing(label) and section in {'逻辑存储空间','存储文件'}:
        label=pick(row,'name','名称','logical_name')
    fill(row,'逻辑存储空间名称',label,'原报告存储对象名称')
    fill(row,'文件路径',pick(row,'file_name','physical_name','file_path','物理名称'),'原报告数据文件属性')
    if missing(row.get('类型')) and section=='逻辑存储空间' and pick(row,'tablespace_name','表空间名称') is not None:
        fill(row,'类型','表空间','原生表空间视图')
    amount=number(pick(row,'bytes','total_bytes','file_size_bytes','容量（字节）'))
    origin='原生字节字段'
    if amount is None:
        for keys,factor,unit in [(('total_mb','size_mb','data_mb','总大小(MB)','大小（MB）'),1024**2,'MiB'),
                                 (('total_gb','size_gb'),1024**3,'GiB')]:
            raw=number(pick(row,*keys))
            if raw is not None:
                amount=raw*factor
                origin='原报告 '+unit+' 容量换算'
                break
    if amount is None and family in {'sqlserver','sqlserver_jdbc'} and section=='存储文件':
        raw=number(pick(row,'size'))
        if raw is not None:
            amount=raw*8192
            origin='sys.database_files.size × 8192 字节'
    if amount is not None and amount>=0:
        row['存储大小（字节）']=int(amount)
        if missing(row.get('存储大小')) or pick(row,'size')==row.get('存储大小'):
            row['存储大小']=f'{amount / (1024**2):.2f} MiB'
            row.setdefault('字段来源',{})['存储大小']=origin
    if missing(row.get('当前使用率')):
        for total_key,used_key,free_key in [('total_mb','used_mb','free_mb'),('total_bytes','used_bytes','free_bytes'),
                                           ('tablespace_size','used_space','free_space')]:
            total,used,free=(number(pick(row,key)) for key in (total_key,used_key,free_key))
            if total is None or total<=0:
                continue
            if used is None and free is not None and 0<=free<=total:
                used=total-free
            if used is not None and used>=0:
                fill(row,'当前使用率',f'{used / total * 100:.2f}%','同一行同单位已用容量 / 总容量')
                break
    if missing(row.get('自动扩展模式')) and family in {'sqlserver','sqlserver_jdbc'}:
        growth=number(pick(row,'growth'))
        if growth is not None and growth>=0:
            percent=str(pick(row,'is_percent_growth')).lower() in {'1','true','yes'}
            fill(row,'自动扩展模式','关闭' if growth==0 else f'每次增长 {growth}%' if percent else
                 f'每次增长 {growth * 8192} 字节','SQL Server 文件增长配置')


def adapt_accounts(rows,asset):
    registered=(asset or {}).get('payload',{}).get('tables',{}).get('accounts',[])
    for row in rows:
        username=pick(row,'用户名','username','user_name','member_name','login_name','grantee')
        fill(row,'用户名',username,'原报告账号/授权主体')
        title=str(row.get('原报告章节','')).casefold()
        if 'sysadmin' in title:
            fill(row,'角色','sysadmin','原报告固定角色成员查询')
        grant=pick(row,'state_desc')
        if row.get('记录类型')=='权限明细' and grant in {'DENY','GRANT','GRANT_WITH_GRANT_OPTION','REVOKE'}:
            row['授权方式']=grant
            if not missing(row.get('权限')) and not str(row['权限']).startswith(str(grant)+' '):
                row['权限']=str(grant)+' '+str(row['权限'])
        for account in registered:
            if str(account.get('username'))==str(username):
                fill(row,'业务用途',account.get('purpose'),'该次巡检关联的账号台账')
                fill(row,'责任归属人',account.get('owner'),'该次巡检关联的账号台账')
        obj=pick(row,'授权对象','object_name','table_name')
        schema=pick(row,'schema_name','table_schema','owner','所有者')
        if obj is not None and schema is not None and '.' not in str(obj):
            row['授权对象']=str(schema)+'.'+str(obj)
    for row in rows:
        if row.get('记录类型')!='用户账号' or missing(row.get('用户名')):
            continue
        username=row['用户名']
        related=[entry for entry in rows if entry.get('用户名')==username]
        roles=sorted({str(entry['角色']) for entry in related if not missing(entry.get('角色'))})
        permissions=sorted({str(entry['权限'])+((' ON '+str(entry['授权对象'])) if not missing(entry.get('授权对象')) else '')
                            for entry in related if not missing(entry.get('权限'))})
        fill(row,'角色','、'.join(roles),'同次账号与角色成员关联')
        fill(row,'权限','；'.join(permissions),'同次账号直接授权关联')


def deduplicate(rows, fields):
    result=[]
    seen={}
    for row in rows:
        if missing(row.get(fields[0])):
            result.append(row)
            continue
        key=tuple('' if field=='参数版本' and row.get('字段来源',{}).get(field)=='本次参数值快照摘要' else
                  str(row.get(field)) for field in fields)
        prior=seen.get(key)
        if prior is None:
            seen[key]=row
            result.append(row)
            continue
        if prior is row:
            continue
        for field,value in row.items():
            if missing(prior.get(field)):
                prior[field]=value
            elif field not in {'字段来源','未记录字段','待登记字段','待采集字段','不适用字段','来源','来源查询','原报告章节','原表号','原表行号',
                               '记录编号','记录时间','观测时间','采集批次','同次合并来源','同次取值差异'} and prior[field]!=value:
                prior.setdefault('同次取值差异',{})[field]=[prior[field],value]
        origin={field:row.get(field) for field in ('来源','来源查询','原报告章节','原表号') if not missing(row.get(field))}
        if origin:
            prior.setdefault('同次合并来源',[]).append(origin)
    return result


def adapt_backup(row,family):
    parameter=pick(row,'policy_parameter')
    configured=pick(row,'policy_value')
    name=pick(row,'policy_name','job_name','schedule_name')
    if parameter is not None:
        row['策略配置项']=parameter
        row['策略配置值']=configured
        if 'RETENTION' in str(parameter).upper() and configured is not None:
            fill(row,'保存周期',configured,'原生 RMAN RETENTION POLICY 配置')
        if 'DEVICE TYPE' in str(parameter).upper():
            fill(row,'备份方式','RMAN / '+str(configured),'原生 RMAN 默认设备配置')
    fill(row,'备份策略编号',pick(row,'job_id','job_name','policy_name'),'原生备份作业标识')
    fill(row,'执行频率',pick(row,'repeat_interval'),'数据库调度器 REPEAT_INTERVAL')
    freq=number(pick(row,'freq_type'))
    interval=number(pick(row,'freq_interval'))
    if missing(row.get('执行频率')) and freq is not None:
        recurrence=number(pick(row,'freq_recurrence_factor')) or Decimal(1)
        if freq==1:
            cadence='一次性'
        elif freq==4 and interval is not None:
            cadence=f'每 {interval} 日'
        elif freq==8 and interval is not None:
            weekdays=['日','一','二','三','四','五','六']
            selected=['周'+day for bit,day in enumerate(weekdays) if int(interval)&(1<<bit)]
            cadence=f'每 {recurrence} 周 '+ '、'.join(selected)
        elif freq==16 and interval is not None:
            cadence=f'每 {recurrence} 月的 {interval} 日'
        else:
            cadence='SQL Agent 计划原编码 '+str(freq)+' / '+str(interval)
        subday=number(pick(row,'freq_subday_type'))
        step=number(pick(row,'freq_subday_interval'))
        if subday in {2,4,8} and step is not None:
            cadence+=f'，活动窗口内每 {step} '+{2:'秒',4:'分钟',8:'小时'}[int(subday)]
        fill(row,'执行频率',cadence,'SQL Server Agent 计划定义')
    start=pick(row,'active_start_time')
    end=pick(row,'active_end_time')
    def clock(value):
        raw=number(value)
        if raw is None or raw<0 or raw>235959:
            return None
        digits=str(int(raw)).zfill(6)
        if int(digits[2:4])>59 or int(digits[4:])>59:
            return None
        return digits[:2]+':'+digits[2:4]+':'+digits[4:]
    if clock(start) is not None:
        window=clock(start)+((' — '+clock(end)) if clock(end) is not None else '')
        fill(row,'备份窗口',window,'SQL Server Agent 计划活动时段')
    sql=pick(row,'sql_text')
    if sql:
        fill(row,'备份内容',sql,'原备份作业语句（已脱敏）')
        fill(row,'备份方式','SQL Server BACKUP' if family.startswith('sqlserver') else '数据库调度作业','原生备份作业类型')
    if name is not None or parameter is not None:
        fill(row,'策略版本','sha256:'+digest(canonical([name,parameter,configured,row.get('执行频率'),row.get('备份窗口'),sql]).encode())[:16],
             '本次备份配置快照摘要')


def adapt_tables(sheets,metadata,asset,observed_at):
    facts=facts_from(sheets,metadata,asset)
    family=str(facts.get('数据库类型') or metadata.get('db_type') or '').casefold()
    for title,rows in sheets.items():
        if title.startswith('原始'):
            continue
        for row in rows:
            if row.get('状态')=='原巡检未记录':
                if title=='全品类数据库资产底账清单' and any(not missing(facts.get(field)) for field in ('数据库名称','数据库地址','配置项编号')):
                    row.pop('状态',None)
                    row['来源']='原报告基础信息关联'
                else:
                    continue
            source_ci=facts.get('配置项编号')
            if not missing(source_ci) and not missing(row.get('配置项编号')) and row['配置项编号']!=source_ci:
                row['原资料配置项编号']=row['配置项编号']
                row['配置项编号']=source_ci
                row.setdefault('字段来源',{})['配置项编号']='同次台账配置项编号；原资料差异值已保留'
            for field in ('配置项编号','实例名称','数据库类型','数据库版本及补丁号'):
                fill(row,field,facts.get(field),'原报告核心属性/该次台账快照')
            if title in {'全品类数据库资产底账清单','数据库基本信息表'}:
                for field in ('数据库名称','数据库地址','端口','字符集','数据库文件大小','归档路径','归档模式','主机型号',
                              '设备位置','OS版本','存储平台','存储模式','备份策略','容灾状态','集群属性','硬件配置','部署方式',
                              '系统归属','业务用途','运维责任人','上线时间','运行状态'):
                    fill(row,field,facts.get(field),facts.get('_字段来源',{}).get(field,'原报告核心属性/该次台账快照'))
            if title in {'数据库参数表','逻辑存储空间属性表','备份执行记录'}:
                fill(row,'数据库名称',facts.get('数据库名称'),'原报告主连接数据库')
            if title=='数据库参数表':
                fill(row,'单位',pick(row,'unit','units'),'原生参数单位')
                name=str(row.get('参数名称') or '').casefold()
                if name in {'max_connections','processes','sessions','thread_cache_size','table_open_cache'}:
                    fill(row,'单位','count','参数定义中的数量单位')
                if (family in {'oracle','oracle_jdbc'} and name in {'sga_target','pga_aggregate_target'}) or \
                        (family in {'mysql','mariadb'} and name=='innodb_buffer_pool_size'):
                    fill(row,'单位','byte','该库型官方参数定义的字节单位')
                if family in {'oracle','oracle_jdbc','dm','dm8','mysql','mariadb','gbase8a'}:
                    fill(row,'运行值',pick(row,'value','para_value','variable_value'),'原生当前参数值视图')
                current=row.get('运行值') if not missing(row.get('运行值')) else row.get('配置值')
                if str(current).casefold() in {'true','false','on','off','yes','no'}:
                    fill(row,'单位','无量纲（开关）','原参数布尔/开关取值')
                if missing(row.get('单位')):
                    description=str(pick(row,'description','参数说明') or '').casefold()
                    units=[unit for pattern,unit in [(r'\bbytes?\b','byte'),(r'\bmilliseconds?\b','ms'),
                            (r'\bseconds?\b','s'),(r'\bminutes?\b','min'),(r'\(mb\)','MiB'),(r'\(kb\)','KiB')]
                           if re.search(pattern,description)]
                    if len(units)==1:
                        fill(row,'单位',units[0],'数据库原生参数 DESCRIPTION 单位说明')
                fill(row,'观测时间',observed_at,'原巡检记录时间')
                if missing(row.get('观测时间')) and not missing(facts.get('报告生成时间')):
                    fill(row,'观测时间',facts['报告生成时间'],'原报告标注的生成时间')
                    row['时间口径']='原报告生成时点；逐项采集时间未保留'
                row['参数快照版本']='sha256:'+digest(canonical([row.get('参数名称'),row.get('配置值'),row.get('运行值'),row.get('单位')]).encode())[:16]
                fill(row,'参数版本',row['参数快照版本'],'本次参数值快照摘要')
            if title=='逻辑存储空间属性表':
                adapt_space(row,family)
            if title=='集群参数表':
                fill(row,'数据库版本',facts.get('数据库版本及补丁号'),'原报告数据库版本')
            if title=='备份策略表':
                adapt_backup(row,family)
    adapt_accounts(sheets['用户及权限表'],asset)
    sheets['数据库参数表']=deduplicate(sheets['数据库参数表'],('参数名称','数据库名称','实例名称','配置值','运行值','参数版本'))
    for row in sheets['数据库参数表']:
        row['参数快照版本']='sha256:'+digest(canonical([row.get('参数名称'),row.get('配置值'),row.get('运行值'),row.get('单位')]).encode())[:16]
        if row.get('字段来源',{}).get('参数版本')=='本次参数值快照摘要':
            row['参数版本']=row['参数快照版本']
    sheets['用户及权限表']=deduplicate(sheets['用户及权限表'],('用户名','记录类型','角色','权限','授权对象'))
    nodes=sheets['集群参数表']
    cluster_description=str(facts.get('集群属性') or '')
    if cluster_description.casefold() in {'单实例','单机','standalone','single instance'} and all(
            row.get('状态')=='原巡检未记录' for row in nodes):
        nodes[:]=[{'记录类型':'单机适用性说明','集群类型':cluster_description,
                   '适用性':'不适用（原报告标记单实例）','配置项编号':facts.get('配置项编号'),
                   '数据库版本':facts.get('数据库版本及补丁号'),'节点名称':facts.get('实例名称'),
                   '依据':'原报告数据库基本信息中的集群属性'}]
    names={(str(pick(row,'inst_id','node_id','instance_id')),str(row.get('节点名称')),str(pick(row,'host_name','主机名')))
           for row in nodes if row.get('记录类型')=='集群节点' and not missing(row.get('节点名称'))}
    for row in nodes:
        if row.get('记录类型')=='集群节点' and names:
            fill(row,'节点数量',facts.get('节点数量'),'该次台账登记节点数量')
            fill(row,'节点数量',len(names),'本次报告可见节点实例/主机标识去重统计')
            row['本次可见节点数']=len(names)
            row['节点数量口径']='登记数量或本次可见节点统计，具体见字段来源'
            if len(names)==1 and cluster_description.casefold() in {'单实例','单机','standalone','single instance'} and missing(facts.get('集群编号')):
                fill(row,'集群类型',cluster_description,'原报告明确标注单实例')
    return facts


def classify_missing(title, row, headers):
    management={'配置项编号','集群编号','硬件配置','部署方式','系统归属','业务用途','运维责任人','责任归属人',
                '上线时间','设备位置','存储平台','使用率阈值'}
    references={'变更引用','权限变更引用'}
    remediation={'问题根因','整改方案','整改时限','责任人','验证记录','闭环状态'}
    inapplicable=set()
    if title=='集群参数表' and row.get('记录类型')=='集群节点':
        inapplicable={'参数名称','参数值'}
    if title=='集群参数表' and row.get('记录类型')=='单机适用性说明':
        inapplicable={'集群编号','节点角色','节点数量','集群网络配置','参数名称','参数值'}
    if title=='集群参数表' and str(row.get('集群类型') or '').casefold() in {'单实例','单机','standalone','single instance'}:
        inapplicable|={field for field in ('集群编号','节点角色','集群网络配置') if missing(row.get(field))}
    absent=[field for field in headers if missing(row.get(field)) and field not in inapplicable]
    row['未记录字段']=absent
    row['待登记字段']=[field for field in absent if field in management]
    row['未关联记录字段']=[field for field in absent if field in references]
    row['待补充处置字段']=[field for field in absent if title=='异常及整改记录' and field in remediation]
    row['待采集字段']=[field for field in absent if field not in management|references and
                    not (title=='异常及整改记录' and field in remediation)]
    row['不适用字段']=[field for field in headers if field in inapplicable]
