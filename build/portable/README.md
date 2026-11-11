# DBPulse 统一便携包

统一包包含 Windows x86_64、Linux x86_64、Linux ARM64、macOS ARM64、macOS Intel 的独立可执行文件，使用同一套资源、驱动与数据目录。客户机使用随包 Python 和 Java 运行时。

```text
DBPulse-Portable/
  start.bat
  start.sh
  start.command
  dbpulse-portable.json
  bin/<系统-架构>/dbcheck[.exe]
  runtime/<系统-架构>/jre/
  drivers/
  templates/
  plugins/
  modules/config/
  modules/pro/rules/
  data/
```

Windows 双击 `start.bat`，Linux 执行 `bash start.sh`，macOS 使用 `start.command` 或 `bash start.sh`。包应放在可写目录。Linux 二进制基于 glibc 2.28。

应用的账号、加密数据源、集群配置、巡检模板、基线、监控历史和定时任务均位于共享 `data/` 中。密码密钥保存在 `data/.db_key`。Windows 配置完成后，停止应用并复制整个便携目录到 Linux，启动脚本会选择对应二进制并读取原配置。随包文件链接已转为实际文件，Linux 脚本会恢复从 Windows 复制时丢失的执行权限。

保存位于目录外的 SSH 密钥或证书引用时，便携模式会将文件复制至 `data/portable_files/`，配置保存相对路径。数据目录随迁移携带这些文件。

## Windows ARM64 运行方式

Windows x64 二进制在 Windows 11 ARM64 上使用系统内置的 x64 仿真运行，启动入口同为 `start.bat`。随包 Python 扩展与 JVM 均使用 x64 架构，程序按进程编译架构选择运行时。Windows 11 24H2 的 Prism 提供了更新的 x64 仿真能力。

此兼容路径的最低系统为 Windows 11 ARM64。当前锁定依赖中的 NumPy 1.26.4 缺少 Windows ARM64 预编译包，发布的 Windows 可执行文件采用 AMD64 架构。Windows ARM64 实机验收待完成。

微软运行方式说明见 https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation 。

## 运行检查与旧版导入

```bat
start.bat --portable-self-test
start.bat --portable-state-check
start.bat --portable-import C:\旧版DBCheck
```

```bash
bash start.sh --portable-self-test
bash start.sh --portable-state-check
bash start.sh --portable-import /path/to/old-installation
```

`--portable-self-test` 检查打包模块、资源和随包 JVM。`--portable-state-check` 检查现有数据源密码可读性及本地密钥、证书引用，不输出密码。旧版导入需要尚未初始化的新 `data/` 目录，使用 SQLite backup 复制已提交的 WAL 内容并保留原 Fernet 密钥。原安装目录保留。

首次安装默认账号为 `admin`，默认密码为 `admin123`。Windows 默认显示运行状态窗口，关闭后保留在系统托盘，托盘菜单可以恢复、启动、停止和退出。`DBPULSE_PORT` 和 `DBPULSE_BIND_HOST` 可以覆盖当前进程的 Web 监听设置，默认使用 `127.0.0.1:5003`。

## 原生构建与统一组装

构建继续使用原仓库三个 PyInstaller spec。`DBPULSE_PORTABLE_BUILD=1` 选择共享资源布局并排除运行时用户数据。原生构建使用 Python 3.12 及锁定依赖。Intel macOS 使用支持其预编译 ABI 的独立依赖锁。

```bash
python build/portable_release.py drivers
python build/portable_release.py native --platform linux-x86_64
python build/portable_bundle.py runtime --platform linux-x86_64 --cache .portable-build/downloads
python build/portable_bundle.py stage --output dist/DBPulse-Portable \
  --platform linux-x86_64 \
  --component .portable-build/native/linux-x86_64/DBPulse-Linux \
  --runtime /path/to/verified-java-archive \
  --receipt .portable-build/downloads/linux-x86_64-java.json
```

其余平台在对应系统及 CPU 架构的构建环境执行相同入口。五个平台都组装后生成唯一 ZIP。

```bash
python build/portable_bundle.py archive \
  --directory dist/DBPulse-Portable --output dist/DBPulse-Portable.zip
```

构建器会校验原生二进制格式及 CPU 架构、Java 下载 SHA-256、JDBC 驱动类，并拒绝把包含用户数据的目录作为干净发行包归档。手动工作流 `.github/workflows/portable-release.yml` 在各平台构建、检查后汇总为一个包。

运行期库来自 Python / PyInstaller 及项目依赖，Java 使用 Eclipse Temurin 11。下载来源和校验值记录在构建收据与包清单中。

DBCheck / RaccoonX © 2025–2026 fiyo (Jack Ge) · sdfiyon@gmail.com · https://github.com/fiyo/DBCheck

DBPulse 衍生创作 © 2025–2026 sec.hn.cn (X.U zx) · xuzhongxin@sec.hn.cn · https://github.com/zhongxinxuhk/DBCheck
