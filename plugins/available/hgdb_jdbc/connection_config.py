#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: Apache-2.0
# Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
# Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

from dataclasses import dataclass


@dataclass
class HgdbConnectionConfig:
    """HGDB JDBC 连接配置。

    Attributes:
        host: HGDB 服务器主机名或 IP
        port: HGDB 实例端口（默认 5866）
        user: 登录用户名（默认 highgo）
        password: 登录密码
        database: 目标数据库名（默认 highgo）
        jdbc_url: 完整 JDBC URL（可选；以 jdbc:postgresql 开头则直接透传，不做拼接）
    """

    host: str = "127.0.0.1"
    port: int = 5866
    user: str = "highgo"
    password: str = ""
    database: str = "highgo"
    jdbc_url: str = ""

    # 连接/登录/读取超时（秒）。PostgreSQL JDBC 驱动默认值为 0 = 永不超时，
    # 主机不可达或被防火墙丢包时 getConnection 会挂到 OS 级 TCP 超时（分钟级），
    # 表现为「点测试连接后长时间无响应」，故此处给出兜底上限。
    connect_timeout_s: int = 15
    socket_timeout_s: int = 30

    def build_jdbc_url(self) -> str:
        """构建 JDBC URL。

        - 若 jdbc_url 以 'jdbc:postgresql' 开头：原样透传（支持自定义属性）
        - 否则按标准格式拼接并附加超时参数：
          ``jdbc:postgresql://{host}:{port}/{database}?connectTimeout=..&socketTimeout=..&loginTimeout=..``

        Returns:
            可用的 JDBC 连接 URL 字符串
        """
        # 统一连接层：基础连接串 + PG 系超时参数段（connectTimeout/loginTimeout/
        # socketTimeout）均由 modules.jdbc_connector build_jdbc_url 生成（透传
        # connect_timeout_s/socket_timeout_s），本插件不再手拼（避免双重拼接）。
        from modules.jdbc_connector import build_jdbc_url as _build_jdbc_url
        return _build_jdbc_url(
            'hgdb', self.host, self.port,
            database=self.database,
            jdbc_url=self.jdbc_url,
            connect_timeout_s=self.connect_timeout_s,
            socket_timeout_s=self.socket_timeout_s,
        )

    def build_properties(self):
        """构建 JDBC 连接属性（java.util.Properties，含 user / password）。

        返回 Java 的 java.util.Properties 对象，供 DriverManager.getConnection
        直接消费。该方法需在 JVM 已启动（ensure_jvm 之后）调用，因为底层依赖
        java.util.Properties。

        Returns:
            java.util.Properties 实例（含 user / password 键）
        """
        # 不用 ``from java.util import ...``：``java`` 是 JPype 在运行时注入的虚拟包，
        # 静态分析阶段无法解析（会报“未解析的引用”）；JClass 是等价写法且可被 IDE 识别。
        import jpype

        props = jpype.JClass("java.util.Properties")()
        props.setProperty("user", str(self.user))
        props.setProperty("password", str(self.password))
        return props

    @classmethod
    def from_instance(cls, inst: dict) -> "HgdbConnectionConfig":
        """从 web_ui / inspection_dal 透传的实例字典构建配置。

        Args:
            inst: 含 host/port/user/password/database/jdbc_url 等键的字典
                  （缺失键使用默认值，向后兼容）

        Returns:
            HgdbConnectionConfig 实例
        """
        if not inst:
            inst = {}

        def _get_str(key: str, default: str = "") -> str:
            val = inst.get(key, default)
            return str(val) if val is not None else default

        def _get_int(key: str, default: int) -> int:
            try:
                return int(inst.get(key, default))
            except (TypeError, ValueError):
                return default

        return cls(
            host=_get_str("host", "127.0.0.1") or "127.0.0.1",
            port=_get_int("port", 5866) or 5866,
            user=_get_str("user", "highgo") or "highgo",
            password=_get_str("password"),
            database=_get_str("database", "highgo") or "highgo",
            jdbc_url=_get_str("jdbc_url"),
        )

    def __repr__(self) -> str:
        """安全的字符串表示（隐藏密码）。"""
        safe_pass = "***" if self.password else ""
        return (
            f"HgdbConnectionConfig(host={self.host!r}, port={self.port}, "
            f"database={self.database!r}, user={self.user!r}, "
            f"jdbc_url={self.jdbc_url!r}, password={safe_pass!r})"
        )
