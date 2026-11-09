# DBPulse 上游功能同步

DBPulse（数据库智能巡检系统）基于 RaccoonX / DBCheck。原作者为 fiyo (Jack Ge) <sdfiyon@gmail.com>，原仓库为 https://github.com/fiyo/DBCheck ，许可证为 Apache-2.0。

## 同步来源与时间

- 上游为 `fiyo/DBCheck` 的 `main` 分支。
- 目标为 `zhongxinxuhk/DBCheck` 的默认分支。
- `Sync upstream functionality` 每天北京时间 03:23 执行，也可在 Actions 页面手动运行。
- `.github/upstream-sync.json` 记录已检查的上游提交与每个待处理文件的原始合并基线。初始基线取自本次修改前的仓库 HEAD。
- 同步不导入上游标签。DBPulse 的版本号和发布标签由本仓库维护。

## 自动合并范围

`scripts/sync_upstream.py` 使用默认拒绝的文件白名单。当前允许模块和插件内的 Python 功能代码、`db/` 内 SQL、两份部署依赖清单，以及数据库类型和驱动注册表 JSON。

以下内容属于本仓库保护范围。

- `web_templates/`、`assets/`、静态文件、CSS、JavaScript、图片和报告模板。
- `i18n/`、统一品牌模块、版本元数据和用户界面菜单种子。
- `.github/` 下工作流和同步配置、`build/`、发布脚本、文档和运行时数据。
- 第三方 vendored 代码。

Python 文件的变更中出现 HTML、模板渲染、品牌或明确的样式标记时，整个文件进入待处理列表。文件路径白名单和这些检查无法完整判断所有代码的界面语义。新增后端功能若需要新的页面、翻译、报表模板或前端调用，需要单独集成并验收。

## 本仓库修改与冲突

功能文件使用“上次成功处理的上游版本、本仓库 HEAD、当前上游版本”进行三方合并。

- 非重叠功能修改自动合入，同时保留本仓库的修改。
- 同位置修改冲突、修改与删除冲突、不同内容的同时新增、符号链接、二进制内容或 Python 语法错误都会保留本仓库文件，并记录待处理原因。
- 待处理文件保存自己的旧基线。后续运行会继续重试，避免全局检查点前进后遗漏冲突。
- 上游改写历史、工作区存在未提交修改或校验失败时，运行失败。
- 每次运行在 Actions Summary 中列出已合并、受保护、待处理文件。待处理列表也保存在 `.github/upstream-sync.json`。
- 提交前执行许可证、品牌、Python 编译和差异检查。推送使用普通 fast-forward 检查，用户并发推送或分支保护会拒绝本次推送，工作流显示失败，可从最新默认分支重跑。

这套流程执行文本合并和静态验证。数据库驱动连接、业务行为及需要外部数据库的集成验收仍由相应运行环境完成。

同步校验使用 Python 3.12，与 Docker 运行时一致。原有维护脚本 `scripts/pro_merge_dbs.py` 使用 Python 3.12 的 f-string 语法，Python 3.11 全量编译会失败。

## 启用条件

将本次文件提交并推送到当前仓库默认分支后，在仓库 Actions 页面启用工作流。Fork 的计划任务可能需要手动启用。

同步工作流使用仓库自带的 `GITHUB_TOKEN`，声明 `contents: write`，无需新增个人访问令牌。组织策略和默认分支保护须允许相应机器人写入，否则任务会失败并保留日志。

使用 `GITHUB_TOKEN` 推送产生的 push 事件通常不会再次触发工作流，因此同步工作流自身在提交前完成验证。规则依据 https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow 。

## 构建与系统更新

继承上游的六个工作流，构建平台、触发方式和停用任务与上游对应。适配项包括 DBPulse 产物名、当前仓库链接和镜像归属，以及上游 YAML 中两个无效参数的修正。

- 系统“关于”中的系统更新入口指向 https://github.com/zhongxinxuhk/DBCheck/releases 。
- GHCR 镜像为 `ghcr.io/zhongxinxuhk/dbpulse`。首次发布后的包访问权限由仓库所有者配置。
- Docker Hub 可选，需同时配置仓库变量 `DOCKERHUB_IMAGE` 和 Secrets `DOCKERHUB_USERNAME`、`DOCKERHUB_TOKEN`。
- 推送本仓库 `v*` 标签时触发发布，也可手动运行构建工作流。计划同步只更新代码，不创建发布标签。
- 原上游停用的 Linux 二进制构建、Linux 补包和旧 Docker 工作流继续处于停用状态。多架构 Docker 工作流可构建 `linux/amd64` 与 `linux/arm64`。
- Python 包名、命令、数据库、环境变量和数据目录沿用兼容标识。Pro 授权标识、第三方驱动下载源和原作者归属链接保留各自用途。

## 本地验证与预览

```bash
python scripts/verify_dbpulse.py
python scripts/verify_upstream_sync.py
# 需要干净工作区。预览只输出计划，--apply 写入工作区，不提交。
git fetch --no-tags https://github.com/fiyo/DBCheck.git main:refs/remotes/upstream/main
python scripts/sync_upstream.py
python scripts/sync_upstream.py --apply
```
