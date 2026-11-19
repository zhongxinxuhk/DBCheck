# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""慢查询页数据源清单：无慢 SQL 的库也必须出现在下拉框和计数中。"""

from modules.monitor.engine import (
    build_slow_source_catalog,
    flatten_slow_queries,
)


def instance(iid, name, db_type='mysql', enabled=True, host='10.0.0.1'):
    return {
        'id': iid,
        'name': name,
        'db_type': db_type,
        'enabled': enabled,
        'host': host,
        'port': 3306,
    }


def snapshot(label, db_type='mysql', rows=1, error=None):
    return {
        'label': label,
        'db_type': db_type,
        'ts': 123,
        'error': error,
        'data': [{'sql_text': 'select 1'}] * rows,
    }


def test_catalog_includes_configured_sources_without_slow_rows():
    instances = [
        instance('dm-1', '达梦一', 'dm'),
        instance('dm-2', '达梦二', 'dm'),
        instance('mysql-1', '业务 MySQL', 'mysql'),
    ]
    catalog = build_slow_source_catalog(instances, {
        'dm-1': snapshot('达梦一 (10.0.0.1:3306)', 'dm', 2),
        'dm-2': snapshot('达梦二 (10.0.0.1:3306)', 'dm', 1),
    })

    assert [item['instance_id'] for item in catalog] == ['dm-1', 'dm-2', 'mysql-1']
    assert [item['slow_count'] for item in catalog] == [2, 1, 0]
    assert catalog[2]['collected'] is False
    assert catalog[2]['source'] == '业务 MySQL (10.0.0.1:3306)'


def test_catalog_skips_disabled_empty_host_and_duplicate_instances():
    instances = [
        instance('on', '启用', 'oracle'),
        instance('off', '停用', 'oracle', enabled=False),
        instance('empty', '空地址', 'oracle', host=''),
        instance('on', '重复', 'oracle'),
    ]
    catalog = build_slow_source_catalog(instances, {})

    assert [item['instance_id'] for item in catalog] == ['on']
    assert catalog[0]['collected'] is False


def test_catalog_keeps_error_status_but_zero_slow_count():
    catalog = build_slow_source_catalog(
        [instance('broken', '故障库', 'pg')],
        {'broken': snapshot('故障库 (10.0.0.1:3306)', 'pg', 4, error='连接超时')},
    )

    assert len(catalog) == 1
    assert catalog[0]['error'] == '连接超时'
    assert catalog[0]['slow_count'] == 0
    assert catalog[0]['collected'] is True


def test_catalog_falls_back_to_engine_snapshots_when_instance_read_fails():
    catalog = build_slow_source_catalog(None, {
        'gbase-1': snapshot('GBase 8a (10.0.0.8:5258)', 'gbase8a', 0),
    })

    assert catalog == [{
        'instance_id': 'gbase-1',
        'source': 'GBase 8a (10.0.0.8:5258)',
        'label': 'GBase 8a (10.0.0.8:5258)',
        'db_type': 'gbase8a',
        'error': None,
        'ts': 123,
        'slow_count': 0,
        'collected': True,
    }]


def test_empty_instance_list_means_no_current_sources():
    assert build_slow_source_catalog([], {'stale': snapshot('旧库', rows=1)}) == []


def test_flatten_uses_current_label_after_instance_rename():
    instances = [instance('mysql-1', '新名称', 'mysql')]
    sources = build_slow_source_catalog(
        instances,
        {'mysql-1': snapshot('旧名称 (10.0.0.1:3306)', 'mysql', 2)},
    )

    items = flatten_slow_queries(
        {'mysql-1': snapshot('旧名称 (10.0.0.1:3306)', 'mysql', 2)},
        sources,
    )

    assert len(items) == 2
    assert {item['source'] for item in items} == {'新名称 (10.0.0.1:3306)'}
    assert {item['label'] for item in items} == {'新名称 (10.0.0.1:3306)'}


def test_flatten_ignores_stale_or_empty_snapshots():
    sources = build_slow_source_catalog([], {'stale': snapshot('旧库', rows=1)})

    assert flatten_slow_queries({'stale': snapshot('旧库', rows=1)}, sources) == []
    assert flatten_slow_queries({'x': {'data': None}}, [
        {'instance_id': 'x', 'source': 'X', 'db_type': 'mysql'}
    ]) == []


def test_catalog_tolerates_missing_snapshots():
    assert build_slow_source_catalog(None, None) == []
