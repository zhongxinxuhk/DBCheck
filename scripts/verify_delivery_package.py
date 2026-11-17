#!/usr/bin/env python3
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Verify a delivery ZIP offline using an independently trusted public-key hash."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from modules.service_management.delivery_render import verify_package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--fingerprint', required=True, help='可信渠道确认的公钥SHA-256指纹')
    args = parser.parse_args()
    try:
        result = verify_package(args.package.read_bytes(), args.fingerprint)
    except Exception as error:
        print('FAIL ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
