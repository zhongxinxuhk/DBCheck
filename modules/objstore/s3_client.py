# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""S3 兼容对象存储接入层（AWS Signature V4，纯标准库实现）。

为什么不用 boto3
----------------
* 打包版走 PyInstaller，boto3/botocore 会额外带入约 30MB 依赖与大量数据文件，
  只为「把备份包 PUT 上去」并不划算；
* S3 协议真正需要的东西只有 Signature V4 与少数 REST 调用
  （PUT / GET / HEAD / DELETE / ListObjectsV2 / 分片上传），
  urllib + hashlib + hmac 足以覆盖，且行为可完全掌控（超时、代理、TLS 校验）；
* 自建网关（MinIO、Ceph RGW、OSCA 联盟云 fgws3 等）对 path-style 与
  virtual-hosted style 的支持差异很大，两种寻址方式都必须实现。

对外契约
--------
``S3Client`` 只暴露「对象级」操作，不关心备份语义：

    client.put_object(key, data)          -> {etag, size}
    client.put_file(key, path)            -> {etag, size, multipart}
    client.get_object(key)                -> bytes
    client.download(key, dest)            -> {size, etag}
    client.head_object(key)               -> dict | None
    client.delete_object(key)             -> None
    client.list_objects(prefix)           -> [{key, size, etag, last_modified}]
    client.bucket_status()                -> {ok, message}

所有失败统一抛 ``S3Error``（带 status / code / request_id，便于前端提示）。
"""



from __future__ import annotations

import hashlib
import hmac
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["S3Client", "S3Error", "normalize_endpoint"]

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DEFAULT_REGION = "us-east-1"
# 分片阈值：超过 32MB 走分片上传，避免单次 PUT 在跨境/公网链路上超时重传整个文件
MULTIPART_THRESHOLD = 32 * 1024 * 1024
MULTIPART_PART_SIZE = 16 * 1024 * 1024


class S3Error(Exception):
    """对象存储调用失败（网络 / 鉴权 / 桶不存在 / 5xx 等）。"""

    def __init__(self, message: str, status: Optional[int] = None,
                 code: str = "", request_id: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or ""
        self.request_id = request_id or ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "status": self.status,
            "code": self.code,
            "request_id": self.request_id,
        }


def normalize_endpoint(endpoint: str) -> str:
    """把用户填写的 endpoint 归一成带协议头的地址。

    OSCA 联盟云公网网关是 ``https://fgws3-ocloud.ihep.ac.cn``，用户经常只填
    主机名，这里补默认 https；同时去掉末尾斜杠与误填的桶路径。
    """
    value = (endpoint or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    return value


def _uri_encode(value: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(str(value), safe=safe)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str, service: str = "s3") -> bytes:
    k_date = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_text(node: ET.Element, name: str) -> str:
    for child in node.iter():
        if _strip_ns(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _parse_error_body(raw: bytes) -> Dict[str, str]:
    """解析 S3 错误 XML（Code / Message / RequestId）。"""
    out: Dict[str, str] = {}
    if not raw:
        return out
    try:
        root = ET.fromstring(raw)
    except Exception:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            out["Message"] = text[:400]
        return out
    for child in root.iter():
        name = _strip_ns(child.tag)
        if name in ("Code", "Message", "RequestId"):
            out[name] = (child.text or "").strip()
    return out


class S3Client:
    """极简 S3 客户端（SigV4 签名，支持 path-style / virtual-hosted 两种寻址）。"""

    def __init__(self, endpoint: str, access_key: str, secret_key: str,
                 bucket: str = "", region: str = DEFAULT_REGION,
                 path_style: bool = True, timeout: int = 60,
                 verify_tls: bool = True, session_token: str = "") -> None:
        self.endpoint = normalize_endpoint(endpoint)
        if not self.endpoint:
            raise S3Error("对象存储 Endpoint 未配置")
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme not in ("http", "https"):
            raise S3Error("Endpoint 协议只能是 http 或 https")
        if not parsed.netloc:
            raise S3Error("Endpoint 格式不正确，示例：https://fgws3-ocloud.ihep.ac.cn")
        self.scheme = parsed.scheme
        self.host = parsed.netloc
        self.base_path = parsed.path.rstrip("/")
        self.access_key = (access_key or "").strip()
        self.secret_key = (secret_key or "").strip()
        if not self.access_key or not self.secret_key:
            raise S3Error("AccessKey / SecretKey 未配置")
        self.bucket = (bucket or "").strip()
        self.region = (region or DEFAULT_REGION).strip() or DEFAULT_REGION
        self.path_style = bool(path_style)
        self.timeout = max(5, int(timeout or 60))
        self.verify_tls = bool(verify_tls)
        self.session_token = (session_token or "").strip()

        handlers: List[urllib.request.BaseHandler] = []
        if self.scheme == "https" and not self.verify_tls:
            ctx = ssl._create_unverified_context()  # noqa: SLF001 - 自签名网关的显式降级
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)

    # ── 内部：签名与请求 ────────────────────────────────────────────────
    def _target(self, bucket: str, key: str = "") -> tuple:
        """返回 (host, path, url)。"""
        key = (key or "").lstrip("/")
        if self.path_style:
            host = self.host
            path = f"{self.base_path}/{bucket}" if bucket else (self.base_path or "")
            if key:
                path = f"{path}/{key}"
        else:
            host = f"{bucket}.{self.host}" if bucket else self.host
            path = self.base_path or ""
            if key:
                path = f"{path}/{key}"
        if not path.startswith("/"):
            path = "/" + path
        if not key and path.endswith("/"):
            path = path.rstrip("/") or "/"
        return host, path, f"{self.scheme}://{host}{path}"

    def _authorization(self, method: str, host: str, path: str,
                       query: Dict[str, str], headers: Dict[str, str],
                       payload_hash: str) -> tuple:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        send_headers: Dict[str, str] = {k: str(v) for k, v in headers.items()}
        send_headers["Host"] = host
        send_headers["x-amz-content-sha256"] = payload_hash
        send_headers["x-amz-date"] = amz_date
        if self.session_token:
            send_headers["x-amz-security-token"] = self.session_token

        lowered = {k.lower(): (k, str(v).strip()) for k, v in send_headers.items()}
        signed_headers = ";".join(sorted(lowered))
        canonical_headers = "".join(f"{k}:{lowered[k][1]}\n" for k in sorted(lowered))

        query_items = sorted((str(k), str(v)) for k, v in query.items())
        canonical_query = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in query_items)
        canonical_uri = _uri_encode(path, safe="/~")

        canonical_request = "\n".join([
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ])
        signature = hmac.new(
            _signing_key(self.secret_key, datestamp, self.region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
                         f"SignedHeaders={signed_headers}, Signature={signature}")
        send_headers["Authorization"] = authorization
        return send_headers, canonical_request

    def _execute(self, method: str, bucket: str, key: str = "",
                 query: Optional[Dict[str, str]] = None,
                 body: Optional[bytes] = None,
                 headers: Optional[Dict[str, str]] = None):
        """签名并发起请求，返回未读取的 response（调用方负责 close）。"""
        payload = body if body is not None else b""
        payload_hash = _sha256_hex(payload)
        host, path, url = self._target(bucket, key)
        query = dict(query or {})
        headers = dict(headers or {})
        if not payload and method in ("POST", "PUT"):
            # 空体 POST（如 CreateMultipartUpload）显式给 Content-Length: 0，
            # 部分网关缺这个头会直接 411，S3 规范也要求签名覆盖实际发送的头部
            headers.setdefault("Content-Length", "0")
        send_headers, _ = self._authorization(
            method, host, path, query, headers, payload_hash
        )
        if query:
            encoded = "&".join(
                f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items())
            )
            url = f"{url}?{encoded}"
        request = urllib.request.Request(
            url, data=payload if payload else None, method=method, headers=send_headers
        )
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:
                pass
            info = _parse_error_body(raw)
            message = info.get("Message") or f"HTTP {exc.code}"
            raise S3Error(
                message,
                status=exc.code,
                code=info.get("Code", ""),
                request_id=info.get("RequestId", ""),
            ) from None
        except urllib.error.URLError as exc:
            raise S3Error(f"无法连接对象存储：{getattr(exc, 'reason', exc)}") from None
        except TimeoutError:
            raise S3Error("对象存储请求超时") from None

    # ── 对象操作 ───────────────────────────────────────────────────────
    def put_object(self, key: str, data: bytes,
                   content_type: str = "application/octet-stream") -> Dict[str, Any]:
        resp = self._execute(
            "PUT", self.bucket, key, body=data,
            headers={"Content-Type": content_type},
        )
        try:
            etag = (resp.headers.get("ETag") or "").strip('"')
            status = resp.status
            resp.read()
        finally:
            resp.close()
        if status not in (200, 201):
            raise S3Error(f"上传失败（HTTP {status}）", status=status)
        return {"key": key, "size": len(data), "etag": etag, "multipart": False}

    def put_file(self, key: str, path: str,
                 content_type: str = "application/gzip") -> Dict[str, Any]:
        size = os.path.getsize(path)
        if size <= MULTIPART_THRESHOLD:
            with open(path, "rb") as fh:
                data = fh.read()
            result = self.put_object(key, data, content_type=content_type)
            return result
        return self._multipart_upload(key, path, size, content_type)

    def _multipart_upload(self, key: str, path: str, size: int,
                          content_type: str) -> Dict[str, Any]:
        init = self._execute("POST", self.bucket, key,
                             query={"uploads": ""},
                             headers={"Content-Type": content_type})
        try:
            upload_id = _find_text(ET.fromstring(init.read() or b"<r/>"), "UploadId")
        finally:
            init.close()
        if not upload_id:
            raise S3Error("分片上传初始化失败：未返回 UploadId")

        parts: List[Dict[str, Any]] = []
        try:
            with open(path, "rb") as fh:
                index = 1
                while True:
                    chunk = fh.read(MULTIPART_PART_SIZE)
                    if not chunk:
                        break
                    resp = self._execute(
                        "PUT", self.bucket, key,
                        query={"partNumber": str(index), "uploadId": upload_id},
                        body=chunk,
                    )
                    try:
                        etag = (resp.headers.get("ETag") or "").strip('"')
                        resp.read()
                    finally:
                        resp.close()
                    parts.append({"n": index, "etag": etag})
                    index += 1
            xml_parts = "".join(
                f"<Part><PartNumber>{p['n']}</PartNumber><ETag>\"{p['etag']}\"</ETag></Part>"
                for p in parts
            )
            body = ("<CompleteMultipartUpload>" + xml_parts +
                    "</CompleteMultipartUpload>").encode("utf-8")
            resp = self._execute("POST", self.bucket, key,
                                 query={"uploadId": upload_id}, body=body,
                                 headers={"Content-Type": "application/xml"})
            try:
                resp.read()
            finally:
                resp.close()
        except Exception:
            try:
                abort = self._execute("DELETE", self.bucket, key,
                                      query={"uploadId": upload_id})
                abort.close()
            except Exception:
                pass
            raise
        return {"key": key, "size": size, "etag": parts[-1]["etag"] if parts else "",
                "multipart": True, "parts": len(parts)}

    def get_object(self, key: str) -> bytes:
        resp = self._execute("GET", self.bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()

    def download(self, key: str, dest: str) -> Dict[str, Any]:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        resp = self._execute("GET", self.bucket, key)
        written = 0
        try:
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
            etag = (resp.headers.get("ETag") or "").strip('"')
        finally:
            resp.close()
        return {"key": key, "path": dest, "size": written, "etag": etag}

    def head_object(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self._execute("HEAD", self.bucket, key)
        except S3Error as exc:
            if exc.status in (403, 404):
                return None
            raise
        try:
            return {
                "key": key,
                "size": int(resp.headers.get("Content-Length") or 0),
                "etag": (resp.headers.get("ETag") or "").strip('"'),
                "last_modified": resp.headers.get("Last-Modified") or "",
            }
        finally:
            resp.close()

    def delete_object(self, key: str) -> None:
        try:
            resp = self._execute("DELETE", self.bucket, key)
            resp.close()
        except S3Error as exc:
            if exc.status == 404:
                return
            raise

    def list_objects(self, prefix: str = "", limit: int = 1000) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        token = ""
        while True:
            query = {"list-type": "2", "max-keys": str(min(1000, max(1, limit)))}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            resp = self._execute("GET", self.bucket, "", query=query)
            try:
                raw = resp.read()
            finally:
                resp.close()
            try:
                root = ET.fromstring(raw)
            except Exception:
                raise S3Error("列表解析失败：返回内容不是合法 XML") from None
            for node in root.iter():
                if _strip_ns(node.tag) != "Contents":
                    continue
                item = {
                    "key": _find_text(node, "Key"),
                    "size": int(_find_text(node, "Size") or 0),
                    "etag": _find_text(node, "ETag").strip('"'),
                    "last_modified": _find_text(node, "LastModified"),
                }
                if item["key"]:
                    out.append(item)
            truncated = _find_text(root, "IsTruncated").lower() == "true"
            token = _find_text(root, "NextContinuationToken")
            if not truncated or not token or len(out) >= limit:
                break
        return out[:limit] if limit else out

    def bucket_status(self) -> Dict[str, Any]:
        """探测桶是否可用（HEAD bucket；部分网关不支持 HEAD，降级为 list）。"""
        if not self.bucket:
            return {"ok": False, "message": "未配置 Bucket"}
        try:
            resp = self._execute("HEAD", self.bucket, "")
            resp.close()
            return {"ok": True, "message": "连接成功，桶可访问"}
        except S3Error as exc:
            if exc.status in (403, 405):
                try:
                    self.list_objects(prefix="", limit=1)
                    return {"ok": True, "message": "连接成功（HEAD 受限，已用列表接口验证）"}
                except S3Error as exc2:
                    return {"ok": False, "message": f"{exc2.message}", "status": exc2.status,
                            "code": exc2.code}
            if exc.status == 404:
                return {"ok": False, "message": f"桶不存在：{self.bucket}", "status": 404,
                        "code": exc.code}
            return {"ok": False, "message": exc.message, "status": exc.status,
                    "code": exc.code}
        except Exception as exc:  # pragma: no cover - 兜底
            return {"ok": False, "message": str(exc)}
