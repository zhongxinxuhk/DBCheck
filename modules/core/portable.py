# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Portable startup, bundled JVM discovery and lossless installation data import."""

import importlib
import json
import os
import platform
import shutil
import sqlite3
import sys
import sysconfig
from pathlib import Path


def platform_key():
    system = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(
        sys.platform, sys.platform
    )
    machine = platform.machine().lower()
    if sys.platform == "win32":
        # An emulated x64 process must load x64 JNI libraries on an ARM64 host.
        machine = sysconfig.get_platform().removeprefix("win-").lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
        machine, machine
    )
    return system + "-" + arch


def bundled_java_home():
    from modules.core import paths

    if paths.PORTABLE_ROOT is None:
        return None
    root = paths.PORTABLE_ROOT / "runtime" / platform_key() / "jre"
    executable = "java.exe" if sys.platform == "win32" else "java"
    for candidate in (root, root / "Contents/Home"):
        if (candidate / "bin" / executable).is_file():
            return candidate
    return None


def self_test():
    from modules.core import paths

    required = (
        "flask",
        "flask_socketio",
        "gevent",
        "jinja2",
        "cryptography",
        "bcrypt",
        "jwt",
        "paramiko",
        "docx",
        "docxtpl",
        "openpyxl",
        "requests",
        "yaml",
        "apscheduler",
        "pymysql",
        "pytds",
        "jaydebeapi",
        "jpype",
        "docxcompose.composer",
        "babel.numbers",
        "croniter",
        "numpy",
        "pandas",
        "pymongo",
        "redis",
        "oracledb",
        "psycopg2",
        "tzdata",
        "psutil",
        "six",
    )
    errors = {}
    for name in required:
        try:
            importlib.import_module(name)
        except Exception as error:
            errors[name] = str(error)
    java = bundled_java_home()
    jvm = None
    if java is None:
        errors["bundled_java"] = "当前平台缺少随包 Java 运行时"
    else:
        try:
            import jpype

            if not jpype.isJVMStarted():
                jpype.startJVM("-Djava.awt.headless=true")
            jvm = str(jpype.JClass("java.lang.System").getProperty("java.version"))
            jpype.shutdownJVM()
        except Exception as error:
            errors["jvm"] = str(error)
    resources = (
        "web_templates",
        "assets/web",
        "db",
        "plugins",
        "drivers",
        "modules/config",
        "modules/pro/rules",
    )
    for resource in resources:
        if not (paths.PROJECT_ROOT / resource).is_dir():
            errors["resource:" + resource] = "缺少共享资源目录"
    internal = getattr(sys, "_MEIPASS", None)
    if internal is not None:
        for resource in ("i18n", "modules/config", "modules/user_management"):
            if not (Path(internal) / resource).is_dir():
                errors["internal_resource:" + resource] = "缺少包内模块资源"
    return {
        "ok": not errors,
        "platform": platform_key(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "data_root": str(paths.DATA_ROOT),
        "resources_root": str(paths.PROJECT_ROOT),
        "bundled_java": str(java) if java else None,
        "jvm_version": jvm,
        "db_key_present": paths.DB_KEY_PATH.is_file(),
        "python_version": platform.python_version(),
        "errors": errors,
    }


def import_installation(source, target):
    """Copy old installation state and its Fernet key into a fresh portable directory.

    SQLite backup consolidates committed WAL contents. The source is opened
    read-only and the destination is published only after every copy succeeds.
    """
    source = Path(source).expanduser().resolve()
    target = Path(target).expanduser().resolve()
    if (
        source == target
        or target.is_relative_to(source)
        or source.is_relative_to(target)
    ):
        raise ValueError("迁移源与目标目录必须相互独立")
    candidates = [source / "data", source / "_internal/data"]
    data = next(
        (path for path in candidates if path.is_dir() and any(path.rglob("*.db"))), None
    )
    if data is None:
        raise ValueError("原安装目录没有可迁移的 SQLite 数据")
    destination = target / "data"
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("目标数据目录已有内容，请选择尚未初始化的便携目录")
    key = next(
        (
            path
            for path in (
                data / ".db_key",
                source / ".db_key",
                source / "_internal/.db_key",
            )
            if path.is_file()
        ),
        None,
    )
    if key is None:
        raise ValueError("原安装缺少 data/.db_key 或旧版 .db_key，无法保留加密账号")
    from cryptography.fernet import Fernet

    Fernet(key.read_bytes().strip())
    staging = target / ".portable-import-tmp"
    if staging.exists():
        raise ValueError("存在未完成的迁移暂存目录，请先复核该目录")
    staging.mkdir(parents=True, mode=0o700)
    files = 0
    databases = 0
    try:
        for current in data.rglob("*"):
            if current.is_symlink():
                raise ValueError(
                    "迁移数据包含符号链接，请先将其转为安装目录内的实际文件"
                )
            if not current.is_file() or current.name.endswith(
                ("-wal", "-shm", ".lock")
            ):
                continue
            relative = current.relative_to(data)
            output = staging / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            if current.suffix == ".db":
                reader = sqlite3.connect(current.as_uri() + "?mode=ro", uri=True)
                writer = sqlite3.connect(output)
                try:
                    reader.backup(writer)
                finally:
                    writer.close()
                    reader.close()
                databases += 1
            else:
                shutil.copy2(current, output)
            files += 1
        shutil.copy2(key, staging / ".db_key")
        instance_db = staging / "pro_data/instances.db"
        if instance_db.is_file():
            with sqlite3.connect(instance_db) as database:
                columns = {
                    row[1] for row in database.execute("PRAGMA table_info(instances)")
                }
                for field in ("ssh_key_file", "tls_ca_file", "tls_cert_key_file"):
                    if field not in columns:
                        continue
                    for iid, value in database.execute(
                        "SELECT id," + field + " FROM instances"
                    ).fetchall():
                        if not value:
                            continue
                        original = Path(value).expanduser()
                        if not original.is_absolute():
                            original = source / original
                        if original.exists():
                            relative = preserve_file_reference(
                                str(original), field, state_dir=staging
                            )
                            database.execute(
                                "UPDATE instances SET " + field + "=? WHERE id=?",
                                (relative, iid),
                            )
        if (
            not (staging / "dbc_config.json").exists()
            and (source / "dbc_config.json").is_file()
        ):
            shutil.copy2(source / "dbc_config.json", staging / "dbc_config.json")
        # Drivers and user-edited rule files are architecture-independent resources.
        driver_source = next(
            (
                path
                for path in (source / "drivers", source / "_internal/drivers")
                if path.is_dir()
            ),
            None,
        )
        if driver_source:
            shutil.copytree(driver_source, target / "drivers", dirs_exist_ok=True)
        for resource in (
            "modules/pro/rules/custom",
            "modules/pro/rules/overrides.yaml",
        ):
            original = source / resource
            if not original.exists():
                original = source / "_internal" / resource
            if original.is_dir():
                shutil.copytree(original, target / resource, dirs_exist_ok=True)
            elif original.is_file():
                (target / resource).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target / resource)
        if destination.exists():
            destination.rmdir()
        staging.replace(destination)
        return {
            "ok": True,
            "files": files,
            "databases": databases,
            "credentials_key_preserved": True,
        }
    except Exception:
        # Keep a failed staging copy for review and never overwrite the source.
        raise


def state_check():
    """Verify copied encrypted datasource state without printing credentials."""
    from modules.core import paths
    from modules.pro import get_instance_manager

    failures = []
    manager = get_instance_manager()
    sources = manager.get_all_instances(mask_password=True)
    for source in sources:
        try:
            value = manager.get_instance_decrypted(source["id"])
            for field in ("ssh_key_file", "tls_ca_file", "tls_cert_key_file"):
                if value.get(field) and not Path(value[field]).exists():
                    failures.append(
                        {
                            "instance_id": source["id"],
                            "field": field,
                            "error": "配置引用文件缺失",
                        }
                    )
        except Exception:
            failures.append(
                {
                    "instance_id": source["id"],
                    "error": "加密配置无法读取，请核对原 data/.db_key",
                }
            )
    users = 0
    user_db = paths.USER_DB_DIR / "um_rbac.db"
    if user_db.is_file():
        with sqlite3.connect(user_db) as database:
            users = database.execute("SELECT COUNT(*) FROM um_user").fetchone()[0]
    return {
        "ok": not failures,
        "platform": platform_key(),
        "datasource_count": len(sources),
        "user_count": users,
        "credentials_readable": not failures,
        "errors": failures,
    }


def transfer_test(seed=False):
    """Exercise real saved credentials, users and portable key references in CI."""
    import tempfile
    from modules.core import paths

    if paths.PORTABLE_ROOT is None:
        raise ValueError("Transfer verification requires a portable bundle")
    if seed and paths.DATA_DIR.exists() and any(paths.DATA_DIR.iterdir()):
        raise ValueError("Transfer fixture requires an empty data directory")
    from modules.pro.instance_manager import InstanceManager, DatabaseInstance
    from modules.user_management.models.db_manager import DBManager
    from modules.user_management.seed import init_seed_data
    from modules.user_management.utils.password import hash_password, verify_password

    marker = paths.DATA_DIR / "ci-transfer-fixture.json"
    expected = {
        "password": "CI-portable-密码-2026",
        "ssh_password": "CI-SSH-password",
        "ssh_key_password": "CI-key-passphrase",
    }
    manager_directory = str(paths.PRO_DATA_DIR)
    if seed:
        manager = InstanceManager(manager_directory)
        with tempfile.TemporaryDirectory(prefix="dbpulse-ci-key-") as temporary:
            key = Path(temporary) / "fixture.pem"
            key.write_text("CI fixture key content", encoding="utf-8")
            instance = DatabaseInstance(
                id="ci-portable-fixture",
                name="CI migration fixture",
                db_type="mysql",
                host="127.0.0.1",
                port=1,
                user="ci-fixture",
                password=expected["password"],
                ssh_password=expected["ssh_password"],
                ssh_key_password=expected["ssh_key_password"],
                ssh_key_file=str(key),
            )
            result = manager.add_instance(instance)
            if not result.get("ok"):
                raise RuntimeError("Unable to save the CI datasource fixture")
        init_seed_data()
        DBManager().execute(
            "UPDATE um_user SET password=? WHERE username='admin'",
            (hash_password("CI-migrated-admin"),),
        )
        marker.write_text(
            json.dumps(
                {
                    "fixture": "dbpulse-portable-transfer-v1",
                    "source_platform": platform_key(),
                }
            ),
            encoding="utf-8",
        )
    if (
        not marker.is_file()
        or json.loads(marker.read_text(encoding="utf-8")).get("fixture")
        != "dbpulse-portable-transfer-v1"
    ):
        raise ValueError("Portable transfer fixture is missing")
    manager = InstanceManager(manager_directory)
    instance = manager.get_instance_decrypted("ci-portable-fixture")
    credentials = bool(instance) and all(
        instance.get(key) == value for key, value in expected.items()
    )
    key_file = Path(instance.get("ssh_key_file", "")) if instance else None
    reference = bool(
        key_file
        and key_file.is_file()
        and key_file.read_text(encoding="utf-8") == "CI fixture key content"
    )
    account = DBManager().query_one(
        "SELECT password FROM um_user WHERE username='admin'"
    )
    account_ok = bool(
        account and verify_password("CI-migrated-admin", account["password"])
    )
    return {
        "ok": credentials and reference and account_ok,
        "platform": platform_key(),
        "source_platform": json.loads(marker.read_text(encoding="utf-8"))[
            "source_platform"
        ],
        "credentials_preserved": credentials,
        "key_reference_preserved": reference,
        "account_password_preserved": account_ok,
    }


def bootstrap():
    arguments = sys.argv[1:]
    if "--portable-root" in arguments:
        index = arguments.index("--portable-root")
        if index + 1 >= len(arguments):
            raise SystemExit("--portable-root requires a directory")
        os.environ["DBPULSE_PORTABLE_HOME"] = str(
            Path(arguments[index + 1]).expanduser().resolve()
        )
    from modules.core import paths

    if paths.PORTABLE_ROOT is not None:
        if (
            paths.PORTABLE_ROOT / ".portable-import-tmp"
        ).exists() and "--portable-import" not in arguments:
            raise SystemExit("便携数据迁移尚未完成，请先复核 .portable-import-tmp")
        os.environ["DBPULSE_PORTABLE_HOME"] = str(paths.PORTABLE_ROOT)
        os.chdir(paths.PORTABLE_ROOT)
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        if os.name != "nt":
            if paths.DATA_DIR.is_dir():
                paths.DATA_DIR.chmod(0o700)
            if paths.DB_KEY_PATH.is_file():
                paths.DB_KEY_PATH.chmod(0o600)
        java = bundled_java_home()
        if java is not None:
            os.environ["JAVA_HOME"] = str(java)
            os.environ["PATH"] = (
                str(java / "bin") + os.pathsep + os.environ.get("PATH", "")
            )
        elif getattr(sys, "frozen", False) and "--portable-self-test" not in arguments:
            raise SystemExit(
                "便携包缺少 " + platform_key() + " Java 运行时，请核对完整包内容"
            )
    if "--portable-self-test" in arguments:
        result = self_test()
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result["ok"] else 1)
    if (
        "--portable-transfer-seed" in arguments
        or "--portable-transfer-check" in arguments
    ):
        result = transfer_test(seed="--portable-transfer-seed" in arguments)
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result["ok"] else 1)
    if "--portable-state-check" in arguments:
        result = state_check()
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(0 if result["ok"] else 1)
    if "--portable-import" in arguments:
        index = arguments.index("--portable-import")
        if paths.PORTABLE_ROOT is None or index + 1 >= len(arguments):
            raise SystemExit("请在便携目录内指定 --portable-import 原安装目录")
        try:
            print(
                json.dumps(
                    import_installation(arguments[index + 1], paths.PORTABLE_ROOT),
                    ensure_ascii=False,
                )
            )
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            raise SystemExit(1)
        raise SystemExit(0)


def open_portable_connection(instance):
    """Use the shipped JDBC stack for OS-independent Oracle/DM/SQL Server access."""
    from modules.driver_registry import db_family
    from modules.jdbc_connector import open_jdbc_connection
    from modules.ssh import resolve_db_endpoint

    family = db_family(instance.get("db_type"))
    profile = {"oracle": "oracle_jdbc", "sqlserver": "sqlserver_jdbc", "dm": "dm"}.get(
        family
    )
    if profile is None:
        raise ValueError("此数据库未配置便携 JDBC 接入")
    host = instance.get("host") or instance.get("ip")
    port = instance.get("port")
    if not instance.get("_tunnel_ready"):
        host, port, tunnel = resolve_db_endpoint(instance, log=lambda *args: None)
    else:
        tunnel = None
    url = str(instance.get("jdbc_url") or "").strip()
    if url.startswith("(") and family == "oracle":
        url = "jdbc:oracle:thin:@" + url
    if tunnel and url:
        raise ValueError("SSH 便携接入请使用主机、端口、库名或服务名配置")
    connection, metadata = open_jdbc_connection(
        profile,
        host,
        int(port),
        instance.get("user") or "",
        instance.get("password") or "",
        database=instance.get("database"),
        service_name=instance.get("service_name") or instance.get("sid"),
        driver_version=instance.get("driver_version") or "",
        use_sid=bool(instance.get("use_sid")),
        jdbc_url=url or None,
        encrypt=bool(instance.get("encrypt", False)),
        trust_server_certificate=bool(instance.get("trust_server_certificate", False)),
        properties=(
            {"internal_logon": "sysdba"}
            if family == "oracle" and instance.get("sysdba")
            else None
        ),
    )
    if connection is None:
        raise ValueError(metadata.get("error") or "便携 JDBC 连接失败")
    return connection


def preserve_file_reference(value, field, state_dir=None):
    """Bring a selected SSH/certificate file into shared portable application state."""
    from modules.core import paths

    if not value or paths.PORTABLE_ROOT is None:
        return value
    source = Path(value).expanduser()
    if not source.is_absolute():
        source = paths.PORTABLE_ROOT / source
    source = source.resolve()
    if not source.exists():
        raise ValueError("配置文件不存在 " + str(value))
    if source.is_relative_to(paths.PORTABLE_ROOT):
        return source.relative_to(paths.PORTABLE_ROOT).as_posix()
    if field == "ssh_key_file" and not source.is_file():
        raise ValueError("SSH 密钥须为文件")
    import hashlib

    files = (
        [source]
        if source.is_file()
        else sorted(path for path in source.rglob("*") if path.is_file())
    )
    if (
        len(files) > 4096
        or sum(path.stat().st_size for path in files) > 16 * 1024 * 1024
    ):
        raise ValueError("密钥或证书目录超过便携导入大小限制")
    checksum = hashlib.sha256()
    for path in files:
        checksum.update(
            str(path.relative_to(source) if path != source else path.name).encode(
                "utf-8"
            )
        )
        checksum.update(path.read_bytes())
    relative = Path("portable_files") / checksum.hexdigest()[:24] / source.name
    data = Path(state_dir) if state_dir is not None else paths.DATA_DIR
    destination = data / relative
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not destination.exists():
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=False)
        else:
            shutil.copy2(source, destination)
    for path in (
        [destination, *destination.rglob("*")]
        if destination.is_dir()
        else [destination]
    ):
        if path.is_file():
            path.chmod(0o600)
    return (Path("data") / relative).as_posix()
