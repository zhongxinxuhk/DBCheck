# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""Shared authentication, browser CSRF and report disclosure boundary."""



import hmac
import os
import re
import secrets
from flask import g, jsonify, redirect, request, session

PUBLIC = {'/um/login','/api/auth/login','/api/auth/status','/api/um/auth/login',
          '/api/i18n','/version.json','/api/v1/health'}
LOGIN = {'/api/auth/login','/api/um/auth/login'}
SELF_WRITE = {'/api/auth/logout','/api/um/auth/logout','/api/auth/change-password',
              '/api/auth/update-profile'}
REPORT_PREFIXES = ('/api/reports','/api/report_detail/','/api/download','/api/delete_report',
                   '/api/share','/share/','/api/db_inspect_share','/api/server_inspect_share')

# 监控大屏投屏场景：页面与只读数据接口允许匿名访问。
# 严格限制 GET + 白名单路径；阈值保存等写接口、其它 API 仍需登录与 CSRF。
# 设 DBPULSE_PUBLIC_MONITOR=0 可一键恢复全局登录要求。
MONITOR_PUBLIC_PATHS = {'/monitor-screen',
                        '/api/dashboard/overview',
                        '/api/dashboard/host-series',
                        '/api/dashboard/thresholds',
                        '/api/dashboard/history/overview'}
MONITOR_PUBLIC_PREFIXES = ('/api/dashboard/instance/',)


def monitor_public_enabled():
    return os.environ.get('DBPULSE_PUBLIC_MONITOR', '1') != '0'


def current_principal():
    from modules.access import get_principal, principal_from_api_key
    principal = None
    bearer = request.headers.get('Authorization','')
    if bearer.startswith('Bearer '):
        try:
            from modules.user_management.utils.jwt_util import decode_token
            candidate = get_principal(decode_token(bearer[7:]).get('user_id'))
            session_uid = session.get('user_id')
            if (candidate is not None and session_uid is not None
                    and str(session_uid) != str(candidate.id)):
                # 浏览器 Cookie 里的会话与 Authorization 里的 Bearer 指向不同账户。
                # 最常见的成因：另一个标签页/窗口后来登录了别的账户——会话 Cookie 是
                # 整站共享的，最后一次登录会覆盖前一次——或者 localStorage 里残留了旧
                # token。此时以签名 Cookie 的会话身份为准：它是用户此刻的真实登录态，
                # 权限也更低；绝不采纳 Bearer，否则残留 token 会变相提权。标记本次请求，
                # 由前端清掉过期 token 并提示身份已变更。
                g.auth_identity_adjusted = True
            elif candidate is not None:
                principal = candidate
                g.independent_auth = True
        except Exception:
            pass
    if principal is None and request.headers.get('X-API-Key'):
        principal = principal_from_api_key(request.headers['X-API-Key'])
        if principal is not None:
            g.independent_auth = True
    if principal is None:
        principal = get_principal(session.get('user_id'))
    return principal


def install_security(app):
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      MAX_CONTENT_LENGTH=8 * 1024 * 1024)

    @app.before_request
    def data_security_boundary():
        if request.endpoint == 'static':
            return None
        # 匿名只读大屏：先于会话检查放行，但只认 GET 与显式白名单，
        # 不触碰写方法、CSRF 校验与其它业务接口。
        if (request.method == 'GET' and monitor_public_enabled()
                and (request.path in MONITOR_PUBLIC_PATHS
                     or request.path.startswith(MONITOR_PUBLIC_PREFIXES))):
            return None
        if request.path in PUBLIC:
            if request.path in LOGIN:
                origin = request.headers.get('Origin')
                if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
                    return jsonify(ok=False,error='登录请求来源不匹配'),403
            return None
        if request.method == 'OPTIONS':
            return '',204
        principal = current_principal()
        if principal is None or principal.is_anonymous:
            if request.path.startswith(('/api/','/socket.io/')):
                return jsonify(ok=False,error='请先登录',error_code='AUTH_REQUIRED'),401
            return redirect('/um/login')
        if session.get('user_id') is not None and str(session['user_id'])!=str(principal.id):
            return jsonify(ok=False,error='请求中的登录身份与当前会话不一致，请重新登录',
                           error_code='SESSION_IDENTITY_CHANGED'),401
        g.security_principal = principal
        # Historical flat report storage has no reliable per-file tenant owner.
        # Only the installation tenant administrator may access these old files.
        if request.path.startswith(REPORT_PREFIXES) and not (principal.is_admin and principal.tenant_id == 1):
            return jsonify(ok=False,error='历史报告仅安装租户管理员可访问'),403
        if request.path.startswith('/share/') or request.path.startswith('/api/share') or request.path.endswith('_share'):
            return jsonify(ok=False,error='数据安全策略关闭公开报告分享'),403
        if request.method in {'POST','PUT','PATCH','DELETE'}:
            independent_auth = getattr(g,'independent_auth',False)
            if not independent_auth:
                expected = session.get('_csrf','')
                actual = request.headers.get('X-CSRF-Token','')
                if not expected or not hmac.compare_digest(expected, actual):
                    return jsonify(ok=False,error='CSRF 校验失败'),403
            if request.path not in SELF_WRITE and not principal.is_admin and 'operator' not in principal.roles:
                return jsonify(ok=False,error='当前角色无变更权限'),403
        return None

    @app.after_request
    def disclosure_headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        # 本次请求的 Bearer 与 Cookie 会话指向不同账户，已按会话身份处理。
        # 用响应头告知前端清理 localStorage 中的残留 token，避免每轮请求都被降级。
        if getattr(g,'auth_identity_adjusted',False):
            response.headers['X-Auth-Identity-Adjusted']='1'
        # 系统管理、运维台账及监控大屏使用主界面的同源工作区。
        _um_embed = (request.path.startswith('/um/')
                     and request.args.get('embed') == '1')
        _operations_embed = request.path in {'/operations/inventory', '/monitor-screen'}
        response.headers['X-Frame-Options']='SAMEORIGIN' if (_um_embed or _operations_embed) else 'DENY'
        response.headers['Referrer-Policy']='same-origin'
        if request.path.startswith(('/api/','/services','/operations/','/share/')):
            response.headers['Cache-Control']='no-store, private'
        if response.mimetype == 'text/html' and not response.is_streamed and request.path!='/api/services/report':
            token = session.setdefault('_csrf',secrets.token_hex(32))
            injection = '<meta name="csrf-token" content="'+token+'"><script src="/static/js/data_security.js"></script>'
            text = response.get_data(as_text=True)
            text = re.sub(r'(<head\b[^>]*>)',lambda m:m.group(1)+injection,text,count=1,flags=re.I)
            response.set_data(text)
        return response
