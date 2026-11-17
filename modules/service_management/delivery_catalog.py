# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Stable delivery identifiers and purpose-specific workbook sections."""

DELIVERABLES = {
    'D01': ('全品类数据库资产底账清单', 'register', ['资产底账', '季度核对记录', '台账变更记录']),
    'D02': ('集群参数表', 'register', ['集群基本信息', '集群节点', '集群参数']),
    'D03': ('数据库参数表', 'register', ['数据库参数']),
    'D04': ('数据库基本信息表', 'register', ['数据库基本信息']),
    'D05': ('逻辑存储空间属性表', 'register', ['逻辑存储空间', '存储文件']),
    'D06': ('用户及权限表', 'register', ['用户账号', '角色成员', '权限明细']),
    'D07': ('备份策略表', 'register', ['备份策略', '策略变更']),
    'D08': ('数据库日运行监控表', 'week', ['日运行监控', '监控采集明细', '检查明细', '异常整改']),
    'D09': ('数据库月运行监控表', 'month', ['月运行汇总', '日运行监控', '监控采集明细', '专项巡检', '异常整改']),
    'D10': ('数据库健康检查表', 'month', ['健康评分', '健康检查项', '风险清单', '整改闭环']),
    'D11': ('数据库例行维护周报', 'week', ['维护计划与执行', '验证记录', '遗留问题']),
    'D12': ('数据库例行维护月报', 'month', ['维护计划与执行', '验证记录', '遗留问题']),
    'D13': ('数据库例行维护年报', 'year', ['年度运行概况', '维护计划与执行', '重大事项', '遗留问题']),
    'D14': ('数据库变更台账', 'event', ['变更记录', '审批执行', '前后对比', '回滚验证']),
    'D15': ('备份恢复演练报告', 'event', ['演练概况', '恢复步骤', '校验结果', 'RPO与RTO', '整改闭环']),
    'D16': ('系统软件故障记录表', 'event', ['故障记录', '故障时间线', '恢复验证', '预防措施']),
    'D17': ('专项故障复盘报告', 'event', ['影响分析', '故障时间线', '根因证据', '处置评估', '改进闭环']),
    'D18': ('数据库性能优化报告', 'month', ['性能观测', '基线对比', '瓶颈分析', '优化效果', '后续计划']),
    'D19': ('慢SQL治理台账', 'event', ['慢SQL', '治理记录', '效果验证']),
    'D20': ('索引优化清单', 'event', ['索引观测', '索引建议', '变更验证']),
    'D21': ('参数调优记录', 'event', ['参数调优', '负载与验证']),
    'D22': ('系统软件季度运行分析报告', 'quarter', ['季度运行概况', '资源趋势', '故障分析', '运维分析', '优化计划']),
    'D23': ('架构改造技术支撑文档', 'event', ['现状与目标', '方案评估', '容量测算', '实施回退', '验证结果']),
    'D24': ('巡检异常整改说明及闭环记录', 'event', ['问题清单', '整改过程', '复测与闭环']),
    'RUN': ('单次巡检结果', 'run', ['数据库基本信息', '数据库参数', '逻辑存储空间', '存储文件',
                                       '用户账号', '权限明细', '备份执行', '检查明细', '风险清单']),
}

COMBINATIONS = {
    'register': [f'D{i:02}' for i in range(1, 8)],
    'week': ['D08', 'D11', 'D24'],
    'month': ['D09', 'D10', 'D12', 'D18', 'D19', 'D20', 'D21', 'D24'],
    'quarter': ['D22', *[f'D{i:02}' for i in range(1, 8)], 'D24'],
    'year': ['D13', 'D16', 'D18', 'D24'],
}

WORKFLOW_KEYS = {
    'maintenance': {'maintenance_schedule', 'log_cleanup', 'retention_cleanup', 'object_repair',
                    'temporary_support', 'replication_support', 'inspection_schedule'},
    'change': {'asset_change', 'sql_change', 'platform_change', 'change_observation', 'release_window'},
    'drill': {'recovery_drill', 'restore_validation'},
    'incident': {'incident_lifecycle', 'incident_intake', 'incident_classification', 'incident_plan',
                 'incident_risk', 'incident_verification', 'incident_review', 'incident_sla', 'incident_escalation'},
    'performance': {'performance_plan', 'bottleneck', 'sql_optimization', 'index_change', 'parameter_change'},
    'architecture': {'architecture_change', 'improvement_plan'},
    'review': {'asset_review', 'quarterly_review', 'period_review'},
    'remediation': {'remediation', 'risk_register', 'high_risk', 'prevention'},
}


def choices():
    return [{'id': key, 'title': value[0], 'period': value[1], 'sheets': value[2]}
            for key, value in DELIVERABLES.items()]
