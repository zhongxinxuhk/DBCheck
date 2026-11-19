# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""文件名安全化工具。

巡检报告文件名由「模板 + 数据源名称 + 时间戳 + 任务号」拼成，其中数据源名称
完全由用户输入。Windows 下 ``\\ / : * ? " < > |`` 属于非法字符（数据源名写成
``10.60.6.216:1433`` 这类地址非常常见），``Document.save()`` 会直接抛
``OSError: [Errno 22] Invalid argument``；文件名过长同样会保存失败。这里把
用户可控片段收敛成跨平台可用的安全文件名，避免报告在最后一步写盘时失败。
"""

from __future__ import annotations

import re
import unicodedata

# Windows 文件名禁用字符（POSIX 只禁用 "/" 和 NUL，取并集即可跨平台）。
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# 连续空白折叠，避免文件名里出现大段空格。
_WHITESPACE = re.compile(r'\s+')

# Windows 保留设备名：即使带扩展名也不能作为文件名。
_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
}


def sanitize_filename_component(value, *, max_length=80, fallback='report'):
    """把任意文本收敛为单个安全的文件名片段。

    - 非法字符替换为 ``_``，控制字符直接丢弃；
    - 连续空白折叠为单个空格；
    - 去掉 Windows 不允许的结尾点和空格；
    - 保留设备名加前缀下划线；
    - 超长按字符截断（默认 80，足以容纳 CJK 与时间戳且远小于 255）。

    Args:
        value: 原始片段，``None`` 视为空。
        max_length: 最大字符数，必须为正整数。
        fallback: 清洗后为空时使用的兜底片段。

    Returns:
        可直接拼进文件名的字符串，绝不为空。
    """
    text = '' if value is None else str(value)
    text = unicodedata.normalize('NFC', text)
    text = _ILLEGAL_CHARS.sub('_', text)
    text = _WHITESPACE.sub(' ', text).strip(' .')
    if not text:
        text = str(fallback or 'report')
        text = _ILLEGAL_CHARS.sub('_', text).strip(' .') or 'report'
    limit = max(1, int(max_length))
    if len(text) > limit:
        text = text[:limit].rstrip(' .') or text[:limit]
    if text.split('.', 1)[0].upper() in _RESERVED_NAMES:
        text = '_' + text
    return text


def build_report_filename(template, *, ip='', name='', timestamp='',
                          suffix='', extension='.docx',
                          max_length=140, fallback='inspection_report'):
    """按模板生成安全报告文件名。

    模板里的 ``{ip}`` / ``{name}`` / ``{ts}`` 会先用安全片段替换，再对整名做一次
    清洗：这样即使某个语言的模板本身含非法字符（例如路径分隔符），也不会把报告
    写到目录之外或写盘失败。

    Args:
        template: 形如 ``"...{ip}_{name}_{ts}"`` 的本地化模板。
        ip / name / timestamp: 模板占位符取值。
        suffix: 追加在文件名尾部的任务号（可为空）。
        extension: 扩展名，含点。
        max_length: 文件名主体最大字符数。
        fallback: 主体为空时的兜底名称。
    """
    safe_ip = sanitize_filename_component(ip, max_length=46, fallback='host')
    safe_name = sanitize_filename_component(name, max_length=40, fallback='db')
    safe_ts = sanitize_filename_component(timestamp, max_length=24, fallback='')
    try:
        body = str(template or '').format(ip=safe_ip, name=safe_name, ts=safe_ts)
    except (KeyError, IndexError, ValueError):
        # 模板损坏（多出未知占位符）时退回固定命名，报告比命名规范更重要。
        body = f'{fallback}_{safe_ip}_{safe_ts}'
    body = sanitize_filename_component(body, max_length=max_length, fallback=fallback)
    if suffix:
        body = f'{body}_{sanitize_filename_component(suffix, max_length=64, fallback="task")}'
    ext = str(extension or '')
    if ext and not ext.startswith('.'):
        ext = '.' + ext
    return body + ext
