# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Opt-in collection scheduling with per-asset database leases and fresh ACLs."""



from datetime import datetime,timedelta,timezone
from .service import run_inspection


def tick(store,catalog,principal_resolver,instance_resolver,now=None):
    now = now or datetime.now(timezone.utc)
    completed = 0
    for record in store.scheduled_assets():
        if record['tenant'] != int(catalog.get('tenant_id',1)):
            continue
        asset = record['payload']
        if asset.get('lifecycle') not in {'active','maintenance'}:
            continue
        principal = principal_resolver(record['owner'])
        if principal is None or principal.tenant_id != record['tenant']:
            continue
        actor = {'id':principal.id,'tenant_id':principal.tenant_id,'admin':principal.is_admin}
        plan_periodic_services(store,catalog,actor,record,now)
        if not asset.get('monitoring_enabled') or not asset.get('instance_id'):
            continue
        seconds = int(asset.get('schedule_seconds',300))
        if not 60 <= seconds <= 86400:
            continue
        # Normalize to UTC for deterministic lexical comparisons in SQLite.
        due = (now + timedelta(seconds=seconds)).isoformat()
        if not store.claim_schedule(actor,record['id'],now.isoformat(),due):
            continue
        try:
            instance = instance_resolver(principal,asset['instance_id'])
            run_inspection(store,actor,record['id'],catalog,instance)
            completed += 1
        except Exception as error:
            from modules.core.data_security import redact
            store.save(actor,'inspection',{'status':'error','reason':redact(str(error),mask_identity=True)[:240],
                       'collected_at':now.isoformat(),'provenance':'scheduler'},asset_id=record['id'],item_id='scheduler-health')
    return completed


def start_scheduler(app):
    if app.extensions.get('service_management_scheduler'):
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(timezone='Asia/Shanghai')

    def collect_due():
        from .catalog import default_catalog_path,load_catalog
        if not default_catalog_path().is_file():
            return
        from .routes import store
        from modules.access import get_principal,visible_to,get_owner
        from modules.pro import get_instance_manager
        def resolve(principal,instance_id):
            if not visible_to(principal,get_owner('instance',instance_id)):
                raise PermissionError('定时巡检无目标数据源权限')
            return get_instance_manager().get_instance_decrypted(instance_id)
        with app.app_context():
            tick(store(),load_catalog(),get_principal,resolve)

    scheduler.add_job(collect_due,'interval',seconds=30,id='service-metadata-monitor',max_instances=1,coalesce=True)
    scheduler.start()
    app.extensions['service_management_scheduler']=scheduler


def plan_periodic_services(store,catalog,actor,asset_record,now):
    """Create one pending record per configured service period, without executing changes."""
    import hashlib
    from zoneinfo import ZoneInfo
    from .catalog import all_items,FIELDS
    from .service import period_bounds,first_business_day
    asset = asset_record['payload']
    if not asset.get('service_schedule_enabled'):
        return 0
    start_date = asset.get('service_start_date')
    if not start_date:
        raise ValueError('启用周期服务前须登记实际服务开始日期')
    items = all_items(catalog)
    created = 0
    for period,key in (('week','inspection_schedule'),('month','inspection_schedule'),
                       ('quarter','quarterly_review'),('year','maintenance_schedule')):
        start,end = period_bounds(period,now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat())
        if end.date().isoformat()<=start_date:
            continue
        item = next(v for v in items.values() if v['key']==key)
        identifier = hashlib.sha256((asset_record['id']+period+start.isoformat()).encode()).hexdigest()
        try:
            store.get(actor,identifier)
            continue
        except PermissionError:
            pass
        details = {field:'' for field in FIELDS[key]}
        details.update(frequency=period,period_start=start.isoformat(),period_end=end.isoformat(),owner=asset.get('owner',''))
        reason = '等待服务执行与独立复核'
        if period=='month':
            calendar = asset.get('customer_calendar') or {}
            try:
                details['scheduled_service_day'] = first_business_day(start.year,start.month,calendar.get('holidays'),calendar.get('working_days'))
            except ValueError:
                reason = '客户工作日日历尚未配置'
        body = {'title':period+' '+item['title'],'details':details,'module_id':item['module_id'],
                'asset_version':asset_record['version'],
                'risk_assessment':'周期计划仅登记服务范围，实际维护须使用批准方案',
                'rollback_plan':'采集异常终止连接，生产变更依据独立回滚方案',
                'provenance':'periodic_service_plan','reason':reason}
        try:
            store.save(actor,'workflow',body,record_id=identifier,asset_id=asset_record['id'],item_id=item['id'])
            created += 1
        except ValueError:
            # Another process may have committed the same deterministic plan.
            store.get(actor,identifier)
    return created
