# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""生成 DBPulse 系统信息页使用的离线激活码（无界面包装）。

本脚本只加载独立的 ``license-console/license_console.py`` 算法，不导入
Flask、``modules`` 或任何 DBCheck 运行时代码，因此可以在没有启动主程序的
签发机上使用。图形界面请直接运行：

    python license-console/license_console.py

示例：
    python scripts/generate_system_license.py \\
        --type organization --to "某某单位" \\
        --device DBP-AAAA-BBBB-CCCC-DDDD \\
        --deployment-org "信息中心" --expires 2027-12-31
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "license-console" / "license_console.py"


def _load_console():
    spec = importlib.util.spec_from_file_location("standalone_license_console", CONSOLE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载独立授权算法：{CONSOLE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DBPulse 离线激活码（独立算法）")
    parser.add_argument(
        "--type",
        dest="licensed_to_type",
        choices=("personal", "organization"),
        default="organization",
        help="授权对象类型：personal（个人）或 organization（单位）",
    )
    parser.add_argument("--to", required=True, help="授权对象名称，例如个人姓名或单位名称")
    parser.add_argument("--device", required=True, help="目标设备序列号（从系统信息页复制）")
    parser.add_argument("--deployment-org", default="", help="软件部署单位")
    parser.add_argument("--edition", default="商业专用版", help="授权版本名称")
    parser.add_argument("--expires", default="", help="到期日 YYYY-MM-DD；留空表示长期有效")
    parser.add_argument("--days", type=int, default=0, help="授权天数；与 --expires 二选一")
    parser.add_argument("--license-id", default="", help="授权编号；留空自动生成")
    args = parser.parse_args()
    if args.days and args.expires:
        parser.error("--days 与 --expires 只能填写一个")

    console = _load_console()
    serial = args.device.strip().upper()
    license_id = (args.license_id or console.new_license_id()).strip()
    code = console.issue_activation_code(
        licensed_to_type=args.licensed_to_type,
        licensed_to=args.to,
        deployment_org=args.deployment_org,
        edition=args.edition,
        expires_at=args.expires,
        device_serial=serial,
        license_id=license_id,
        valid_days=args.days,
    )

    print(f"设备序列号: {serial}")
    print(f"授权编号: {license_id}")
    print(f"授权对象: {'个人' if args.licensed_to_type == 'personal' else '单位'} / {args.to}")
    print(f"部署单位: {args.deployment_org or '—'}")
    print(f"有效期: {args.expires or (f'{args.days} 天' if args.days else '长期有效')}")
    print()
    print(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
