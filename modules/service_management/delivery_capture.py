# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Bridge existing inventory and completed inspections into report observations."""

from modules.core.data_security import redact
from .delivery_data import sources_from_context


def capture_inspection(task, context):
    from modules.access import get_principal, get_owner, visible_to
    from modules.operations.routes import resources
    from .routes import store
    from .delivery_routes import archive

    instance_id = task.get('datasource_id')
    principal = get_principal(task.get('owner_user_id'))
    if not instance_id or principal is None or principal.tenant_id != task.get('owner_tenant_id'):
        return None
    if not visible_to(principal, get_owner('instance', instance_id)):
        raise PermissionError('采集完成时数据源权限已变化')
    identity = {'id': principal.id, 'tenant_id': principal.tenant_id, 'admin': principal.is_admin}
    services = store()
    assets = [a for a in services.list(identity, 'asset') if a['payload'].get('instance_id') == instance_id]
    if len(assets) > 1:
        raise ValueError('同一数据源绑定多个服务资产，请确认报告归属')
    if not assets:
        inventory, _, _ = resources()
        record = inventory.get(instance_id)
        source, metadata = record['source'], record['metadata']
        payload = {**metadata, 'instance_id': instance_id, 'configuration_id': record['configuration_id'],
                   'asset_name': source.get('name') or instance_id, 'db_type': source['db_type'],
                   'host': source.get('host'), 'port': source.get('port'),
                   'lifecycle': metadata.get('lifecycle') or 'active',
                   'provenance': 'operations_inventory', 'inventory_revision': record['revision']}
        identity_id = 'inventory-' + str(principal.tenant_id) + '-' + instance_id
        try:
            asset = services.save(identity, 'asset', redact(payload), record_id=identity_id)
        except ValueError:
            asset = services.get(identity, identity_id)
    else:
        asset = assets[0]
    sources = sources_from_context(context)
    sources['inspection_issues'] = {'status': 'collected', 'scope': 'selected_inspection_template',
                                    'rows': context.get('auto_analyze') or []}
    observation = archive().observe(identity, asset['id'], sources, run_id=task['id'] if task.get('id') else None)
    from flask import current_app
    from .catalog import all_items, load_catalog
    from .delivery_store import digest
    try:
        requirements = load_catalog(current_app.config.get('SERVICE_CATALOG_PATH'))
        remediation = next((item['id'] for item in all_items(requirements).values() if item['key'] == 'remediation'), None) \
            if int(requirements.get('tenant_id', 1)) == identity['tenant_id'] else None
    except ValueError:
        remediation = None
    if remediation:
        for issue in context.get('auto_analyze') or []:
            check_id = 'native-' + digest(str(issue.get('col1') or issue.get('title') or issue).encode())[:24]
            services.record_finding(identity, asset['id'], check_id, observation['id'], observation['id'], remediation)
    return observation
