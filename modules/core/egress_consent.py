# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""AI 助手外发数据的人工审批闸门（替代一刀切拦截）。

为什么要有这个模块
------------------
原先的 ``ensure_egress_allowed`` 是"要么放行、要么抛错"的二元策略：
目标主机不在白名单就直接报「数据安全策略阻断未批准的外部数据发送」，
连一句「你好」都发不出去，聊天功能整体不可用。

本模块把它改成**可审批的中间态**：

1. 命中未批准主机时，不抛致命错误，而是登记一张**审批票据**；
2. 把**将要外发的确切内容**（已脱敏）回显到聊天框，由用户决定；
3. 用户批准 → 放行这一次发送；用户拒绝 → 静默跳过，聊天继续可用。

安全要点
--------
- **载荷指纹绑定**：票据记录待发内容的 SHA-256。放行时重新计算并比对，
  内容只要有一个字节不同就拒绝——防止"批准的是无害内容、实际发出的是别的"。
- **一次性**：票据用后即废，不能复用于后续请求。
- **有时效**：默认 10 分钟过期，避免长期悬挂的授权。
- **按会话隔离**：票据只对登记的 session 有效，别的会话拿不到。
"""


import hashlib
import re
import threading
import time
import uuid

# 票据有效期（秒）
DEFAULT_TTL = 600
# 回显内容的最大字符数（超出截断，避免聊天框被大 prompt 冲爆）
PREVIEW_LIMIT = 4000

_lock = threading.Lock()
# {(session_id, ticket_id): {host, purpose, fingerprint, preview, created, ttl, state}}
_TICKETS = {}


def fingerprint(payload):
    """计算待发内容的指纹（UTF-8 原文，不做任何归一化）。"""
    return hashlib.sha256((payload or '').encode('utf-8')).hexdigest()


def _normalize(text):
    """仅用于**展示**的轻量压缩：连续空白折叠，便于阅读。"""
    return re.sub(r'[ \t]{2,}', '  ', (text or ''))


def build_preview(payload, limit=PREVIEW_LIMIT):
    """生成回显文本：超长时保留头尾，中间标注省略长度。"""
    text = _normalize(payload)
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return (text[:head]
            + '\n\n…（省略 %d 字符）…\n\n' % (len(text) - limit)
            + text[-tail:])


def _purge_expired(now=None):
    now = now or time.time()
    dead = [k for k, v in _TICKETS.items() if now - v['created'] > v['ttl']]
    for k in dead:
        _TICKETS.pop(k, None)


class EgressApprovalRequired(Exception):
    """需要用户批准才能外发。携带可直接渲染到聊天框的信息。"""

    def __init__(self, host, purpose, payload, ticket_id, expires_in=DEFAULT_TTL):
        self.host = host
        self.purpose = purpose
        self.payload = payload
        self.fingerprint = fingerprint(payload)
        self.ticket_id = ticket_id
        self.expires_in = expires_in
        self.preview = build_preview(payload)
        super().__init__('需要用户批准后方可外发（目标 %s）' % host)

    def to_dict(self):
        return {
            'type': 'approval_required',
            'ticket_id': self.ticket_id,
            'host': self.host,
            'purpose': self.purpose,
            'fingerprint': self.fingerprint,
            'bytes': len((self.payload or '').encode('utf-8')),
            'chars': len(self.payload or ''),
            'expires_in': self.expires_in,
            'preview': self.preview,
        }


def register(session_id, host, purpose, payload, ttl=DEFAULT_TTL):
    """登记审批票据，返回 ``EgressApprovalRequired`` 异常实例（由调用方抛出）。"""
    ticket_id = uuid.uuid4().hex
    with _lock:
        _purge_expired()
        _TICKETS[(session_id or '', ticket_id)] = {
            'host': host,
            'purpose': purpose,
            'fingerprint': fingerprint(payload),
            'preview': build_preview(payload),
            'created': time.time(),
            'ttl': ttl,
            'state': 'pending',
        }
    return EgressApprovalRequired(host, purpose, payload, ticket_id, expires_in=ttl)


def decide(session_id, ticket_id, approved):
    """记录用户决定。返回 ``{ok, state, host, purpose, reason}``。

    ``state`` 取值：``approved`` / ``denied`` / ``expired`` / ``not_found`` / ``used``
    """
    key = (session_id or '', ticket_id or '')
    with _lock:
        _purge_expired()
        item = _TICKETS.get(key)
        if not item:
            return {'ok': False, 'state': 'not_found', 'reason': '票据不存在或已过期'}
        if item['state'] == 'used':
            return {'ok': False, 'state': 'used', 'reason': '票据已使用，不可重复批准'}
        if item['state'] == 'denied':
            return {'ok': False, 'state': 'denied', 'reason': '该请求已被拒绝'}
        item['state'] = 'approved' if approved else 'denied'
        return {
            'ok': True,
            'state': item['state'],
            'host': item['host'],
            'purpose': item['purpose'],
            'fingerprint': item['fingerprint'],
        }


def consume(session_id, ticket_id, payload):
    """放行前校验并作废票据。指纹不符或状态不对一律拒绝。

    返回 ``(ok: bool, reason: str)``。
    """
    key = (session_id or '', ticket_id or '')
    with _lock:
        _purge_expired()
        item = _TICKETS.get(key)
        if not item:
            return False, '票据不存在或已过期'
        if item['state'] != 'approved':
            return False, '票据未获批准（当前状态：%s）' % item['state']
        if fingerprint(payload) != item['fingerprint']:
            return False, '待发内容与批准内容不一致，已拒绝外发'
        item['state'] = 'used'
        return True, 'ok'


def peek(session_id, ticket_id, payload):
    """只校验、不消费——供「发送前预检」使用。

    预检若直接 ``consume`` 会把一次性票据提前作废，导致真正发送时再次被拦，
    表现就是"批准了却永远等不到回答"。因此预检走本函数。
    """
    key = (session_id or '', ticket_id or '')
    with _lock:
        _purge_expired()
        item = _TICKETS.get(key)
        if not item:
            return False
        if item['state'] != 'approved':
            return False
        return fingerprint(payload) == item['fingerprint']


def get(session_id, ticket_id):
    """查询票据状态（供前端轮询或二次确认）。"""
    with _lock:
        _purge_expired()
        item = _TICKETS.get((session_id or '', ticket_id or ''))
        if not item:
            return None
        return {
            'host': item['host'],
            'purpose': item['purpose'],
            'state': item['state'],
            'preview': item['preview'],
            'age': time.time() - item['created'],
            'ttl': item['ttl'],
        }


def clear_session(session_id):
    """会话结束时清理其票据（内存泄漏防护）。"""
    with _lock:
        for k in [k for k in _TICKETS if k[0] == (session_id or '')]:
            _TICKETS.pop(k, None)
