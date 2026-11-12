# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Business labels used consistently in customer service delivery reports."""


FIELD_LABELS = {
 'transport':'传输方式','oracle_client_dir':'Oracle客户端目录','oracle_config_dir':'Oracle网络配置目录','oracle_server_dn':'Oracle服务端证书DN',
 'configuration_id':'配置项编号','asset_name':'资产名称','db_type':'数据库类型','version':'数据库版本',
 'owner':'运维责任人','business':'业务用途','system':'系统归属','lifecycle':'运行状态',
 'host':'数据库地址','port':'端口','instance_id':'加密数据源引用','credential_reference':'加密凭据引用',
 'project_id':'项目编号','cluster_id':'集群编号','cluster_type':'集群类型','node_count':'节点数量',
 'node_names':'节点清单','architecture':'集群架构','cluster_network':'集群网络配置',
 'deployment':'部署方式','hardware':'硬件配置','software_version':'软件版本','online_date':'上线时间',
 'host_model':'主机型号','location':'设备位置','os_version':'操作系统版本','storage_platform':'存储平台',
 'database':'数据库名称','instance_name':'实例名称','charset':'字符集','file_size':'数据库文件大小',
 'archive_path':'归档路径','archive_mode':'归档模式','patch_level':'补丁版本','storage_mode':'存储模式',
 'backup_policy_id':'备份策略编号','backup_window':'备份窗口','backup_frequency':'备份频率',
 'backup_retention':'备份保存周期','disaster_recovery':'容灾状态','backup_method':'备份方式',
 'backup_content':'备份内容','backup_data_type':'备份数据类型','backup_owner':'备份责任人',
 'backup_policy_version':'备份策略版本','backup_change_reference':'备份策略变更记录',
 'tablespaces':'逻辑存储空间名称','storage_types':'逻辑存储类型','storage_sizes':'逻辑存储大小',
 'file_paths':'存储文件路径','autoextend':'自动扩展模式','usage_threshold':'使用率阈值',
 'accounts':'数据库账号','purposes':'账号业务用途','privileges':'具体权限','account_owners':'账号责任人',
 'privilege_change_reference':'权限变更轨迹','parameter_snapshot':'参数快照','parameter_version':'参数版本',
 'least_privilege_reference':'最小权限复核证据','tls_ca_file':'可信证书或钱包位置',
 'ssh_host_role':'SSH目标角色','core_files':'关键文件完整性基线','log_paths':'日志路径',
 'monitoring_enabled':'常态监控状态','schedule_seconds':'采集周期（秒）','customer_calendar':'客户工作日日历',
 'detected_at':'故障发现时间','responded_at':'响应时间','plan_at':'方案形成时间','intervened_at':'介入时间',
 'resolved_at':'恢复登记时间','escalated_at':'专家升级时间','onsite_at':'现场支撑登记时间',
 'priority':'故障等级','impact':'影响范围','root_cause':'问题根因','risk_assessment':'风险评估',
 'rollback_plan':'回滚方案','approval_reference':'审批记录','execution_reference':'执行记录',
 'verification_reference':'验证证据','validation_reference':'校验记录','review_reference':'复盘记录',
 'rpo_minutes':'RPO（分钟）','rto_minutes':'RTO（分钟）','deadline':'整改时限','action':'处置步骤',
 'preventive_actions':'预防措施','maintenance_window':'维护窗口','sql_fingerprint':'SQL指纹',
 'baseline_reference':'基线引用','after_metrics':'变更后指标','before_metrics':'变更前指标',
 'optimization_plan':'优化方案','backup_reference':'备份引用','backup_sha256':'备份SHA256',
 'isolated_target':'隔离恢复目标','period_start':'周期开始','period_end':'周期结束',
 'details':'服务明细','item_id':'需求项编号','created_at':'登记时间','state':'工作流状态','id':'记录编号',
}

FIELD_LABELS.update({'after_state': '变更后状态', 'alert_reference': '告警引用', 'architecture_plan': '架构优化方案', 'archive_reference': '归档引用', 'artifact_sha256': '工具文件SHA256', 'audit_reference': '审计引用', 'availability_analysis': '可用性分析', 'backup_restore_tool': '备份恢复工具', 'baseline_version': '基线版本', 'before_state': '变更前状态', 'before_value': '变更前取值', 'business_validation': '业务验证', 'calendar_owner': '工作日日历维护人', 'capacity_plan': '容量扩展方案', 'change_reference': '变更记录引用', 'change_type': '变更类型', 'checksum': '校验和', 'compatibility_reference': '兼容性验证引用', 'completed_at': '完成时间', 'component_version': '组件版本', 'contingency_plan': '应急预案', 'customer_contact': '客户联络人', 'data_archival_plan': '历史数据剥离计划', 'database_validation': '数据库验证', 'effective_at': '生效时间', 'environment_tool': '环境分析工具', 'evidence_reference': '证据引用', 'expected_result': '预期结果', 'expert': '升级处理专家', 'feasibility_reference': '可行性论证引用', 'file_scope': '文件范围', 'finding_reference': '问题发现引用', 'first_business_day': '首个工作日', 'frequency': '频次', 'growth_analysis': '数据增长分析', 'historical_analysis': '历史异常分析', 'holidays': '节假日', 'incident_analysis': '故障分析', 'incident_cases': '故障案例', 'incident_reference': '故障记录引用', 'index_reference': '索引引用', 'inventory_date': '盘点日期', 'investigation': '排查过程', 'isolated_validation_reference': '隔离验证引用', 'license_reference': '工具授权引用', 'lock_analysis': '锁分析', 'maintenance_analysis': '运维记录分析', 'maintenance_reference': '维护记录引用', 'metric_reference': '指标引用', 'notification_reference': '通知记录引用', 'notified_at': '通知时间', 'object_reference': '对象引用', 'observer': '观察人员', 'onsite_reference': '现场支撑引用', 'operations_standard': '运维规范方案', 'outcome': '处理结果', 'parameter_name': '参数名称', 'parameter_plan': '参数优化方案', 'performance_plan': '性能治理方案', 'planned_at': '计划时间', 'proposed_order': '拟定索引字段顺序', 'proposed_value': '拟定参数值', 'query_reference': '查询场景引用', 'read_write_split': '读写分离方案', 'reconciliation_result': '差异核对结论', 'recovery_tool': '损坏恢复工具', 'redundancy_analysis': '冗余分析', 'remediation_plan': '整改方案', 'replication_errors': '复制错误', 'replication_lag': '复制延迟', 'request_reference': '客户请求引用', 'resource_analysis': '资源分析', 'response_improvements': '响应流程改进', 'restore_finished': '恢复结束时间', 'restore_started': '恢复开始时间', 'retention_policy': '保存策略', 'risk_level': '风险级别', 'risk_reference': '风险记录引用', 'rollback_result': '回退结果', 'root_causes': '根因归纳', 'scenario': '验证场景', 'scope_reference': '范围依据', 'slow_sql_analysis': '慢SQL分析', 'source': '证据来源', 'sql_analysis_tool': 'SQL压力分析工具', 'support_plan': '支撑方案', 'symptom': '故障现象', 'target_scope': '目标范围', 'target_version': '目标版本', 'task_scope': '任务范围', 'timezone': '时区', 'tool_name': '工具名称', 'tool_version': '工具版本', 'topology': '拓扑信息', 'topology_after': '变更后拓扑', 'topology_before': '变更前拓扑', 'traffic_distribution': '负载分流方案', 'transaction_analysis': '事务分析', 'verified_at': '验证时间', 'working_days': '调休工作日', 'workload_reference': '业务负载依据', 'recorded_at': '记录时间', 'version': '版本'})
