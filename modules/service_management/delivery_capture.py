# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Bridge existing inventory and completed inspections into report observations."""

from modules.core.data_security import redact
from .delivery_data import sources_from_context


def capture_inspection(task, context):
    from modules.access import get_principal
    from .report_sources import sources_for
    from .delivery_store import DeliveryStore

    instance_id = task.get('datasource_id')
    principal = get_principal(task.get('owner_user_id'))
    if not instance_id or principal is None or principal.tenant_id != task.get('owner_tenant_id'):
        return None
    identity = {'id': principal.id, 'tenant_id': principal.tenant_id, 'admin': principal.is_admin}
    records = sources_for(identity)
    records.get(identity, instance_id)
    sources = sources_from_context(context)
    sources['inspection_issues'] = {'status': 'collected', 'scope': 'selected_inspection_template',
                                    'rows': context.get('auto_analyze') or []}
    return DeliveryStore(records).observe(identity, instance_id, sources, run_id=task.get('id'))
