# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Bridge existing inventory and completed inspections into report observations."""

from modules.core.data_security import redact
from .delivery_data import sources_from_context
from pathlib import Path


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
    archive=DeliveryStore(records)
    observation=archive.observe(identity,instance_id,sources,run_id=task.get('id'))
    if task.get('report_path'):
        from modules.core import paths
        from .delivery_store import digest
        report=Path(task['report_path']).resolve()
        if report.parent != paths.REPORTS_DIR.resolve() or not report.is_file():
            raise ValueError('巡检报告路径不在报告目录')
        archive.link_inspection_report(identity,report.name,observation,records.get(identity,instance_id),digest(report.read_bytes()))
    return observation
