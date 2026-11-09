# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
权限校验装饰器 - JWT 认证 + RBAC 权限校验
"""

# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck


from functools import wraps
from flask import request, g, jsonify, session
from modules.user_management.utils.jwt_util import decode_token
from modules.user_management.services.user_service import UserService
from modules.user_management.services.perm_service import PermService


def login_required(f):
    """验证用户是否登录（JWT Token 或 Flask session）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        principal = getattr(g,'security_principal',None)
        if principal is not None:
            g.current_user = {'user_id':principal.id,'username':principal.username,'roles':principal.roles}
            return f(*args, **kwargs)
        # 1. 先尝试 JWT Token
        token = request.headers.get(
            'Authorization', ''
        ).replace('Bearer ', '')
        if token:
            try:
                payload = decode_token(token)
                g.current_user = payload
                return f(*args, **kwargs)
            except ValueError:
                pass  # JWT 无效，继续尝试 session

        # 2. 再尝试 Flask session
        if session.get('user_id'):
            try:
                user_service = UserService()
                user = user_service.get_user(session['user_id'])
                if user:
                    g.current_user = {
                        'user_id': user['id'],
                        'username': user['username'],
                        'roles': [r['role_code'] for r in user.get('roles', [])]
                    }
                    return f(*args, **kwargs)
            except Exception:
                pass

        return jsonify({
            'code': 401,
            'msg': '未登录，请先登录'
        }), 401
    return decorated


def require_permission(menu_code: str):
    """
    验证当前用户对指定菜单是否有访问权限
    权限级别只有两种：0=无权限，1=有权限
    超级管理员(admin角色)自动跳过权限检查
    """
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated(*args, **kwargs):
            # 超级管理员跳过权限检查
            if 'admin' in g.current_user.get('roles', []):
                return f(*args, **kwargs)
            
            user_id = g.current_user['user_id']
            perm_service = PermService()
            actual_level = perm_service.get_user_menu_perm_level(
                user_id, menu_code
            )
            # 只有 0=无权限、1=有权限 两种
            if actual_level < 1:
                return jsonify({
                    'code': 403,
                    'msg': '权限不足：无访问权限'
                }), 403
            return f(*args, **kwargs)
        return decorated
    return decorator


def require_admin(f):
    """需要管理员权限（admin角色或system_manage菜单有权限）"""
    @wraps(f)
    @require_permission('system_manage')
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated


def asset_filter(f):
    """注入数据权限过滤：g.allowed_asset_ids"""
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        user_id = g.current_user['user_id']
        perm_service = PermService()
        # admin 角色不过滤
        if 'admin' in g.current_user.get('roles', []):
            g.allowed_asset_ids = None  # None 表示不限制
        else:
            g.allowed_asset_ids = perm_service.get_allowed_asset_ids(user_id)
        return f(*args, **kwargs)
    return decorated
