# 专项巡检报告：数据库核心属性与补丁读取

专项业务巡检报告在**封面之后、正文章节之前**新增「数据库基础信息（核心属性）」表，
固定 16 项字段，逐项给出取值与来源。补丁级别不再只停留在版本号，而是读取数据库
真实补丁记录（Oracle `dba_registry_history`、SQL Server `ProductUpdateLevel` 等）。

## 一、16 项字段与来源

| # | 字段 | 自动来源 | 读不到时 |
|---|---|---|---|
| 1 | 配置项编号 | 资产台账镜像 `data/basic_info.json` | 未登记 |
| 2 | 数据库名称 | 库内查询（`v$database` / `current_database()` / `DB_NAME()` 等） | 未采集 |
| 3 | 实例名 | 库内查询（`v$instance` / `@@hostname` / `SERVERPROPERTY`） | 未采集 |
| 4 | 字符集 | `nls_database_parameters` / `character_set_server` / `server_encoding` / Collation | 未采集 |
| 5 | 数据库文件大小 | 数据文件+临时文件求和 / `information_schema` / `pg_database_size` / `sys.database_files` | 未采集 |
| 6 | 归档路径 | `v$archive_dest` / binlog basename / `archive_command` / DM `V$DM_ARCH_INI` / 备份介质 | 未采集 |
| 7 | 归档模式 | `log_mode` / `log_bin` / `archive_mode` / 恢复模式 / DM 归档类型 | 未采集 |
| 8 | 主机型号 | SSH 采集 DMI `product_name`（`dmidecode` 兜底） | 未登记 |
| 9 | 设备位置 | 资产台账镜像 | 未登记 |
| 10 | OS 版本 | SSH `/etc/os-release` 的 `PRETTY_NAME`（未配 SSH 时用本机） | 未登记 |
| 11 | 存储平台 | 资产台账镜像 | 未登记 |
| 12 | 数据库版本及补丁号 | 库内版本 + 补丁记录（见第二节） | 未采集 |
| 13 | 存储模式 | ASM/文件系统自动判定（Oracle/DM）；其余按数据目录 | 未登记 |
| 14 | 备份策略 | Oracle `v$rman_configuration`；其余以台账为准 | 未登记 |
| 15 | 容灾状态 | Data Guard / 主从 / 流复制 / AlwaysOn / DSC 实测 | 未登记 |
| 16 | 集群属性 | `gv$instance` / `pg_stat_replication` / MGR / AlwaysOn / `V$DSC_EP_INFO` | 未登记 |

「未采集」= 数据库侧确实没读到（权限不足、版本不支持、视图不存在）；
「未登记」= 属于台账静态信息，库内本就没有。两者都不臆造。

## 二、补丁读取口径

| 库型 | 版本 | 补丁 |
|---|---|---|
| Oracle | `v$instance.version_full`（回退 `version`、`v$version`） | `dba_registry_history` 最近 5 条：`version` + `comments` + `id` |
| MySQL / MariaDB | `VERSION()` | 版本串后缀即补丁级别 |
| TiDB | `TIDB_VERSION()` | 同左 |
| OceanBase | `@@version` + `@@version_comment` | — |
| PostgreSQL 族 | `SHOW server_version` | 小版本号即补丁级别 |
| SQL Server | `ProductVersion` | `ProductUpdateLevel`（CUxx）+ `ProductLevel` + Edition |
| DM8 | `V$VERSION` | 构建号随版本串输出 |
| DB2 | `ENV_GET_INST_INFO().SERVICE_LEVEL` | 同左 |

Oracle 结果形如：

```
19.19.0.0.0；补丁：19.19.0.0.231017 Database Release Update (ID 34086851)
```

如果只有版本、补丁视图不可读，报告会明确写
`11.2.0.4.0（补丁级别未读取到）`，不会把版本号冒充补丁号。

权限要求（最小集）：

- Oracle：`SELECT` on `v$instance`、`v$database`、`v$version`、`v$archive_dest`、
  `dba_data_files`、`dba_temp_files`、`nls_database_parameters`、`v$rman_configuration`、
  `dba_registry_history`（无 `DBA` 角色时通常不可读 `dba_registry_history`，会降级为「未读取到」）。
- SQL Server：`VIEW SERVER STATE`（AlwaysOn DMV）、`msdb` 读权限（备份介质）。
- PostgreSQL：`pg_read_all_stats`（`pg_stat_replication`）。
- MySQL：`PROCESS`（`SHOW SLAVE STATUS`）、`performance_schema` 读权限（MGR）。

## 三、静态登记项：`data/basic_info.json`

设备位置、存储平台、配置项编号等无法从数据库读取，用台账镜像补齐：

```json
{
  "defaults": {
    "storage_platform": "华为 OceanStor 5310"
  },
  "instances": {
    "CI-2026-0001": {
      "configuration_id": "CI-2026-0001",
      "location": "A 机房 3 排 R12 机柜",
      "host_model": "Huawei 2288H V5",
      "os_version": "Kylin Linux Advanced Server V10 SP3",
      "storage_platform": "华为 OceanStor 5310",
      "storage_mode": "本地 SSD + 集中存储",
      "backup_policy": "每日 01:00 全备 + 每小时归档，保留 30 天",
      "dr_status": "同城灾备（ADG），RPO ≤ 5 分钟",
      "cluster_attrs": "Oracle RAC 2 节点 + ADG 备库"
    },
    "10.0.0.8:1521": {
      "configuration_id": "CI-2026-0002",
      "location": "B 机房"
    }
  }
}
```

匹配顺序（命中第一个即停止）：

1. 实例主键（巡检上下文 `_instance_id`）
2. `host:port`
3. `host`
4. 实例名（`instance_name`）

优先级：**实例级覆盖 > defaults > 自动采集**。即台账登记值与库内实测冲突时以台账为准，
便于按合同口径出表；如需以实测为准，删掉对应 key 即可。

修改后无需重启：按文件 mtime 自动重载。

## 四、报告中的位置与呈现

- 位置：封面 → **数据库基础信息（核心属性）** → 第 1 章起正文。
- 形式：两列 16 行表格（属性 / 取值），标签浅蓝底、9.5pt，长文本自动换行。
- 表下有一行来源说明，避免把「未采集」误读为「不存在」。
- 中英文报告都输出（英文标签见 `basic_info.LABELS_EN`）。

涉及代码：

- `modules/inspection/basic_info.py`：探测、合并、字段顺序（单一真源）
- `modules/inspection/engine.py`：巡检查询阶段采集（复用同一游标）、系统信息后合并、报告渲染
- 宿主机型号：`RemoteSystemInfoCollector` / `LocalSystemInfoCollector`

## 五、验证

```bash
python -m unittest modules.inspection.tests.test_basic_info -v
```

覆盖：Oracle 版本+补丁+ASM+RAC、MySQL binlog+主从、PG 流复制、SQL Server CU+AlwaysOn、
DM8 归档+DMDSC、全探测失败容错、16 字段缺省语义、台账覆盖优先级、仅有版本号时的补丁缺失标注。

## 六、已知限制

- 未配置 SSH 时，「主机型号 / OS 版本」取巡检工具所在主机，报告中会与目标库主机不一致；
  生产环境请为实例配置「查询服务器（SSH）」。
- 备份策略因备份工具（NBU / 爱数 / 云备份）不在库内，默认以台账为准；
  Oracle 仅在配置了 RMAN 持久参数时给出自动值。
- 资产台账（CMDB）与 `basic_info.json` 目前是镜像关系，不自动同步；CMDB 变更后需同步该文件。
- 单实例多库（如 SQL Server 多 database）的「库大小」按当前连接库统计。
