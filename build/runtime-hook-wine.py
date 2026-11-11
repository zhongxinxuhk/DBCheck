# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Use CPython's native version fallback when checking builds under Wine."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

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
