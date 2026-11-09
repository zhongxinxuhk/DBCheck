# 业务专项巡检兼容性

业务专项 SQL 的权威目录为 `modules/inspection/business_templates.py`。初始化巡检配置库时为 Oracle 11g、SQL Server、DM8、GBase 8a 各播种 12 个专项模板：日巡检、周巡检、月度专项、月度健康、集群架构、故障取证、变更验证、恢复验证、新库验收、性能诊断、季度分析、年度台账。周巡检作为内置业务默认；已有用户指定的默认模板、阈值、启停状态保留。

原生/JDBC 别名可以选择同一数据库系列的模板及基线；精确类型优先。GBase 8a 和 GBase 8s 属于不同产品，不共享模板。显式选择的模板 ID 同时用于采集与报告渲染，避免专项报告意外回落到通用模板。

## 数据库与驱动

| 对象 | 接入与检查 |
|---|---|
| Oracle 11g | JDBC `ojdbc6` / `ojdbc8`，或兼容的 Oracle Thick Client；Thin 模式不覆盖 11g。JDBC 包装器实现连续读取的 `fetchmany`，保留 NUMBER 精度。 |
| SQL Server 2005 | 已准备 Microsoft JDBC 4.0；按实例指定驱动版本，仍需真实 Windows 2005 实例验收。 |
| SQL Server 2008 / 2014 | 已准备 Microsoft JDBC 4.2、6.4 及现有新版驱动；不要把最新版驱动视作覆盖所有旧版的证明。 |
| DM8 | 本地已有 DmJdbcDriver6/7/8/11；按 JVM 版本选择。查询使用 DM8 实际字段，作业系统未初始化或账号不可见时明确标为未采集。 |
| GBase 8a MPP | 原生 MySQL 协议；先 `USE VC` 选择虚拟集群，再选择普通 schema。专用 JDBC 驱动必须含 `com.gbase.jdbc.Driver`。错误的 8s JAR 不得登记/激活为 8a。 |

驱动下载属于本地运行环境准备。厂商 JAR 和测试环境连接信息不随本次代码提交发布。

- GBase 官方驱动：[gbase-connector-java-9.5.0.7-build1-bin.jar](https://cdn.gbase.cn/products/30/p5CiVwXBKQYIUGN8ecHvk/gbase-connector-java-9.5.0.7-build1-bin.jar)
- Microsoft 历史驱动：[官方 release notes](https://learn.microsoft.com/en-us/sql/connect/jdbc/release-notes-for-the-jdbc-driver)、[支持矩阵](https://learn.microsoft.com/en-us/sql/connect/jdbc/microsoft-jdbc-driver-for-sql-server-support-matrix)
- DM 作业初始化与权限：[官方作业环境文档](https://eco.dameng.com/document/dm/zh-cn/pm/create-job-environment.html)

## 集群证据与限制

GBase 默认周巡检及集群专项采集数据节点、协调节点、gcware/同步组件状态、资源池和逐节点存储配额。DM8 采集 DSC 节点、MAL 通信拓扑、守护组及归档配置和状态。配置为空时保留空结果，不据此声称集群健康。跨节点参数比较、分片一致性、心跳网、主机日志、故障切换和恢复演练仍需真实多节点环境的证据。

SQL Server 2005/2008 的 AlwaysOn 查询明确标记不适用。CHECKDB 成功时间无法取得时保留未知，禁止用完整备份时间替代。规则取数记录 `_metric_status`，模板查询记录 `_query_status`；缺权限、缺功能与执行失败不等价于通过。基线命中率仅计通过和不符，错误不计有效命中。

## 验证

```bash
python scripts/verify_database_compatibility.py
python scripts/audit_compatibility_drivers.py
python scripts/verify_service_management.py
python scripts/check_license_headers.py --fix
python scripts/check_license_headers.py
```

离线回归覆盖新库播种、重复初始化、保留用户配置、别名匹配、显式模板、JDBC 连续取数、只读语句校验、旧版功能限制、节点离线规则。真实旧版本与多节点集群验收必须另行执行，不能用兼容级别或单实例模拟替代。
