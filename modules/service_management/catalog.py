# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""Validated local requirements and independently verifiable service fields."""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


import json
import os
from pathlib import Path
from modules.core import paths

CATALOG_PATH = paths.DATA_DIR / 'service_management' / 'catalog.json'
DEVELOPMENT_CATALOG_PATH = paths.PROJECT_ROOT / 'notes' / 'private' / 'service_catalog.json'


def default_catalog_path():
    environment = os.environ.get('DBPULSE_ENV',os.environ.get('FLASK_ENV','development'))
    if not CATALOG_PATH.is_file() and environment=='development':
        return DEVELOPMENT_CATALOG_PATH
    return CATALOG_PATH
# Fields are evidence requirements, never executable commands or target SQL.
FIELDS = {
 'asset_scope': 'scope_reference inventory_date reconciliation_result',
 'asset_register': 'configuration_id asset_name db_type version lifecycle owner business system',
 'cluster_registry': 'project_id cluster_id cluster_type node_count node_names architecture cluster_network',
 'configuration_history': 'configuration_id baseline_version change_reference verified_at',
 'asset_identity': 'host port instance_id credential_reference',
 'asset_lifecycle': 'system business owner online_date lifecycle',
 'host_registry': 'deployment hardware software_version host_model location os_version storage_platform',
 'parameters': 'parameter_snapshot parameter_version change_reference',
 'database_info': 'database instance_name charset file_size archive_path archive_mode',
 'database_layout': 'patch_level storage_mode backup_policy_id disaster_recovery cluster_id',
 'tablespace_registry': 'tablespaces storage_types storage_sizes file_paths autoextend usage_threshold',
 'permission_registry': 'accounts purposes privileges account_owners privilege_change_reference',
 'backup_policy': 'backup_data_type backup_content backup_window backup_method backup_frequency',
 'backup_retention': 'backup_retention backup_owner backup_policy_version backup_change_reference',
 'asset_change': 'change_type before_state after_state change_reference effective_at',
 'asset_review': 'inventory_date scope_reference reconciliation_result audit_reference',
 'inspection_schedule': 'frequency period_start period_end planned_at completed_at archive_reference',
 'log_cleanup': 'file_scope retention_policy risk_assessment approval_reference execution_reference verification_reference rollback_plan',
 'business_calendar': 'timezone working_days holidays first_business_day calendar_owner',
 'remediation': 'finding_reference alert_reference owner deadline root_cause action verification_reference',
 'risk_register': 'risk_level root_cause remediation_plan owner deadline verification_reference',
 'prevention': 'incident_reference historical_analysis preventive_actions owner deadline',
 'object_repair': 'object_reference risk_assessment approval_reference rollback_plan execution_reference verification_reference',
 'bottleneck': 'metric_reference root_cause optimization_plan baseline_reference expected_result verification_reference',
 'restore_validation': 'backup_reference isolated_target checksum backup_sha256 restore_started restore_finished validation_reference rpo_minutes rto_minutes',
 'high_risk': 'risk_reference risk_level owner deadline remediation_plan approval_reference verification_reference',
 'maintenance_schedule': 'frequency period_start period_end task_scope owner completed_at verification_reference',
 'retention_cleanup': 'file_scope retention_policy risk_assessment approval_reference rollback_plan execution_reference verification_reference',
 'recovery_drill': 'backup_reference backup_sha256 isolated_target scenario approval_reference rollback_plan restore_started restore_finished validation_reference rpo_minutes rto_minutes',
 'sql_change': 'sql_fingerprint audit_reference risk_assessment approval_reference rollback_plan backup_reference execution_reference verification_reference',
 'release_window': 'lock_analysis transaction_analysis slow_sql_analysis maintenance_window approval_reference',
 'change_observation': 'change_reference before_metrics after_metrics rollback_result observer verification_reference',
 'platform_change': 'change_type target_version risk_assessment compatibility_reference approval_reference rollback_plan backup_reference execution_reference verification_reference',
 'temporary_support': 'request_reference target_scope owner support_plan outcome verification_reference',
 'replication_support': 'component_version topology replication_lag replication_errors maintenance_reference verification_reference',
 'incident_lifecycle': 'incident_reference detected_at source impact priority root_cause action resolved_at verification_reference review_reference',
 'incident_intake': 'incident_reference source detected_at evidence_reference',
 'incident_classification': 'incident_reference priority impact customer_contact notified_at notification_reference',
 'incident_plan': 'incident_reference evidence_reference root_cause action owner maintenance_window',
 'incident_risk': 'incident_reference risk_assessment contingency_plan rollback_plan approval_reference',
 'incident_verification': 'incident_reference business_validation database_validation verification_reference verified_at',
 'incident_review': 'incident_reference detected_at symptom investigation action resolved_at preventive_actions review_reference',
 'tool_register': 'recovery_tool sql_analysis_tool environment_tool backup_restore_tool license_reference owner',
 'tool_verification': 'tool_name tool_version artifact_sha256 license_reference isolated_validation_reference verified_at',
 'incident_sla': 'incident_reference priority detected_at responded_at plan_at intervened_at',
 'incident_escalation': 'incident_reference detected_at resolved_at escalated_at expert onsite_reference',
 'performance_plan': 'period_start period_end target_scope performance_plan data_archival_plan owner approval_reference',
 'sql_optimization': 'sql_fingerprint baseline_reference optimization_plan approval_reference after_metrics verification_reference',
 'index_change': 'index_reference query_reference redundancy_analysis proposed_order risk_assessment approval_reference rollback_plan verification_reference',
 'parameter_change': 'parameter_name before_value proposed_value workload_reference risk_assessment approval_reference rollback_plan verification_reference',
 'architecture_change': 'topology_before topology_after read_write_split traffic_distribution capacity_plan risk_assessment approval_reference rollback_plan verification_reference',
 'quarterly_review': 'period_start period_end availability_analysis resource_analysis incident_analysis maintenance_analysis optimization_plan owner',
 'period_review': 'period_start period_end incident_cases root_causes response_improvements owner deadline',
 'improvement_plan': 'growth_analysis parameter_plan architecture_plan capacity_plan operations_standard feasibility_reference owner',
}
FIELDS = {key: value.split() for key, value in FIELDS.items()}
INSPECTION_KEYS = {'availability','instance_sessions','alerts','backup_jobs','capacity_cluster','file_integrity','statistics','growth','health_assessment','process_health','deep_logs','file_status','objects','resource_parameters','performance','backup_trend','backup_integrity','slow_sql','indexes','tuning_parameters','operations_analysis'}


def load_catalog(path=None):
    path = Path(path or default_catalog_path())
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError('需求目录尚未导入或文件过大')
    catalog = json.loads(path.read_text(encoding='utf-8'))
    ids = set()
    for requirement in catalog['requirements']:
        for item in requirement['items']:
            if item['id'] in ids or item['key'] not in (FIELDS.keys() | INSPECTION_KEYS):
                raise ValueError('需求编号重复或类型未实现')
            ids.add(item['id'])
            item['fields'] = FIELDS.get(item['key'], [])
    return catalog


def all_items(catalog):
    return {item['id']: {**item, 'requirement_id': r['id'], 'module_id': r['module_id']}
            for r in catalog['requirements'] for item in r['items']}
