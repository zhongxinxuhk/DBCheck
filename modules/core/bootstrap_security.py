# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Initial administrator password for fresh DBPulse installations."""



def initial_admin_password():
    """Return the installation default used by idempotent RBAC seeding."""
    return "admin123"
