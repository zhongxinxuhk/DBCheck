# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
SSH 隧道工具模块 v3
提供本地端口转发功能（Paramiko direct-tcpip 通道），供「SSH 跳板 + 数据库」场景使用。

设计要点
--------
1. 数据库主机填写 127.0.0.1 / localhost 时，**不能**用本机回环直连——那是巡检机
   自己的 127.0.0.1（Navicat 之所以能连，是它自己做了 SSH 隧道）。本模块把
   「SSH 主机的 127.0.0.1:远程端口」映射到「巡检机的 127.0.0.1:随机端口」。
2. 未知主机密钥采用 WarningPolicy：全新机器没有 known_hosts 条目时，
   RejectPolicy 会直接失败（Server '...' not found in known_hosts），
   而巡检工具面对的多是首次接入的客户环境。
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck




import socket
import sys
import threading
import time
import select

# 视作「本机回环」的主机名：需要通过 SSH 主机侧解析，而非巡检机自己
LOOPBACK_HOSTS = {'127.0.0.1', 'localhost', '::1', '0.0.0.0', '', 'host.docker.internal'}


def build_ssh_client(known_hosts_file: str = ''):
    """构造一个 SSHClient（未知主机密钥仅告警，不拒绝）。

    原实现统一用 RejectPolicy，导致两类现场失败：
      - 首次接入的服务器（known_hosts 无条目）→ SSHException: not found in known_hosts
      - 容器/CI 里没有 ~/.ssh/known_hosts → 一律失败
    WarningPolicy 保留「可疑指纹告警」的能力，同时允许首次接入。
    """
    import paramiko

    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
    except Exception:
        pass
    if known_hosts_file:
        try:
            client.load_host_keys(known_hosts_file)
        except Exception:
            pass
    client.set_missing_host_key_policy(paramiko.WarningPolicy())
    return client


def _ssh_connect_kwargs(ssh_info: dict) -> dict:
    """把 db_info / ssh_info 里的 SSH 字段整理成 paramiko.connect 参数。"""
    kwargs = {
        'hostname': (ssh_info.get('ssh_host') or ssh_info.get('host') or '').strip(),
        'port': int(ssh_info.get('ssh_port') or ssh_info.get('port') or 22),
        'username': (ssh_info.get('ssh_user') or ssh_info.get('user') or 'root').strip(),
        'timeout': int(ssh_info.get('ssh_timeout') or 15),
        'banner_timeout': 30,   # 云端 sshd 偶发「Error reading SSH protocol banner」
        'auth_timeout': 30,
        'look_for_keys': False,
        'allow_agent': False,
        'disabled_algorithms': {'pubkeys': ['ssh-rsa']},
    }
    key_file = (ssh_info.get('ssh_key_file') or ssh_info.get('key_file') or '').strip()
    if key_file:
        from modules.core.paths import resolve_portable_file
        kwargs['key_filename'] = str(resolve_portable_file(key_file))
        key_pwd = ssh_info.get('ssh_key_password') or ''
        if key_pwd:
            kwargs['passphrase'] = key_pwd
    else:
        kwargs['password'] = ssh_info.get('ssh_password') or ssh_info.get('password') or ''
    return kwargs


def open_db_tunnel(db_info: dict, log=print):
    """按 db_info 中的 SSH 配置建立到数据库的本地端口转发。

    Args:
        db_info: 同时包含数据库字段（ip/port）与 SSH 字段（ssh_host/ssh_port/...）
        log: 日志回调，默认 print

    Returns:
        (tunnel, local_host, local_port)；未配置 SSH 时返回 None。
        调用方负责 tunnel.close()。
    """
    ssh_host = (db_info.get('ssh_host') or '').strip()
    if not ssh_host:
        return None

    remote_host = (db_info.get('ip') or '').strip() or '127.0.0.1'
    remote_port = int(db_info.get('port') or 0)
    tunnel = SSHTunnel(
        ssh_host,
        int(db_info.get('ssh_port') or 22),
        (db_info.get('ssh_user') or 'root').strip(),
        ssh_password=db_info.get('ssh_password') or '',
        ssh_key=db_info.get('ssh_key_file') or '',
        remote_host=remote_host,
        remote_port=remote_port,
        ssh_key_password=db_info.get('ssh_key_password') or '',
    )
    tunnel.__enter__()
    log(f'  🔗 SSH 隧道 {ssh_host} → {remote_host}:{remote_port} '
        f'已映射到 127.0.0.1:{tunnel.local_port}')
    return tunnel, '127.0.0.1', tunnel.local_port


# ── 隧道缓存：跨功能复用同一条 SSH 连接 ────────────────────────
# 监控采集每 10s 一轮、大屏采样每 15s 一轮、数据源测试随时点击。若每次都
# 新建 SSHTunnel，会瞬间打满目标 sshd 的 MaxStartups（metrics_collector 已因
# 同类问题加过槽位锁）。故按「SSH 端点 + 数据库端点」做键缓存，复用同一条
# 隧道；transport 失效时自动重建。
_tunnel_cache = {}
_tunnel_lock = threading.Lock()


def tunnel_enabled(db_info: dict) -> bool:
    """该配置是否需要「数据库连接经 SSH 隧道」（与 UI 开关同口径）。"""
    return bool((db_info.get('ssh_host') or '').strip()
                and db_info.get('ssh_tunnel'))


def _tunnel_key(db_info: dict):
    return (
        (db_info.get('ssh_host') or '').strip(),
        int(db_info.get('ssh_port') or 22),
        (db_info.get('ssh_user') or 'root').strip(),
        (db_info.get('ip') or db_info.get('host') or '').strip() or '127.0.0.1',
        int(db_info.get('port') or 0),
    )


def resolve_db_endpoint(db_info: dict, log=None):
    """把 (host, port) 解析成实际可连的端点，必要时建立/复用 SSH 隧道。

    这是「数据库连接层」的唯一入口：巡检、连接测试、实时监控、监控大屏、
    JDBC 采集子进程都应经过本函数，避免各功能各自直连 host/port，导致
    同一实例「巡检正常、大屏宕机」的分裂现象。

    Args:
        db_info: 含 ip/host、port 及 SSH 字段（ssh_host/ssh_port/ssh_user/
                 ssh_password/ssh_key_file/ssh_key_password/ssh_tunnel）的字典
        log: 可选日志回调

    Returns:
        (host, port, tunnel)：未启用隧道时为入参 host/port 与 None；
        启用时为 ('127.0.0.1', 本地转发端口, SSHTunnel 实例)。

    语义说明：隧道映射「SSH 主机侧的 remote_host:remote_port」。数据库填
    127.0.0.1 时 remote_host 即 127.0.0.1 —— 指 SSH 服务器自己的回环，
    正是堡垒机 / 仅监听回环场景的正确语义。
    """
    host = (db_info.get('ip') or db_info.get('host') or '').strip()
    try:
        port = int(db_info.get('port') or 0)
    except (TypeError, ValueError):
        port = 0

    if not tunnel_enabled(db_info) or not host or port <= 0:
        return host, port, None

    key = _tunnel_key(db_info)
    with _tunnel_lock:
        tunnel = _tunnel_cache.get(key)
        if tunnel is not None:
            try:
                tr = tunnel._transport
                if tr is not None and tr.is_active() and tunnel.local_port:
                    return '127.0.0.1', int(tunnel.local_port), tunnel
            except Exception:
                pass
            try:
                tunnel.close()
            except Exception:
                pass
            _tunnel_cache.pop(key, None)

        tunnel = SSHTunnel(
            key[0], key[1], key[2],
            ssh_password=db_info.get('ssh_password') or '',
            ssh_key=(db_info.get('ssh_key_file') or db_info.get('ssh_key') or ''),
            remote_host=key[3], remote_port=key[4],
            ssh_key_password=db_info.get('ssh_key_password') or '',
        )
        tunnel.__enter__()
        _tunnel_cache[key] = tunnel
        if log:
            try:
                log(f'  🔗 SSH 隧道就绪: 127.0.0.1:{tunnel.local_port} '
                    f'→ {key[3]}:{key[4]}（经 {key[0]}:{key[1]}）')
            except Exception:
                pass
        return '127.0.0.1', int(tunnel.local_port), tunnel


def apply_tunnel(db_info: dict, log=None):
    """返回把端点替换为隧道本地转发端点的 db_info 副本。

    面向「只拿到一个 dict、随后到处用 db_info.get('host') / .get('port')」
    的调用点（Web 端点多为此形态）。相比逐处替换局部变量，直接换掉字典
    端点更不容易漏改分支。

    同时把 ssh_tunnel 置 False，避免下游 modules.db_types.compatibility
    再次建隧道（sqlserver / gbase8a 会自建）造成双重端口转发。

    隧道建立失败时原样返回，让上层的连接错误如实暴露。
    """
    if not db_info or not tunnel_enabled(db_info):
        return db_info
    try:
        host, port, tunnel = resolve_db_endpoint(db_info, log=log)
    except Exception as e:
        # 关键：不能静默回退。隧道建不起来时，下游会拿原始 127.0.0.1 直连并
        # 报「Connection refused」，把「SSH 认证/网络失败」伪装成「数据库没启
        # 动」——正是本次要消除的误导。此处显式告警，控制台能看到真实原因。
        print('[ssh] 隧道建立失败，回退直连（真实原因如下）: %s: %s'
              % (type(e).__name__, e), file=sys.stderr, flush=True)
        return db_info
    if tunnel is None or not host or not port:
        return db_info
    out = dict(db_info)
    out['ip'] = host
    out['host'] = host
    out['port'] = port
    # 端点已是隧道本地端口，下游不得再建隧道
    out['ssh_tunnel'] = False
    return out


def close_all_tunnels():
    """关闭全部缓存隧道（进程退出 / 配置变更时调用）。"""
    with _tunnel_lock:
        for t in list(_tunnel_cache.values()):
            try:
                t.close()
            except Exception:
                pass
        _tunnel_cache.clear()


def create_local_port_forward(transport, remote_host: str, remote_port: int,
                             ssh_tunnel_obj=None):
    """
    创建 SSH 本地端口转发
    返回: (local_port, server_socket, server_thread)
    """
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(('127.0.0.1', 0))
    server_socket.listen(5)
    actual_local_port = server_socket.getsockname()[1]

    if ssh_tunnel_obj is not None:
        ssh_tunnel_obj._server_socket = server_socket

    def _forward_worker():
        while not ssh_tunnel_obj._stop_event.is_set():
            try:
                readable, _, _ = select.select([server_socket], [], [], 0.5)
                if not readable:
                    continue
                client_socket, addr = server_socket.accept()
            except OSError:
                break

            handler_thread = threading.Thread(
                target=_handle_connection,
                args=(client_socket, transport, remote_host, remote_port),
                daemon=True
            )
            handler_thread.start()

    def _handle_connection(client_socket, transport, remote_host, remote_port):
        channel = None
        try:
            channel = transport.open_channel(
                'direct-tcpip',
                (remote_host, remote_port),
                client_socket.getsockname()
            )
            if channel is None:
                raise Exception("Failed to open direct-tcpip channel")
            _bidirectional_forward(client_socket, channel)
        except Exception as e:
            print(f"SSH隧道连接处理失败: {e}")
        finally:
            try:
                client_socket.close()
            except:
                pass
            try:
                if channel:
                    channel.close()
            except:
                pass

    def _bidirectional_forward(sock1, sock2, chunk_size=4096):
        try:
            while True:
                readable, _, exceptional = select.select(
                    [sock1, sock2], [], [sock1, sock2], 1.0)
                if exceptional:
                    break
                if not readable:
                    if sock1.fileno() == -1 or sock2.fileno() == -1:
                        break
                    continue
                for sock in readable:
                    try:
                        data = sock.recv(chunk_size)
                        if not data:
                            return
                        peer = sock2 if sock == sock1 else sock1
                        peer.sendall(data)
                    except:
                        return
        except:
            pass

    server_thread = threading.Thread(target=_forward_worker, daemon=True)
    server_thread.start()
    time.sleep(0.1)

    return actual_local_port, server_socket, server_thread


class SSHTunnel:
    """
    SSH 隧道上下文管理器
    用法:
        with SSHTunnel(ssh_host, ssh_port, ...) as t:
            local_port = t.local_port
            # 使用 localhost:local_port 连接远程数据库
        # 退出 with 块时自动关闭隧道
    或使用 close() 手动关闭:
        t = SSHTunnel(...)
        t.__enter__()
        local_port = t.local_port
        # ... 使用隧道 ...
        t.close()
    """

    def __init__(self, ssh_host: str, ssh_port: int, ssh_user: str,
                 ssh_password: str = '', ssh_key: str = '',
                 remote_host: str = '', remote_port: int = 0,
                 ssh_key_password: str = ''):
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self.ssh_key = ssh_key
        self.ssh_key_password = ssh_key_password
        self.remote_host = remote_host
        self.remote_port = remote_port

        self._client = None
        self._transport = None
        self._local_port = None
        self._server_socket = None
        self._server_thread = None
        self._stop_event = threading.Event()

    def __enter__(self):
        self._client = build_ssh_client()

        connect_kwargs = _ssh_connect_kwargs({
            'ssh_host': self.ssh_host,
            'ssh_port': self.ssh_port,
            'ssh_user': self.ssh_user,
            'ssh_password': self.ssh_password,
            'ssh_key_file': self.ssh_key,
            'ssh_key_password': self.ssh_key_password,
        })

        self._client.connect(**connect_kwargs)
        self._transport = self._client.get_transport()

        self._local_port, self._server_socket, self._server_thread = \
            create_local_port_forward(
                self._transport,
                self.remote_host,
                self.remote_port,
                self
            )

        print(f"  🔗 SSH 隧道已建立: localhost:{self._local_port}"
              f" → {self.remote_host}:{self.remote_port}")
        return self

    @property
    def local_port(self):
        return self._local_port

    def close(self):
        """关闭 SSH 隧道，释放所有资源"""
        self._stop_event.set()
        if self._server_socket:
            try:
                self._server_socket.close()
            except:
                pass
            self._server_socket = None
        if self._client:
            try:
                self._client.close()
            except:
                pass
        self._client = None
        self._transport = None
        self._local_port = None
        self._server_thread = None

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


if __name__ == '__main__':
    print("SSH Tunnel Utils v2 - 单元测试")
    print("这个模块需要配合 Paramiko 使用")
    # 简单测试（需要真实的 SSH 服务器）
    # with SSHTunnel('example.com', 22, 'user', ssh_password='pass',
    #              remote_host='localhost', remote_port=1521) as t:
    #     print(f"Local port: {t.local_port}")
    #     time.sleep(5)
