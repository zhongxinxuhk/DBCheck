# 后台服务运行（Linux / macOS / Windows）

DBPulse（RaccoonX）的巡检与监控都在**服务端**执行，与浏览器页面无关。但仍有两种情况会打断任务：

| 操作 | 任务是否中断 | 说明 |
| --- | --- | --- |
| 切换页面 / 打开其它菜单 | 不中断 | 任务在后端线程与子进程中继续，页面重新进入可看到任务状态与报告 |
| 关闭浏览器标签 | 不中断 | 同上；但页面上的实时日志推送会停止，报告仍会生成 |
| 关闭终端 / 关闭启动窗口（源码运行） | **中断** | 进程退出会按 `proc_registry` 回收巡检子进程 |
| 服务化后 SSH 断线 / 关终端 | 不中断 | 由 systemd / launchd / Windows 服务托管 |
| 服务重启（升级、手动 restart） | 正在跑的巡检中断 | 定时任务按 cron 下次触发；已完成报告不受影响 |

**结论：要"用户随便切页面、关窗口都不怕"，用下面的服务方式运行。**

---

## 一、Linux（systemd）

仓库已带单元文件 `deploy/service/dbpulse.service`。

```bash
# 1. 准备目录与运行用户（示例路径 /opt/dbpulse）
sudo useradd -r -s /sbin/nologin -d /opt/dbpulse dbpulse
sudo mkdir -p /opt/dbpulse && sudo chown -R dbpulse:dbpulse /opt/dbpulse

# 2. 部署代码与虚拟环境
sudo -u dbpulse git clone https://github.com/zhongxinxuhk/DBCheck.git /opt/dbpulse
cd /opt/dbpulse
sudo -u dbpulse python3 -m venv .venv
sudo -u dbpulse .venv/bin/pip install -r deploy/requirements.txt

# 3. 安装单元文件（如路径不同，先编辑 WorkingDirectory / ExecStart）
sudo cp deploy/service/dbpulse.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dbpulse

# 4. 验证
systemctl status dbpulse --no-pager
curl -fsS http://127.0.0.1:5003/api/v1/health
journalctl -u dbpulse -n 50 --no-pager
```

说明：

- 单元里默认 `DBPULSE_BIND_HOST=0.0.0.0`；如果只允许本机访问，改回 `127.0.0.1` 或在前置 nginx 后反代。
- `Restart=always` + `RestartSec=5`：进程异常退出后自动拉起。
- `KillMode=mixed` + `TimeoutStopSec=30`：停止时先通知主进程，再回收剩余子进程，避免 JVM 巡检残留。
- 运行日志：`data/logs/web_service_restart.log`、`data/logs/scheduler.log`。

### 升级与回滚

```bash
# 升级
sudo systemctl stop dbpulse
sudo -u dbpulse git -C /opt/dbpulse pull --ff-only
sudo -u dbpulse /opt/dbpulse/.venv/bin/pip install -r /opt/dbpulse/deploy/requirements.txt
sudo systemctl start dbpulse

# 回滚
sudo -u dbpulse git -C /opt/dbpulse checkout <上一个 tag>
sudo systemctl restart dbpulse
```

`data/` 目录与代码分离，升级不会影响账号、数据源、报告与历史。

---

## 二、macOS（launchd）

仓库已带 `deploy/service/com.dbpulse.web.plist`（登录用户级服务）。

```bash
# 1. 按实际路径修改 ProgramArguments / WorkingDirectory / 日志路径
cp deploy/service/com.dbpulse.web.plist ~/Library/LaunchAgents/
vim ~/Library/LaunchAgents/com.dbpulse.web.plist

# 2. 加载并开机自启
launchctl load -w ~/Library/LaunchAgents/com.dbpulse.web.plist

# 3. 验证
launchctl list | grep dbpulse
curl -fsS http://127.0.0.1:5003/api/v1/health
tail -f data/logs/launchd.out.log

# 停止 / 卸载
launchctl unload -w ~/Library/LaunchAgents/com.dbpulse.web.plist
```

说明：

- `KeepAlive=true` 崩溃自动重启；`RunAtLoad=true` 登录即启动。
- 若要开机（未登录）就运行，需改为系统级 LaunchDaemon（`/Library/LaunchDaemons/`，`UserName` 指定运行用户）。
- 休眠会暂停进程，巡检/监控随之中断；长期运行建议关闭休眠或使用台式机/服务器。

---

## 三、Windows（服务或计划任务）

仓库已带 `deploy/service/install_windows_service.ps1`，管理员 PowerShell 执行：

```powershell
# 方式 A：已安装 NSSM（推荐，真正的 Windows 服务）
powershell -ExecutionPolicy Bypass -File deploy\service\install_windows_service.ps1 `
    -AppDir "C:\dbpulse" -ServiceName "DBPulse" -Workers 3 -MonitorWorkers 8

# 方式 B：未安装 NSSM 时，脚本自动退化为「开机计划任务 + SYSTEM 账户」，同样无窗口常驻
powershell -ExecutionPolicy Bypass -File deploy\service\install_windows_service.ps1 -AppDir "C:\dbpulse"

# 卸载
powershell -ExecutionPolicy Bypass -File deploy\service\install_windows_service.ps1 -Uninstall
```

说明：

- 脚本会把 `DBPULSE_BIND_HOST=0.0.0.0`、巡检并发、监控并发等写入服务环境变量。
- 打包版（`dbcheck.exe`）同样适用：脚本会自动识别 `.venv\Scripts\python.exe` 或 `dbcheck.exe`。
- 服务日志：`data\logs\service.out.log` / `service.err.log`（NSSM 方式）。
- 请勿同时用「双击 exe」再跑一份：单实例守卫会拒绝第二个实例，服务看似启动失败。

---

## 四、启动后确认容量参数已生效

启动日志与 `/api/system/runtime-tuning` 会输出生效值（`modules/core/runtime_tuning.describe()`）：

```text
巡检并发=3(JVM 1)、排队上限=1800s、看门狗=1800s/宽限300s；
监控并发=8、间隔=15s、单实例超时=8s；大屏并发=8、间隔=15s
```

若数值与预期不符，检查优先级：环境变量（服务单元） > `data/runtime_tuning.json` > 代码默认值。

大规模部署的具体取值见 [超大实例规模配置](./SCALE_zh.md)。

> 日志轮转、保留期与磁盘预算见 [`LOG_LIFECYCLE_zh.md`](LOG_LIFECYCLE_zh.md)。
