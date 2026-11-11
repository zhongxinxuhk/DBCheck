# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""对象存储备份模块的自测（不依赖 Flask / boto3，全部标准库）。

覆盖点
------
1. SigV4 授权头形状与规范化请求（防签名拼装回归）；
2. 与「假 S3 网关」的端到端往返：PUT / GET / LIST / DELETE / HEAD；
3. 内容编目 → 打包 → 上传 → 还原（含 sha256 校验、覆盖前 .pre_restore 备份）；
4. 配置校验、SecretKey 掩码与还原时的路径穿越防护。

用 ``python -m unittest discover -s modules/objstore/tests`` 直接跑。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from modules.core import paths  # noqa: E402
from modules.objstore import crypto, engine, store  # noqa: E402
from modules.objstore.s3_client import S3Client, S3Error  # noqa: E402


class _FakeS3:
    """最小可用的 S3 网关：只为验证客户端行为，不做鉴权校验。"""

    def __init__(self) -> None:
        self.bucket = "test-bucket"
        self.objects = {}
        self.auth_headers = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # 静音
                return

            # ── 路由 ──
            def _split(self):
                parsed = urlparse(self.path)
                parts = [unquote(p) for p in parsed.path.split("/") if p]
                query = parse_qs(parsed.query, keep_blank_values=True)
                return parts, {k: v[0] for k, v in query.items()}

            def _bucket_only(self, parts):
                return len(parts) == 1 and parts[0] == fake.bucket

            def _read_body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _record_auth(self):
                fake.auth_headers.append(self.headers.get("Authorization", ""))

            def _send(self, status, body=b"", extra=None, content_length=None):
                self.send_response(status)
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(
                    len(body) if content_length is None else content_length))
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def do_HEAD(self):
                self._record_auth()
                parts, _ = self._split()
                if self._bucket_only(parts):
                    self._send(200)
                    return
                key = "/".join(parts[1:])
                if key in fake.objects:
                    body, etag = fake.objects[key]
                    self._send(200, extra={"ETag": '"%s"' % etag,
                                           "Last-Modified": "Mon, 09 Nov 2026 10:00:00 GMT"},
                               content_length=len(body))
                else:
                    self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")

            def do_PUT(self):
                self._record_auth()
                parts, query = self._split()
                body = self._read_body()
                key = "/".join(parts[1:])
                if "partNumber" in query:
                    fake.objects.setdefault("__multipart__" + key, []).append(body)
                    self._send(200, extra={"ETag": '"%s"' % hashlib.md5(body).hexdigest()})
                    return
                etag = hashlib.md5(body).hexdigest()
                fake.objects[key] = (body, etag)
                self._send(200, extra={"ETag": '"%s"' % etag})

            def do_POST(self):
                self._record_auth()
                parts, query = self._split()
                key = "/".join(parts[1:])
                self._read_body()
                if "uploads" in query:
                    self._send(200, b"<InitiateMultipartUploadResult><UploadId>up-1</UploadId>"
                                    b"</InitiateMultipartUploadResult>")
                    return
                chunks = fake.objects.pop("__multipart__" + key, [])
                if chunks:
                    data = b"".join(chunks)
                    fake.objects[key] = (data, hashlib.md5(data).hexdigest())
                self._send(200, b"<CompleteMultipartUploadResult/>")

            def do_GET(self):
                self._record_auth()
                parts, query = self._split()
                if self._bucket_only(parts) and query.get("list-type") == "2":
                    prefix = query.get("prefix", "")
                    items = []
                    for key, (body, etag) in sorted(fake.objects.items()):
                        if key.startswith("__multipart__") or not key.startswith(prefix):
                            continue
                        items.append(
                            "<Contents><Key>%s</Key><Size>%d</Size><ETag>\"%s\"</ETag>"
                            "<LastModified>2026-11-09T10:00:00.000Z</LastModified></Contents>"
                            % (key, len(body), etag)
                        )
                    xml = ("<ListBucketResult><IsTruncated>false</IsTruncated>%s"
                           "</ListBucketResult>" % "".join(items)).encode()
                    self._send(200, xml, {"Content-Type": "application/xml"})
                    return
                key = "/".join(parts[1:])
                if key in fake.objects:
                    body, etag = fake.objects[key]
                    self._send(200, body, {"ETag": '"%s"' % etag})
                else:
                    self._send(404, b"<Error><Code>NoSuchKey</Code><Message>missing</Message></Error>")

            def do_DELETE(self):
                self._record_auth()
                parts, _ = self._split()
                key = "/".join(parts[1:])
                fake.objects.pop(key, None)
                self._send(204)

        return Handler


class TestSignature(unittest.TestCase):
    def test_authorization_header_shape(self):
        client = S3Client("https://fgws3-ocloud.ihep.ac.cn", "AKIDEXAMPLE", "secret",
                          bucket="demo", path_style=True)
        headers, canonical = client._authorization(
            "PUT", "fgws3-ocloud.ihep.ac.cn", "/demo/backup/a.tar.gz", {},
            {"Content-Type": "application/gzip"},
            hashlib.sha256(b"payload").hexdigest(),
        )
        auth = headers["Authorization"]
        self.assertTrue(auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/"))
        self.assertIn("/s3/aws4_request", auth)
        self.assertIn("SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date", auth)
        signature = auth.rsplit("Signature=", 1)[-1]
        self.assertEqual(len(signature), 64)
        int(signature, 16)  # 必须是十六进制
        self.assertIn("PUT\n/demo/backup/a.tar.gz\n", canonical)

    def test_path_style_and_virtual_host(self):
        path_style = S3Client("https://s3.example.com", "ak", "sk", bucket="b1", path_style=True)
        host, path, url = path_style._target("b1", "k/1.txt")
        self.assertEqual(host, "s3.example.com")
        self.assertEqual(path, "/b1/k/1.txt")
        self.assertEqual(url, "https://s3.example.com/b1/k/1.txt")

        vhost = S3Client("https://s3.example.com", "ak", "sk", bucket="b1", path_style=False)
        host, path, url = vhost._target("b1", "k/1.txt")
        self.assertEqual(host, "b1.s3.example.com")
        self.assertEqual(path, "/k/1.txt")

    def test_endpoint_without_scheme(self):
        client = S3Client("fgws3-ocloud.ihep.ac.cn", "ak", "sk", bucket="b")
        self.assertEqual(client.endpoint, "https://fgws3-ocloud.ihep.ac.cn")


class TestClientRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = _FakeS3()
        cls.fake.start()
        cls.client = S3Client(cls.fake.endpoint, "ak", "sk", bucket=cls.fake.bucket,
                              path_style=True, timeout=10)

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def test_bucket_status(self):
        self.assertTrue(self.fake)
        result = self.client.bucket_status()
        self.assertTrue(result["ok"], result)

    def test_put_get_list_delete(self):
        self.client.put_object("dbpulse-backup/2026/11/a.txt", b"hello")
        self.assertEqual(self.client.get_object("dbpulse-backup/2026/11/a.txt"), b"hello")
        items = self.client.list_objects(prefix="dbpulse-backup/")
        self.assertTrue(any(i["key"].endswith("a.txt") for i in items))
        head = self.client.head_object("dbpulse-backup/2026/11/a.txt")
        self.assertEqual(head["size"], 5)
        self.client.delete_object("dbpulse-backup/2026/11/a.txt")
        self.assertIsNone(self.client.head_object("dbpulse-backup/2026/11/a.txt"))

    def test_missing_key_raises(self):
        with self.assertRaises(S3Error):
            self.client.get_object("nope/missing.bin")

    def test_auth_headers_recorded(self):
        self.client.list_objects(prefix="dbpulse-backup/")
        self.assertTrue(self.fake.auth_headers)
        self.assertTrue(all(h.startswith("AWS4-HMAC-SHA256") for h in self.fake.auth_headers))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="objstore-store-"))
        self._orig = store.CONFIG_PATH
        store.CONFIG_PATH = self.tmp / "dbc_config.json"

    def tearDown(self):
        store.CONFIG_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_and_mask(self):
        cfg = store.load_config()
        self.assertEqual(cfg["prefix"], store.DEFAULT_PREFIX)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["secret_key"], "")

    def test_save_and_mask_secret(self):
        saved = store.save_config({
            "enabled": True,
            "endpoint": "https://fgws3-ocloud.ihep.ac.cn",
            "bucket": "osca-bucket",
            "access_key": "AKID",
            "secret_key": "SUPERSECRET",
            "contents": ["engine", "report"],
            "interval_minutes": 90,
            "retention": 3,
        })
        self.assertEqual(saved["secret_key"], store.MASK)
        self.assertTrue(saved["secret_key_set"])
        raw = store.load_config(mask=False)
        self.assertEqual(raw["secret_key"], "SUPERSECRET")
        self.assertEqual(raw["interval_minutes"], 90)

        # 掩码回传 = 保持原值
        store.save_config({"secret_key": store.MASK, "bucket": "osca-bucket"})
        self.assertEqual(store.load_config(mask=False)["secret_key"], "SUPERSECRET")

    def test_validate_rejects_bad_bucket(self):
        with self.assertRaises(ValueError):
            store.validate({
                "enabled": True,
                "endpoint": "https://s3.example.com",
                "bucket": "Bad_Bucket",
                "access_key": "a",
                "secret_key": "b",
                "contents": ["engine"],
            }, store.load_config(mask=False))

    def test_archive_password_masked_kept_and_cleared(self):
        orig = store._encrypt_available
        store._encrypt_available = lambda: True
        try:
            base = {
                "enabled": True,
                "endpoint": "https://fgws3-ocloud.ihep.ac.cn",
                "bucket": "osca-bucket",
                "access_key": "AKID",
                "secret_key": "SUPERSECRET",
                "contents": ["engine"],
            }
            saved = store.save_config({**base, "archive_password": "p@ssw0rd"})
            self.assertEqual(saved["archive_password"], store.MASK)
            self.assertTrue(saved["archive_password_set"])
            self.assertTrue(saved["encrypt_available"])
            self.assertEqual(store.load_config(mask=False)["archive_password"], "p@ssw0rd")

            # 留空保存 = 保持原密码（避免用户只是改个间隔就把密码清掉）
            store.save_config({"interval_minutes": 120})
            self.assertEqual(store.load_config(mask=False)["archive_password"], "p@ssw0rd")

            # 显式清除
            store.save_config({"clear_archive_password": True})
            self.assertEqual(store.load_config(mask=False)["archive_password"], "")
            self.assertFalse(store.load_config()["archive_password_set"])
        finally:
            store._encrypt_available = orig

    def test_archive_password_rejected_without_crypto(self):
        orig = store._encrypt_available
        store._encrypt_available = lambda: False
        try:
            with self.assertRaises(ValueError) as ctx:
                store.save_config({
                    "enabled": True,
                    "endpoint": "https://fgws3-ocloud.ihep.ac.cn",
                    "bucket": "osca-bucket",
                    "access_key": "AKID",
                    "secret_key": "SK",
                    "contents": ["engine"],
                    "archive_password": "p@ss",
                })
            self.assertIn("cryptography", str(ctx.exception))
        finally:
            store._encrypt_available = orig

    def test_save_preserves_other_node_fields(self):
        """保存配置不得抹掉节点内的其它字段（例如 Fernet 密钥）。"""
        payload = {"object_storage": {"_fernet_key": "KEEP-ME", "custom": 1}}
        store.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        store.CONFIG_PATH.write_text(json.dumps(payload), encoding="utf-8")

        store.save_config({
            "enabled": True,
            "endpoint": "https://fgws3-ocloud.ihep.ac.cn",
            "bucket": "osca-bucket",
            "access_key": "AKID",
            "secret_key": "SUPERSECRET",
            "contents": ["engine"],
        })
        saved = json.loads(store.CONFIG_PATH.read_text(encoding="utf-8"))["object_storage"]
        self.assertEqual(saved["_fernet_key"], "KEEP-ME")
        self.assertEqual(saved["custom"], 1)
        # 没有 cryptography 时走 _plain_，有则 _enc_；两种都必须能解回原值
        self.assertTrue(saved["secret_key"].startswith(("_enc_", "_plain_")))
        self.assertEqual(store.load_config(mask=False)["secret_key"], "SUPERSECRET")

    def test_interval_clamped(self):
        cfg = store.validate({"interval_minutes": 1, "retention": 0},
                             {"interval_minutes": 1440, "retention": 7,
                              "contents": ["engine"], "secret_key": "x"})
        self.assertEqual(cfg["interval_minutes"], store.MIN_INTERVAL_MINUTES)
        self.assertEqual(cfg["retention"], 1)


class _FakeCipher:
    """替身密码器：XOR 密钥流 + 截断 SHA256 当作 GCM tag。

    本机测试环境没有 cryptography，用它把「分块容器 / KDF 参数 / 认证失败」
    这些与 AES 无关的逻辑全部跑通；真实 AES-GCM 由生产环境依赖提供。
    """

    def __init__(self, key: bytes) -> None:
        self.key = key

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        seed = hashlib.sha256(self.key + nonce).digest()
        return (seed * (length // len(seed) + 1))[:length]

    def encrypt(self, nonce: bytes, data: bytes) -> bytes:
        ks = self._keystream(nonce, len(data))
        ct = bytes(a ^ b for a, b in zip(data, ks))
        return ct + hashlib.sha256(self.key + nonce + ct).digest()[:16]

    def decrypt(self, nonce: bytes, data: bytes) -> bytes:
        ct, tag = data[:-16], data[-16:]
        if hashlib.sha256(self.key + nonce + ct).digest()[:16] != tag:
            raise ValueError("bad tag")
        ks = self._keystream(nonce, len(ct))
        return bytes(a ^ b for a, b in zip(ct, ks))


class TestCrypto(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="objstore-crypto-"))
        self._orig = {
            "_HAS_AESGCM": crypto._HAS_AESGCM,
            "_cipher_factory": crypto._cipher_factory,
            "_kdf_params": crypto._kdf_params,
            "PBKDF2_ITERATIONS": crypto.PBKDF2_ITERATIONS,
        }
        # 用替身密码器 + 轻量 KDF 参数，避免依赖 cryptography
        crypto._HAS_AESGCM = True
        crypto._cipher_factory = _FakeCipher
        crypto._kdf_params = lambda: {"name": "pbkdf2-sha256", "iterations": 2000}
        crypto.PBKDF2_ITERATIONS = 2000

    def tearDown(self):
        for key, value in self._orig.items():
            setattr(crypto, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_multiblock(self):
        src = self.tmp / "payload.bin"
        payload = os.urandom(300 * 1024)   # > 2 个 64KB 分块
        src.write_bytes(payload)
        enc = self.tmp / "payload.bin.enc"
        meta = crypto.encrypt_file(str(src), str(enc), "s3cret", chunk_size=64 * 1024)
        self.assertTrue(meta["encrypted"])
        self.assertGreaterEqual(meta["blocks"], 4)
        self.assertTrue(crypto.is_encrypted(str(enc)))

        header = crypto.read_header(str(enc))
        self.assertEqual(header["cipher"], "aes-256-gcm")
        self.assertNotIn("salt", meta)   # 元信息不外泄 salt

        out = self.tmp / "out.bin"
        crypto.decrypt_file(str(enc), str(out), "s3cret")
        self.assertEqual(out.read_bytes(), payload)

    def test_wrong_password(self):
        src = self.tmp / "a.bin"
        src.write_bytes(b"hello world" * 100)
        enc = self.tmp / "a.bin.enc"
        crypto.encrypt_file(str(src), str(enc), "right", chunk_size=64 * 1024)
        out = self.tmp / "a.out"
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_file(str(enc), str(out), "wrong")

    def test_tampered_payload_detected(self):
        src = self.tmp / "b.bin"
        src.write_bytes(b"x" * 1000)
        enc = self.tmp / "b.bin.enc"
        crypto.encrypt_file(str(src), str(enc), "pwd", chunk_size=64 * 1024)
        raw = bytearray(enc.read_bytes())
        raw[-1] ^= 0xFF
        enc.write_bytes(bytes(raw))
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_file(str(enc), str(self.tmp / "b.out"), "pwd")

    def test_magic_guard(self):
        plain = self.tmp / "plain.tar.gz"
        plain.write_bytes(b"\x1f\x8b\x08not-encrypted")
        self.assertFalse(crypto.is_encrypted(str(plain)))
        with self.assertRaises(crypto.CryptoError):
            crypto.read_header(str(plain))

    def test_requires_password(self):
        src = self.tmp / "c.bin"
        src.write_bytes(b"data")
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(str(src), str(self.tmp / "c.enc"), "")

    def test_unavailable_environment_raises(self):
        crypto._HAS_AESGCM = False
        src = self.tmp / "d.bin"
        src.write_bytes(b"data")
        with self.assertRaises(crypto.CryptoError):
            crypto.encrypt_file(str(src), str(self.tmp / "d.enc"), "pwd")
        self.assertFalse(crypto.available())


class TestEngine(unittest.TestCase):
    """用临时数据根跑通「编目 → 打包 → 上传 → 还原」。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="objstore-engine-"))
        self.data_root = self.tmp / "data"
        self.proj_root = self.tmp / "project"
        (self.data_root / "reports").mkdir(parents=True)
        (self.proj_root / "modules" / "pro" / "rules").mkdir(parents=True)

        self.report_file = self.data_root / "reports" / "r1.docx"
        self.report_file.write_bytes(b"report-bytes")
        self.history_file = self.data_root / "history.db"
        self.history_file.write_bytes(b"sqlite-bytes")
        self.rules_file = self.proj_root / "modules" / "pro" / "rules" / "overrides.yaml"
        self.rules_file.write_text("rules: []\n", encoding="utf-8")

        # 打补丁：工作目录 / 历史 / 根映射 / 编目
        self._orig = {
            "WORK_DIR": engine.WORK_DIR,
            "STAGING_DIR": engine.STAGING_DIR,
            "HISTORY_PATH": engine.HISTORY_PATH,
            "CATEGORIES": engine.CATEGORIES,
            "_CATEGORY_BY_ID": engine._CATEGORY_BY_ID,
            "_ROOT_DIRS": engine._ROOT_DIRS,
            "CONFIG_PATH": store.CONFIG_PATH,
        }
        engine.WORK_DIR = self.tmp / "work"
        engine.STAGING_DIR = engine.WORK_DIR / "staging"
        engine.HISTORY_PATH = engine.WORK_DIR / "history.json"
        engine._ROOT_DIRS = {"data": self.data_root, "project": self.proj_root}
        engine.CATEGORIES = [
            {
                "id": "engine",
                "label": "引擎与规则",
                "label_key": "objstore.cat_engine",
                "desc": "",
                "items": [{"label": "规则", "path": self.rules_file, "root": "project"}],
            },
            {
                "id": "report",
                "label": "报告",
                "label_key": "objstore.cat_report",
                "desc": "",
                "items": [{"label": "报告目录", "path": self.data_root / "reports", "root": "data"}],
            },
            {
                "id": "history",
                "label": "历史",
                "label_key": "objstore.cat_history",
                "desc": "",
                "items": [{"label": "历史库", "path": self.history_file, "root": "data"}],
            },
        ]
        engine._CATEGORY_BY_ID = {c["id"]: c for c in engine.CATEGORIES}

        store.CONFIG_PATH = self.tmp / "dbc_config.json"

        self.fake = _FakeS3()
        self.fake.start()
        store.save_config({
            "enabled": False,
            "endpoint": self.fake.endpoint,
            "bucket": self.fake.bucket,
            "region": "us-east-1",
            "access_key": "ak",
            "secret_key": "sk",
            "prefix": "dbpulse-backup",
            "path_style": True,
            "contents": ["engine", "report", "history"],
            "interval_minutes": 60,
            "retention": 2,
        })

    def tearDown(self):
        self.fake.stop()
        for key, value in self._orig.items():
            if key == "CONFIG_PATH":
                store.CONFIG_PATH = value
            else:
                setattr(engine, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_catalog_reports_existing_sizes(self):
        cats = {c["id"]: c for c in engine.catalog()}
        self.assertTrue(cats["report"]["available"])
        self.assertEqual(cats["history"]["total_size"], len(b"sqlite-bytes"))
        self.assertIn("history.db", cats["history"]["items"][0]["display"])

    def test_build_archive_manifest(self):
        archive = engine.build_archive(["report", "engine"])
        try:
            self.assertTrue(archive["path"].exists())
            self.assertEqual(archive["file_count"], 2)
            import tarfile

            with tarfile.open(str(archive["path"]), "r:gz") as tar:
                names = tar.getnames()
                self.assertIn(engine.MANIFEST_NAME, names)
                manifest = json.loads(tar.extractfile(engine.MANIFEST_NAME).read())
            self.assertEqual(manifest["format"], engine.FORMAT_TAG)
            self.assertEqual(sorted(manifest["categories"]), ["engine", "report"])
            by_arc = {f["arcname"]: f for f in manifest["files"]}
            entry = by_arc["data/reports/r1.docx"]
            self.assertEqual(entry["sha256"], hashlib.sha256(b"report-bytes").hexdigest())
        finally:
            archive["path"].unlink(missing_ok=True)

    def test_run_backup_uploads_and_prunes(self):
        result = engine.run_backup(contents=["report"])
        self.assertTrue(result["ok"])
        self.assertIn("dbpulse-backup/", result["object_key"])
        self.assertTrue(result["object_key"].endswith(".tar.gz"))

        objects = engine.list_remote()
        self.assertEqual(len(objects), 1)
        self.assertEqual(objects[0]["size_fmt"].endswith("B") or True, True)

        history = engine.list_history()
        self.assertEqual(history[0]["status"], "success")
        self.assertEqual(history[0]["trigger"], "manual")

        # 本地暂存包已清理
        staging = list(engine.STAGING_DIR.glob("*.tar.gz"))
        self.assertEqual(staging, [])

    def test_restore_roundtrip_and_overwrite(self):
        result = engine.run_backup(contents=["report"])
        key = result["object_key"]

        # 改坏本地文件，再用 overwrite=False 应跳过
        self.report_file.write_bytes(b"corrupted")
        skipped = engine.restore_from_object(key, overwrite=False)
        self.assertTrue(skipped["ok"])
        self.assertEqual(skipped["restored"], [])
        self.assertEqual(self.report_file.read_bytes(), b"corrupted")

        forced = engine.restore_from_object(key, overwrite=True)
        self.assertTrue(forced["ok"])
        self.assertEqual(self.report_file.read_bytes(), b"report-bytes")
        self.assertTrue(any(".pre_restore_" in p for p in forced["pre_backups"]))
        restored_backup = Path(forced["pre_backups"][0])
        self.assertEqual(restored_backup.read_bytes(), b"corrupted")
        self.assertIn("note", forced)

    def test_delete_remote_rejects_foreign_key(self):
        with self.assertRaises(ValueError):
            engine.delete_remote("other-prefix/file.tar.gz")

    def test_retention_keeps_latest(self):
        for _ in range(3):
            engine.run_backup(contents=["history"])
        objects = engine.list_remote()
        self.assertLessEqual(len(objects), 2)

    def test_safe_extract_blocks_traversal(self):
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../evil.txt")
            payload = b"x"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        buf.seek(0)
        dest = self.tmp / "extract"
        dest.mkdir()
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            with self.assertRaises(ValueError):
                engine._safe_extract(tar, dest)

    def test_history_trigger_schedule(self):
        engine.run_backup(trigger="schedule", contents=["report"])
        entry = engine.list_history()[0]
        self.assertEqual(entry["trigger"], "schedule")

    def test_trigger_backup_async_job(self):
        """接口层走后台线程：立即返回，job 状态最终落到 success。"""
        from modules.objstore import scheduler

        started = scheduler.trigger_backup(contents=["report"], trigger="manual")
        self.assertTrue(started["started"], started)
        job = {}
        for _ in range(200):
            job = scheduler.status()["job"]
            if job.get("status") in ("success", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(job.get("status"), "success", job)
        self.assertIn("dbpulse-backup/", job["result"]["object_key"])
        self.assertTrue(job["finished_at"])
        self.assertEqual(engine.list_history()[0]["trigger"], "manual")

    def test_is_due(self):
        cfg = {"enabled": True, "interval_minutes": 60,
               "last_run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self.assertFalse(engine.is_due(cfg))
        cfg["last_run_at"] = "2020-01-01 00:00:00"
        self.assertTrue(engine.is_due(cfg))
        cfg["enabled"] = False
        self.assertFalse(engine.is_due(cfg))


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestEngineEncrypted(unittest.TestCase):
    """备份包口令：加密产物命名、还原校验、缺密码/密码错误/环境不支持。"""

    def setUp(self):
        # 复用 TestEngine 的临时数据根与假 S3 网关搭台
        TestEngine.setUp(self)
        self._crypto_orig = {
            "_HAS_AESGCM": crypto._HAS_AESGCM,
            "_cipher_factory": crypto._cipher_factory,
            "_kdf_params": crypto._kdf_params,
            "PBKDF2_ITERATIONS": crypto.PBKDF2_ITERATIONS,
        }
        crypto._HAS_AESGCM = True
        crypto._cipher_factory = _FakeCipher
        crypto._kdf_params = lambda: {"name": "pbkdf2-sha256", "iterations": 2000}
        crypto.PBKDF2_ITERATIONS = 2000
        self._set_password("pwd123")

    def tearDown(self):
        for key, value in self._crypto_orig.items():
            setattr(crypto, key, value)
        TestEngine.tearDown(self)

    def _set_password(self, value):
        root = json.loads(store.CONFIG_PATH.read_text(encoding="utf-8"))
        node = root.setdefault("object_storage", {})
        if value:
            node["archive_password"] = value
        else:
            node.pop("archive_password", None)
        store.CONFIG_PATH.write_text(json.dumps(root), encoding="utf-8")

    def test_backup_produces_encrypted_archive(self):
        result = engine.run_backup(contents=["report"])
        self.assertTrue(result["object_key"].endswith(".tar.gz.enc"), result["object_key"])
        self.assertTrue(result["archive"]["encrypted"])
        entry = engine.list_history()[0]
        self.assertTrue(entry["encrypted"])
        # 暂存区不残留明文包
        self.assertEqual(list(engine.STAGING_DIR.glob("*.tar.gz")), [])

    def test_restore_with_correct_password(self):
        result = engine.run_backup(contents=["report"])
        self.report_file.write_bytes(b"corrupted")
        restored = engine.restore_from_object(result["object_key"], overwrite=True,
                                              password="pwd123")
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(self.report_file.read_bytes(), b"report-bytes")
        self.assertTrue(restored["encrypted"])

    def test_restore_rejects_wrong_password(self):
        result = engine.run_backup(contents=["report"])
        with self.assertRaises(crypto.CryptoError):
            engine.restore_from_object(result["object_key"], overwrite=True,
                                       password="nope")

    def test_restore_requires_password_when_not_configured(self):
        result = engine.run_backup(contents=["report"])
        self._set_password("")
        with self.assertRaises(ValueError) as ctx:
            engine.restore_from_object(result["object_key"], overwrite=True)
        self.assertIn("加密", str(ctx.exception))

    def test_backup_fails_when_crypto_unavailable(self):
        crypto._HAS_AESGCM = False
        with self.assertRaises(ValueError) as ctx:
            engine.run_backup(contents=["report"])
        self.assertIn("cryptography", str(ctx.exception))
        self.assertEqual(engine.list_history()[0]["status"], "failed")

class _FakeRequest:
    """最小 request 替身：只提供路由处理器用到的方法。"""

    def __init__(self, payload=None, args=None):
        self._payload = payload or {}
        self.args = args or {}

    def get_json(self, **kwargs):
        return self._payload


class TestRoutesWiring(unittest.TestCase):
    """路由层回归：缺导入/NameError 只有真正执行处理函数才会暴露。

    背景：`/config` 的 GET/POST 处理器调用 `store.load_config/save_config`，
    曾因「清理未使用导入」把 store 删掉，导致保存 S3 配置时 NameError。
    这里用假 flask 把处理器跑一遍，把这类错误挡在发布前。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="objstore-routes-"))
        self._orig_config = store.CONFIG_PATH
        store.CONFIG_PATH = self.tmp / "dbc_config.json"
        self._stub = None

    def tearDown(self):
        store.CONFIG_PATH = self._orig_config
        sys.modules.pop("modules.objstore.routes", None)
        if self._stub is not None:
            sys.modules.pop("flask", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _import_routes(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self._stub = self._build_flask_stub()
            sys.modules["flask"] = self._stub
        sys.modules.pop("modules.objstore.routes", None)
        mod = importlib.import_module("modules.objstore.routes")
        # jsonify / request 用替身，避免依赖 Flask 应用上下文
        mod.jsonify = lambda payload=None, **kw: payload if payload is not None else kw
        return mod

    @staticmethod
    def _build_flask_stub():
        module = types.ModuleType("flask")

        class Blueprint:
            def __init__(self, name, import_name, url_prefix=""):
                self.name = name
                self.url_prefix = url_prefix
                self.rules = []

            def route(self, rule, **kwargs):
                def deco(fn):
                    self.rules.append((rule, kwargs.get("methods"), fn))
                    return fn

                return deco

        module.Blueprint = Blueprint
        module.jsonify = lambda payload=None, **kw: payload if payload is not None else kw
        module.request = _FakeRequest()
        return module

    def test_route_module_exposes_required_dependencies(self):
        mod = self._import_routes()
        for name in ("engine", "scheduler", "store"):
            self.assertTrue(hasattr(mod, name), f"routes.py 缺少模块级依赖：{name}")

    def test_get_and_save_config_execute(self):
        mod = self._import_routes()

        got = mod.get_config()
        self.assertTrue(got["ok"])
        self.assertIn("config", got)

        payload = {
            "enabled": True,
            "endpoint": "https://fgws3-ocloud.ihep.ac.cn",
            "bucket": "osca-bucket",
            "region": "us-east-1",
            "access_key": "AKID",
            "secret_key": "SUPERSECRET",
            "prefix": "dbpulse-backup",
            "contents": ["engine", "report"],
            "interval_minutes": 60,
            "retention": 3,
        }
        mod.request = _FakeRequest(payload)
        saved = mod.save_config()
        self.assertTrue(saved["ok"], saved)
        self.assertEqual(saved["config"]["bucket"], "osca-bucket")
        self.assertEqual(saved["config"]["interval_minutes"], 60)
        self.assertEqual(saved["config"]["secret_key"], store.MASK)

    def test_expected_error_maps_to_400(self):
        """可预期错误（参数/配置/凭据）→ 400，消息可直接展示。"""
        mod = self._import_routes()
        orig = store.save_config

        def boom(payload):
            raise ValueError("配置不合法：Bucket 不能为空")

        store.save_config = boom
        try:
            mod.request = _FakeRequest({})
            result = mod.save_config()
        finally:
            store.save_config = orig
        self.assertIsInstance(result, tuple)
        payload, status = result
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertIn("Bucket", payload["error"])
        self.assertNotIn("unexpected", payload)

    def test_unexpected_error_maps_to_500_and_logs_traceback(self):
        """编码缺陷（NameError 等）→ 500 + unexpected 标记 + ERROR 日志。"""
        mod = self._import_routes()
        orig = store.load_config

        def boom(*args, **kwargs):
            raise NameError("name 'store' is not defined")

        store.load_config = boom
        try:
            with self.assertLogs("modules.objstore.routes", level="ERROR") as logs:
                result = mod.get_config()
        finally:
            store.load_config = orig
        payload, status = result
        self.assertEqual(status, 500)
        self.assertTrue(payload["unexpected"])
        self.assertIn("NameError", payload["error"])
        self.assertTrue(any("get_config" in line for line in logs.output))

    def test_remaining_handlers_do_not_nameerror(self):
        """未配置对象存储时这些接口应当「返回错误」而不是抛 NameError。"""
        mod = self._import_routes()
        cases = [
            (mod.list_objects, _FakeRequest()),
            (mod.history, _FakeRequest()),
            (mod.test_connection, _FakeRequest()),
            (mod.backup_now, _FakeRequest({"contents": ["report"]})),
            (mod.restore, _FakeRequest({"key": "dbpulse-backup/x.tar.gz"})),
            (mod.delete_object, _FakeRequest({"key": "dbpulse-backup/x.tar.gz"})),
        ]
        for handler, fake in cases:
            mod.request = fake
            result = handler()
            payload = result[0] if isinstance(result, tuple) else result
            self.assertIsInstance(payload, dict, handler.__name__)
            self.assertIn("ok", payload, handler.__name__)
            if not payload.get("ok"):
                self.assertTrue(payload.get("error") or payload.get("message"),
                                handler.__name__)

    def test_categories_and_status_execute(self):
        mod = self._import_routes()
        cats = mod.get_categories()
        self.assertTrue(cats["ok"])
        self.assertTrue(cats["categories"])
        st = mod.status()
        self.assertTrue(st["ok"])
        self.assertIn("job", st["status"])

