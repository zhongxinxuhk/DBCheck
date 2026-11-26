# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Register the Dameng client DLL directories in the frozen Windows build.

The PyPI ``dmpython`` wheel keeps the Dameng client DLLs (``dm*.dll``) beside
``dmPython.pyd`` and the OpenSSL / VC runtimes in ``dmssl/``.  After freezing,
both live under ``_internal``.  ``dmPython`` is imported lazily by the Dameng
driver code rather than at start-up, so the directories are registered here
instead of relying on the process DLL search path.
"""

import os
import sys


def _register():
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    for directory in (base, os.path.join(base, "dmssl")):
        if os.path.isdir(directory):
            os.add_dll_directory(directory)


if os.name == "nt":
    _register()
