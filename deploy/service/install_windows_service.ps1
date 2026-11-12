# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
#
# 将 DBPulse 注册为 Windows 后台服务（开机自启、无窗口、崩溃自动重启）
#
# 用法（管理员 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File deploy\service\install_windows_service.ps1 `
#       -AppDir "C:\dbpulse" -ServiceName "DBPulse"
#
# 依赖：优先使用 NSSM（https://nssm.cc，放到 PATH 即可）；
#       未安装 NSSM 时退化为「计划任务 + 开机启动」，同样无窗口常驻。

param(
    [string]$AppDir = "$(Split-Path -Parent (Split-Path -Parent $PSScriptRoot))",
    [string]$ServiceName = "DBPulse",
    [string]$Python = "",
    [int]$Workers = 3,
    [int]$MonitorWorkers = 8,
    [int]$MonitorInterval = 15,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    param([string]$Root, [string]$Explicit)
    if ($Explicit) { return $Explicit }
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    $exe = Join-Path $Root "dbcheck.exe"
    if (Test-Path $exe) { return $exe }
    return "python.exe"
}

$AppDir = (Resolve-Path $AppDir).Path
$Python = Resolve-Python -Root $AppDir -Explicit $Python
$LogDir = Join-Path $AppDir "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$envPairs = @(
    "DBPULSE_BIND_HOST=0.0.0.0",
    "PYTHONUNBUFFERED=1",
    "DBPULSE_INSPECTION_MAX_CONCURRENT=$Workers",
    "DBPULSE_MONITOR_WORKERS=$MonitorWorkers",
    "DBPULSE_MONITOR_INTERVAL=$MonitorInterval"
)

if ($Uninstall) {
    if (Get-Command nssm -ErrorAction SilentlyContinue) {
        nssm stop $ServiceName 2>$null | Out-Null
        nssm remove $ServiceName confirm 2>$null | Out-Null
    }
    Unregister-ScheduledTask -TaskName $ServiceName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "已移除服务/计划任务：$ServiceName"
    exit 0
}

if (Get-Command nssm -ErrorAction SilentlyContinue) {
    nssm install $ServiceName $Python "web_ui.py" | Out-Null
    nssm set $ServiceName AppDirectory $AppDir | Out-Null
    nssm set $ServiceName AppEnvironmentExtra ($envPairs -join "`r`n") | Out-Null
    nssm set $ServiceName AppStdout (Join-Path $LogDir "service.out.log") | Out-Null
    nssm set $ServiceName AppStderr (Join-Path $LogDir "service.err.log") | Out-Null
    nssm set $ServiceName AppRotateFiles 1 | Out-Null
    nssm set $ServiceName AppRotateOnline 1 | Out-Null
    nssm set $ServiceName AppRotateBytes 20971520 | Out-Null
    nssm set $ServiceName Start SERVICE_AUTO_START | Out-Null
    nssm set $ServiceName AppExit Default Restart | Out-Null
    nssm set $ServiceName AppRestartDelay 5000 | Out-Null
    Start-Service $ServiceName
    Write-Host "已通过 NSSM 注册并启动服务：$ServiceName（AppDir=$AppDir）"
    exit 0
}

# 无 NSSM：使用计划任务（SYSTEM 账户，开机启动，不弹窗）
$action = New-ScheduledTaskAction -Execute $Python -Argument "web_ui.py" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

foreach ($pair in $envPairs) {
    $kv = $pair.Split("=", 2)
    [Environment]::SetEnvironmentVariable($kv[0], $kv[1], "Machine")
}

Register-ScheduledTask -TaskName $ServiceName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $ServiceName
Write-Host "未检测到 NSSM，已注册开机计划任务：$ServiceName（AppDir=$AppDir）"
