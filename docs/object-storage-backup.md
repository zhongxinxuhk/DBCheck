# 对象存储备份（S3 兼容）

> 适用版本：v26.11.6.0 及以后
> 入口：Web UI →「数据管理」→「对象存储备份」

把 DBCheck / DBPulse 自身的**引擎与规则、基线、报告、历史、配置密钥、用户权限**
打包成单个 `tar.gz`，按设定间隔上传到 S3 兼容对象存储。目标既可以是 AWS S3，
也可以是自建网关（MinIO、Ceph RGW、OSCA 联盟云 fgws3 等）。

---

## 1. 为什么不是 boto3

本项目以 PyInstaller one-folder 分发。引入 `boto3` + `botocore` 会额外带入约 30MB
依赖与大量数据文件，而这里只需要「PUT 一个包、偶尔列一下、偶尔拉回来」，因此
`modules/objstore/s3_client.py` 用标准库实现了 **AWS Signature V4**：

| 能力 | 实现方式 |
| --- | --- |
| 签名 | `hmac` + `hashlib`，`AWS4-HMAC-SHA256` |
| 传输 | `urllib.request`（可设超时、TLS 校验开关、走系统代理） |
| 分片上传 | 超过 32MB 自动切 16MB 分片（`CreateMultipartUpload` / `UploadPart` / `Complete`） |
| 列目录 | `ListObjectsV2` + `continuation-token` 翻页 |
| 寻址 | Path-Style 与 Virtual-Hosted 两种（自建网关多半只支持前者） |

想用官方 SDK 复核？等价 boto3 写法：

```python
import boto3

s3 = boto3.client(
    "s3",
    endpoint_url="https://fgws3-ocloud.ihep.ac.cn",   # 自建网关必须给
    aws_access_key_id="<AK>",
    aws_secret_access_key="<SK>",
    region_name="us-east-1",
    config=boto3.session.Config(s3={"addressing_style": "path"}),  # Path-Style
)
s3.upload_file("dbpulse-backup-20261109-120000.tar.gz", "my-bucket",
               "dbpulse-backup/2026/11/dbpulse-backup-20261109-120000.tar.gz")
print(s3.list_objects_v2(Bucket="my-bucket", Prefix="dbpulse-backup/"))
```

本项目**不依赖** boto3；装不装都能用。

---

## 2. OSCA 联盟云（fgws3）接入步骤

1. 打开 <https://ocloud.ihep.ac.cn>，用单位统一认证登录并等待审核通过。
2. 左侧「秘钥管理」→ 获取 **AccessKey / SecretKey**（SK 只显示一次，请立即保存；
   建议同时指定访问范围与轮换日期）。
3. 左侧「网关地址」→ 取 Endpoint。不确定用哪个时，用公网网关：
   `https://fgws3-ocloud.ihep.ac.cn`。
4. 在 Web UI 填写：

   | 字段 | 值 |
   | --- | --- |
   | Endpoint | `https://fgws3-ocloud.ihep.ac.cn` |
   | Bucket | 控制台里创建的桶名（如 `20003-osca-test`） |
   | Region | `us-east-1`（网关不校验时可任意，但签名必须与填写值一致） |
   | AccessKey / SecretKey | 第 2 步获取 |
   | 对象前缀 | `dbpulse-backup`（默认） |
   | 路径风格 | **勾选**（自建网关通常不支持 virtual-hosted 风格） |
   | 校验 TLS | 默认勾选；内网自签名网关才关闭 |

5. 点「测试连接」——成功会返回「连接成功，桶可访问」。
6. 勾选备份内容 → 设置间隔与保留份数 → 勾「启用自动备份」→「保存配置」。

用官方 `jcli` 复核（等价于本模块的写入位置）：

```bash
jcli auth -ak <AK> -sk <SK> -bucket <BUCKET> -endpoint https://fgws3-ocloud.ihep.ac.cn
jcli list dbpulse-backup/            # 等价于「刷新云端列表」
jcli stat dbpulse-backup/2026/11/dbpulse-backup-20261109-120000.tar.gz
jcli get  dbpulse-backup/2026/11/dbpulse-backup-20261109-120000.tar.gz -o restore.tar.gz
```

用 AWS CLI 亦可（同为 S3 协议）：

```bash
aws --endpoint-url https://fgws3-ocloud.ihep.ac.cn s3 ls s3://<BUCKET>/dbpulse-backup/
```

---

## 3. AWS S3 / MinIO 配置差异

| | AWS S3 | MinIO / Ceph RGW | OSCA fgws3 |
| --- | --- | --- | --- |
| Endpoint | `https://s3.<region>.amazonaws.com` 或留空用默认 | `http(s)://<host>:9000` | `https://fgws3-ocloud.ihep.ac.cn` |
| Region | 必须与桶实际区域一致（如 `ap-southeast-2`） | 任意（`us-east-1` 常用） | `us-east-1` |
| 路径风格 | 可不勾（virtual-hosted） | **勾选** | **勾选** |
| 凭据来源 | IAM 用户的 Access Key | 控制台 Access Keys | 控制台秘钥管理 |

> 注意：**Region 一旦填错，签名就是错的**，网关通常回 `403 SignatureDoesNotMatch`，
> 而不是提示「区域不对」。排查时优先核对 Region 与系统时钟（偏差 > 15 分钟也会 403）。

---

## 4. 备份内容

| 类别 | 包含 | 说明 |
| --- | --- | --- |
| 引擎与规则 `engine` | `data/drivers.db`、`modules/pro/rules/overrides.yaml` | 驱动注册表、规则引擎自定义阈值/外观 |
| 基线 `baseline` | `data/fleet_baseline.db` | 集群基线（期望值）与比对结果 |
| 报告 `report` | `data/reports/` | Word / PDF / HTML 报告与分享页产物 |
| 历史 `history` | `history.db`、`server_history.db`、`inspection.db`、`monitor_history.db` | 巡检快照、服务器巡检、巡检任务、监控历史 |
| 配置与密钥 `config` | `dbc_config.json`、`.db_key`、`pro_data/{instances,groups,api_keys}.db` | 系统配置、密码加密密钥、数据源与 API Key |
| 用户与权限 `rbac` | `data/user_db/um_rbac.db` | RBAC 用户、角色、权限 |

- 文件清单在 `modules/objstore/engine.py` 的 `CATEGORIES` 单一维护，版本升级新增数据
  文件只需改这一处，前端勾选项自动跟随。
- 勾选项仅表示「要打包哪些」；本机不存在的项会在历史记录里标为 `missing`，不阻断备份。
- 包内 `manifest.json` 记录每个文件的 `sha256`、字节数、来源根标记（`data` / `project`）
  与类别，用于还原前完整性校验。

---

## 5. 间隔与保留

| 参数 | 说明 |
| --- | --- |
| `interval_minutes` | 备份间隔（分钟），最小 5，最大 30 天。语义是「距上次**执行**满 N 分钟即触发」，进程重启后下一次 tick 自动补跑。 |
| `retention` | 云端保留份数（1–365）。上传成功后按 `LastModified` 倒序，仅清理 `对象前缀/` 下超出的 `*.tar.gz`，**不会碰桶内其他前缀的对象**。 |
| `archive_password` | 压缩包密码。留空 = 不加密；设置后产物为 `.tar.gz.enc`（AES-256-GCM）。 |
| `enabled` | 总开关。关闭后调度线程跳过，但「立即备份」仍可手动执行。 |

调度由 `modules/objstore/scheduler.py` 的守护线程每 30 秒 tick 一次；`engine.run_backup`
持有非阻塞锁，定时与手动不会并发打包。

「立即备份」不走同步请求：报告目录可能到 GB 级，打包 + 上传耗时分钟级，浏览器或反向代理
会先超时（用户看到「失败」但对象其实已上传成功）。因此接口只负责在后台线程起任务并立即
返回 `job_id`，前端轮询 `/api/objstore/status` 的 `job` 字段拿最终结果。

---

## 6. 还原

UI「云端备份」列表 →「还原」：

- 默认**只补回本机缺失的文件**，不覆盖已有数据；
- 勾选「还原时覆盖已存在文件」后才会覆盖，覆盖前自动在同目录留
  `<文件名>.pre_restore_<时间戳>` 副本；
- 还原前逐文件校验 `sha256`，不匹配的文件跳过并在结果里列出；
- 解包路径做越界校验（拒绝 `../` 与绝对路径成员），并额外使用
  `filter="data"`（Python 3.12+）。

还原完成请**重启服务**：运行中的 SQLite 可能持有 WAL/锁，热覆盖存在不一致风险。

---

## 7. 安全边界

- SecretKey 永不出现在 API 响应里（返回 `••••••••` 掩码 + `secret_key_set` 标记）；
  前端留空即表示「保持原值」。
- 落盘加密：`cryptography` 可用时用 Fernet（密钥在 `dbc_config.json` →
  `object_storage._fernet_key`）；缺失时降级为 `_plain_` 并在 UI 显示告警。
  生产环境 `cryptography` 是硬依赖，正常不会走到降级分支。
- 桶建议保持私有；需要分享时用 `jcli token` / `aws s3 presign` 生成临时链接，不要开公共读。
- 只授予该桶 `前缀/` 下的读写权限，不要把整个账号的 AK 交给本功能。

---

## 8. 压缩包口令加密

界面上「压缩包密码」留空即不加密；填写后备份包变为 **AES-256-GCM 加密**，文件名为
`dbpulse-backup-<时间戳>.tar.gz.enc`。

### 8.1 容器格式

```
b"DBPULSE-ENC-v1\n"            魔数
{JSON 头部}\n                   v / cipher / kdf 参数 / salt / chunk / 原文件名
[nonce(12B) ‖ len(4B, 大端) ‖ ciphertext(明文+16B GCM tag)] × N
```

- 密钥派生：优先 **scrypt**（N=16384, r=8, p=1），OpenSSL 未编译 scrypt 时自动退到
  **PBKDF2-SHA256 600k 次**；实际算法与参数写在头部，解密端按头部复算，不靠猜。
- **分块认证**：每块独立 nonce 与 tag，1 MiB 一块流式处理——GB 级报告也不会整包进内存，
  中途被篡改或密码错误会在对应块立即失败，不会产出「看起来正常」的半截数据。
- 解出来第一层是标准 `tar.gz`，可以用本工具还原，也可以先解密再用任意解包工具打开。

### 8.2 它不是 zip 口令包

用 7-Zip / WinRAR 双击 `.enc` **打不开**，这是刻意的：标准库 `zipfile` 只能写出早已被
击穿的 ZipCrypto，而要写 AES zip 得额外引入 `pyzipper` 并且必须整包进内存。需要脱离
Web 解密时用内置命令行：

```bash
python -m modules.objstore.crypto info    backup.tar.gz.enc     # 看头部（算法/KDF/原文件名）
python -m modules.objstore.crypto decrypt backup.tar.gz.enc backup.tar.gz
python -m modules.objstore.crypto encrypt backup.tar.gz backup.tar.gz.enc
# 口令交互输入；脚本里可用 -p
```

### 8.3 密码的保存与清除

- 定时备份要用密码，所以密码必须存在本地：与 SecretKey 同一套 Fernet 加密
  （`dbc_config.json → object_storage.archive_password`），API 只回 `••••••••` 掩码。
- 表单留空保存 = **保持原密码**（避免只想改间隔却把密码清掉）；要关闭加密请点
  「清除压缩包密码」，已上传的加密包仍需原密码才能还原。
- **密码丢失不可恢复**：没有后门、没有恢复码。请在密码管理器里留一份。

### 8.4 还原

- 列表里 `*.enc` 对象 → 「还原」时会要求密码：优先用界面上的「还原密码」，
  留空则回落到已保存的压缩包密码；两者都没有会直接提示「该备份包已加密」。
- 密码错误返回明确错误（GCM 校验失败），不会误覆盖本地文件。

---

## 9. REST 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/objstore/config` | 读取配置（SK 掩码） |
| POST | `/api/objstore/config` | 保存配置 |
| GET | `/api/objstore/categories` | 备份内容编目（含存在性与大小） |
| POST | `/api/objstore/test` | 测试连通性（可带表单值覆盖，不落盘） |
| POST | `/api/objstore/backup` | 触发备份 `{contents?: []}`，**异步**：返回 `job_id`，前端轮询 `/status` |
| GET | `/api/objstore/objects` | 云端备份列表（受前缀限制） |
| POST | `/api/objstore/restore` | 还原 `{key, overwrite?, password?}` |
| POST | `/api/objstore/delete` | 删除云端对象 `{key}` |
| GET | `/api/objstore/history` | 执行历史 |
| GET | `/api/objstore/status` | 调度状态与下次执行时间 |

**错误语义**（排查时先看状态码）：

| 状态码 | 含义 | 响应特征 |
| --- | --- | --- |
| 200 | 成功 | `{"ok": true, ...}` |
| 400 | 可预期错误：参数缺失、配置不合法、凭据/连接失败、加密包缺密码 | `{"ok": false, "error": "<可直接展示的中文原因>"}` |
| 409 | 已有备份任务在执行 | `{"ok": false, "message": "已有备份任务在执行…"}` |
| 500 | **未预期错误**（编码缺陷：NameError / AttributeError / TypeError） | `{"ok": false, "unexpected": true, "error": "NameError: …"}`，同时服务端日志有完整 traceback |

> 500 刻意保留 `异常类型: 消息`：早先所有接口都 `except Exception` 把 `NameError: name 'store'
> is not defined` 吞成普通业务错误、日志无 traceback，只能靠用户报错定位。现在可预期错误回
> 400（`logger.warning`），未预期错误回 500 并 `logger.exception` 记完整堆栈。

---

## 10. 故障排查

| Symptom | 常见原因与处理 |
| --- | --- |
| `403 SignatureDoesNotMatch` | Region 与桶不一致；系统时钟偏差 > 15 分钟；Path-Style 开关与服务端要求相反 |
| `403 AccessDenied` | AK 没有该桶权限，或桶策略只允许特定前缀 |
| `404 NoSuchBucket` | 桶名拼错 / 桶在别的 Endpoint 上（OSCA 同一桶在每个 Endpoint 都可读，但名字必须一致） |
| `SSL: CERTIFICATE_VERIFY_FAILED` | 自建网关用自签名证书：关闭「校验 TLS」或导入 CA |
| 连接超时 | 出网需代理：`HTTPS_PROXY` 环境变量会被继承；或 Endpoint 用了内网地址 |
| 上传中断 | 大包会自动分片；跨境链路建议把 `timeout` 调大（默认 120s） |
| 列表为空但桶里有对象 | 对象不在「对象前缀/」下——前缀必须与上传时一致 |
| `密码错误，或备份包已损坏` | 口令不对，或对象传输中被改写（GCM 校验失败）。先确认密码，必要时 `crypto info` 看头部是否完好 |
| 保存配置报「缺少 cryptography」 | 该环境未安装 cryptography，无法启用压缩包密码；装上或关闭密码 |

---

## 11. 自测

```bash
python -m unittest discover -s modules/objstore/tests -t . -v
```

覆盖：SigV4 头形状与规范化请求、Path-Style / Virtual-Hosted 寻址、与假 S3 网关的
PUT/GET/HEAD/LIST/DELETE 往返、打包 manifest 与 sha256、上传+保留策略、还原
（缺失补齐 / 强制覆盖 / `.pre_restore` 备份）、路径穿越防护、配置校验与 SK 掩码。
