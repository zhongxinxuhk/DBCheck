# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""AI 助手「任务类操作」的人工授权闸门。

为什么要有这个模块
------------------
巡检任务、智能诊断、工作流编排都会**真实连库并产生读写负载**。
此前用户只要说一句「帮我巡检 MySQL-01」，后端匹配到唯一数据源就直接开跑，
用户既没看到将要做什么，也没有拒绝的机会。

本模块把「意图」与「执行」拆成两步：

1. 命中任务类意图 → 不执行，只登记一张**授权票据**，把
   「要对谁、做什么、做多久、影响面」摊给用户看；
2. 用户点「确认执行」→ 前端携带票据重发原请求 → 校验通过才真正执行；
   用户点「取消」→ 直接返回，一条 SQL 都不会发出去。

安全要点
--------
- **执行计划指纹绑定**：票据记录 ``{kind, instance_id, scope, ...}`` 的 SHA-256。
  放行时重新计算比对，计划只要有一个字段不同就拒绝——
  防止「批准的是查连接数、实际执行的是全库重负载巡检」。
- **一次性**：票据用后即废。
- **有时效**：默认 5 分钟。任务授权比外发授权更短，避免挂着的历史授权被复放。
- **按会话隔离**：票据只对登记的 session 有效。
- **无静默放行**：本模块不提供 any bypass 参数；要执行只能走 decide→consume。
"""


import hashlib
import json
import threading
import time
import uuid

# 票据有效期（秒）：任务授权比外发授权更短
DEFAULT_TTL = 300

_lock = threading.Lock()
# {(session_id, ticket_id): {kind, action, target, detail, fingerprint,
#                            plan, created, ttl, state}}
_TICKETS = {}

# 已登记但尚未消费的票据（供前端重放时查询）
KIND_LABELS = {
    'inspect': '巡检任务',
    'diagnose': '智能诊断',
    'workflow': '工作流执行',
}


def fingerprint(plan) -> str:
    """计算执行计划指纹：按键排序序列化，保证同计划同指纹。"""
    if isinstance(plan, str):
        raw = plan
    else:
        raw = json.dumps(plan or {}, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _purge_expired(now=None):
    now = now or time.time()
    dead = [k for k, v in _TICKETS.items() if now - v['created'] > v['ttl']]
    for k in dead:
        _TICKETS.pop(k, None)


class TaskApprovalRequired(Exception):
    """任务类操作需用户授权。携带可直接渲染到聊天框的确认卡信息。"""

    def __init__(self, kind, action, target, detail, plan, ticket_id,
                 expires_in=DEFAULT_TTL, impact=None):
        self.kind = kind
        self.action = action
        self.target = target
        self.detail = detail
        self.plan = plan or {}
        self.fingerprint = fingerprint(self.plan)
        self.ticket_id = ticket_id
        self.expires_in = expires_in
        self.impact = impact or []
        super().__init__('任务「%s」需用户授权后方可执行' % action)

    def to_dict(self):
        return {
            'type': 'task_approval_required',
            'ticket_id': self.ticket_id,
            'kind': self.kind,
            'action': self.action,
            'target': self.target,
            'detail': self.detail,
            'impact': self.impact,
            'plan': self.plan,
            'fingerprint': self.fingerprint,
            'expires_in': self.expires_in,
        }


def register(session_id, kind, action, target, plan, detail='', impact=None,
             ttl=DEFAULT_TTL):
    """登记任务授权票据，返回 ``TaskApprovalRequired``（由调用方 yield / raise）。"""
    ticket_id = uuid.uuid4().hex
    with _lock:
        _purge_expired()
        _TICKETS[(session_id or '', ticket_id)] = {
            'kind': kind,
            'action': action,
            'target': target,
            'detail': detail,
            'fingerprint': fingerprint(plan),
            'plan': plan or {},
            'impact': impact or [],
            'created': time.time(),
            'ttl': ttl,
            'state': 'pending',
        }
    return TaskApprovalRequired(kind, action, target, detail, plan, ticket_id,
                                expires_in=ttl, impact=impact)


def decide(session_id, ticket_id, approved):
    """记录用户决定。返回 ``{ok, state, kind, action, target, reason}``。

    ``state``：``approved`` / ``denied`` / ``expired`` / ``not_found`` / ``used``
    """
    key = (session_id or '', ticket_id or '')
    with _lock:
        _purge_expired()
        item = _TICKETS.get(key)
        if not item:
            return {'ok': False, 'state': 'not_found', 'reason': '授权已失效（不存在或已过期），请重新发起任务'}
        if item['state'] == 'used':
            return {'ok': False, 'state': 'used', 'reason': '该授权已使用，不可重复执行'}
        if item['state'] == 'denied':
            return {'ok': False, 'state': 'denied', 'reason': '该任务已被取消'}
        item['state'] = 'approved' if approved else 'denied'
        return {
            'ok': True,
            'state': item['state'],
            'kind': item['kind'],
            'action': item['action'],
            'target': item['target'],
        }


def consume(session_id, ticket_id, plan=None):
    """执行前校验并作废票据。

    返回 ``(ok: bool, reason: str, item: dict|None)``。
    传入 ``plan`` 时校验指纹；不传则只校验状态（供前端重放时使用）。
    """
    key = (session_id or '', ticket_id or '')
    with _lock:
        _purge_expired()
        item = _TICKETS.get(key)
        if not item:
            return False, '授权已失效（不存在或已过期），请重新发起任务', None
        if item['state'] != 'approved':
            return False, '任务未获授权（当前状态：%s）' % item['state'], None
        if plan is not None and fingerprint(plan) != item['fingerprint']:
            return False, '执行计划与授权时不一致，已拒绝执行', None
        item['state'] = 'used'
        return True, 'ok', dict(item)


def get(session_id, ticket_id):
    """查询票据状态（供前端二次确认 / 轮询）。"""
    with _lock:
        _purge_expired()
        item = _TICKETS.get((session_id or '', ticket_id or ''))
        if not item:
            return None
        return {
            'kind': item['kind'],
            'action': item['action'],
            'target': item['target'],
            'detail': item['detail'],
            'state': item['state'],
            'age': time.time() - item['created'],
            'ttl': item['ttl'],
        }


def clear_session(session_id):
    """会话结束时清理其票据（内存泄漏防护）。"""
    with _lock:
        for k in [k for k in _TICKETS if k[0] == (session_id or '')]:
            _TICKETS.pop(k, None)
