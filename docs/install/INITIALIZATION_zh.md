# 初始化文档：启动方式与初始账号

本文档说明 DBPulse（RaccoonX）在 **Linux / Windows / macOS / Docker** 四种环境下的启动步骤，
以及首次安装后用于登录的**初始账号与角色**。

> 登录页已不再展示任何账号口令；初始账号只在本初始化文档中维护，便于部署方统一保管与首次登录后立即修改。

---

## 一、通用前置条件

| 项目 | 要求 |
| --- | --- |
| Python（源码/打包运行） | 3.10 及以上，推荐 3.12 |
| 端口 | 默认 `5003`，需可被浏览器访问 |
| 数据目录 | 源码运行：仓库根目录 `data/`；Docker：容器内 `/app/data`（务必挂载持久卷） |
| 数据库驱动 | 按需安装，详见 [环境要求速查](../../README_zh.md#-环境要求速查) |

访问入口统一为：

```text
http://<服务器地址>:5003
```

首次访问会自动跳转到登录页 `/um/login`。

---

## 二、Linux 启动

适用于 CentOS / RHEL / Ubuntu / 银河麒麟等发行版。

```bash
# 1. 获取源码
git clone https://github.com/zhongxinxuhk/DBCheck.git
cd DBCheck

# 2. 创建虚拟环境（推荐，避免污染系统 Python）
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r deploy/requirements.txt

# 4. 启动（自动安装依赖检查 + RBAC 种子初始化 + 启动 Web 服务）
bash deploy/start.sh

# 或直接启动 Web 服务（依赖与种子数据已就绪时）
python3 web_ui.py
```

要点：

- `deploy/start.sh` 会切换到仓库根目录执行，并在 `data/pro_data/.rbac_seeded` 不存在时自动执行
  `python -m user_management.seed` 播种角色与初始账号。
- 日志写入 `data/logs/web_service_restart.log`。
- 若系统 Python 受 PEP 668 保护，脚本会先尝试 `--break-system-packages` 安装依赖；生产环境仍建议使用虚拟环境。

免 Python 环境（打包版）：

```bash
bash build/build_linux.sh
cd dist
./dbcheck
```

---

## 三、Windows 启动

适用于 Windows 10 / 11 / Server 2016 及以上（x64）。

```bat
REM 1. 获取源码
git clone https://github.com/zhongxinxuhk/DBCheck.git
cd DBCheck

REM 2. 创建虚拟环境
py -3.12 -m venv .venv
.venv\Scripts\activate

REM 3. 安装依赖
pip install -r deploy\requirements.txt

REM 4. 启动
deploy\start.bat

REM 或直接启动 Web 服务
python web_ui.py
```

要点：

- `deploy\start.bat` 会自动切换到仓库根目录，首次运行自动安装依赖并执行 RBAC 种子初始化。
- 需保证 `python` 在 `PATH` 中（脚本启动时会校验）。
- 打包版：执行 `build\build_windows.bat`，在 `dist\` 下得到 `dbcheck.exe`，双击或命令行运行。
  启动后若出现控制台窗口，请勿关闭，否则服务会随之退出。

---

## 四、macOS 启动

适用于 macOS 12 (Monterey) 及以上，Intel 与 Apple Silicon 均可。

```bash
# 1. 获取源码
git clone https://github.com/zhongxinxuhk/DBCheck.git
cd DBCheck

# 2. 创建虚拟环境（若未安装 Python，可先 brew install python@3.12）
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r deploy/requirements.txt

# 4. 启动
bash deploy/start.sh

# 或直接启动 Web 服务
python3 web_ui.py
```

要点：

- 与 Linux 共用 `deploy/start.sh`，行为一致。
- 首次运行若被 Gatekeeper 拦截（打包版），可在「系统设置 → 隐私与安全性」中允许运行，
  或执行 `xattr -dr com.apple.quarantine dist/dbcheck`。
- 打包版：`bash build/build_macos.sh`，产物在 `dist/`。

---

## 五、Docker 启动

### 方式一：单条命令

```bash
# 两个镜像源，任选其一
docker pull ghcr.io/zhongxinxuhk/dbpulse:latest
# docker pull jackge12345/dbcheck:latest

docker run -d --name dbcheck \
  -p 5003:5003 \
  -v dbcheck_data:/app/data \
  -v dbcheck_reports:/app/data/reports \
  --restart unless-stopped \
  ghcr.io/zhongxinxuhk/dbpulse:latest
```

### 方式二：docker compose

```bash
curl -o deploy/docker-compose.yml \
  https://raw.githubusercontent.com/zhongxinxuhk/DBCheck/main/deploy/docker-compose.yml

docker compose -f deploy/docker-compose.yml up -d
```

### 说明

- **必须挂载数据卷。** 账号、数据源、巡检历史、密钥、报告均位于 `/app/data`；
  不挂载则容器重建后全部丢失。
- 容器启动时会自动建表并播种 RBAC 种子数据（幂等，不会清空已有用户与授权）。
- 查看容器日志与初始管理员口令：

```bash
docker logs -f dbcheck
docker exec dbcheck cat /app/data/bootstrap_admin_password
```

- 健康检查：`curl -f http://localhost:5003/api/v1/health`。
- 麒麟 / ARM64 服务器可直接拉取同一镜像（多架构，`linux/amd64` + `linux/arm64`）。

---

## 六、初始账号与角色信息

首次初始化（源码启动或容器首次启动）会创建以下账户：

| 账号 | 初始密码 | 角色 | 角色说明 | 可用菜单范围 |
| --- | --- | --- | --- | --- |
| `admin` | `admin123` | 管理员（`admin`） | 拥有所有权限 | 全部菜单 |
| `viewer` | `viewer123` | 只读用户（`viewer`） | 只能查看，不可修改 | 首页、报告、慢查询监控、AI 诊断 |
| `operator` | `operator123` | 运维人员（`operator`） | 可读写大部分运维功能 | 首页、巡检向导、慢查询监控、AWR、报告、SQL 编辑器、数据源、容灾备份 |

### 关于 `admin` 初始口令

当前版本对**管理员**采用安装期随机初始口令，避免出厂弱口令：

- 优先级 1：环境变量 `DBPULSE_INITIAL_ADMIN_PASSWORD`（长度须 ≥ 16 位）。
- 优先级 2：首次安装时随机生成并写入受限文件 `data/bootstrap_admin_password`（权限 `0600`），
  容器内对应 `/app/data/bootstrap_admin_password`。

因此：

- **新安装**请优先使用 `docker exec dbcheck cat /app/data/bootstrap_admin_password`
  （或查看源码部署下的 `data/bootstrap_admin_password`）获取 `admin` 的实际初始口令；
  此时上表中的 `admin123` 仅适用于历史数据卷 / 旧版本升级场景。
- **已有数据卷升级**不会被重置：种子逻辑为 `INSERT OR IGNORE`，已存在的 `admin` 账户及其密码保持不变。
- `viewer` / `operator` 为固定初始口令，仅用于功能演示与只读/运维分权验证。

用户与角色数据保存在 `data/user_db/um_rbac.db`（容器内 `/app/data/user_db/um_rbac.db`）。

---

## 七、首次登录后的安全加固

1. 使用 `admin` 登录后立即在「账户中心」修改密码；`viewer` / `operator` 建议改密或按需禁用。
2. 不需要演示账户的生产环境，登录后删除或禁用 `viewer` / `operator`。
3. 限制 5003 端口暴露范围，或置于反向代理 / HTTPS 之后。
4. 备份 `data/user_db/um_rbac.db` 与 `data/bootstrap_admin_password`，并保持后者权限为 `0600`。
5. Docker 部署请勿使用 `-v ./data:/app/data` 覆盖镜像内目录结构，改用命名卷。

---

## 八、常见启动问题

| 现象 | 排查方向 |
| --- | --- |
| 端口被占用 | 修改启动端口或结束占用 5003 的进程；打包版重复双击可能触发单实例守卫 |
| 页面 502 / 无法访问 | 确认进程存活、端口监听、防火墙放行 5003 |
| 登录提示账号或密码错误 | 源码部署核对 `data/bootstrap_admin_password`；旧数据卷沿用原密码 |
| 重启后账号消失 | 检查 `/app/data` 是否挂载为持久卷（Docker）或 `data/` 是否被覆盖（打包版升级） |
| 依赖安装失败 | 确认 Python ≥ 3.10，并使用与平台匹配的 `deploy/requirements.txt` |

---

## 相关文档

- [安装说明（INSTALL_zh.md）](./INSTALL_zh.md)
- [后台服务运行（SERVICE_zh.md）](./SERVICE_zh.md)
- [超大实例规模配置（SCALE_zh.md）](./SCALE_zh.md)
- [项目 README（中文）](../../README_zh.md)
- [安全策略](../SECURITY.md)
- [贡献指南](../CONTRIBUTING.md)
