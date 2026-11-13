# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""System information and offline authorization records.

The authorization code is a compact, signed payload bound to the local device
serial. It is intended for offline registration and display; it does not replace
a signed commercial contract.
"""
from flask import Blueprint

bp = Blueprint("system_info", __name__, url_prefix="/api/system-info")

from . import routes  # noqa: E402,F401
