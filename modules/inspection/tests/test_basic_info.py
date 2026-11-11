# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""核心属性采集与补丁读取的单元测试（不依赖真实数据库驱动）。"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from modules.inspection import basic_info as bi


class FakeCursor:
    """按 SQL 片段匹配预置结果；未命中视为执行失败（模拟权限/版本不支持）。"""

    def __init__(self, cases):
        self.cases = cases
        self.description = None
        self._rows = []

    def execute(self, sql):
        lowered = ' '.join(str(sql).lower().split())
        for needle, cols, rows in self.cases:
            if needle.lower() in lowered:
                self.description = [(c,) for c in cols] if cols else None
                self._rows = list(rows)
                return
        raise RuntimeError('unsupported query: %s' % sql)

    def fetchall(self):
        return list(self._rows)


def _engine(db_type='oracle', **kw):
    ctx = kw.pop('context', {})
    return SimpleNamespace(db_type=db_type, host=kw.pop('host', '10.0.0.8'),
                           port=kw.pop('port', 1521), database=kw.pop('database', None),
                           context=ctx, instance_name=kw.pop('instance_name', ''))


class CollectDbTest(unittest.TestCase):
    def test_oracle_reads_version_patch_and_attributes(self):
        cur = FakeCursor([
            ('version_full', ['V'], [('19.19.0.0.0',)]),
            ('dba_registry_history', ['VERSION', 'ID', 'COMMENTS'],
             [('19.19.0.0.231017', '34086851', 'Database Release Update')]),
            ('nls_database_parameters', ['V'], [('AL32UTF8',)]),
            ('dba_data_files) +', ['TOTAL_BYTES'], [(1073741824,)]),
            ('log_mode', ['V'], [('ARCHIVELOG',)]),
            ('v$archive_dest', ['V'], [('/arch/orcl',)]),
            ('v$datafile where name', ['C'], [(0,)]),
            ('v$rman_configuration', ['N', 'V'], [('RETENTION POLICY', 'TO RECOVERY WINDOW OF 7 DAYS')]),
            ('database_role', ['V'], [('PRIMARY',)]),
            ('v$dataguard_stats', ['N', 'V'], [('apply lag', '0 seconds')]),
            ('gv$instance', ['N', 'H'], [('orcl1', 'rac1'), ('orcl2', 'rac2')]),
            ('v$instance', ['V'], [('orcl1',)]),
        ])
        out = bi.collect_db(cur, 'oracle')
        self.assertIn('19.19.0.0.0', out['version_patch'])
        self.assertIn('34086851', out['version_patch'])
        self.assertEqual(out['charset'], 'AL32UTF8')
        self.assertEqual(out['archive_mode'], 'ARCHIVELOG')
        self.assertEqual(out['archive_path'], '/arch/orcl')
        self.assertIn('1.00 GB', out['db_size'])
        self.assertIn('文件系统', out['storage_mode'])
        self.assertIn('RAC', out['cluster_attrs'])
        self.assertEqual(out['dr_status'].split('；')[0], 'PRIMARY')
        self.assertIn('RETENTION', out['backup_policy'])

    def test_oracle_asm_and_patch_fallback(self):
        cur = FakeCursor([
            ('version_full', ['V'], [('12.2.0.1.0',)]),
            ('v$datafile where name', ['C'], [(5,)]),
            ('dba_data_files) +', ['TOTAL_BYTES'], [(0,)]),
        ])
        out = bi.collect_db(cur, 'oracle')
        self.assertIn('ASM', out['storage_mode'])
        self.assertIn('未读取到', out['version_patch'])

    def test_mysql_binlog_and_replica(self):
        cur = FakeCursor([
            ('version()', ['V'], [('8.0.36',)]),
            ('database()', ['V'], [('biz',)]),
            ('@@hostname', ['V'], [('db01',)]),
            ('character_set_server', ['Variable_name', 'Value'], [('character_set_server', 'utf8mb4')]),
            ('information_schema.tables', ['G'], [(12.5,)]),
            ('log_bin_basename', ['Variable_name', 'Value'], [('log_bin_basename', '/data/binlog/mysql-bin')]),
            ('log_bin', ['Variable_name', 'Value'], [('log_bin', 'ON')]),
            ('datadir', ['Variable_name', 'Value'], [('datadir', '/data/mysql/')]),
            ('slave status', ['Slave_IO_Running', 'Slave_SQL_Running', 'Seconds_Behind_Master'],
             [('Yes', 'Yes', 0)]),
        ])
        out = bi.collect_db(cur, 'mysql')
        self.assertEqual(out['charset'], 'utf8mb4')
        self.assertEqual(out['db_size'], '12.50 GB')
        self.assertEqual(out['archive_path'], '/data/binlog/mysql-bin')
        self.assertIn('binlog 开启', out['archive_mode'])
        self.assertIn('Yes', out['dr_status'])
        self.assertIn('主从', out['cluster_attrs'])

    def test_pg_stream_replication(self):
        cur = FakeCursor([
            ('show server_version', ['server_version'], [('16.3',)]),
            ('pg_database_size', ['B'], [(2147483648,)]),
            ('current_database()', ['V'], [('app',)]),
            ('server_encoding', ['server_encoding'], [('UTF8',)]),
            ('archive_mode', ['archive_mode'], [('on',)]),
            ('archive_command', ['archive_command'], [("cp %p /archive/%f",)]),
            ('data_directory', ['data_directory'], [('/var/lib/pgsql/data',)]),
            ('pg_is_in_recovery', ['V'], [(False,)]),
            ('pg_stat_replication', ['C', 'S'], [('10.0.0.9', 'streaming')]),
        ])
        out = bi.collect_db(cur, 'postgresql')
        self.assertEqual(out['charset'], 'UTF8')
        self.assertEqual(out['db_size'], '2.00 GB')
        self.assertIn('流复制', out['dr_status'])
        self.assertIn('主备', out['cluster_attrs'])

    def test_sqlserver_patch_and_alwayson(self):
        cur = FakeCursor([
            ("serverproperty('productversion')", ['V', 'LVL', 'ED', 'UPD'],
             [('15.0.4360.2', 'RTM', 'Enterprise Edition', 'CU21')]),
            ('recovery_model_desc', ['V'], [('FULL',)]),
            ('db_name()', ['V'], [('biz',)]),
            ("serverproperty('instancename')", ['V'], [('MSSQLSERVER',)]),
            ("serverproperty('collation')", ['V'], [('Chinese_PRC_CI_AS',)]),
            ('sys.database_files', ['G'], [(3.25,)]),
            ('backupmediafamily', ['physical_device_name'], [('D:\\bak\\biz.bak',)]),
            ('sys.availability_groups', ['N', 'S', 'R'],
             [('AG1', 'node1', 'PRIMARY'), ('AG1', 'node2', 'SECONDARY')]),
        ])
        out = bi.collect_db(cur, 'sqlserver')
        self.assertIn('CU21', out['version_patch'])
        self.assertEqual(out['charset'], 'Chinese_PRC_CI_AS')
        self.assertIn('FULL', out['archive_mode'])
        self.assertIn('AlwaysOn', out['cluster_attrs'])

    def test_dm_archive_and_dsc(self):
        cur = FakeCursor([
            ('v$version', ['BANNER'], [('DM Database Server 64 V8 8.1.3.100',)]),
            ('v$database', ['V'], [('DAMENG',)]),
            ('v$instance', ['V'], [('DMSERVER',)]),
            ('v$dm_ini', ['V'], [('UTF-8',)]),
            ('dba_data_files', ['B'], [(5368709120,)]),
            ('v$dm_arch_ini', ['T', 'D'], [('LOCAL', '/dm/arch')]),
            ('v$dsc_ep_info', ['EP_NAME'], [('EP01',), ('EP02',)]),
        ])
        out = bi.collect_db(cur, 'dm8')
        self.assertEqual(out['charset'], 'UTF-8')
        self.assertIn('5.00 GB', out['db_size'])
        self.assertEqual(out['archive_path'], '/dm/arch')
        self.assertIn('DMDSC', out['cluster_attrs'])

    def test_oracle_sqlpatch_fallback(self):
        cur = FakeCursor([
            ('version_full', ['V'], [('19.20.0.0.0',)]),
            ('dba_registry_sqlpatch', ['ACTION_TIME', 'ACTION', 'COMMENTS', 'ID', 'VERSION'],
             [('2026-01-01', 'APPLY', 'Database Release Update', '35042068', '19.20.0.0.0')]),
        ])
        out = bi.collect_db(cur, 'oracle')
        self.assertIn('35042068', out['version_patch'])
        self.assertIn('Release Update', out['version_patch'])

    def test_all_probes_failing_is_tolerated(self):
        out = bi.collect_db(FakeCursor([]), 'oracle')
        self.assertEqual(out, {})
        self.assertEqual(bi.collect_db(None, 'oracle'), {})
        self.assertEqual(bi.collect_db(FakeCursor([]), 'unknown_db'), {})


class BuildTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_file = bi.OVERRIDE_FILE
        bi.OVERRIDE_FILE = Path(self._tmp.name) / 'basic_info.json'
        with bi._cache_lock:
            bi._cache.update({'mtime': None, 'data': {}})

    def tearDown(self):
        bi.OVERRIDE_FILE = self._old_file
        with bi._cache_lock:
            bi._cache.update({'mtime': None, 'data': {}})
        self._tmp.cleanup()

    def test_all_16_fields_present_with_explicit_unknown(self):
        info = bi.build(_engine('oracle'))
        self.assertEqual(len(info), 16)
        labels = dict(bi.FIELD_ORDER)
        self.assertEqual(labels['configuration_id'], '配置项编号')
        self.assertEqual(info['configuration_id'], '未登记')
        self.assertEqual(info['charset'], '未采集')
        self.assertEqual(len(bi.to_rows(info)), 16)

    def test_overrides_fill_static_fields_and_win(self):
        bi.OVERRIDE_FILE.write_text(json.dumps({
            'defaults': {'storage_platform': '华为 OceanStor'},
            'instances': {
                '10.0.0.8:1521': {
                    'configuration_id': 'CI-2026-0001',
                    'location': 'A 机房 3 排',
                    'host_model': 'Huawei 2288H V5',
                    'os_version': 'Kylin V10 SP3',
                    'charset': 'ZHS16GBK',
                }
            },
        }, ensure_ascii=False), encoding='utf-8')
        eng = _engine('oracle', context={'basic_info_db': {'charset': 'AL32UTF8'}})
        info = bi.build(eng)
        self.assertEqual(info['configuration_id'], 'CI-2026-0001')
        self.assertEqual(info['location'], 'A 机房 3 排')
        self.assertEqual(info['host_model'], 'Huawei 2288H V5')
        self.assertEqual(info['storage_platform'], '华为 OceanStor')
        self.assertEqual(info['charset'], 'ZHS16GBK')

    def test_system_info_supplies_os_and_host_model(self):
        eng = _engine('mysql', context={'system_info': {
            'os_version': 'Kylin Linux Advanced Server V11',
            'host_model': 'TaiShan 200',
        }})
        info = bi.build(eng)
        self.assertEqual(info['os_version'], 'Kylin Linux Advanced Server V11')
        self.assertEqual(info['host_model'], 'TaiShan 200')

    def test_build_from_parts_used_by_legacy_builder(self):
        bi.OVERRIDE_FILE.write_text(json.dumps({
            'instances': {'10.0.0.8:1521': {'configuration_id': 'CI-LEGACY'}}
        }, ensure_ascii=False), encoding='utf-8')
        info = bi.build_from_parts(
            host='10.0.0.8', port=1521, db_type='oracle',
            database='orcl', instance_name='orcl1',
            db_part={'version_patch': '19.19.0.0.0；补丁：RU 19.19'},
            system_info={'os_version': 'Kylin V10', 'host_model': '2288H V5'},
        )
        self.assertEqual(len(info), 16)
        self.assertEqual(info['configuration_id'], 'CI-LEGACY')
        self.assertEqual(info['db_name'], 'orcl')
        self.assertEqual(info['instance_name'], 'orcl1')
        self.assertEqual(info['os_version'], 'Kylin V10')
        self.assertIn('19.19', info['version_patch'])

    def test_version_fallback_marks_patch_missing(self):
        eng = _engine('oracle', context={'version': [{'VERSION': '11.2.0.4.0'}]})
        info = bi.build(eng)
        self.assertIn('11.2.0.4.0', info['version_patch'])
        self.assertIn('补丁', info['version_patch'])


if __name__ == '__main__':
    unittest.main()
