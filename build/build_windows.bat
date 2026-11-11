@echo off
REM SPDX-License-Identifier: Apache-2.0
REM Copyright 2025-2026 fiyo (Jack Ge) ^<sdfiyon@gmail.com^>
REM Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck
REM SPDX-License-Identifier: Apache-2.0
REM Copyright 2025-2026 sec.hn.cn (X.U zx) ^<xuzhongxin@sec.hn.cn^>
REM Author: sec.hn.cn (X.U zx) - https://github.com/zhongxinxuhk/DBCheck
chcp 65001 >nul
cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 exit /b 1
python build\ci_install_deps.py
if errorlevel 1 exit /b 1
if defined DBCHECK_WHEEL_DIR (
    python -m pip install --no-index --find-links "%DBCHECK_WHEEL_DIR%" pyinstaller
) else (
    python -m pip install pyinstaller
)
if errorlevel 1 exit /b 1
python build\build_release.py --platform windows
if errorlevel 1 exit /b 1
echo DBPulse Windows release verified. See dist for the ZIP and checksum.
if not defined GITHUB_ACTIONS pause
