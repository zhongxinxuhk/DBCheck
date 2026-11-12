#!/usr/bin/env python3
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Inspect bundled/local JDBC identities, bytecode requirements and SHA-256 offline."""


import argparse
import hashlib
import json
import struct
import zipfile
from pathlib import Path

CLASSES = {
    'oracle': 'oracle.jdbc.OracleDriver',
    'sqlserver': 'com.microsoft.sqlserver.jdbc.SQLServerDriver',
    'gbase8a': 'com.gbase.jdbc.Driver',
    'dm': 'dm.jdbc.driver.DmDriver',
}


def audit(root):
    rows=[]
    for family,driver in CLASSES.items():
        for path in sorted((root/family).rglob('*.jar')):
            row={'path':str(path.relative_to(root)), 'db_type':family,
                 'driver_class':driver, 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            try:
                with zipfile.ZipFile(path) as jar:
                    data=jar.read(driver.replace('.','/')+'.class')
                    major=struct.unpack('>H',data[6:8])[0]
                    row.update(status='class_verified',minimum_java=major-44)
            except (OSError,KeyError,zipfile.BadZipFile) as error:
                row.update(status='invalid_driver',reason=type(error).__name__)
            rows.append(row)
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--drivers',type=Path,default=Path(__file__).resolve().parents[1]/'drivers')
    args=parser.parse_args()
    rows=audit(args.drivers)
    print(json.dumps(rows,ensure_ascii=False,indent=2))
    # A quarantined legacy mismatch is reported, while every product must have a valid alternative.
    raise SystemExit(0 if all(any(r['db_type']==f and r['status']=='class_verified' for r in rows) for f in CLASSES) else 1)
