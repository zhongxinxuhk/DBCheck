# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

"""
认证路由 - 登录/登出/状态
"""

from flask import Blueprint, request, jsonify, g, session
from modules.user_management.services.auth_service import AuthService
from modules.user_management.utils.auth_decorator import login_required
from modules.user_management.services.user_service import UserService
from modules.user_management.services.perm_service import PermService

auth_bp = Blueprint('um_auth', __name__, url_prefix='/api/um/auth')
auth_service = AuthService()
user_service = UserService()
perm_service = PermService()


@auth_bp.route('/login', methods=['POST'])
def login():
    """用户登录"""
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({
            'code': 400,
            'msg': '请输入用户名和密码'
        }), 400

    result = auth_service.login(username, password)
    if result:
        # 设置Flask session
        # 先清掉上一次会话遗留的鉴权字段，避免换账户后 is_admin / user_roles
        # 仍沿用上一个账户的值（会让界面权限与后端真实角色不一致）。
        for _k in ('is_admin', 'user_roles', 'role', 'display_name'):
            session.pop(_k, None)
        session['user_id'] = result['user']['id']
        session['username'] = result['user']['username']
        # 存角色（用于首页菜单权限判断，roles 已是字符串列表如 ['admin']）
        roles = result['user'].get('roles', [])
        session['user_roles'] = roles
        session['is_admin'] = 'admin' in roles
        print(f'[DEBUG] login(): session set - user_id={session["user_id"]}, is_admin={session["is_admin"]}, roles={roles}', flush=True)
        return jsonify({'code': 0, 'data': result, 'msg': '登录成功'})
    return jsonify({
        'code': 401,
        'msg': '用户名或密码错误'
    }), 401


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    """用户登出"""
    session.clear()
    return jsonify({'code': 0, 'msg': '已登出'})


@auth_bp.route('/status', methods=['GET'])
@login_required
def status():
    """获取当前用户状态"""
    user_id = g.current_user['user_id']
    user = user_service.get_user(user_id)
    if not user:
        return jsonify({'code': 401, 'msg': '用户不存在'}), 401

    menus = perm_service.get_user_visible_menus(user_id)

    return jsonify({
        'code': 0,
        'data': {
            # 该接口受 @login_required 保护，能进入函数体即表示已登录，
            # 因此 logged_in 恒为 True，供前端 checkLogin() 判定使用。
            'logged_in': True,
            'user': {
                'id': user['id'],
                'username': user['username'],
                'nickname': user['nickname'],
                'email': user['email'],
                'roles': [r['role_code'] for r in user.get('roles', [])]
            },
            'menus': menus
        }
    })


@auth_bp.route('/change-password', methods=['POST'])
@login_required
def change_password():
    """修改密码"""
    data = request.get_json(silent=True) or {}
    old_pw = data.get('old_password', '')
    new_pw = data.get('new_password', '')

    if not old_pw or not new_pw:
        return jsonify({
            'code': 400,
            'msg': '请输入旧密码和新密码'
        }), 400

    if len(new_pw) < 6:
        return jsonify({
            'code': 400,
            'msg': '新密码至少6位'
        }), 400

    from modules.user_management.utils.password import verify_password, hash_password

    user = user_service.get_user(g.current_user['user_id'])
    if not user or not verify_password(old_pw, user['password']):
        return jsonify({
            'code': 403,
            'msg': '旧密码错误'
        }), 403

    user_service.update_user(g.current_user['user_id'], password=new_pw)
    return jsonify({'code': 0, 'msg': '密码修改成功'})
