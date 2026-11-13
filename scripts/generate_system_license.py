# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""生成 DBPulse 系统信息页使用的离线激活码。

示例：
    python scripts/generate_system_license.py \
        --type organization --to "某某单位" \
        --deployment-org "信息中心" --expires 2027-12-31
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.system_info.license import get_device_serial, issue_activation_code  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DBPulse 离线激活码")
    parser.add_argument(
        "--type",
        dest="licensed_to_type",
        choices=("personal", "organization"),
        default="organization",
        help="授权对象类型：personal（个人）或 organization（单位）",
    )
    parser.add_argument("--to", required=True, help="授权对象名称，例如个人姓名或单位名称")
    parser.add_argument("--deployment-org", default="", help="软件部署单位")
    parser.add_argument("--edition", default="商业专用版", help="授权版本名称")
    parser.add_argument("--expires", default="", help="到期日 YYYY-MM-DD；留空表示长期有效")
    parser.add_argument("--device", default="", help="目标设备序列号；留空读取本机序列号")
    args = parser.parse_args()

    serial = (args.device or get_device_serial()).strip().upper()
    code = issue_activation_code(
        licensed_to_type=args.licensed_to_type,
        licensed_to=args.to,
        deployment_org=args.deployment_org,
        edition=args.edition,
        expires_at=args.expires,
        device_serial=serial,
    )

    print(f"设备序列号: {serial}")
    print(f"授权对象: {'个人' if args.licensed_to_type == 'personal' else '单位'} / {args.to}")
    print(f"部署单位: {args.deployment_org or '—'}")
    print(f"有效期: {args.expires or '长期有效'}")
    print()
    print(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
