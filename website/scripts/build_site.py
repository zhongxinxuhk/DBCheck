# Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"""Build DBPulse's static GitHub Pages site from public GitHub Releases."""

import argparse
import datetime as dt
import html
import json
from pathlib import Path
import re
import shutil
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/zhongxinxuhk/DBCheck'
SITE = 'https://zhongxinxuhk.github.io/DBCheck'
LICENSE_HEADER = '''<!--
Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
-->'''
DATABASES = ['MySQL', 'PostgreSQL', 'Oracle', 'SQL Server', 'DM8', 'TiDB', 'MariaDB', 'IvorySQL', 'YashanDB', 'KingbaseES', 'GBase 8s', 'UXDB', 'HGDB', 'Vastbase G100', 'HaloDB', 'GBase 8a', 'MongoDB', 'DB2 LUW', 'OceanBase', 'TDSQL-C', 'Redis', 'ClickHouse']
PLATFORMS = [
    ('windows', 'Windows', 'x64', 'windows-x86_64', '解压后双击 start.bat 启动。', '⊞'),
    ('linux', 'Linux', 'x64', 'linux-x86_64', 'glibc 2.28+，执行 bash start.sh。', '▧'),
    ('linux', 'Linux', 'ARM64', 'linux-arm64', 'glibc 2.28+，适用于 aarch64 系统。', '▧'),
    ('portable', '跨平台便携包', 'Windows / Linux', 'windows-linux-portable', '包含 Windows x64、Linux x64 与 ARM64，共用数据目录。', '⇄'),
    ('macos', 'macOS', 'Apple Silicon', 'macos-arm64', '解压后运行 start.command，启动方式以对应发行说明为准。', '⌘'),
    ('macos', 'macOS', 'Intel x64', 'macos-x86_64', '安装方式请查看对应发行说明。', '⌘'),
]


def esc(value):
    return html.escape(str(value), quote=True)


def get_releases():
    request = urllib.request.Request(
        'https://api.github.com/repos/zhongxinxuhk/DBCheck/releases?per_page=30',
        headers={'User-Agent': 'DBPulse-Website', 'Accept': 'application/vnd.github+json'},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                releases = json.load(response)
            return [item for item in releases if not item['draft'] and not item['prerelease']]
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def public_release(release):
    return {
        'tag_name': release['tag_name'], 'published_at': release['published_at'],
        'html_url': release['html_url'], 'body': release.get('body') or '',
        'assets': [
            {'name': asset['name'], 'size': asset['size'], 'browser_download_url': asset['browser_download_url']}
            for asset in release['assets']
        ],
    }


def inline_markdown(value):
    value = esc(value)
    value = re.sub(r'`([^`]+)`', r'<code>\1</code>', value)
    value = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', value)
    # Only GitHub HTTPS links are allowed in generated release-note markup.
    return re.sub(r'\[([^\]]+)\]\((https://github\.com/[^\s)]+)\)', r'<a href="\2">\1</a>', value)


def markdown(value):
    lines = re.sub(r'<!--.*?-->', '', value, flags=re.S).splitlines()
    result = []
    in_list = False
    in_code = False
    for line in lines:
        if line.startswith('```'):
            if in_list:
                result.append('</ul>')
                in_list = False
            result.append('</code></pre>' if in_code else '<pre><code>')
            in_code = not in_code
            continue
        if in_code:
            result.append(esc(line)+'\n')
            continue
        if line.startswith('- ') or line.startswith('* '):
            if not in_list:
                result.append('<ul>')
                in_list = True
            result.append('<li>'+inline_markdown(line[2:])+'</li>')
            continue
        if in_list:
            result.append('</ul>')
            in_list = False
        heading = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading:
            level = min(4, max(3, len(heading[1])+1))
            result.append(f'<h{level}>{inline_markdown(heading[2])}</h{level}>')
        elif line.strip():
            result.append('<p>'+inline_markdown(line)+'</p>')
    if in_list:
        result.append('</ul>')
    if in_code:
        result.append('</code></pre>')
    return ''.join(result)


def shell(page, title, description, content, latest, base):
    def link(path=''):
        return base + path
    nav = [('home', '', '首页'), ('download', 'download/', '下载'), ('changelog', 'changelog/', '更新日志'), ('docs', 'docs/', '文档')]
    links = ''.join(f'<a href="{link(path)}"'+(' aria-current="page"' if key == page else '')+f'>{name}</a>' for key,path,name in nav)
    canonical = SITE + ('/' if page == 'home' else '/'+page+'/')
    symbol = f'<img class="wordmark dark-mark" src="{link("assets/logo-dark.png")}" width="140" height="43" alt="DBPulse"><img class="wordmark light-mark" src="{link("assets/logo-light.png")}" width="140" height="43" alt="DBPulse">'
    return f'''<!doctype html>
{LICENSE_HEADER}
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · DBPulse</title><meta name="description" content="{esc(description)}">
<meta name="theme-color" content="#0b1114"><link rel="canonical" href="{canonical}">
<meta property="og:type" content="website"><meta property="og:title" content="{esc(title)} · DBPulse"><meta property="og:description" content="{esc(description)}"><meta property="og:url" content="{canonical}">
<link rel="stylesheet" href="{link('assets/site.css')}"><link rel="icon" href="{link('assets/favicon.png')}" type="image/png">
<script src="{link('assets/site.js')}" defer></script>
</head>
<body data-page="{page}" data-base="{base}">
<a class="skip" href="#main">跳至正文</a>
<header class="header"><div class="container nav"><a class="brand" href="{link()}" aria-label="DBPulse 首页">{symbol}</a>
<nav class="nav-links" id="site-navigation" aria-label="主导航">{links}<a href="{REPO}">GitHub ↗</a></nav>
<div class="nav-tools"><button type="button" class="icon-button" data-theme-toggle aria-label="切换浅色主题">☀</button><a class="button primary" href="{link('download/')}">下载 DBPulse <span aria-hidden="true">↗</span></a><button type="button" class="menu-button" data-menu-toggle aria-expanded="false" aria-controls="site-navigation" aria-label="展开导航">☰</button></div>
</div></header>
<main id="main">{content}</main>
<footer class="footer"><div class="container"><div class="footer-main"><a class="brand" href="{link()}">{symbol}</a><div class="footer-links"><a href="{link('download/')}">下载</a><a href="{link('changelog/')}">更新日志</a><a href="{link('docs/')}">文档与许可</a><a href="{REPO}/issues">反馈问题 ↗</a></div></div><p class="footer-note">数据库智能巡检与运维平台。<a href="{link('docs/#project')}">项目来源与许可</a> · <a href="{REPO}">GitHub</a> · <a href="{REPO}/releases">GitHub Releases</a></p></div></footer>
<noscript><div class="container no-js-note">当前显示最近构建的发行记录。最新版本请查看 <a href="{REPO}/releases">GitHub Releases</a>。</div></noscript>
</body></html>'''


def home(latest):
    version = esc(latest['tag_name'])
    databases = ''.join(f'<span>{name}</span>' for name in DATABASES)
    features = [
        ('▤', '多数据源，一处管理', '按实例与分组组织数据库连接，支持批量巡检、台账和配置管理。'),
        ('⌕', '发现问题，定位原因', '围绕慢 SQL、锁等待、索引和配置基线，整理巡检结果与风险线索。'),
        ('✦', 'AI 辅助诊断', '连接本地 Ollama 或兼容云 API，结合巡检信息与知识库辅助分析。'),
        ('▥', '从巡检到 Word 报告', '基于模板输出结构化报告，记录风险、指标和建议，便于交付与归档。'),
        ('↗', '保留历史，持续对比', '关联多轮巡检记录，查看健康趋势、容量变化与配置差异。'),
        ('⚙', '融入日常运维', '支持定时任务、通知、REST API 与 MCP，衔接现有运维工作流。'),
    ]
    feature_html = ''.join(f'<article class="feature"><div class="feature-icon" aria-hidden="true">{icon}</div><h3>{title}</h3><p>{text}</p></article>' for icon,title,text in features)
    return f'''<section class="hero"><div class="container hero-grid"><div>
<a class="version-link" href="changelog/"><b data-latest-version>{version}</b><span>查看最新版本更新 →</span></a>
<h1>读懂数据库，<br>让运维<span class="accent">更从容。</span></h1>
<p class="hero-description">连接、巡检、诊断、报告。DBPulse 将分散的数据库检查工作汇集到一个平台，帮助你从日常数据中发现问题，形成可跟踪的运维结果。</p>
<div class="actions"><a class="button primary" href="download/">下载 DBPulse <span aria-hidden="true">↓</span></a><a class="button" href="docs/">阅读使用文档 <span aria-hidden="true">→</span></a></div>
<p class="hero-note">Windows · Linux · macOS 历史版本 &nbsp; / &nbsp; 支持容器与源码部署</p></div>
<div><div class="console"><div class="console-top"><i class="dot"></i><i class="dot"></i><i class="dot"></i><span class="console-title">DBPULSE / INSPECTION WORKSPACE</span></div><div class="console-body"><div class="console-heading"><div><h3>一次巡检，串起整个流程。</h3><p>从实例连接，到可交付的巡检报告</p></div><span class="demo-label">流程示意</span></div>
<div class="flow-tabs" role="tablist" aria-label="巡检流程"><button data-flow="connect" role="tab" aria-selected="true" aria-controls="flow-panel">连接</button><button data-flow="inspect" role="tab" aria-selected="false" aria-controls="flow-panel">巡检</button><button data-flow="analyze" role="tab" aria-selected="false" aria-controls="flow-panel">分析</button><button data-flow="report" role="tab" aria-selected="false" aria-controls="flow-panel">报告</button></div>
<div class="demo-row"><span class="db-icon">My</span><span>MySQL / PostgreSQL</span><span class="right">开源数据库</span></div><div class="demo-row"><span class="db-icon">Or</span><span>Oracle / SQL Server</span><span class="right">商业数据库</span></div><div class="demo-row"><span class="db-icon">DM</span><span>DM8 / KingbaseES / TiDB</span><span class="right">国产数据库</span></div>
<div class="demo-details" id="flow-panel" role="tabpanel" aria-live="polite"><h4 data-flow-title>统一数据源入口</h4><p data-flow-description>按数据库类型配置连接参数，组织实例分组，验证连接后开始巡检。</p></div></div></div><p class="console-caption">点击流程标签，了解 DBPulse 的巡检工作方式。</p></div></div></section>
<div class="support-strip"><div class="container support-inner"><span class="support-label">面向多种数据库<br>覆盖日常运维场景</span><div class="database-names"><span>MySQL</span><span>PostgreSQL</span><span>Oracle</span><span>SQL Server</span><span>DM8</span><span>Redis</span></div></div></div>
<section class="section"><div class="container"><div class="section-heading"><span class="eyebrow">Built for database operations</span><h2>把检查做完整，<br>把结果讲清楚。</h2><p>从实例信息到性能与风险分析，将数据库运维中相互关联的工作放在一起。</p></div><div class="feature-grid">{feature_html}</div></div></section>
<section class="section section-alt"><div class="container split"><div><span class="eyebrow">From connection to insight</span><h2>从一个实例开始，<br>建立持续巡检的节奏。</h2><ul class="step-list"><li><span class="step-number">01</span><div><h3>接入并组织数据源</h3><p>选择数据库类型，配置连接信息、驱动和实例分组。</p></div></li><li><span class="step-number">02</span><div><h3>选择模板，执行巡检</h3><p>按业务场景选择检查范围，可立即执行或配置定时任务。</p></div></li><li><span class="step-number">03</span><div><h3>查看风险与报告</h3><p>结合数据库上下文评估建议，导出报告并跟踪后续变化。</p></div></li></ul></div><div class="quickstart"><div class="code-head"><span>从源码启动</span><button class="copy-button" data-copy="source-command">复制命令</button></div><pre class="code" id="source-command">git clone {REPO}.git
cd DBCheck
python -m venv .venv
# 激活虚拟环境后执行
pip install -r deploy/requirements.txt
python web_ui.py</pre><p class="code-description">Python 3.10+。启动后访问 http://localhost:5003。数据库驱动与平台依赖请参考安装文档。</p></div></div></section>
<section class="section"><div class="container"><div class="section-heading"><span class="eyebrow">Database coverage</span><h2>连接你的数据库环境。</h2><p>项目提供以下数据库与数据系统的适配。检查项与驱动要求随数据库类型、版本和模板变化。</p></div><div class="db-grid">{databases}</div></div></section>
<section class="section section-alt"><div class="container"><span class="eyebrow">What's new</span><h2>持续更新，逐步完善。</h2><div class="release-callout"><div><h3><span data-latest-version>{version}</span> <span class="release-date" data-latest-date>{latest['published_at'][:10]}</span></h3><p>查看每次发布的新增功能、修复与安装升级说明，选择适合你当前环境的版本。</p></div><a href="changelog/" class="button">浏览更新日志 →</a></div></div></section>
<section class="section"><div class="container"><div class="cta"><div><h2>下一次巡检，从这里开始。</h2><p>为你的系统选择安装包，或从部署文档开始。</p></div><a href="download/" class="button primary">前往下载中心 ↓</a></div></div></section>'''


def choose_packages(releases):
    packages = []
    for platform, title, architecture, match, note, icon in PLATFORMS:
        found = None
        for release in releases:
            for asset in release['assets']:
                name = asset['name'].lower()
                # Accept both DBPulse-vX-platform and legacy DBPulse-platform-vX names.
                if match in name and name.endswith(('.zip', '.tar.gz', '.dmg')):
                    found = (release, asset)
                    break
            if found:
                break
        if found:
            release, asset = found
            packages.append({'platform': platform, 'title': title, 'architecture': architecture,
                             'note': note, 'icon': icon, 'release': release, 'asset': asset})
    return packages


def download_cards(releases):
    latest = releases[0]['tag_name']
    cards = []
    for package in choose_packages(releases):
        platform, title, architecture, note, icon = (package[key] for key in ['platform','title','architecture','note','icon'])
        release, asset = package['release'], package['asset']
        archive = release['tag_name'] != latest
        checksum = next((item for item in release['assets'] if item['name'] == 'SHA256SUMS.txt'), None)
        archive_html = '<span class="archive">历史可用版本 · '+esc(release['tag_name'])+'</span>' if archive else ''
        checksum_html = f'<a href="{esc(checksum["browser_download_url"])}">SHA-256 校验文件 ↗</a>' if checksum else ''
        cards.append(f'''<article class="download-card" data-platform="{platform}"><div class="platform-icon" aria-hidden="true">{icon}</div><h2>{title}</h2><div class="download-meta"><span>{architecture}</span><span>{esc(release['tag_name'])}</span><span>{asset['size']/1024/1024:.1f} MiB</span></div>{archive_html}<p>{note}</p><a class="button primary" href="{esc(asset['browser_download_url'])}" aria-label="下载 {title} {architecture} {esc(release['tag_name'])}">下载 {architecture} ↓</a><span class="file-name">{esc(asset['name'])}</span><div class="secondary-links"><a href="{esc(release['html_url'])}">发行说明 ↗</a>{checksum_html}</div></article>''')
    return ''.join(cards)


def download(releases):
    return f'''<div class="container"><header class="page-heading"><span class="eyebrow">Download DBPulse</span><h1>选择你的平台，<br><span class="accent">开始使用 DBPulse。</span></h1><p>最新正式版 <strong data-latest-version>{esc(releases[0]['tag_name'])}</strong>。安装包来自本项目的 GitHub Releases，各平台可用版本在卡片中标明。</p></header><section class="download-layout" aria-label="平台安装包"><p class="small" data-device-hint>请根据操作系统和 CPU 架构选择安装包。</p><div class="platform-filter" aria-label="筛选下载平台"><button class="filter-button" data-platform-filter="all" aria-pressed="true">全部平台</button><button class="filter-button" data-platform-filter="windows" aria-pressed="false">Windows</button><button class="filter-button" data-platform-filter="linux" aria-pressed="false">Linux</button><button class="filter-button" data-platform-filter="macos" aria-pressed="false">macOS</button><button class="filter-button" data-platform-filter="portable" aria-pressed="false">便携包</button></div><div class="download-grid" data-download-grid>{download_cards(releases)}</div><p class="download-tip" data-release-status>页面载入后同步 GitHub Releases。若同步不可用，可从 <a class="accent" href="{REPO}/releases">完整发行列表 ↗</a> 查看最新资产。</p>
<div class="install-section"><div><h2>解压，然后启动。</h2><ul class="step-list"><li><span class="step-number">01</span><div><h3>下载并校验</h3><p>选择系统与架构，使用对应 Release 的 SHA256SUMS.txt 校验下载文件。</p></div></li><li><span class="step-number">02</span><div><h3>启动程序</h3><p>Windows 双击 start.bat，Linux 执行 bash start.sh。macOS 历史包请参考发行说明。</p></div></li><li><span class="step-number">03</span><div><h3>打开管理界面</h3><p>访问 http://localhost:5003，配置数据源并执行首次巡检。</p></div></li></ul></div><div><h2>校验与升级</h2><div class="quickstart"><div class="code-head"><span>Linux SHA-256 校验</span><button class="copy-button" data-copy="checksum-command">复制命令</button></div><pre class="code" id="checksum-command">sha256sum -c SHA256SUMS.txt --ignore-missing</pre><p class="code-description">校验文件和安装包需来自同一个版本，置于同一目录后执行。</p></div><div class="notice">升级前停止服务并备份整个原目录，保留 data/ 及其中的数据库密码密钥。跨 Windows / Linux 迁移时，按便携包说明复制完整目录。</div><a class="accent small" href="{REPO}/releases">查看全部版本和完整升级说明 ↗</a></div></div></section></div>'''


def release_entry(release, latest=False):
    tag = esc(release['tag_name'])
    # Expand highlights first, with the complete release notes still available.
    body = release.get('body') or '本版本的详细信息请查看 GitHub Release。'
    parts = re.split(r'(?m)^## (?:安装包与平台|安装与升级说明)', body, maxsplit=1)
    highlights = parts[0]
    details = markdown(body)
    badge = '<span class="release-badge">最新正式版</span>' if latest else ''
    return f'''<article class="release-entry" id="release-{tag}"><header class="release-heading"><h2>{tag}{badge}</h2><time class="release-date" datetime="{esc(release['published_at'])}">{release['published_at'][:10]}</time></header><div class="release-body">{markdown(highlights)}<details><summary>完整发行说明与升级信息</summary>{details}</details></div><a class="button" href="{esc(release['html_url'])}">在 GitHub 查看此版本 ↗</a></article>'''


def changelog(releases):
    sidebar = ''.join(f'<a href="#release-{esc(release["tag_name"])}">{esc(release["tag_name"])}</a>' for release in releases)
    entries = ''.join(release_entry(release, i == 0) for i,release in enumerate(releases))
    return f'''<div class="container"><header class="page-heading"><span class="eyebrow">Changelog</span><h1>每一步更新，<br><span class="accent">都有迹可循。</span></h1><p>来自本项目 GitHub Releases 的正式发行记录，包含功能更新、修复和安装升级信息。</p><p class="small" data-release-status>正在检查最新发行记录。</p></header><div class="document-layout"><nav class="sidebar" aria-label="版本索引" data-version-index>{sidebar}</nav><div data-changelog>{entries}</div></div></div>'''


def docs():
    cards = [
        ('安装与部署', '环境准备、源码安装、平台驱动与部署方式。', '/blob/main/docs/install/INSTALL_zh.md'),
        ('首次初始化', '完成初始配置，开始管理数据源和巡检任务。', '/blob/main/docs/install/INITIALIZATION_zh.md'),
        ('数据库兼容性', '查看数据库类型、驱动要求及适配范围。', '/blob/main/docs/database-compatibility.md'),
        ('业务巡检适配', '按业务检查项目查看巡检与报告的适配说明。', '/blob/main/docs/business-inspection-compatibility.md'),
        ('服务化运行', '了解服务启动、运行维护与日志管理。', '/blob/main/docs/install/SERVICE_zh.md'),
        ('插件开发', '了解插件的结构、生命周期与开发方式。', '/blob/main/docs/plugin/plugin-development-guide-v2.8.0.md'),
    ]
    cards_html = ''.join(f'<article class="doc-card"><h3>{title}</h3><p>{description}</p><a href="{REPO}{path}">阅读文档 ↗</a></article>' for title,description,path in cards)
    return f'''<div class="container"><header class="page-heading"><span class="eyebrow">Documentation</span><h1>从安装到日常运维，<br><span class="accent">找到需要的说明。</span></h1><p>从快速入门开始，按你的数据库与部署环境查阅对应文档。</p></header><div class="article-content"><div class="docs-grid">{cards_html}</div><section id="quickstart"><h2>首次使用</h2><ol><li>从下载中心选择平台安装包，或按安装文档部署源码。</li><li>启动服务后访问 http://localhost:5003。</li><li>管理员账号为 admin。请按初始化文档获取安装时的实际初始密码，首次登录后可在用户中心修改。</li><li>添加数据库连接，确认驱动与权限，再选择巡检模板。</li><li>查看结果并生成报告，按需设置定时任务。</li></ol></section><section id="faq"><h2>常见问题</h2><details><summary>为什么某些数据库的巡检章节为空？</summary><p>巡检模板和可采集指标随数据库类型、版本及权限变化。请结合数据库兼容性说明检查驱动和账号权限。</p></details><details><summary>如何选择 Linux 安装包？</summary><p>x86_64 选择 x64，aarch64 选择 ARM64。v1.0.4 便携包要求 glibc 2.28 及以上，具体依赖以对应发行说明为准。</p></details><details><summary>如何启用 AI 辅助诊断？</summary><p>在 Web UI 中配置本地 Ollama 或兼容的云 API，并确认模型可用。可结合知识库文档辅助诊断。</p></details><details><summary>升级时需要保留哪些数据？</summary><p>停止服务并备份整个原目录，保留 data/、数据库密码密钥、账号、任务和自定义规则。详细步骤见对应 Release。</p></details></section><section id="project"><h2>项目来源与许可</h2><p>DBPulse 基于 fiyo（Jack Ge）创建的上游 DBCheck / RaccoonX 持续演进。本仓库使用 DBPulse 品牌，保留上游代码的版权和许可证信息。</p><p>自研部分采用 PolyForm Noncommercial 1.0.0，商业用途需取得书面商业授权。上游 DBCheck 及其修改文件采用 Apache License 2.0。</p><p><a href="{REPO}/blob/main/LICENSE-PolyForm-Noncommercial-1.0.0.txt">非商业许可</a> · <a href="{REPO}/blob/main/LICENSE-COMMERCIAL.md">商业授权说明</a> · <a href="{REPO}/blob/main/LICENSE">Apache 2.0</a> · <a href="https://github.com/fiyo/DBCheck">上游项目</a> · <a href="{REPO}/blob/main/NOTICE">NOTICE</a></p></section><section><h2>反馈与交流</h2><p>遇到安装、数据库适配或报告问题，可在 GitHub Issues 提交版本、操作系统、数据库类型和复现步骤。</p><a class="button" href="{REPO}/issues">提交问题 ↗</a></section></div></div><div class="section"></div>'''


def build(output, releases):
    if not releases:
        raise ValueError('No stable GitHub Releases available')
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT/'assets', output/'assets', dirs_exist_ok=True)
    (output/'.nojekyll').touch()
    pages = [
        ('home', '首页', 'DBPulse 数据库智能巡检与运维平台，连接数据源、执行巡检、辅助诊断并生成报告。', home(releases[0])),
        ('download', '下载中心', '下载 DBPulse Windows、Linux 和 macOS 可用安装包，查看平台版本、校验文件与安装升级步骤。', download(releases)),
        ('changelog', '更新日志', '查看 DBPulse 的版本更新、新增功能、修复和安装升级说明。', changelog(releases)),
        ('docs', '使用文档', 'DBPulse 安装部署、初始化、数据库兼容性、插件和许可说明。', docs()),
    ]
    for page,title,description,content in pages:
        directory = output if page == 'home' else output/page
        directory.mkdir(exist_ok=True)
        (directory/'index.html').write_text(shell(page,title,description,content,releases[0],'./' if page=='home' else '../'),encoding='utf-8')
    (output/'assets/releases.json').write_text(json.dumps({'releases':releases},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'404.html').write_text(shell('404','页面未找到','返回 DBPulse 官网首页。','<section class="section container"><h1>页面未找到</h1><p>你可以返回首页，或从下载中心选择安装包。</p><a class="button primary" href="/DBCheck/">返回首页 →</a></section>',releases[0],'/DBCheck/'),encoding='utf-8')
    urls = ['','download/','changelog/','docs/']
    (output/'sitemap.xml').write_text('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join(f'<url><loc>{SITE}/{path}</loc></url>' for path in urls)+'</urlset>',encoding='utf-8')
    (output/'robots.txt').write_text(f'User-agent: *\nAllow: /DBCheck/\nSitemap: {SITE}/sitemap.xml\n',encoding='utf-8')
    print(f'Built 4 pages, 404 page and {len(releases)} releases into {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'_site')
    parser.add_argument('--releases-json',type=Path)
    args = parser.parse_args()
    source = json.loads(args.releases_json.read_text()) if args.releases_json else get_releases()
    if isinstance(source,dict):
        source = source['releases']
    releases = [public_release(release) for release in source if not release.get('draft') and not release.get('prerelease')]
    build(args.output.resolve(),releases)
