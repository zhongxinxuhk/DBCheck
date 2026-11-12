# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck
# RaccoonX Windows 打包配置

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

import os
import sys

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Build script cd's to project root before calling pyinstaller.
# So CWD == project root directory.
PROJECT_DIR = os.getcwd()

# Directories to include as data
# NOTE: 'data' is a runtime directory (SQLite DBs), not packaged.
data_dirs = [
    'web_templates', 'i18n', 'templates',
    'assets',           # 静态资源(web/品牌logo/db_logos)随包复制，static_folder 依赖
    'modules/rag', 'modules/pro',
    'drivers',
    'plugins',          # oracle_jdbc 等插件由 plugin_loader 动态加载，需随包复制
    'modules/user_management',  # RBAC 蓝图模板(html)与初始化库(schema)需随包复制
    'modules/config',   # builtin_registry/builtin_types/version 等 JSON 配置需随包复制
    'db',               # user_management_schema.sql 建表脚本需随包复制
]

# Build from clean defaults and collect dynamically loaded first-party modules.
sys.path.insert(0, os.path.join(PROJECT_DIR, 'build'))
from release_inputs import release_datas, dynamic_modules

portable_build = os.environ.get('DBPULSE_PORTABLE_BUILD') == '1'
if portable_build:
    data_dirs = ['i18n', 'modules/rag', 'modules/pro', 'modules/user_management', 'modules/config']
datas = release_datas(PROJECT_DIR, data_dirs, portable=portable_build)
datas += collect_data_files('docxcompose')
datas += collect_data_files('tzdata')
datas += collect_data_files('babel')
release_modules = dynamic_modules(PROJECT_DIR)

a = Analysis(
    [os.path.join(PROJECT_DIR, 'web_ui.py')],
    pathex=[PROJECT_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=[
        *release_modules,
        'croniter', 'tzdata', 'jpype', '_jpype', 'pymongo',
        'babel.numbers', 'babel.dates',
        'numpy', 'pandas', 'docxtpl',
        *collect_submodules('modules.service_management'),
        *collect_submodules('modules.operations'),
        *collect_submodules('docxcompose'),
        'modules.entrypoints.main_gbase8a',
        'modules.db_types.compatibility',
        *collect_submodules('pytds'),
        'flask',
        'pymysql', 'pymysql.constants', 'pymysql.constants.CLIENT',
        'psycopg2', 'psycopg2._psycopg',
        'oracledb',
        'pyodbc',
        'jaydebeapi',       # DM8 / GBase 8s JDBC 连接（DM8 巡检与测试连接现已走 jaydebeapi）
        'jpype',           # jaydebeapi 的 JVM 后端；SQL Server/DB2/HGDB/DM8 JDBC 驱动均依赖
        'redis',  # Redis / Redis Cluster 插件动态 import，需显式 hiddenimport 才能打进冻结版
        'paramiko', 'paramiko.transport', 'paramiko.auth_handler',
        'jinja2', 'jinja2.ext',
        'docx',
        'openpyxl',
        'psutil', 'psutil._psutil_windows', 'psutil._pswindows',
        'charset_normalizer', 'charset_normalizer.md',
        'certifi',
        'cryptography',
        'cryptography.fernet',
        'cryptography.utils',
        'cryptography.__about__',
        # fix(#57): oracledb thin 模式运行时动态导入 cryptography.hazmat.primitives.kdf.*，
        # 手工罗列曾漏掉 kdf/hashes/serialization 等（DPY-3016），改为全量收集。
        *collect_submodules('cryptography.hazmat'),
        'bcrypt',
        'markupsafe', 'markupsafe._speedups',
        'werkzeug', 'werkzeug._internal', 'werkzeug.utils', 'werkzeug.wrappers',
        'itsdangerous',
        'click', 'click._compat',
        'blinker',
        'cffi', 'cffi.api', 'cffi.backend_ctypes',
        'six',
        'idna',
        'urllib3', 'urllib3.util', 'urllib3.util.ssl_',
        'et_xmlfile', 'et_xmlfile.xmlfile',
        'yaml', 'yaml.composer', 'yaml.constructor', 'yaml.cyaml',
        'dotenv',
        'asyncio',
        'gevent', 'gevent.monkey', 'gevent.socket', 'gevent.pywsgi',
        'gevent.local', 'gevent.hub',
        'gevent.server', 'gevent._greenlet_primitives',
        'greenlet',
        'engineio.async_drivers.gevent',
        'socketio.async_server.gevent',
        # App modules（已统一迁入 modules/ 命名空间；列举各子包，PyInstaller 递归收集全部子模块）
        'modules.entrypoints', 'modules.inspection', 'modules.web',
        'modules.pro', 'modules.rag', 'modules.user_management',
        'modules.desensitize', 'modules.notify', 'modules.monitor',
        # modules/skill 是对外分发的 SkillHub 技能包（非运行时 import，含 2.9MB JDBC jar），
        # 故意不列入 hiddenimports，避免把整包打进安装包。
        'modules.server', 'modules.ssh', 'modules.ingest',
        'modules.intelligence', 'modules.core', 'modules.config',
        'modules.db_types', 'modules.disaster_recovery', 'modules.objstore',
        'modules.pluginkit',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(PROJECT_DIR, 'build', 'runtime-hook-wine.py')],
    excludes=['pyodbc','dmPython','gbase8sdb','cx_Oracle'] if portable_build else [],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='dbcheck',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=not portable_build,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# A windowed launcher owns the status window and notification icon.
gui_analysis = Analysis(
    [os.path.join(PROJECT_DIR, 'modules', 'desktop', 'windows_launcher.py')],
    pathex=[PROJECT_DIR], binaries=[], datas=[],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.messagebox', 'psutil'],
    hookspath=[], hooksconfig={}, runtime_hooks=[os.path.join(PROJECT_DIR, 'build', 'runtime-hook-wine.py')], excludes=[], noarchive=False,
)
gui_pyz = PYZ(gui_analysis.pure)
gui = EXE(gui_pyz, gui_analysis.scripts, [], exclude_binaries=True,
    name='DBPulse', console=False, debug=False, strip=False, upx=False,
    icon=os.path.join(PROJECT_DIR, 'assets', 'brand', 'favicon.ico'))

coll = COLLECT(
    exe,
    gui,
    gui_analysis.binaries,
    gui_analysis.datas,
    a.binaries,
    a.datas,
    name='DBPulse-Windows',
    upx=not portable_build,
    upx_exclude=[],
    bootloader_ignore_signals=False,
    target_arch=None,
    strip=False,
    debug=False,
)


# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck
