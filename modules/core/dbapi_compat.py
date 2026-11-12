# -*- coding: utf-8 -*-
# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: Apache-2.0

"""DB-API 2.0 取数兼容层。

为什么需要这一层
================
DBCheck 的巡检路径同时对接三类「游标」：

1. 原生 DB-API 驱动（pymysql / psycopg2 / oracledb / pyodbc / dmPython）
   —— 完整实现 fetchone / fetchmany / fetchall；
2. JDBC 包装游标（各 ``*_jdbc`` 插件的 ``JdbcCursorWrapper``）
   —— 方法集由插件自己实现，历史上就出现过只写 fetchall/fetchone、
   漏掉 fetchmany 的情况；
3. 测试替身 / 元数据代理包装（``_MetadataCursor`` 等）
   —— 只转发部分方法。

调用方直接写 ``cursor.fetchmany(n)`` 时，只要目标游标属于第 2、3 类缺方法
的实现，就会抛 ``AttributeError: 'JdbcCursorWrapper' object has no attribute
'fetchmany'``。这个错误会被上层「章节级 try/except」吞成一个「查询失败，已
跳过」，最终表现为**整库所有章节全红**，而错误文案完全不指向真实原因，极易
被误判为「JDBC 驱动版本不兼容」。

统一走 ``fetch_rows()`` 后，缺方法的游标退化为 ``fetchall()`` 后切片，行为
等价、结果一致，且不会因为一个可选方法缺失就丢掉整个章节。

语义约定
========
* 返回值始终是 ``list``（原生驱动可能返回 tuple / generator）；
* 最多返回 ``limit`` 行；``limit <= 0`` 视为不限量（返回全部）；
* 绝不抛出 AttributeError —— 游标彻底不可读时返回空列表。
"""


from __future__ import annotations

from typing import Any, List


def supports_fetchmany(cursor: Any) -> bool:
    """游标是否实现了可调用的 fetchmany。"""
    return callable(getattr(cursor, 'fetchmany', None))


def fetch_rows(cursor: Any, limit: int = 0, *, strict: bool = False) -> List[Any]:
    """按上限取行，兼容缺少 ``fetchmany`` 的游标实现。

    参数:
        cursor: 任意 DB-API 风格游标（原生驱动 / JDBC 包装 / 元数据代理）
        limit:  期望的最大行数；``0`` 或负数表示「不限」，取全部

    返回:
        行列表（``list``）。游标不可读时返回 ``[]``。
    """
    if cursor is None:
        return []

    _limit = int(limit or 0)

    # 首选：真正的 DB-API 分页方法（不把大结果集整体读进内存）
    if _limit > 0 and supports_fetchmany(cursor):
        try:
            rows = cursor.fetchmany(_limit)
            return list(rows or [])
        except (AttributeError, NotImplementedError, TypeError):
            # TypeError：某些包装器的 fetchmany 不接受位置参数
            # NotImplementedError：sqlite3 等对非查询语句的显式拒绝
            pass
        except Exception:
            # 驱动层真实报错（如 JDBC ResultSet 已关闭）交给上层处理，
            # 这里不吞，避免把「连接坏掉」误报成「没数据」。
            raise

    if _limit > 0:
        one = getattr(cursor,'fetchone',None)
        if callable(one):
            rows=[]
            for _ in range(_limit):
                row=one()
                if row is None:break
                rows.append(row)
            return rows
        if strict:
            raise ValueError('巡检游标不支持有界取数，已停止该项采集')

    # 退化：一次性取全，再按上限切片
    try:
        rows = cursor.fetchall()
    except AttributeError:
        return []
    rows = list(rows or [])
    if _limit > 0:
        return rows[:_limit]
    return rows


def fetch_one(cursor: Any, default: Any = None) -> Any:
    """安全取单行；游标不可读或没有下一行时返回 ``default``。"""
    if cursor is None:
        return default
    fn = getattr(cursor, 'fetchone', None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            raise
    rows = fetch_rows(cursor, 1)
    return rows[0] if rows else default


def fetch_scalar(cursor: Any, default: Any = None) -> Any:
    """安全取单行首列（聚合查询最常用形态）。"""
    row = fetch_one(cursor, None)
    if row is None:
        return default
    try:
        return row[0]
    except (TypeError, IndexError, KeyError):
        return default


__all__ = ['fetch_rows', 'fetch_one', 'fetch_scalar', 'supports_fetchmany']
