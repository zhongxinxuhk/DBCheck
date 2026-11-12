# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Architecture-specific cluster inspections built from the existing SQL catalog."""


from .model import KINDS


def template_config(kind):
    from modules.inspection.business_templates import build_cluster, _chapter
    from .collector import queries
    family = KINDS[kind]['family']
    if kind=='oracle_rac':
        chapters = build_cluster('oracle')
    elif kind=='dm_dsc':
        chapters = [
            _chapter(1,'DSC 节点与通信拓扑','DSC Nodes & MAL','核对预期节点与实际返回节点',
                     [('C_EP','DSC 节点'),('C_MAL','MAL 通信拓扑')],'dm8'),
            _chapter(2,'实例与共享存储','Instances & Storage','逐节点核查实例和存储状态',
                     [('W01','实例状态'),('W06','表空间'),('M02','归档配置')],'dm8'),
            _chapter(3,'节点参数与锁','Parameters & Locks','跨节点比较参数、版本和锁状态',
                     [('W09','关键参数'),('W14','锁状态')],'dm8'),
        ]
    else:
        catalog = queries(kind)
        groups = [('实例身份与主备角色',['identity','instance']),
                  ('复制与归档状态',['repl','destinations']),
                  ('参数与容量',['parameters','tbs'])]
        if kind=='dm_guard':
            groups[1][1].extend(['watcher','archive_config','mal'])
        chapters = []
        for number,(title,keys) in enumerate(groups,1):
            chapters.append({'chapter_number':number,'chapter_title_zh':title,
                'chapter_title_en':title,'description':'逐节点取证，缺少权限或未配置功能保留采集缺口。',
                'enabled':1,'sort_order':number,'queries':[
                    {'query_key':'cluster_'+key,'query_sql':catalog[key],'query_description_zh':title+' / '+key,
                     'enabled':1,'sort_order':order} for order,key in enumerate(keys,1)]})
    return {'db_type':'oracle' if family=='oracle' else 'dm8',
            'template_name':KINDS[kind]['label']+' 集群统一巡检',
            'description':'按显式登记的全部启用成员执行，统一汇总节点报告和采集缺口。',
            'version':'cluster-v1','is_default':0,'chapters':chapters}


def ensure_template(kind):
    from modules.inspection import dal
    config = template_config(kind)
    connection = dal.get_db_connection()
    try:
        row = connection.execute('SELECT id FROM inspection_template WHERE db_type=? AND template_name_zh=?',
                                 (config['db_type'],config['template_name'])).fetchone()
    finally:
        connection.close()
    if row:
        return row['id']
    try:
        template_id = dal.import_template(config,overwrite=False)
    except ValueError:
        connection = dal.get_db_connection()
        try:
            row = connection.execute('SELECT id FROM inspection_template WHERE db_type=? AND template_name_zh=?',
                                     (config['db_type'],config['template_name'])).fetchone()
            if row is None:
                raise
            return row['id']
        finally:
            connection.close()
    connection = dal.get_db_connection()
    try:
        connection.execute('UPDATE inspection_template SET is_preset=1 WHERE id=?',(template_id,))
        connection.commit()
    finally:
        connection.close()
    return template_id
