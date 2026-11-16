# 本地数据存储加密

## 目标与边界

目标是把**凭据类字段**从本地文件中移走：用文本编辑器、`sqlite3` 或 `cat`
直接打开运行时文件时，不应看到数据库口令、JDBC 连接串中的口令、AI Key、
BIC-QA Key、MCP Token 或 Webhook Token。

系统运行期间仍允许在内存中使用明文：连接数据库、调用 AI、发送通知时由
读取方透明解密，巡检、监控、报告生成的调用方式不变。

本方案是**字段级加密**，不是 SQLite 全库加密：

- 已加密：数据库口令、SSH 口令/私钥口令、`jdbc_url`、AI/BIC-QA/MCP/Webhook
  敏感配置、SMTP 口令、对象存储 SecretKey、备份包口令。
- 未加密：数据源名称、主机、端口、库名、分组、标签、巡检结果、报告正文等
  业务数据。它们由 `data/` 目录的 0700 权限和文件 0600 权限保护。
- 需要抗“拥有整机 root/管理员权限并复制走整个 data 目录”的场景时，应叠加
  操作系统全盘加密（BitLocker / FileVault / LUKS）和独立密钥备份。

## 字段与格式

| 文件/字段 | 落盘格式 | 读取方 |
| --- | --- | --- |
| `pro_data/instances.db` → `password` / `ssh_password` / `ssh_key_password` | `base64(Fernet token)` | `InstanceManager.get_instance_decrypted()` 等 |
| `pro_data/instances.db` → `jdbc_url` | `jdbc:enc:v1:` + `base64(Fernet token)` | 启动时透明解密到内存；监控/巡检/插件拿到的仍是明文 URL |
| `dbc_config.json` → `ai.api_key` / `ai.online_api_key` | `enc:v1:` + `base64(Fernet token)` | `local_secrets.load_config()` |
| `dbc_config.json` → `bicqa.api_key` | 同上 | BIC-QA 面板、专家、MCP bridge |
| `dbc_config.json` → `mcp_server.token` / `auth_token` | 同上 | MCP 配置读取方 |
| `dbc_config.json` → `notification.webhook.url/secret/token` | 同上 | `modules.notify` |
| `dbc_config.json` → `notification.email.password` | `_enc_`（通知模块既有格式） | `modules.notify` |
| `dbc_config.json` → `object_storage.secret_key` / `archive_password` | `_enc_`（对象存储既有格式） | `modules.objstore` |
| `scheduler_jobs.json` → `db_info.password` / `ssh_password` / `ssh_key_password` / `jdbc_url` | `enc:v1:` + `base64(Fernet token)` | 调度器启动时透明解密 |
| `pro_data/api_keys.db` | 只存 `key_hash` / `key_prefix` | API Token 校验 |
| `user_db/um_rbac.db` | 用户口令 bcrypt | 登录鉴权 |
| `data/backups/*.tar.gz.enc` | AES-256-GCM 分块加密 | 备份还原 |

`jdbc_url` 使用独立前缀，是为了兼容历史明文行：读到没有前缀的值按明文处理，
下一次启动/保存时自动转为密文；前缀值解密失败则返回空串，绝不把密文当 URL
交给 JDBC 驱动。

## 密钥管理

- 所有字段加密共用 `data/.db_key`（Fernet，32 字节随机密钥）。
- `.db_key`、数据库、JSON 配置在 POSIX 下分别为 0600；`data/` 及其子目录为
  0700。Windows 不使用 POSIX mode，依赖 NTFS ACL 与全盘加密。
- 密钥缺失时：写入路径拒绝明文降级；读取路径不会自动生成新密钥，避免把
  “密钥丢失”伪装成“解密失败”。配置字段解密失败时保留原密文（不返回明文、
  也不清空），避免一次无关保存把密钥永久覆盖；已有加密凭据但密钥缺失时启动
  会报错，提示恢复原密钥。
- 迁移/备份必须把 `data/.db_key` 与数据一起备份；只恢复数据库而不恢复密钥，
  历史口令将无法解密。反过来，复制整个 `data/`（含 `.db_key`）的人仍可用
  本程序解密，因此整个目录应按最高敏感级别保管。

## 启动迁移

Web 启动时执行一次幂等迁移：

1. `notify.migrate_plaintext_credentials()`：把历史明文/`_b64_` 邮件口令转为
   `_enc_`。
2. `local_secrets.migrate_config()`：把 AI/BIC-QA/MCP/Webhook 的明文值转为
   `enc:v1:`。
3. `SchedulerManager` 初始化时迁移 `scheduler_jobs.json` 中的历史明文
   `db_info` 凭据；`InstanceManager` 初始化时扫描 `instances.db`，把仍为明文的
   `jdbc_url` 原地加密。
4. `paths.harden_runtime_permissions()`：收紧 `data/`、数据库、JSON、密钥和
   日志权限。

迁移失败只告警、不阻断启动；失败时原文件保持不变，不会写入半截配置。配置
文件损坏时，写入路径使用 `strict=True` 拒绝覆盖，避免把其他节点清空。

## 兼容与回滚注意

- 旧版本代码不认识 `enc:v1:` / `jdbc:enc:v1:`，回滚到旧版本后会把密文当成
  普通字符串使用，表现为 AI/通知不可用或 JDBC 连接串无效。回滚前应先导出
  配置并保留 `.db_key`，或回滚到支持该格式的版本。
- 数据源导出（`export_json`）在 `include_password=False` 时会同时剥离
  `jdbc_url`，避免导出文件成为旁路明文凭据；换机迁移需重新录入 JDBC URL。
- 该方案不改变巡检 SQL、报告字段和数据库驱动调用；巡检读取的是内存明文。
