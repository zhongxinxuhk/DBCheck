# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

from modules.monitor import alert_notify


def test_pending_and_unsupported_samples_do_not_emit_recovery(monkeypatch):
    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    tracker = alert_notify.AlertTracker()
    notifications = []
    monkeypatch.setattr(alert_notify, '_cfg', lambda: {'enabled': True, 'min_level': 'warn'})
    monkeypatch.setattr(alert_notify.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(tracker, '_dispatch', lambda kind, iid, *_: notifications.append((kind, iid)))
    monkeypatch.setattr(tracker, '_dispatch_startup',
                        lambda snaps: notifications.append(('startup', [s['id'] for s in snaps])))

    for level in ('pending', 'down', 'down', 'pending', 'unsupported', 'ok', 'ok', 'warn'):
        tracker.update([{'id': 'a', 'status': level}])
    tracker.update([{'id': 'a', 'status': 'crit'}, {'id': 'b', 'status': 'down'}])
    tracker.update([{'id': 'b', 'status': 'down'}])

    assert notifications == [
        ('startup', ['a']), ('recover', 'a'), ('alert', 'a'), ('change', 'a'), ('alert', 'b'),
    ]
