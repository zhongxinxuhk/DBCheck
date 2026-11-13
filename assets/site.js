/* Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0 */
const root = document.documentElement;
const themeButton = document.querySelector('[data-theme-toggle]');
function setTheme(theme) {
  root.dataset.theme = theme;
  if (themeButton) {
    themeButton.textContent = theme === 'dark' ? '☀' : '☾';
    themeButton.setAttribute('aria-label', theme === 'dark' ? '切换浅色主题' : '切换深色主题');
  }
}
let savedTheme;
try { savedTheme = localStorage.getItem('dbpulse-theme'); } catch { /* Storage can be disabled. */ }
setTheme(savedTheme === 'light' ? 'light' : 'dark');
themeButton?.addEventListener('click', () => {
  const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
  setTheme(next);
  try { localStorage.setItem('dbpulse-theme', next); } catch { /* Keep the current theme in memory. */ }
});
const menuButton = document.querySelector('[data-menu-toggle]');
const menu = document.querySelector('.nav-links');
menuButton?.addEventListener('click', () => {
  const open = menuButton.getAttribute('aria-expanded') !== 'true';
  menuButton.setAttribute('aria-expanded', String(open));
  menu.classList.toggle('open', open);
});
const flowSteps = {
  connect: ['统一数据源入口', '按数据库类型配置连接参数，组织实例分组，验证连接后开始巡检。'],
  inspect: ['按模板执行巡检', '采集配置、性能、容量与安全信息，按数据库类型和巡检模板评估风险。'],
  analyze: ['结合上下文分析', '汇总慢 SQL、锁等待与配置问题，可结合本地模型或兼容云 API 辅助诊断。'],
  report: ['沉淀可交付结果', '生成 Word 巡检报告，保留历史结果，支持后续对比与问题跟踪。']
};
document.querySelectorAll('[data-flow]').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-flow]').forEach(tab => tab.setAttribute('aria-selected', String(tab === button)));
    document.querySelector('[data-flow-title]').textContent = flowSteps[button.dataset.flow][0];
    document.querySelector('[data-flow-description]').textContent = flowSteps[button.dataset.flow][1];
  });
  button.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll('[data-flow]')];
    let index = tabs.indexOf(button);
    if (event.key === 'ArrowRight') index = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') index = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') index = 0;
    if (event.key === 'End') index = tabs.length - 1;
    tabs[index].focus();
    tabs[index].click();
  });
});
document.querySelectorAll('[data-platform-filter]').forEach(button => {
  button.addEventListener('click', () => {
    const platform = button.dataset.platformFilter;
    document.querySelectorAll('[data-platform-filter]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    document.querySelectorAll('[data-platform]').forEach(card => { card.hidden = platform !== 'all' && card.dataset.platform !== platform; });
  });
});
const platformText = document.querySelector('[data-device-hint]');
if (platformText) {
  const device = (navigator.userAgentData?.platform || navigator.platform || '').toLowerCase();
  const detected = device.includes('win') ? 'Windows' : device.includes('mac') ? 'macOS' : device.includes('linux') ? 'Linux' : '';
  if (detected) platformText.textContent = `当前设备可能使用 ${detected}，请确认 CPU 架构后选择安装包。`;
}
let toastTimer;
function notify(message) {
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    toast.setAttribute('role', 'status');
    document.body.append(toast);
  }
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3000);
}
document.querySelectorAll('[data-copy]').forEach(button => {
  button.addEventListener('click', async () => {
    const target = document.getElementById(button.dataset.copy);
    try {
      await navigator.clipboard.writeText(target.textContent);
      notify('已复制到剪贴板');
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(target);
      selection.removeAllRanges();
      selection.addRange(range);
      notify('请复制已选中的命令');
    }
  });
});

const repositoryUrl = 'https://github.com/zhongxinxuhk/DBCheck';
const platformDefinitions = [
  ['windows', 'Windows', 'x64', 'windows-x86_64', '解压后双击 start.bat 启动。', '⊞'],
  ['linux', 'Linux', 'x64', 'linux-x86_64', 'glibc 2.28+，执行 bash start.sh。', '▧'],
  ['linux', 'Linux', 'ARM64', 'linux-arm64', 'glibc 2.28+，适用于 aarch64 系统。', '▧'],
  ['portable', '跨平台便携包', 'Windows / Linux', 'windows-linux-portable', '包含 Windows x64、Linux x64 与 ARM64，共用数据目录。', '⇄'],
  ['macos', 'macOS', 'Apple Silicon', 'macos-arm64', '解压后运行 start.command，启动方式以对应发行说明为准。', '⌘'],
  ['macos', 'macOS', 'Intel x64', 'macos-x86_64', '安装方式请查看对应发行说明。', '⌘']
];
function element(tag, className, content) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (content !== undefined) item.textContent = content;
  return item;
}
function githubLink(label, url, className = '') {
  const link = element('a', className, label);
  const parsed = new URL(url);
  if (parsed.protocol !== 'https:' || parsed.hostname !== 'github.com') throw new Error('Unexpected release URL');
  link.href = parsed.href;
  return link;
}
function appendInline(parent, text) {
  const pattern = /\[([^\]]+)\]\((https:\/\/github\.com\/[^\s)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*/g;
  let position = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(position, match.index)));
    if (match[1]) parent.append(githubLink(match[1], match[2]));
    else parent.append(element(match[3] ? 'code' : 'strong', '', match[3] || match[4]));
    position = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(position)));
}
function releaseMarkdown(markdown) {
  const fragment = document.createDocumentFragment();
  let list = null;
  let code = null;
  for (const line of markdown.replace(/<!--[\s\S]*?-->/g, '').split('\n')) {
    if (line.startsWith('```')) {
      list = null;
      if (code) code = null;
      else {
        const pre = element('pre');
        code = element('code');
        pre.append(code);
        fragment.append(pre);
      }
      continue;
    }
    if (code) { code.append(document.createTextNode(line + '\n')); continue; }
    if (!line.trim()) { list = null; continue; }
    let target;
    if (/^[-*] /.test(line)) {
      if (!list) { list = element('ul'); fragment.append(list); }
      target = element('li');
      appendInline(target, line.slice(2));
      list.append(target);
      continue;
    }
    list = null;
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    target = element(heading ? 'h' + Math.min(4, Math.max(3, heading[1].length + 1)) : 'p');
    appendInline(target, heading ? heading[2] : line);
    fragment.append(target);
  }
  return fragment;
}
function updateDownloads(releases) {
  const grid = document.querySelector('[data-download-grid]');
  if (!grid) return;
  const fragment = document.createDocumentFragment();
  for (const [platform, title, architecture, match, note, icon] of platformDefinitions) {
    let selected;
    for (const release of releases) {
      const asset = release.assets.find(item => item.name.toLowerCase().includes(match) && /\.(zip|tar\.gz|dmg)$/i.test(item.name));
      if (asset) { selected = { release, asset }; break; }
    }
    if (!selected) continue;
    const { release, asset } = selected;
    const card = element('article', 'download-card');
    card.dataset.platform = platform;
    const platformIcon = element('div', 'platform-icon', icon);
    platformIcon.setAttribute('aria-hidden', 'true');
    card.append(platformIcon, element('h2', '', title));
    const meta = element('div', 'download-meta');
    for (const label of [architecture, release.tag_name, (asset.size / 1024 / 1024).toFixed(1) + ' MiB']) meta.append(element('span', '', label));
    card.append(meta);
    if (release.tag_name !== releases[0].tag_name) card.append(element('span', 'archive', '历史可用版本 · ' + release.tag_name));
    card.append(element('p', '', note));
    const download = githubLink('下载 ' + architecture + ' ↓', asset.browser_download_url, 'button primary');
    download.setAttribute('aria-label', `下载 ${title} ${architecture} ${release.tag_name}`);
    card.append(download, element('span', 'file-name', asset.name));
    const links = element('div', 'secondary-links');
    links.append(githubLink('发行说明 ↗', release.html_url));
    const checksum = release.assets.find(item => item.name === 'SHA256SUMS.txt');
    if (checksum) links.append(githubLink('SHA-256 校验文件 ↗', checksum.browser_download_url));
    card.append(links);
    fragment.append(card);
  }
  grid.replaceChildren(fragment);
  const selected = document.querySelector('[data-platform-filter][aria-pressed="true"]');
  selected?.click();
}
function updateChangelog(releases) {
  const container = document.querySelector('[data-changelog]');
  if (!container) return;
  const entries = document.createDocumentFragment();
  const index = document.createDocumentFragment();
  releases.forEach((release, number) => {
    const article = element('article', 'release-entry');
    article.id = 'release-' + release.tag_name;
    const heading = element('header', 'release-heading');
    const title = element('h2', '', release.tag_name);
    if (number === 0) title.append(element('span', 'release-badge', '最新正式版'));
    const date = element('time', 'release-date', release.published_at.slice(0, 10));
    date.dateTime = release.published_at;
    heading.append(title, date);
    const body = element('div', 'release-body');
    const notes = release.body || '本版本的详细信息请查看 GitHub Release。';
    const highlights = notes.split(/^## (?:安装包与平台|安装与升级说明)/m)[0];
    body.append(releaseMarkdown(highlights));
    const details = element('details');
    details.append(element('summary', '', '完整发行说明与升级信息'), releaseMarkdown(notes));
    body.append(details);
    article.append(heading, body, githubLink('在 GitHub 查看此版本 ↗', release.html_url, 'button'));
    entries.append(article);
    const anchor = element('a', '', release.tag_name);
    anchor.href = '#' + article.id;
    index.append(anchor);
  });
  container.replaceChildren(entries);
  document.querySelector('[data-version-index]').replaceChildren(index);
}
async function refreshReleases() {
  const status = document.querySelector('[data-release-status]');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch('https://api.github.com/repos/zhongxinxuhk/DBCheck/releases?per_page=30', {
      signal: controller.signal,
      headers: { Accept: 'application/vnd.github+json' }
    });
    if (!response.ok) throw new Error('GitHub Releases unavailable');
    const data = await response.json();
    const releases = data.filter(item => !item.draft && !item.prerelease);
    if (!releases.length) throw new Error('No stable releases');
    document.querySelectorAll('[data-latest-version]').forEach(item => { item.textContent = releases[0].tag_name; });
    document.querySelectorAll('[data-latest-date]').forEach(item => { item.textContent = releases[0].published_at.slice(0, 10); });
    updateDownloads(releases);
    updateChangelog(releases);
    if (status) status.textContent = '已同步 GitHub Releases · 最新正式版 ' + releases[0].tag_name;
  } catch {
    if (status) {
      status.replaceChildren(document.createTextNode('当前显示网站构建时的发行快照。最新记录请查看 '), githubLink('GitHub Releases ↗', repositoryUrl + '/releases', 'accent'));
    }
  } finally { clearTimeout(timeout); }
}
refreshReleases();
