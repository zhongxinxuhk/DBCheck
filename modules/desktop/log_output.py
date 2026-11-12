# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Incremental UTF-8 desktop log output without holding server files open."""


import codecs
import os
import re
import threading
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class LogOutput:
    def __init__(self, path, chunk_size=64 * 1024):
        self.path = Path(path)
        self.chunk_size = chunk_size
        self.lock = threading.RLock()
        self.offset = 0
        self.identity = None
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def reset(self):
        with self.lock:
            self.identity = None
            self.offset = 0
            self.decoder.reset()

    def read(self):
        with self.lock:
            try:
                with self.path.open("rb") as file:
                    info = os.fstat(file.fileno())
                    identity = (info.st_dev, info.st_ino)
                    switched = self.identity is not None and (
                        self.identity != identity or info.st_size < self.offset
                    )
                    prefix = ""
                    if self.identity is None or switched:
                        self.decoder.reset()
                        self.offset = max(0, info.st_size - self.chunk_size)
                        if self.offset:
                            file.seek(self.offset)
                            file.readline()
                            self.offset = file.tell()
                        prefix = "[日志已切换]\n" if switched else ""
                    self.identity = identity
                    file.seek(self.offset)
                    content = file.read(self.chunk_size)
                    self.offset = file.tell()
            except FileNotFoundError:
                return ""
            text = self.decoder.decode(content)
            return prefix + ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
