@echo off
rem Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
rem SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "DBPULSE_PORTABLE_HOME=%~dp0"
set "JAVA_HOME=%~dp0runtime\windows-x86_64\jre"
if not exist "%~dp0bin\windows-x86_64\dbcheck.exe" (
    echo Incomplete Windows portable binary.
    exit /b 1
)
if not exist "%JAVA_HOME%\bin\java.exe" (
    echo Incomplete Windows portable Java runtime.
    exit /b 1
)
cd /d "%~dp0"
if "%~1"=="" (
    if exist "%~dp0bin\windows-x86_64\DBPulse.exe" (
        start "" "%~dp0bin\windows-x86_64\DBPulse.exe"
        exit /b 0
    )
)
"%~dp0bin\windows-x86_64\dbcheck.exe" %*
exit /b %errorlevel%
