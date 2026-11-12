# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""备份包口令加密（AES-256-GCM，流式分块）。

为什么不用 zip 口令
-------------------
* 标准库 `zipfile` **只能读**传统 ZipCrypto（且该算法早已被已知明文攻击击穿），
  无法写出加密 zip；
* 引入 `pyzipper` 只为写 AES zip 会多一个依赖，且 AES zip 仍要整包进内存；
* 备份包可能是 GB 级报告，必须**流式**加密，不能一次性读进内存。

容器格式（``.tar.gz.enc``）
--------------------------
::

    ┌ b"DBPULSE-ENC-v1\\n"                 魔数（12 字节）
    ├ 一行 JSON 头部 + b"\\n"               版本 / 算法 / KDF 参数 / salt / 分块大小 / 原文件名
    └ 若干数据块，每块 = nonce(12B) ‖ len(4B, 大端) ‖ ciphertext(明文长度 + 16B GCM tag)

* 每个数据块独立 nonce 与 tag：任一块被篡改或口令错误，解密时 ``InvalidTag`` 立即失败，
  不会产出半截「看起来正常」的备份；
* 解出来的第一层永远是**标准 tar.gz**，用本工具还原，或先 ``decrypt`` 再用任意解包工具打开；
* 它是本工具的自有格式，**不是 zip 口令包**——用 7-Zip / WinRAR 直接双击打不开，这是刻意的。

命令行（脱离 Web 也能解密）：::

    python -m modules.objstore.crypto info    backup.tar.gz.enc
    python -m modules.objstore.crypto decrypt backup.tar.gz.enc backup.tar.gz
    python -m modules.objstore.crypto encrypt backup.tar.gz backup.tar.gz.enc
"""



from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import struct
import sys
from datetime import datetime
from typing import Any, Callable, Dict, Optional

MAGIC = b"DBPULSE-ENC-v1\n"
FORMAT_VERSION = 1
CHUNK_SIZE = 1024 * 1024          # 1 MiB
NONCE_SIZE = 12                   # AES-GCM 推荐 96-bit nonce
TAG_SIZE = 16
KEY_SIZE = 32                     # AES-256
SALT_SIZE = 16
SCRYPT_N, SCRYPT_R, SCRYPT_P = 16384, 8, 1
SCRYPT_MAXMEM = 64 * 1024 * 1024  # OpenSSL 默认上限 32MB，n=2^14/r=8 需放宽
PBKDF2_ITERATIONS = 600_000

try:  # cryptography 是运行期硬依赖；极简/测试环境可能缺失
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

    _HAS_AESGCM = True
except ImportError:  # pragma: no cover - 取决于运行环境
    AESGCM = None  # type: ignore
    _HAS_AESGCM = False


class CryptoError(Exception):
    """口令错误、包损坏或环境不支持加密。"""


def available() -> bool:
    """当前环境能否做口令加密（AES-GCM 可用）。"""
    return _HAS_AESGCM


def _require_crypto() -> None:
    if not _HAS_AESGCM:
        raise CryptoError("当前环境缺少 cryptography，无法加密/解密备份包；"
                          "请安装 cryptography 或关闭压缩包密码")


class _AesGcmCipher:
    """AES-256-GCM 的最小封装（块级 nonce，AAD 为空）。"""

    def __init__(self, key: bytes) -> None:
        _require_crypto()
        self._aead = AESGCM(key)

    def encrypt(self, nonce: bytes, data: bytes) -> bytes:
        return self._aead.encrypt(nonce, data, None)

    def decrypt(self, nonce: bytes, data: bytes) -> bytes:
        return self._aead.decrypt(nonce, data, None)


# 测试可替换：签名 (key: bytes) -> 具备 encrypt/decrypt 的对象
_cipher_factory: Callable[[bytes], Any] = _AesGcmCipher


def _new_cipher(key: bytes):
    return _cipher_factory(key)


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _scrypt_available() -> bool:
    """探测本机 OpenSSL 是否支持 scrypt（少数发行版未编译）。"""
    try:
        hashlib.scrypt(b"probe", salt=b"0" * SALT_SIZE, n=SCRYPT_N, r=SCRYPT_R,
                       p=SCRYPT_P, dklen=KEY_SIZE, maxmem=SCRYPT_MAXMEM)
        return True
    except (AttributeError, ValueError, MemoryError):
        return False


def _derive_scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    """严格按参数派生；失败必须报错，绝不静默换算法（否则解密端算出的 key 不同）。"""
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=KEY_SIZE, maxmem=SCRYPT_MAXMEM)


def _derive_pbkdf2(password: str, salt: bytes,
                   iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               int(iterations), dklen=KEY_SIZE)


def _kdf_params() -> Dict[str, Any]:
    """决定本次使用的 KDF，并把参数写进头部（解密端按头部复算，不靠猜）。"""
    if _scrypt_available():
        return {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P}
    return {"name": "pbkdf2-sha256", "iterations": PBKDF2_ITERATIONS}


def _derive(password: str, salt: bytes, kdf: Dict[str, Any]) -> bytes:
    if not password:
        raise CryptoError("压缩包密码不能为空")
    name = (kdf or {}).get("name", "scrypt")
    if name == "scrypt":
        try:
            return _derive_scrypt(password, salt, int(kdf.get("n") or SCRYPT_N),
                                  int(kdf.get("r") or SCRYPT_R), int(kdf.get("p") or SCRYPT_P))
        except (AttributeError, ValueError, MemoryError):
            raise CryptoError("当前 OpenSSL 不支持 scrypt，无法解开该备份包") from None
    if name == "pbkdf2-sha256":
        return _derive_pbkdf2(password, salt, int(kdf.get("iterations") or PBKDF2_ITERATIONS))
    raise CryptoError(f"不支持的密钥派生算法：{name}")


def encrypt_file(src: str, dst: str, password: str,
                 name: Optional[str] = None,
                 chunk_size: int = CHUNK_SIZE) -> Dict[str, Any]:
    """把 ``src`` 流式加密为 ``dst``，返回头部元信息（不含密钥/口令）。"""
    if not password:
        raise CryptoError("压缩包密码不能为空")
    _require_crypto()
    chunk_size = max(64 * 1024, int(chunk_size))
    salt = os.urandom(SALT_SIZE)
    kdf = _kdf_params()
    key = _derive(password, salt, kdf)
    cipher = _new_cipher(key)

    header = {
        "v": FORMAT_VERSION,
        "cipher": "aes-256-gcm",
        "kdf": kdf,
        "chunk": chunk_size,
        "salt": _b64e(salt),
        "name": name or os.path.basename(src),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    written = 0
    blocks = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC)
        fout.write(json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n")
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            nonce = os.urandom(NONCE_SIZE)
            ciphertext = cipher.encrypt(nonce, chunk)
            fout.write(nonce)
            fout.write(struct.pack(">I", len(ciphertext)))
            fout.write(ciphertext)
            written += len(ciphertext)
            blocks += 1
    meta = dict(header)
    meta.pop("salt", None)
    meta.update({
        "encrypted": True,
        "cipher_bytes": written,
        "plain_bytes": os.path.getsize(src),
        "blocks": blocks,
    })
    return meta


def read_header(src: str) -> Dict[str, Any]:
    """只读头部（不解密），用于 UI 展示与格式识别。"""
    with open(src, "rb") as fh:
        if fh.read(len(MAGIC)) != MAGIC:
            raise CryptoError("不是本工具的加密备份包（魔数不匹配）")
        line = fh.readline(64 * 1024)
    if not line.endswith(b"\n"):
        raise CryptoError("加密包头部不完整（文件可能被截断）")
    try:
        return json.loads(line.decode("utf-8"))
    except Exception:
        raise CryptoError("加密包头部不是合法 JSON，文件可能已损坏") from None


def decrypt_file(src: str, dst: str, password: str) -> Dict[str, Any]:
    """把 ``src`` 解密为 ``dst``；口令错误/被篡改时抛 CryptoError。"""
    if not password:
        raise CryptoError("该备份包已加密，请提供压缩包密码")
    _require_crypto()
    header = read_header(src)
    if int(header.get("v") or 0) != FORMAT_VERSION:
        raise CryptoError(f"不支持的加密包版本：{header.get('v')}")
    salt = _b64d(header.get("salt", ""))
    key = _derive(password, salt, header.get("kdf") or {})
    cipher = _new_cipher(key)

    written = 0
    blocks = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fin.read(len(MAGIC))
        fin.readline(64 * 1024)  # 跳过头部
        while True:
            nonce = fin.read(NONCE_SIZE)
            if not nonce:
                break
            if len(nonce) < NONCE_SIZE:
                raise CryptoError("加密包被截断（缺少 nonce）")
            raw_len = fin.read(4)
            if len(raw_len) < 4:
                raise CryptoError("加密包被截断（缺少块长度）")
            (length,) = struct.unpack(">I", raw_len)
            payload = fin.read(length)
            if len(payload) < length:
                raise CryptoError("加密包被截断（数据块不完整）")
            try:
                plain = cipher.decrypt(nonce, payload)
            except CryptoError:
                raise
            except Exception:
                raise CryptoError("密码错误，或备份包已损坏（GCM 校验失败）") from None
            fout.write(plain)
            written += len(plain)
            blocks += 1
    return {"plain_bytes": written, "blocks": blocks, "header": header}


def is_encrypted(path: str) -> bool:
    """按魔数判断是否为加密包（不读整个文件）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="modules.objstore.crypto",
                                     description="DBCheck 备份包口令加密工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="查看加密包头部信息")
    p_info.add_argument("src")

    p_enc = sub.add_parser("encrypt", help="加密 tar.gz")
    p_enc.add_argument("src")
    p_enc.add_argument("dst", nargs="?")
    p_enc.add_argument("-p", "--password", default="")

    p_dec = sub.add_parser("decrypt", help="解密为 tar.gz")
    p_dec.add_argument("src")
    p_dec.add_argument("dst", nargs="?")
    p_dec.add_argument("-p", "--password", default="")

    args = parser.parse_args(argv)

    if args.cmd == "info":
        header = read_header(args.src)
        header.pop("salt", None)
        print(json.dumps(header, ensure_ascii=False, indent=2))
        return 0

    if not available():
        print("当前环境缺少 cryptography，无法执行加解密", file=sys.stderr)
        return 2

    password = args.password or getpass.getpass("压缩包密码: ")
    if args.cmd == "encrypt":
        dst = args.dst or (args.src + ".enc")
        meta = encrypt_file(args.src, dst, password)
        print(f"已加密 → {dst}（{meta['cipher_bytes']} 字节，{meta['blocks']} 块）")
        return 0

    dst = args.dst or (args.src[:-4] if args.src.endswith(".enc") else args.src + ".dec")
    try:
        meta = decrypt_file(args.src, dst, password)
    except CryptoError as exc:
        print(f"解密失败：{exc}", file=sys.stderr)
        return 1
    print(f"已解密 → {dst}（{meta['plain_bytes']} 字节，{meta['blocks']} 块）")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(_main())
