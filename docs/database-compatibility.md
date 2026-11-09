<!-- SPDX-License-Identifier: Apache-2.0
Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck
SPDX-License-Identifier: Apache-2.0
Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck -->

# 数据库类型与旧版本适配

数据库接入沿用既有类型注册表、数据源表单、巡检任务、指标模板与报告渲染器。GBase 8a 的类型标识为 `gbase8a`，与 `gbase`（GBase 8s）分开；不增加独立的数据库管理页面。

| 数据库 | 原有系统中的位置 | 连接与版本处理 | 指标 |
|---|---|---|---|
| GBase 8a MPP Cluster | 数据库类型、数据源管理、数据库巡检、数据库驱动管理 | PyMySQL 协议；默认端口 5258；服务端身份核对；验证证书的 TLS 或明确配置的 SSH 隧道 | 8a 专用模板 14 项，包括集群空间、节点任务、连接、参数、对象、索引、统计信息 |
| DM8 | 原有 DM8 类型与 JDBC/dmPython 入口 | 保留原有连接策略；受控证据采集补充独立进程 JDBC 与 SSH 隧道 | 继续使用 DM8 预置指标；元数据证据与报告覆盖共用指标目录 |
| Oracle 11g | 原有 Oracle / Oracle JDBC 类型 | 原生 11g 需要兼容的 Thick 客户端；11.1 与 11.2 分开验证；不把 23c 客户端默认当作 11g 兼容客户端 | 沿用 Oracle 11g 专用模板 |
| SQL Server 2005 | 原有 SQL Server 类型 | 显式 SSH 隧道内使用 TDS；认证后读取产品版本 | 2005 专用模板 17 项 |
| SQL Server 2008 | 原有 SQL Server 类型 | 验证证书的 ODBC 18 或显式 SSH 隧道；认证后读取产品版本 | 2008 专用模板 17 项 |
| SQL Server 2014 | 原有 SQL Server 类型 | 验证证书的 ODBC 18 或显式 SSH 隧道；认证后读取产品版本 | 2014 专用模板 17 项；内存指标使用 `PAGES_KB` |

SQL Server 通过服务端版本自动选择专用模板。用户显式选择的模板仍然优先。模板初始化采用增量添加；重复执行不会替换已有查询和用户阈值。采集失败或权限不足必须保留实际状态，不能以 0 或正常替代。

GBase 8a 原生连接不依赖 JDBC JAR。JAR 就绪检查核对实际驱动类；目录名或登记名不能证明驱动适配。8s 的 `com.gbasedbt.jdbc.Driver` 不可代替 8a 的 `com.gbase.jdbc.Driver`。

SSH 隧道使用系统 `known_hosts` 验证数据库主机；远端数据库连接限定为该主机的回环地址。不能以跳板机到数据库的未加密网络连接代替上述边界。未配置可信主机密钥、CA 或兼容客户端时给出阻断原因，不自动关闭证书校验。

## 数据外发与运行环境

数据库巡检不因需求目录存在而切换全局“客户模式”。身份认证、资源归属、CSRF、凭据加密、只读查询、结果限量、报告脱敏分别作用在对应环节。

客户数据相关的 AI、向量化、BIC-QA、Webhook 和邮件通道默认仅允许回环服务；外部 HTTPS 主机由部署管理员通过 `DBPULSE_ALLOWED_EGRESS_HOSTS` 明确列出。邮件同样要求允许 SMTP 主机并验证 TLS。数据发送不跟随重定向。网络代理仍需验证最终服务端证书。备份第三方通知通过第一方适配层应用同样控制，不修改第三方许可证。

登录后直接进入系统，在原用户中心自行改密；不强制跳转口令设置页。不存在通过需求目录禁用原有巡检入口的行为。

## 验证边界

`python scripts/verify_service_management.py` 使用隔离目录与模拟连接，不连接实际数据库。检查类型注册、指标初始化幂等、SQL Server 版本选择、只读规则、传输参数、租户权限、加密、报告覆盖和登录入口等。组件可导入、JAR 类可加载、接口模拟回归、实库兼容验收是不同证据，驱动页面分别显示组件状态与连接待验证状态。

待实库提供后逐一核对补丁号、证书、授权视图、节点角色、查询返回字段、集群范围及报告证据。极端损坏恢复能力需要实际工具、合法授权和隔离恢复验证；登记工具名称或哈希不代表已具备恢复能力。

官方参考：[Oracle 驱动兼容说明](https://oracle.github.io/python-oracledb/index.html)、[DM Python 接口参数](https://eco.dameng.com/document/dm/zh-cn/pm/dmpython-interface.html)、[GBase 8a 应用接入说明](https://www.gbase.cn/community/post/9704)、[Microsoft ODBC 兼容说明](https://learn.microsoft.com/en-us/sql/connect/odbc/windows/system-requirements-installation-and-driver-files)。

回归测试默认读取 `scripts/fixtures/service_catalog.json` 的合成数据，不读取客户目录。客户合同回归需在本地显式设置 `DBPULSE_TEST_CATALOG` 指向私有目录。CI 使用 Python 3.12 和 `scripts/requirements-validation.txt` 固定验证依赖；正常推送与自动上游同步均执行服务及数据安全回归。
