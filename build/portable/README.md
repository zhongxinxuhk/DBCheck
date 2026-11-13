# DBPulse 统一便携包

统一包包含 Windows x86_64、Linux x86_64、Linux ARM64 的独立可执行文件，使用同一套资源、驱动与数据目录。客户机使用随包 Python 和 Java 运行时。

```text
DBPulse-Portable/
  start.bat
  start.sh
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

Windows 双击 `start.bat`，Linux 执行 `bash start.sh`。包应放在可写目录。Linux 二进制基于 glibc 2.28。

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

构建使用 Windows、Linux 对应的 PyInstaller spec。`DBPULSE_PORTABLE_BUILD=1` 选择共享资源布局并排除运行时用户数据。原生构建使用 Python 3.12 及锁定依赖。

```bash
python build/portable_release.py drivers
python build/portable_release.py native --platform linux-x86_64
python build/portable_bundle.py runtime --platform linux-x86_64 --cache .portable-build/downloads
python build/portable_bundle.py stage --output dist/DBPulse-Portable \
  --required-platforms linux-x86_64 --platform linux-x86_64 \
  --component .portable-build/native/linux-x86_64/DBPulse-Linux \
  --runtime /path/to/verified-java-archive \
  --receipt .portable-build/downloads/linux-x86_64-java.json
```

其余平台在对应系统及 CPU 架构的构建环境执行相同入口。可按所需平台组装目录并生成 ZIP。

```bash
python build/portable_bundle.py archive \
  --directory dist/DBPulse-Portable --output dist/DBPulse-Portable.zip
```

构建器会校验原生二进制格式及 CPU 架构、Java 下载 SHA-256、JDBC 驱动类，并拒绝把包含用户数据的目录作为干净发行包归档。手动工作流 `.github/workflows/portable-release.yml` 在各平台构建、检查后汇总为一个包。

运行期库来自 Python / PyInstaller 及项目依赖，Java 使用 Eclipse Temurin 11。下载来源和校验值记录在构建收据与包清单中。

DBCheck / RaccoonX © 2025–2026 fiyo (Jack Ge) · sdfiyon@gmail.com · https://github.com/fiyo/DBCheck

DBPulse 衍生创作 © 2025–2026 sec.hn.cn (X.U zx) · xuzhongxin@sec.hn.cn · https://github.com/zhongxinxuhk/DBCheck


## 自动发布与迭代详情

主发布入口为 `.github/workflows/build-release.yml`，推送到 `main` 后依次运行必需检查、分配版本、Windows x64、Linux x64 和 Linux ARM64 原生编译、组装安装包、Windows 配置跨平台迁移验证，最后上传资产并公开 Release。手动运行 `portable-release.yml` 可生成用于验证的构建产物。

发布说明依次包含迭代详情、完整变更记录链接、安装包平台表、安装与升级说明及发布验证说明。构建与发布使用同一源提交，发布任务获取完整 Git 历史及标签，以最近发布且属于本次提交历史的正式 Release 为迭代基线。草稿、预发布版本和其他分支上的发布版本均排除。

维护面向客户的说明时，在 `CHANGELOG.md` 中添加与发布版本完全匹配的章节，填写功能、影响和使用方式。推荐使用以下结构。

```markdown
## v1.0.2 (2026-11-13)

### 新增功能

- 写明本次新增功能及入口。

### 功能改动

- 写明操作、配置或兼容性变化。

### BUG 修复

- 写明修复的问题与受影响场景。

### 其他更新

- 写明文档、构建或依赖更新。
```

没有对应版本章节时，发布流从上次正式版本至本次提交自动汇总提交标题。`feat` 归入新增功能，`fix` / `bugfix` / `hotfix` / `revert` 归入 BUG 修复，`perf` / `refactor` / `style` 归入功能改动，其余进入其他更新，同时识别常见中文前缀。每条自动说明附原始提交链接，合并提交排除，同一范围内的分支提交纳入。首次发布汇总现有历史，超过 200 条时展示最近 200 条并链接完整记录。未发布章节保留为工作草稿，正式说明只读取匹配版本的内容。

发布前可在本地预览，`--base` 指定上一正式版本，省略时按首次发布汇总。

```bash
python build/release_pipeline.py notes --version v1.0.2 --sha HEAD \
  --base v1.0.1 --output /tmp/DBPulse-release-notes.md
```

正式发布时生成 `release-assets/release-notes.md`，同一正文用于 GitHub Release 和工作流运行摘要。发布说明在创建标签或上传资产之前生成，生成失败时可先排查并重试。
