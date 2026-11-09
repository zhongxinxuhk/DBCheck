# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
生成《数据库运维服务清单》配套的业务专项巡检模板并写入 inspection.db。

设计约束（与合同清单一一对应）：
- 全部为「新建」，不覆盖任何既有模板：统一走 dal.import_template(overwrite=False)
- 全部 is_default=0，避免抢占既有默认模板，不改变现网巡检行为
- 阈值与故障判据不写死，落在章节 description 中作为可自定义基线
- 命名规则：{母模板名}_业务专项巡检_{专项名}

用法：
    python scripts/gen_service_inspection_templates.py --dry-run
    python scripts/gen_service_inspection_templates.py
    python scripts/gen_service_inspection_templates.py --cd-per-product
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.inspection.business_templates import *


def main():
    parser = argparse.ArgumentParser(description='播种全部内置业务专项模板与基线')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--cd-per-product', action='store_true', help='保留旧参数；现在始终按全部产品生成')
    parser.add_argument('--db-path')
    args = parser.parse_args()
    if args.dry_run:
        specs = build_template_specs(cd_per_product=True)
        print('内置业务专项模板: %d' % len(specs))
        return 0
    print('新增内置模板: %d' % ensure_business_defaults(args.db_path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
