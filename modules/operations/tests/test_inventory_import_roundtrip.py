# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""台账导出台账再回导的端口兼容回归测试。"""

import io

import pytest
from openpyxl import Workbook, load_workbook

from modules.operations.exports import inventory_xlsx
from modules.operations.imports import parse_excel, source_value


def record(port, ssh_port=22):
    return {
        'configuration_id': 'CI-001',
        'source': {
            'name': '测试库', 'db_type': 'mysql', 'host': '10.0.0.1',
            'port': port, 'user': 'root', 'password': 'secret',
            'database': 'db', 'group': '', 'enabled': True,
            'ssh_port': ssh_port,
        },
        'metadata': {'project_id': 'P1'},
    }


@pytest.mark.parametrize('value', [3306, 3306.0, '3306', ' 3306 ', '3306.0'])
def test_port_accepts_excel_integer_variants(value):
    assert source_value('port', value) == 3306


@pytest.mark.parametrize('value', [True, 3306.5, '3306.5', 'abc', 0, 65536])
def test_port_rejects_non_integer_or_out_of_range(value):
    with pytest.raises(ValueError, match='端口'):
        source_value('port', value)


@pytest.mark.parametrize('port,ssh_port', [
    ('3306.0', '22.0'), (3306.0, 22.0), (' 3306 ', ' 22 '),
])
def test_exported_ledger_can_be_reimported(port, ssh_port):
    content = inventory_xlsx([record(port, ssh_port)], []).read()

    parsed = parse_excel(content)

    assert parsed[0]['source']['port'] == 3306
    assert parsed[0]['source']['ssh_port'] == 22

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
    try:
        page = workbook['全品类数据库资产底账清单']
        headers = [cell.value for cell in next(page.iter_rows(min_row=3, max_row=3))]
        values = [cell.value for cell in next(page.iter_rows(min_row=4, max_row=4))]
        port_cell = values[headers.index('数据库端口')]
        assert port_cell == 3306
        assert isinstance(port_cell, int)
    finally:
        workbook.close()


def test_legacy_ledger_sheet_titles_and_headers_are_accepted():
    """低版本台账的旧工作表名/旧列名应能映射到当前台账字段。"""
    workbook = Workbook()
    page = workbook.active
    page.title = "资产台账"
    page.append([
        "配置项编号", "数据源名称", "数据库类型", "数据库地址", "端口",
        "账号", "数据库密码", "数据库名", "启用", "系统归属",
    ])
    page.append([
        "CI-LEGACY", "旧版库", "mysql", "10.0.0.8", "3306",
        "root", "placeholder", "legacy_db", "是", "旧系统",
    ])

    connection = workbook.create_sheet("数据源连接")
    connection.append(["配置项编号", "JDBC连接串"])
    connection.append(["CI-LEGACY", "jdbc:mysql://10.0.0.8:3306/legacy_db"])

    basic = workbook.create_sheet("数据库基本信息")
    basic.append(["配置项编号", "数据库名称", "字符集"])
    basic.append(["CI-LEGACY", "legacy_db", "utf8mb4"])

    policy = workbook.create_sheet("备份策略")
    policy.append([
        "配置项编号", "备份窗口", "备份类型", "内容", "方式",
        "执行频率", "保留周期", "备份责任人",
    ])
    policy.append([
        "CI-LEGACY", "每日 02:00", "全量", "全库", "物理",
        "每天", "30天", "张三",
    ])

    workbook.create_sheet("说明").append(["旧版模板说明，导入时应忽略"])

    buffer = io.BytesIO()
    workbook.save(buffer)

    parsed = parse_excel(buffer.getvalue())

    assert len(parsed) == 1
    source = parsed[0]["source"]
    assert source["name"] == "旧版库"
    assert source["db_type"] == "mysql"
    assert source["host"] == "10.0.0.8"
    assert source["port"] == 3306
    assert source["user"] == "root"
    assert source["password"] == "placeholder"
    assert source["database"] == "legacy_db"
    assert source["enabled"] is True
    assert source["jdbc_url"] == "jdbc:mysql://10.0.0.8:3306/legacy_db"
    assert parsed[0]["metadata"]["system"] == "旧系统"
    assert parsed[0]["metadata"]["charset"] == "utf8mb4"
    assert "owner" not in parsed[0]["metadata"]
    policy_row = parsed[0]["metadata"]["tables"]["backup_policies"][0]
    assert policy_row["window"] == "每日 02:00"
    assert policy_row["data_type"] == "全量"
    assert policy_row["content"] == "全库"
    assert policy_row["method"] == "物理"
    assert policy_row["frequency"] == "每天"
    assert policy_row["retention"] == "30天"
    assert policy_row["owner"] == "张三"


def test_unknown_nonempty_sheet_still_rejects_instead_of_silently_dropping_data():
    workbook = Workbook()
    page = workbook.active
    page.title = "全品类数据库资产底账清单"
    page.append(["配置项编号", "资产名称", "数据库类型", "数据库IP/主机", "数据库端口"])
    page.append(["CI-001", "测试库", "mysql", "10.0.0.1", 3306])
    workbook.create_sheet("旧版额外数据").append(
        ["配置项编号", "说明", "CI-001", "不能静默丢弃"]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)

    with pytest.raises(ValueError, match="不支持的工作表"):
        parse_excel(buffer.getvalue())


def test_main_sheet_fallback_ignores_sheets_without_source_columns():
    workbook = Workbook()
    note = workbook.active
    note.title = "说明"
    note.append(["配置项编号", "资产名称"])
    note.append(["CI-NOTE", "说明页不是主表"])

    legacy = workbook.create_sheet("旧版资产")
    legacy.append([
        "配置项编号", "资产名称", "数据库类型", "数据库IP/主机", "数据库端口",
    ])
    legacy.append(["CI-001", "测试库", "mysql", "10.0.0.1", 3306])

    buffer = io.BytesIO()
    workbook.save(buffer)

    parsed = parse_excel(buffer.getvalue())
    assert len(parsed) == 1
    assert parsed[0]["source"]["name"] == "测试库"
    assert parsed[0]["source"]["host"] == "10.0.0.1"


def test_duplicate_main_sheet_is_rejected_instead_of_silently_ignored():
    workbook = Workbook()
    main = workbook.active
    main.title = "全品类数据库资产底账清单"
    main.append(["配置项编号", "资产名称", "数据库类型", "数据库IP/主机", "数据库端口"])
    main.append(["CI-001", "主表", "mysql", "10.0.0.1", 3306])
    duplicate = workbook.create_sheet("资产台账")
    duplicate.append(["配置项编号", "资产名称", "数据库类型", "数据库IP/主机", "数据库端口"])
    duplicate.append(["CI-002", "重复表", "mysql", "10.0.0.2", 3306])

    buffer = io.BytesIO()
    workbook.save(buffer)

    with pytest.raises(ValueError, match="重复主表"):
        parse_excel(buffer.getvalue())
