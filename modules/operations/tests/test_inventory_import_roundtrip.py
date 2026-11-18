# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""台账导出台账再回导的端口兼容回归测试。"""

import io

import pytest
from openpyxl import load_workbook

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
