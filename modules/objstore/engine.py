# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""对象存储备份引擎：内容编目 → 打包 → 上传 → 保留策略 → 还原。

设计要点
--------
* **内容按类别编排**（引擎 / 基线 / 报告 / 历史 / 配置 / 权限），前端只勾类别，
  具体文件清单由 ``CATEGORIES`` 单一维护，升级新增数据文件只改这一处；
* 备份产物是**单个 tar.gz**，内含 ``manifest.json``（逐文件 sha256 + 来源根标记），
  这样异地还原时不必依赖原机目录结构，也能校验完整性；
* 还原一律「先备份再覆盖」，被覆盖的文件留 ``.pre_restore_<时间戳>`` 副本；
* 远端保留份数由 ``retention`` 控制，只清理本模块前缀下的对象，绝不碰桶内其他数据。
"""



from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from modules.core import paths
from modules.objstore import store
from modules.objstore.s3_client import S3Error

logger = logging.getLogger(__name__)

FORMAT_TAG = "dbpulse-objstore-backup"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
WORK_DIR = paths.DATA_DIR / "objstore"
STAGING_DIR = WORK_DIR / "staging"
HISTORY_PATH = WORK_DIR / "history.json"
MAX_HISTORY = 200

_RUN_LOCK = threading.Lock()
_HISTORY_LOCK = threading.RLock()

# 根标记 → 实际目录（data 走可写数据根，project 走只读资源根，打包版两者不同）
_ROOT_DIRS: Dict[str, Path] = {
    "data": paths.DATA_DIR,
    "project": paths.PROJECT_ROOT,
}


def _data(*parts: str) -> Path:
    return paths.DATA_DIR.joinpath(*parts)


def _proj(*parts: str) -> Path:
    return paths.PROJECT_ROOT.joinpath(*parts)


# ── 备份内容编目 ──────────────────────────────────────────────────────
CATEGORIES: List[Dict[str, Any]] = [
    {
        "id": "engine",
        "label": "引擎与规则",
        "label_key": "objstore.cat_engine",
        "desc": "驱动注册表、规则引擎自定义规则（外观/阈值覆盖）",
        "items": [
            {"label": "驱动与引擎注册表", "path": _data("drivers.db"), "root": "data"},
            {"label": "规则引擎自定义规则", "path": _proj("modules", "pro", "rules", "overrides.yaml"),
             "root": "project"},
        ],
    },
    {
        "id": "baseline",
        "label": "基线",
        "label_key": "objstore.cat_baseline",
        "desc": "集群基线（期望值）与基线比对结果库",
        "items": [
            {"label": "基线库", "path": _data("fleet_baseline.db"), "root": "data"},
        ],
    },
    {
        "id": "report",
        "label": "报告",
        "label_key": "objstore.cat_report",
        "desc": "巡检报告（Word / PDF / HTML 与分享页产物）",
        "items": [
            {"label": "报告目录", "path": paths.REPORTS_DIR, "root": "data"},
        ],
    },
    {
        "id": "history",
        "label": "历史",
        "label_key": "objstore.cat_history",
        "desc": "巡检快照、服务器巡检、巡检任务与监控历史",
        "items": [
            {"label": "巡检快照历史", "path": _data("history.db"), "root": "data"},
            {"label": "服务器巡检历史", "path": _data("server_history.db"), "root": "data"},
            {"label": "巡检配置与记录", "path": _data("inspection.db"), "root": "data"},
            {"label": "监控历史", "path": _data("monitor_history.db"), "root": "data"},
        ],
    },
    {
        "id": "config",
        "label": "配置与密钥",
        "label_key": "objstore.cat_config",
        "desc": "系统配置、密码加密密钥、数据源与 API Key",
        "items": [
            {"label": "系统配置", "path": paths.DBC_CONFIG_PATH, "root": "data"},
            {"label": "密码加密密钥", "path": paths.DB_KEY_PATH, "root": "data"},
            {"label": "数据源", "path": _data("pro_data", "instances.db"), "root": "data"},
            {"label": "数据源分组", "path": _data("pro_data", "groups.db"), "root": "data"},
            {"label": "API Key", "path": _data("pro_data", "api_keys.db"), "root": "data"},
        ],
    },
    {
        "id": "rbac",
        "label": "用户与权限",
        "label_key": "objstore.cat_rbac",
        "desc": "RBAC 用户、角色与权限库",
        "items": [
            {"label": "RBAC 库", "path": _data("user_db", "um_rbac.db"), "root": "data"},
        ],
    },
]

CATEGORY_IDS: Tuple[str, ...] = tuple(c["id"] for c in CATEGORIES)
store.ALLOWED_CONTENT_IDS = list(CATEGORY_IDS)
_CATEGORY_BY_ID = {c["id"]: c for c in CATEGORIES}


def _file_size_fmt(size: int) -> str:
    size = int(size or 0)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.2f} GB"


def _dir_entries(path: Path) -> Iterable[Path]:
    for root, _dirs, files in os.walk(str(path)):
        for name in files:
            yield Path(root) / name


def _path_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for item in _dir_entries(path):
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def catalog() -> List[Dict[str, Any]]:
    """返回可备份内容编目（含每项是否存在、大小），供前端渲染勾选列表。"""
    out: List[Dict[str, Any]] = []
    for cat in CATEGORIES:
        items = []
        total = 0
        for item in cat["items"]:
            path: Path = item["path"]
            exists = path.exists()
            size = _path_size(path) if exists else 0
            total += size
            items.append({
                "label": item["label"],
                "path": item["path"].name if path.is_dir() else str(path),
                "display": _display_path(path),
                "exists": exists,
                "size": size,
                "size_fmt": _file_size_fmt(size),
            })
        out.append({
            "id": cat["id"],
            "label": cat["label"],
            "label_key": cat["label_key"],
            "desc": cat["desc"],
            "items": items,
            "total_size": total,
            "total_size_fmt": _file_size_fmt(total),
            "available": any(i["exists"] for i in items),
        })
    return out


def _display_path(path: Path) -> str:
    for root in (paths.DATA_DIR, paths.PROJECT_ROOT):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            continue
    return str(path)


def _normalize_contents(contents: Optional[Iterable[str]]) -> List[str]:
    if not contents:
        return []
    out: List[str] = []
    for cid in contents:
        cid = str(cid).strip()
        if cid in _CATEGORY_BY_ID and cid not in out:
            out.append(cid)
    return out


def _collect_files(contents: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """展开类别 → 具体文件清单（含 arcname / root / rel）。"""
    entries: List[Dict[str, Any]] = []
    missing: List[str] = []
    for cid in contents:
        cat = _CATEGORY_BY_ID[cid]
        for item in cat["items"]:
            path: Path = item["path"]
            root = item["root"]
            base = _ROOT_DIRS[root]
            if not path.exists():
                missing.append(_display_path(path))
                continue
            files = [path] if path.is_file() else list(_dir_entries(path))
            for file_path in files:
                try:
                    rel = file_path.relative_to(base).as_posix()
                except ValueError:
                    rel = file_path.name
                entries.append({
                    "abs": file_path,
                    "arcname": f"{root}/{rel}",
                    "root": root,
                    "rel": rel,
                    "category": cid,
                    "label": item["label"],
                })
    return entries, missing


class _HashingReader:
    """边读边算 sha256，避免为了写 manifest 再读一遍大文件。"""

    def __init__(self, fh) -> None:
        self._fh = fh
        self.hasher = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self.hasher.update(chunk)
            self.size += len(chunk)
        return chunk


def _app_version() -> str:
    try:
        with open(str(paths.VERSION_JSON), "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("version", "")) or "unknown"
    except Exception:
        return "unknown"


def build_archive(contents: List[str], dest_dir: Optional[Path] = None) -> Dict[str, Any]:
    """把选定内容打包为 tar.gz，返回包信息（路径、大小、清单、缺失项）。"""
    contents = _normalize_contents(contents)
    if not contents:
        raise ValueError("未选择任何备份内容")
    entries, missing = _collect_files(contents)
    if not entries:
        raise ValueError("选定的备份内容在本机均不存在，无法打包")

    dest_dir = dest_dir or STAGING_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"dbpulse-backup-{stamp}.tar.gz"
    archive_path = dest_dir / filename

    manifest_files: List[Dict[str, Any]] = []
    total = 0
    started = time.time()
    with tarfile.open(str(archive_path), "w:gz") as tar:
        for entry in entries:
            abs_path: Path = entry["abs"]
            try:
                stat = abs_path.stat()
            except OSError:
                missing.append(_display_path(abs_path))
                continue
            info = tarfile.TarInfo(entry["arcname"])
            info.size = stat.st_size
            info.mtime = int(stat.st_mtime)
            info.mode = int(stat.st_mode & 0o777) or 0o644
            with open(str(abs_path), "rb") as fh:
                reader = _HashingReader(fh)
                tar.addfile(info, reader)
            manifest_files.append({
                "arcname": entry["arcname"],
                "root": entry["root"],
                "rel": entry["rel"],
                "category": entry["category"],
                "label": entry["label"],
                "size": reader.size,
                "sha256": reader.hasher.hexdigest(),
            })
            total += reader.size

        manifest = {
            "format": FORMAT_TAG,
            "version": FORMAT_VERSION,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "app_version": _app_version(),
            "host": _hostname(),
            "categories": contents,
            "files": manifest_files,
            "total_size": total,
            "missing": missing,
        }
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(payload)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(payload))

    disk_size = archive_path.stat().st_size
    return {
        "path": archive_path,
        "filename": filename,
        "size": disk_size,
        "size_fmt": _file_size_fmt(disk_size),
        "raw_size": total,
        "raw_size_fmt": _file_size_fmt(total),
        "file_count": len(manifest_files),
        "categories": contents,
        "missing": missing,
        "duration_s": round(time.time() - started, 2),
        "manifest": manifest,
    }


def _hostname() -> str:
    try:
        import socket

        return socket.gethostname()
    except Exception:
        return ""


def _maybe_encrypt(archive: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按配置给备份包加口令；返回的 dict 就地改写为 .enc 产物。"""
    from modules.objstore import crypto

    password = str(cfg.get("archive_password") or "").strip()
    if not password:
        archive["encrypted"] = False
        return archive
    if not crypto.available():
        raise ValueError("当前环境缺少 cryptography，无法生成带密码的备份包")

    src = Path(archive["path"])
    enc_path = Path(str(src) + ".enc")
    meta = crypto.encrypt_file(str(src), str(enc_path), password, name=archive["filename"])
    try:
        src.unlink()
    except OSError:
        pass
    size = enc_path.stat().st_size
    archive.update({
        "path": enc_path,
        "filename": f"{archive['filename']}.enc",
        "size": size,
        "size_fmt": _file_size_fmt(size),
        "encrypted": True,
        "encrypted_meta": {k: v for k, v in meta.items() if k != "salt"},
    })
    return archive


def object_key(cfg: Dict[str, Any], filename: str) -> str:
    now = datetime.now()
    prefix = (cfg.get("prefix") or store.DEFAULT_PREFIX).strip("/")
    return f"{prefix}/{now:%Y}/{now:%m}/{filename}"


# ── 历史记录 ──────────────────────────────────────────────────────────
def list_history(limit: int = 50) -> List[Dict[str, Any]]:
    with _HISTORY_LOCK:
        if not HISTORY_PATH.exists():
            return []
        try:
            with open(str(HISTORY_PATH), "r", encoding="utf-8") as fh:
                data = json.load(fh) or []
        except Exception:
            return []
    if not isinstance(data, list):
        return []
    return list(reversed(data))[: max(1, int(limit or 50))]


def _append_history(entry: Dict[str, Any]) -> None:
    with _HISTORY_LOCK:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: List[Dict[str, Any]] = []
        if HISTORY_PATH.exists():
            try:
                with open(str(HISTORY_PATH), "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, list):
                    data = loaded
            except Exception:
                data = []
        data.append(entry)
        if len(data) > MAX_HISTORY:
            data = data[-MAX_HISTORY:]
        tmp = f"{HISTORY_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(HISTORY_PATH))


# ── 执行备份 ──────────────────────────────────────────────────────────
def run_backup(trigger: str = "manual",
               contents: Optional[List[str]] = None) -> Dict[str, Any]:
    """打包并上传；返回执行结果。失败时同样落历史，再抛出异常给调用方。"""
    cfg = store.load_config(mask=False)
    if not cfg.get("endpoint") or not cfg.get("bucket"):
        raise ValueError("对象存储未配置完整（Endpoint / Bucket）")
    if not cfg.get("access_key") or not cfg.get("secret_key"):
        raise ValueError("对象存储凭据未配置（AccessKey / SecretKey）")

    selected = _normalize_contents(contents) or _normalize_contents(cfg.get("contents"))
    if not selected:
        raise ValueError("未选择任何备份内容")

    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("已有备份任务在执行，请稍后再试")
    started = time.time()
    archive: Optional[Dict[str, Any]] = None
    try:
        archive = build_archive(selected)
        _maybe_encrypt(archive, cfg)
        client = store.build_client(cfg)
        key = object_key(cfg, archive["filename"])
        upload = client.put_file(key, str(archive["path"]))

        pruned = prune_remote(cfg, client, keep=int(cfg.get("retention") or 7))

        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger,
            "status": "success",
            "message": "上传成功",
            "object_key": key,
            "filename": archive["filename"],
            "size": archive["size"],
            "size_fmt": archive["size_fmt"],
            "file_count": archive["file_count"],
            "categories": selected,
            "encrypted": bool(archive.get("encrypted")),
            "merged_missing": archive["missing"],
            "duration_s": round(time.time() - started, 2),
            "pruned": pruned,
        }
        _append_history(entry)
        store.record_run("success", f"已上传 {key}", key)
        return {"ok": True, "object_key": key, "upload": upload,
                "archive": {k: v for k, v in archive.items() if k != "manifest"},
                "pruned": pruned, "duration_s": entry["duration_s"]}
    except Exception as exc:
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger,
            "status": "failed",
            "message": str(exc)[:400],
            "object_key": "",
            "filename": archive["filename"] if archive else "",
            "size": archive["size"] if archive else 0,
            "size_fmt": archive["size_fmt"] if archive else "0 B",
            "file_count": archive["file_count"] if archive else 0,
            "categories": selected,
            "encrypted": bool(archive and archive.get("encrypted")),
            "duration_s": round(time.time() - started, 2),
        }
        _append_history(entry)
        store.record_run("failed", str(exc)[:400])
        raise
    finally:
        _RUN_LOCK.release()
        if archive and archive.get("path"):
            try:
                Path(archive["path"]).unlink(missing_ok=True)
            except Exception:
                pass


# ── 远端对象 ──────────────────────────────────────────────────────────
def list_remote(limit: int = 200) -> List[Dict[str, Any]]:
    cfg = store.load_config(mask=False)
    client = store.build_client(cfg)
    prefix = (cfg.get("prefix") or store.DEFAULT_PREFIX).strip("/") + "/"
    objects = client.list_objects(prefix=prefix, limit=max(1, limit))
    out = []
    for obj in sorted(objects, key=lambda o: o.get("last_modified", ""), reverse=True):
        out.append({
            "key": obj["key"],
            "name": obj["key"].rsplit("/", 1)[-1],
            "size": obj["size"],
            "size_fmt": _file_size_fmt(obj["size"]),
            "etag": obj.get("etag", ""),
            "last_modified": _fmt_time(obj.get("last_modified", "")),
        })
    return out


def _fmt_time(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return value


def prune_remote(cfg: Dict[str, Any], client, keep: int = 7) -> List[str]:
    """只清理本模块前缀下超出保留份数的对象。"""
    keep = max(1, int(keep or 1))
    prefix = (cfg.get("prefix") or store.DEFAULT_PREFIX).strip("/") + "/"
    try:
        objects = client.list_objects(prefix=prefix, limit=1000)
    except Exception:
        logger.warning("保留策略跳过：列出 %s 失败", prefix, exc_info=True)
        return []
    objects = [o for o in objects if o.get("key", "").endswith(".tar.gz")]
    if len(objects) <= keep:
        return []
    objects.sort(key=lambda o: o.get("last_modified", ""), reverse=True)
    removed = []
    for obj in objects[keep:]:
        try:
            client.delete_object(obj["key"])
            removed.append(obj["key"])
        except Exception:
            logger.warning("保留策略：删除旧对象 %s 失败", obj.get("key"), exc_info=True)
            continue
    return removed


def delete_remote(key: str) -> None:
    cfg = store.load_config(mask=False)
    prefix = (cfg.get("prefix") or store.DEFAULT_PREFIX).strip("/") + "/"
    if not key or not key.startswith(prefix):
        raise ValueError("只能删除本模块备份前缀下的对象")
    client = store.build_client(cfg)
    client.delete_object(key)


def test_connection(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """测试连通性；payload 里 SecretKey 为掩码时回落到已保存凭据。"""
    current = store.load_config(mask=False)
    merged = dict(current)
    if payload:
        for key in ("endpoint", "bucket", "region", "access_key", "path_style",
                    "verify_tls", "timeout"):
            if key in payload and payload[key] not in (None, ""):
                merged[key] = payload[key]
        secret = payload.get("secret_key")
        if secret and secret != store.MASK:
            merged["secret_key"] = secret
    merged = store.validate(merged, current)
    try:
        client = store.build_client(merged)
        result = client.bucket_status()
    except (ValueError, RuntimeError, S3Error) as exc:
        # 配置/凭据/网络问题属于可预期失败，回 ok:false 让前端就地提示
        return {"ok": False, "message": str(exc)}
    result["endpoint"] = merged.get("endpoint")
    result["bucket"] = merged.get("bucket")
    return result


# ── 还原 ──────────────────────────────────────────────────────────────
def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    dest_real = os.path.realpath(str(dest))
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(dest_real, member.name))
        if not (target == dest_real or target.startswith(dest_real + os.sep)):
            raise ValueError(f"压缩包内存在越界路径，已中止：{member.name}")
    try:
        # Python 3.12+ 支持 filter='data'，可再挡一层绝对路径/特殊文件
        tar.extractall(str(dest), filter="data")
    except TypeError:  # pragma: no cover - 3.11 及更早
        tar.extractall(str(dest))


def restore_from_object(key: str, overwrite: bool = False,
                        password: Optional[str] = None) -> Dict[str, Any]:
    """从对象存储下载并还原；overwrite=False 时只还原本机缺失的文件。

    加密包（``*.tar.gz.enc``）需要口令：优先用调用方传入的 ``password``，
    否则回落到配置里保存的压缩包密码。
    """
    cfg = store.load_config(mask=False)
    client = store.build_client(cfg)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = key.rsplit("/", 1)[-1] or f"restore-{stamp}.tar.gz"
    local_archive = STAGING_DIR / f"restore-{stamp}-{filename}"
    extract_dir = STAGING_DIR / f"extract-{stamp}"

    download = client.download(key, str(local_archive))
    restored: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    pre_backups: List[str] = []
    work_archive = local_archive
    decrypted_path: Optional[Path] = None
    try:
        from modules.objstore import crypto

        if crypto.is_encrypted(str(local_archive)):
            pwd = str(password or cfg.get("archive_password") or "").strip()
            if not pwd:
                raise ValueError("该备份包已加密，请填写压缩包密码")
            decrypted_path = Path(str(local_archive) + ".plain.tar.gz")
            crypto.decrypt_file(str(local_archive), str(decrypted_path), pwd)
            work_archive = decrypted_path

        with tarfile.open(str(work_archive), "r:gz") as tar:
            _safe_extract(tar, extract_dir)

        manifest_path = extract_dir / MANIFEST_NAME
        if not manifest_path.exists():
            raise ValueError("备份包缺少 manifest.json，非本模块产物")
        with open(str(manifest_path), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        files = manifest.get("files") or []

        for item in files:
            src = extract_dir / item.get("arcname", "")
            if not src.exists():
                failed.append(f"{item.get('arcname')}: 包内缺失")
                continue
            expect = item.get("sha256")
            if expect and _sha256_file(src) != expect:
                failed.append(f"{item.get('arcname')}: 校验和不匹配")
                continue
            base = _ROOT_DIRS.get(item.get("root", "data"))
            if base is None:
                failed.append(f"{item.get('arcname')}: 未知根标记")
                continue
            target = base / item.get("rel", "")
            try:
                if target.exists() and not overwrite:
                    skipped.append(item.get("arcname", ""))
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    backup_path = target.with_name(target.name + f".pre_restore_{stamp}")
                    if target.is_dir():
                        shutil.copytree(target, backup_path)
                    else:
                        shutil.copy2(target, backup_path)
                    pre_backups.append(str(backup_path))
                shutil.copy2(src, target)
                restored.append(item.get("arcname", ""))
            except Exception as exc:
                failed.append(f"{item.get('arcname')}: {exc}")

        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": "restore",
            "status": "success" if not failed else "partial",
            "message": f"还原 {len(restored)} 项，跳过 {len(skipped)} 项，失败 {len(failed)} 项",
            "object_key": key,
            "filename": filename,
            "size": download.get("size", 0),
            "size_fmt": _file_size_fmt(download.get("size", 0)),
            "file_count": len(files),
            "categories": manifest.get("categories", []),
            "encrypted": bool(decrypted_path),
        }
        _append_history(entry)
        return {
            "ok": not failed,
            "message": entry["message"],
            "encrypted": bool(decrypted_path),
            "restored": restored,
            "skipped": skipped,
            "failed": failed,
            "pre_backups": pre_backups,
            "manifest": {k: manifest.get(k) for k in
                         ("format", "version", "created_at", "app_version", "host",
                          "categories", "total_size")},
            "note": "还原后请重启服务，让各模块重新打开数据库连接（运行中的 SQLite 可能持有 WAL）。",
        }
    finally:
        shutil.rmtree(str(extract_dir), ignore_errors=True)
        for tmp in (decrypted_path, local_archive):
            if not tmp:
                continue
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                pass


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ── 状态 ──────────────────────────────────────────────────────────────
def next_run_at(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or store.load_config(mask=True)
    interval = int(cfg.get("interval_minutes") or 0)
    if not cfg.get("enabled") or interval <= 0:
        return ""
    last = cfg.get("last_run_at") or ""
    if not last:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (last_dt + timedelta(minutes=interval)).strftime("%Y-%m-%d %H:%M:%S")


def is_due(cfg: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if not cfg.get("enabled"):
        return False
    interval = int(cfg.get("interval_minutes") or 0)
    if interval <= 0:
        return False
    last = cfg.get("last_run_at") or ""
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return now - last_dt >= timedelta(minutes=interval)


def last_run_error(cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or store.load_config(mask=True)
    if cfg.get("last_status") == "failed":
        return cfg.get("last_message", "")
    return ""


def summary() -> Dict[str, Any]:
    cfg = store.load_config(mask=True)
    history = list_history(limit=1)
    return {
        **store.config_summary(cfg),
        "next_run_at": next_run_at(cfg),
        "last_entry": history[0] if history else None,
        "categories": catalog(),
        "category_ids": list(CATEGORY_IDS),
    }
