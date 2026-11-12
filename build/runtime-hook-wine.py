# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Use CPython's native version fallback when checking builds under Wine."""


import os

if os.name == "nt" and os.environ.get("DBPULSE_WINE_BUILD") == "1":
    import ctypes
    import platform

    try:
        ctypes.windll.ntdll.wine_get_version
    except AttributeError:
        pass
    else:

        def _unavailable_wmi(*args, **kwargs):
            raise OSError("WMI unavailable in Wine build host")

        platform._wmi_query = _unavailable_wmi
        import _winapi

        if hasattr(_winapi, "CopyFile2"):
            del _winapi.CopyFile2
