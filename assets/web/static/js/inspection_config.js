// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
// Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck
// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

/**
 * 巡检配置管理 - 前端逻辑
 * 功能：模板列表、模板编辑、章节管理、SQL 查询管理、导入/导出、拖拽排序
 */

// ==================== 全局状态 ====================
let currentInspectionTemplateId = null;
let currentInspectionChapterId = null;
let inspectionQueryUnsaved = false;  // 查询内联编辑是否有未保存内容
let currentInspectionIsPreset = false;  // 当前编辑的模板是否为预置模板
let inspectionTemplateCatalog = [];     // 模板目录（含章节/指标摘要）
const inspectionTemplateDetails = new Map();  // 已加载的展开明细
const ICFG_CATEGORY_LABELS = { baseline: '通用基线', business: '业务专项', custom: '自定义' };
const ICFG_DB_LABELS = {
    mysql: 'MySQL', postgresql: 'PostgreSQL', oracle: 'Oracle', sqlserver: 'SQL Server',
    dm8: 'DM8 达梦', tidb: 'TiDB', ivorysql: 'IvorySQL', mariadb: 'MariaDB',
    oceanbase: 'OceanBase', clickhouse: 'ClickHouse', db2: 'DB2', gbase: 'GBase 8s',
    gbase8a: 'GBase 8a', halodb: 'HaloDB', hgdb: 'HGDB', kingbase: 'KingbaseES',
    mongodb: 'MongoDB', redis: 'Redis', 'redis-cluster': 'Redis Cluster',
    uxdb: 'UXDB', vastbase: 'Vastbase', yashandb: 'YashanDB', oracle_jdbc: 'Oracle JDBC',
    sqlserver_jdbc: 'SQL Server JDBC'
};
function icfgDbLabel(dbType) {
    return ICFG_DB_LABELS[dbType] || dbType || '—';
}
const ICFG_DOMAIN_KEYS = {
    availability: 'availability', capacity: 'capacity', backup: 'backup',
    performance: 'performance', lock: 'lock', object: 'object',
    security: 'security', ha: 'ha', config: 'config', job: 'job', ledger: 'ledger'
};
const ICFG_CYCLE_KEYS = {
    '日巡检': 'daily', '周巡检': 'weekly', '月度健康检查': 'monthly',
    '月度专项巡检': 'monthly_special', '季度运行分析': 'quarterly',
    '年度台账核对': 'annual', '备份校验与恢复验证': 'backup',
    '变更前评估与变更后验证': 'change', '故障应急取证': 'incident',
    '集群架构专项': 'cluster', '新库上线与迁移验收': 'newdb',
    '性能问题专项诊断': 'performance'
};

function it(key, fallback) {
    if (typeof i18n === 'function') {
        const value = i18n(key);
        if (value !== undefined && value !== '') return value;
    }
    return fallback;
}

function itf(key, fallback, vars) {
    const text = it(key, fallback);
    return String(text).replace(/\{(\w+)\}/g, (match, name) =>
        vars && vars[name] !== undefined ? String(vars[name]) : match);
}

function icfgCategoryLabel(category) {
    return it('webui.inspection_category_' + category,
        ICFG_CATEGORY_LABELS[category] || category || '—');
}

function icfgCycleLabel(cycle) {
    const key = ICFG_CYCLE_KEYS[cycle];
    return key ? it('webui.inspection_cycle_' + key, cycle) : (cycle || '');
}

function icfgDomainLabel(domain) {
    const key = ICFG_DOMAIN_KEYS[domain?.key];
    return key ? it('webui.inspection_domain_' + key, domain.label || key) : (domain?.label || '—');
}

function icfgListSeparator() {
    return (typeof CURRENT_LANG !== 'undefined' && CURRENT_LANG === 'zh') ? '、' : ', ';
}

// 写操作（保存/删除/创建）需要登录：index.html 通过 Jinja 注入 window._IS_ADMIN
// 已放宽限制：所有登录用户都可使用 SQL 查询的查看、新增、保存、删除功能
const IS_ADMIN = window._IS_ADMIN === true;
const IS_LOGGED_IN = window._IS_ADMIN !== undefined;  // 所有登录用户都可以查看


// 搜索仅控制已加载条目的显示，保留内联编辑内容与完整列表顺序。
function filterInspectionConfig(kind) {
    if (kind === 'template') { applyInspectionTemplateFilters(); return; }
    const settings = {
        chapter: ['inspection-chapter-search', 'inspection-chapter-list', '.chapter-item'],
        query: ['inspection-query-search', 'inspection-query-list', '.query-item']
    };
    const [inputId, containerId, selector] = settings[kind];
    const input = document.getElementById(inputId);
    const container = document.getElementById(containerId);
    if (!input || !container) return;
    const words = input.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const rows = [...container.querySelectorAll(selector)];
    let count = 0;
    rows.forEach(row => {
        const text = (row.dataset.searchText || row.textContent).toLocaleLowerCase();
        row.hidden = !words.every(word => text.includes(word));
        if (!row.hidden) count++;
    });
    const status = document.getElementById('inspection-' + kind + '-search-status');
    if (status) status.textContent = rows.length ? (count ? `显示 ${count} / ${rows.length} 项` : '没有匹配项，请更换关键词或清空搜索') : '';
    if (kind === 'chapter' && window._inspectionChapterSortable) {
        window._inspectionChapterSortable.option('disabled', words.length > 0);
    }
}

function clearInspectionConfigSearch(kind) {
    const input = document.getElementById('inspection-' + kind + '-search');
    if (input) { input.value = ''; filterInspectionConfig(kind); input.focus(); }
}

function indexInspectionConfigRows(kind, records, fields) {
    const config = {
        chapter: ['inspection-chapter-list', '.chapter-item'],
        query: ['inspection-query-list', '.query-item']
    };
    const [containerId, selector] = config[kind];
    document.getElementById(containerId).querySelectorAll(selector).forEach((row, index) => {
        row.dataset.searchText = fields.map(field => records[index][field] ?? '').join(' ');
    });
    filterInspectionConfig(kind);
}

// ==================== 页面初始化 ====================

/**
 * 显示巡检配置管理页面
 */
function showInspectionConfigPage() {
    // 不再重复隐藏/显示页面 — showPage() 已经处理过了
    const page = document.getElementById('page-inspection-config');
    if (page) page.style.display = 'flex';
    // 不直接用 getI18N()，交给 applyI18N() 统一处理
    // 仅确保页面显示，标题由 showPage() + applyI18N() 负责
    showInspectionTemplateList();
}


/**
 * 显示模板列表页面
 */
async function showInspectionTemplateList() {
    if (currentInspectionChapterId && inspectionQueryUnsaved) {
        const confirmed = await showConfirmDialog(
            it('webui.inspection_unsaved_title', '未保存'),
            it('webui.inspection_unsaved_message', '当前有未保存的修改，确定要返回列表吗？'));
        if (!confirmed) return;
    }
    const listPage = document.getElementById('inspection-template-list-page');
    const editPage = document.getElementById('inspection-template-edit-page');
    if (listPage) listPage.style.display = 'block';
    if (editPage) editPage.style.display = 'none';
    currentInspectionChapterId = null;
    inspectionQueryUnsaved = false;
    currentInspectionIsPreset = false;
    loadInspectionTemplates();
}


// ==================== 模板列表 ====================

async function loadInspectionTemplates() {
    const container = document.getElementById('inspection-template-list');
    if (container && !container.children.length) {
        container.innerHTML = `<tr><td colspan="7" class="icfg-loading">${it('webui.inspection_loading_templates', '正在加载巡检模板…')}</td></tr>`;
    }
    try {
        const res = await fetch('/api/inspection/templates');
        const data = await res.json();
        if (data.success) {
            renderInspectionTemplateList(data.data);
        } else {
            toast(data.message || it('webui.inspection_load_failed', '加载模板列表失败'), 'error');
        }
    } catch (e) {
        toast(it('webui.inspection_load_failed', '加载模板列表失败') + ': ' + e.message, 'error');
    }
}

function filterInspectionTemplateState() {
    const input = document.getElementById('inspection-template-search');
    const db = document.getElementById('inspection-filter-db');
    const category = document.getElementById('inspection-filter-category');
    const cycle = document.getElementById('inspection-filter-cycle');
    const gap = document.getElementById('inspection-filter-gap');
    return {
        words: (input?.value || '').trim().toLocaleLowerCase().split(/\s+/).filter(Boolean),
        dbType: db?.value || '',
        category: category?.value || '',
        cycle: cycle?.value || '',
        gap: gap?.value || ''
    };
}

function applyInspectionTemplateFilters() {
    const container = document.getElementById('inspection-template-list');
    if (!container) return;
    const state = filterInspectionTemplateState();
    const rows = [...container.querySelectorAll('.icfg-row')];
    let count = 0;
    rows.forEach(row => {
        const searchText = (row.dataset.searchText || row.textContent || '').toLocaleLowerCase();
        const match = state.words.every(word => searchText.includes(word))
            && (!state.dbType || row.dataset.dbType === state.dbType)
            && (!state.category || row.dataset.category === state.category)
            && (!state.cycle || row.dataset.cycle === state.cycle)
            && (!state.gap || (state.gap === 'gap'
                ? row.dataset.baselineGap === '1'
                : row.dataset.domainFull === '1'));
        row.hidden = !match;
        if (match) count++;
        if (!match) {
            row.classList.remove('is-open');
            const detail = document.getElementById('icfg-detail-' + row.dataset.templateId);
            if (detail) detail.hidden = true;
        }
    });
    const status = document.getElementById('inspection-template-search-status');
    if (status) status.textContent = rows.length
        ? itf('webui.inspection_show_range', '显示 {count} / {total} 个模板', {count, total: rows.length})
        : '';
    const visible = document.getElementById('inspection-list-visible-count');
    if (visible) visible.textContent = count;
    const scroll = document.getElementById('inspection-table-scroll');
    if (scroll) scroll.hidden = rows.length > 0 && count === 0;
    const empty = document.getElementById('inspection-list-empty');
    if (empty) {
        empty.hidden = count !== 0;
        if (count === 0 && rows.length) {
            empty.textContent = it('webui.inspection_no_match', '没有匹配的巡检模板，请调整搜索或筛选条件。');
        }
    }
}

function resetInspectionTemplateFilters() {
    ['inspection-template-search', 'inspection-filter-db', 'inspection-filter-category',
     'inspection-filter-cycle', 'inspection-filter-gap']
        .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    filterInspectionConfig('template');
    document.getElementById('inspection-template-search')?.focus();
}

function populateInspectionTemplateFilters(templates) {
    const dbSelect = document.getElementById('inspection-filter-db');
    const cycleSelect = document.getElementById('inspection-filter-cycle');
    const dbLabels = {
        mysql: 'MySQL', postgresql: 'PostgreSQL', oracle: 'Oracle', sqlserver: 'SQL Server',
        dm8: 'DM8 达梦', tidb: 'TiDB', ivorysql: 'IvorySQL', mariadb: 'MariaDB',
        oceanbase: 'OceanBase', clickhouse: 'ClickHouse', db2: 'DB2', gbase: 'GBase 8s',
        gbase8a: 'GBase 8a', halodb: 'HaloDB', hgdb: 'HGDB', kingbase: 'KingbaseES',
        mongodb: 'MongoDB', redis: 'Redis', 'redis-cluster': 'Redis Cluster',
        uxdb: 'UXDB', vastbase: 'Vastbase', yashandb: 'YashanDB', oracle_jdbc: 'Oracle JDBC',
        sqlserver_jdbc: 'SQL Server JDBC'
    };
    if (dbSelect) {
        const current = dbSelect.value;
        const dbs = [...new Set(templates.map(t => t.db_type).filter(Boolean))]
            .sort((a, b) => (dbLabels[a] || a).localeCompare(dbLabels[b] || b, 'zh-CN'));
        dbSelect.innerHTML = `<option value="">${it('webui.inspection_filter_db_all', '全部数据库')}</option>` + dbs.map(db =>
            `<option value="${escapeHtml(db)}">${escapeHtml(dbLabels[db] || db)}</option>`).join('');
        dbSelect.value = dbs.includes(current) ? current : '';
    }
    if (cycleSelect) {
        const current = cycleSelect.value;
        const cycles = [...new Set(templates.filter(t => t.business).map(t => t.cycle).filter(Boolean))]
            .sort((a, b) => a.localeCompare(b, 'zh-CN'));
        cycleSelect.innerHTML = `<option value="">${it('webui.inspection_filter_type_all', '全部类型')}</option>` + cycles.map(cycle =>
            `<option value="${escapeHtml(cycle)}">${escapeHtml(icfgCycleLabel(cycle))}</option>`).join('');
        cycleSelect.value = cycles.includes(current) ? current : '';
    }
}

function inspectionTemplateStatus(t) {
    const domainTotal = Number(t.standard_domain_count) || 0;
    const domainCovered = Number(t.covered_domain_count) || 0;
    const isBaseline = t.category === 'baseline';
    const hasGap = isBaseline && domainTotal > 0 && domainCovered < domainTotal;
    const domainTone = !hasGap
        ? (isBaseline ? 'ok' : 'special')
        : (domainCovered >= Math.ceil(domainTotal * 0.75) ? 'mixed' : 'warn');
    const domainBadge = domainTotal
        ? `<span class="icfg-domain icfg-domain--${domainTone}">${escapeHtml(itf('webui.inspection_domain_badge', '标准域 {covered}/{total}', {covered: domainCovered, total: domainTotal}))}</span>`
        : '';
    let modeBadge;
    if (t.empty_chapter_count > 0) {
        modeBadge = `<span class="icfg-status icfg-status--warn">${escapeHtml(itf('webui.inspection_status_empty_chapters', '{n} 个空章节', {n: t.empty_chapter_count}))}</span>`;
    } else if (t.manual_only_chapter_count > 0) {
        modeBadge = `<span class="icfg-status icfg-status--manual">${escapeHtml(itf('webui.inspection_status_manual_chapters', '{n} 个主机采集章节', {n: t.manual_only_chapter_count}))}</span>`;
    } else if (t.manual_count > 0) {
        modeBadge = `<span class="icfg-status icfg-status--mixed">${escapeHtml(it('webui.inspection_status_mixed', 'SQL + 主机采集'))}</span>`;
    } else {
        modeBadge = `<span class="icfg-status icfg-status--ok">${escapeHtml(it('webui.inspection_status_sql_full', 'SQL 全覆盖'))}</span>`;
    }
    return `<span class="icfg-status-stack">${domainBadge}${modeBadge}</span>`;
}

function renderInspectionTemplateRow(t) {
    const dbTypeMap = {
        mysql: 'MySQL', postgresql: 'PostgreSQL', oracle: 'Oracle',
        sqlserver: 'SQL Server', dm8: 'DM8 达梦', tidb: 'TiDB', ivorysql: 'IvorySQL',
        mariadb: 'MariaDB', oceanbase: 'OceanBase', clickhouse: 'ClickHouse',
        db2: 'DB2', gbase: 'GBase 8s', gbase8a: 'GBase 8a', halodb: 'HaloDB',
        hgdb: 'HGDB', kingbase: 'KingbaseES', mongodb: 'MongoDB', redis: 'Redis',
        'redis-cluster': 'Redis Cluster', uxdb: 'UXDB', vastbase: 'Vastbase',
        yashandb: 'YashanDB', oracle_jdbc: 'Oracle JDBC', sqlserver_jdbc: 'SQL Server JDBC'
    };
    const dbTypeIconMap = {
        mysql: '🐬', postgresql: '🦴', oracle: '🔶', sqlserver: '🗄️', dm8: '💎',
        tidb: '🌊', ivorysql: '🐘', mariadb: '🐬', oceanbase: '🌊',
        clickhouse: '⚡', db2: '🗃️', gbase: '🗄️', gbase8a: '🧮', halodb: '🪐',
        hgdb: '🐘', kingbase: '🐘', mongodb: '🍃', redis: '🧱',
        'redis-cluster': '🧱', uxdb: '🐘', vastbase: '🐘', yashandb: '🔷',
        oracle_jdbc: '🔶', sqlserver_jdbc: '🗄️'
    };
    const dbLabel = dbTypeMap[t.db_type] || t.db_type || '—';
    const dbIcon = dbTypeIconMap[t.db_type] || '📋';
    const categoryLabel = icfgCategoryLabel(t.category);
    const presetBadge = t.is_preset == 1
        ? `<span class="icfg-badge icfg-badge--preset">${escapeHtml(it('webui.inspection_badge_preset', '预置'))}</span>` : '';
    const defaultBadge = t.is_default
        ? `<span class="icfg-badge icfg-badge--default">${escapeHtml(it('webui.inspection_badge_default', '默认'))}</span>` : '';
    const businessBadge = t.business
        ? `<span class="icfg-badge icfg-badge--business">${escapeHtml(it('webui.inspection_badge_business', '业务专项'))}</span>` : '';
    const version = t.version ? `<span class="icfg-version">${escapeHtml(t.version)}</span>` : '';
    const desc = t.description ? `<span class="icfg-template-desc">${escapeHtml(t.description)}</span>` : '';
    const cycle = t.cycle ? `<span class="icfg-cycle">${escapeHtml(icfgCycleLabel(t.cycle))}</span>` : '';
    const searchText = [
        t.search_text, t.template_name, t.template_name_en, t.db_type, dbLabel, t.version,
        t.description, t.category, categoryLabel, t.cycle
    ].filter(Boolean).join(' ');
    const categoryBadge = t.business ? '' : `<span class="icfg-category">${escapeHtml(categoryLabel)}</span>`;
    const inlineMeta = `<span class="icfg-inline-meta">${dbIcon} ${escapeHtml(dbLabel)}${t.cycle ? ' · ' + escapeHtml(icfgCycleLabel(t.cycle)) : ''}${t.business ? ' · ' + escapeHtml(it('webui.inspection_badge_business', '业务专项')) : ''}</span>`;
    const cloneButton = IS_ADMIN
        ? `<button type="button" class="icfg-action" data-icfg-action="clone">${escapeHtml(it('webui.inspection_clone', '另存为'))}</button>` : '';
    const defaultButton = (IS_ADMIN && !t.business && !Number(t.is_default))
        ? `<button type="button" class="icfg-action" data-icfg-action="default">${escapeHtml(it('webui.inspection_set_default', '设为默认'))}</button>` : '';
    const deleteButton = (IS_ADMIN && !t.is_preset)
        ? `<button type="button" class="icfg-action icfg-action--danger" data-icfg-action="delete">${escapeHtml(it('webui.inspection_delete', '删除'))}</button>` : '';
    const detailId = 'icfg-detail-' + t.id;
    return `
    <tr class="icfg-row" data-template-id="${t.id}" data-db-type="${escapeHtml(t.db_type || '')}"
        data-category="${escapeHtml(t.category || '')}" data-cycle="${escapeHtml(t.cycle || '')}"
        data-baseline-gap="${t.has_baseline_gap ? '1' : '0'}"
        data-domain-full="${(Number(t.covered_domain_count) || 0) >= (Number(t.standard_domain_count) || 0) && Number(t.standard_domain_count) ? '1' : '0'}"
        data-search-text="${escapeHtml(searchText)}" tabindex="0" aria-expanded="false"
        aria-controls="${detailId}">
        <td class="icfg-col-template">
            <div class="icfg-template-cell">
                <span class="icfg-caret" aria-hidden="true">›</span>
                <span class="icfg-template-main">
                    <span class="icfg-template-title">${escapeHtml(t.template_name)}${version}</span>
                    ${desc}
                    ${inlineMeta}
                </span>
            </div>
        </td>
        <td class="icfg-col-db"><span class="icfg-db"><span class="icfg-db-icon">${dbIcon}</span>${escapeHtml(dbLabel)}</span></td>
        <td class="icfg-col-type">${businessBadge}${categoryBadge}${cycle}${presetBadge}${defaultBadge}</td>
        <td class="icfg-col-num"><span class="icfg-num">${t.chapter_count || 0}</span><span class="icfg-unit">${escapeHtml(it('webui.inspection_unit_chapter', '章'))}</span></td>
        <td class="icfg-col-coverage">
            <span class="icfg-num">${t.indicator_count || 0}</span><span class="icfg-unit">${escapeHtml(it('webui.inspection_unit_item', '项'))}</span>
            <span class="icfg-sub">${escapeHtml(itf('webui.inspection_sql_host', 'SQL {sql} · 主机 {host}', {sql: t.sql_count || 0, host: t.manual_count || 0}))}</span>
        </td>
        <td class="icfg-col-status">${inspectionTemplateStatus(t)}</td>
        <td class="icfg-col-actions">
            <button type="button" class="icfg-action" data-icfg-action="edit">${escapeHtml(it('webui.inspection_edit', '编辑'))}</button>
            <button type="button" class="icfg-action" data-icfg-action="export">${escapeHtml(it('webui.inspection_export', '导出'))}</button>
            ${cloneButton}
            ${defaultButton}
            ${deleteButton}
        </td>
    </tr>
    <tr class="icfg-detail-row" id="${detailId}" hidden>
        <td colspan="7">
            <div class="icfg-detail"><div class="icfg-detail-loading">${escapeHtml(it('webui.inspection_expand_loading', '展开后加载章节与指标明细…'))}</div></div>
        </td>
    </tr>`;
}

function renderInspectionTemplateList(templates) {
    const container = document.getElementById('inspection-template-list');
    if (!container) return;
    inspectionTemplateCatalog = Array.isArray(templates) ? templates : [];
    inspectionTemplateDetails.clear();
    const empty = document.getElementById('inspection-list-empty');
    if (!inspectionTemplateCatalog.length) {
        container.innerHTML = '';
        if (empty) { empty.hidden = false; empty.textContent = it('webui.inspection_empty_list', '暂无巡检模板，请点击“创建模板”按钮添加。'); }
        applyInspectionTemplateFilters();
        return;
    }
    if (empty) empty.hidden = true;
    populateInspectionTemplateFilters(inspectionTemplateCatalog);
    container.innerHTML = inspectionTemplateCatalog.map(renderInspectionTemplateRow).join('');
    const business = inspectionTemplateCatalog.filter(t => t.business).length;
    const chapterTotal = inspectionTemplateCatalog.reduce((sum, t) => sum + (Number(t.chapter_count) || 0), 0);
    const baselineCount = inspectionTemplateCatalog.filter(t => t.category === 'baseline').length;
    const baselineFull = inspectionTemplateCatalog.filter(t => t.category === 'baseline' && !t.has_baseline_gap).length;
    const sqlTotal = inspectionTemplateCatalog.reduce((sum, t) => sum + (Number(t.sql_count) || 0), 0);
    const indicatorTotal = inspectionTemplateCatalog.reduce((sum, t) => sum + (Number(t.indicator_count) || 0), 0);
    const dbTotal = new Set(inspectionTemplateCatalog.map(t => t.db_type)).size;
    const summary = document.getElementById('inspection-list-summary');
    if (summary) {
        const chip = (text) => `<span class="icfg-summary-chip">${text}</span>`;
        const parts = [
            itf('webui.inspection_summary_templates', '{n} 个模板', {n: `<b>${inspectionTemplateCatalog.length}</b>`}),
            itf('webui.inspection_summary_business', '{n} 个业务专项', {n: `<b>${business}</b>`}),
            itf('webui.inspection_summary_db_types', '{n} 个数据库类型', {n: `<b>${dbTotal}</b>`}),
            itf('webui.inspection_summary_chapters', '{n} 个章节', {n: `<b>${chapterTotal}</b>`}),
            itf('webui.inspection_summary_sql', '{n} 条 SQL', {n: `<b>${sqlTotal}</b>`}),
            itf('webui.inspection_summary_indicators', '{n} 个检查项', {n: `<b>${indicatorTotal}</b>`}),
            itf('webui.inspection_summary_baseline', '{total} 个通用基线 · {full} 个全覆盖',
                {total: `<b>${baselineCount}</b>`, full: `<b>${baselineFull}</b>`}),
            itf('webui.inspection_summary_gap', '{n} 个基线缺口',
                {n: `<b>${inspectionTemplateCatalog.filter(t => t.has_baseline_gap).length}</b>`}),
        ];
        summary.innerHTML = parts.map(chip).join('') +
            `<span class="icfg-summary-chip icfg-summary-chip--muted">${itf('webui.inspection_summary_visible', '当前显示 {n} 个', {n: `<b id="inspection-list-visible-count">${inspectionTemplateCatalog.length}</b>`})}</span>`;
    }
    applyInspectionTemplateFilters();
}

async function toggleInspectionTemplateRow(templateId) {
    const row = document.querySelector(`.icfg-row[data-template-id="${templateId}"]`);
    const detail = document.getElementById('icfg-detail-' + templateId);
    if (!row || !detail) return;
    if (!detail.hidden) {
        detail.hidden = true;
        row.classList.remove('is-open');
        row.setAttribute('aria-expanded', 'false');
        return;
    }
    detail.hidden = false;
    row.classList.add('is-open');
    row.setAttribute('aria-expanded', 'true');
    if (detail.dataset.loaded === '1') return;
    detail.dataset.loaded = '1';
    const body = detail.querySelector('.icfg-detail');
    body.innerHTML = `<div class="icfg-detail-loading">${it('webui.inspection_loading_detail', '正在加载章节与指标明细…')}</div>`;
    try {
        const res = await fetch(`/api/inspection/templates/${templateId}/coverage`);
        const data = await res.json();
        if (!data.success) throw new Error(data.message || it('webui.inspection_detail_failed', '加载模板明细失败'));
        inspectionTemplateDetails.set(Number(templateId), data.data);
        body.innerHTML = renderInspectionTemplateCoverage(data.data);
        requestAnimationFrame(() => detail.scrollIntoView({ block: 'nearest' }));
    } catch (e) {
        detail.dataset.loaded = '0';
        body.innerHTML = `<div class="icfg-detail-error">${escapeHtml(e.message || it('webui.inspection_detail_failed', '加载模板明细失败'))}</div>`;
    }
}

function renderInspectionTemplateCoverage(t) {
    const chapters = t.chapter_summary || [];
    const sqlTotal = Number(t.sql_count) || 0;
    const manualTotal = Number(t.manual_count) || 0;
    let html = `
        <div class="icfg-detail-head">
            <div>
                <span class="icfg-detail-kicker">${escapeHtml(itf('webui.inspection_detail_kicker', '模板 #{id} · {category}', {
                    id: t.id, category: icfgCategoryLabel(t.category) + (t.cycle ? ' · ' + icfgCycleLabel(t.cycle) : '')
                }))}</span>
                <h3>${escapeHtml(t.template_name)}</h3>
                ${t.description ? `<p>${escapeHtml(t.description)}</p>` : ''}
            </div>
            <div class="icfg-detail-actions">
                <button type="button" class="btn btn-sm btn-primary" data-icfg-action="edit">${escapeHtml(it('webui.inspection_edit_template', '编辑模板'))}</button>
                <button type="button" class="btn btn-sm" data-icfg-action="export">${escapeHtml(it('webui.inspection_export_template', '导出模板'))}</button>
                ${IS_ADMIN ? `<button type="button" class="btn btn-sm" data-icfg-action="clone">${escapeHtml(it('webui.inspection_clone_template', '另存为模板'))}</button>` : ''}
                ${(IS_ADMIN && !t.business && !Number(t.is_default)) ? `<button type="button" class="btn btn-sm" data-icfg-action="default">${escapeHtml(it('webui.inspection_set_default', '设为默认'))}</button>` : ''}
            </div>
        </div>
        <div class="icfg-detail-stats">
            <span>${escapeHtml(itf('webui.inspection_detail_chapters', '{n} 章节', {n: chapters.length}))}</span>
            <span>${escapeHtml(itf('webui.inspection_detail_sql', '{n} 条 SQL 自动采集', {n: sqlTotal}))}</span>
            <span>${escapeHtml(itf('webui.inspection_detail_manual', '{n} 项主机/命令行采集', {n: manualTotal}))}</span>
            <span>${escapeHtml(itf('webui.inspection_detail_indicators', '{n} 个检查项', {n: t.indicator_count || 0}))}</span>
            <span>${escapeHtml(itf('webui.inspection_detail_domain_coverage', '{coverage} 标准域覆盖', {coverage: t.standard_coverage || '0/0'}))}</span>
        </div>
        <div class="icfg-domain-panel">
            <div class="icfg-domain-panel-head">
                <span>${escapeHtml(it(t.category === 'baseline'
                    ? 'webui.inspection_domain_panel'
                    : 'webui.inspection_domain_panel_special',
                    t.category === 'baseline' ? '标准巡检域' : '巡检域（专项按场景裁剪）'))}</span>
                <span class="icfg-domain-hint">${escapeHtml((t.missing_domain_keys?.length || t.missing_domains?.length)
                    ? itf(t.category === 'baseline' ? 'webui.inspection_domain_gap' : 'webui.inspection_domain_excluded',
                        t.category === 'baseline' ? '缺口：{list}' : '未纳入：{list}',
                        {list: (t.missing_domain_keys || []).map(key => icfgDomainLabel({key})).join(icfgListSeparator())
                            || (t.missing_domains || []).join(icfgListSeparator())})
                    : it('webui.inspection_domain_all_covered', '全部覆盖'))}</span>
            </div>
            <div class="icfg-domain-list">${(t.standard_domains || []).map(domain =>
                `<span class="icfg-domain-chip ${domain.covered ? 'is-covered' : (t.category === 'baseline' ? 'is-missing' : 'is-special')}">${escapeHtml(icfgDomainLabel(domain))}</span>`).join('')}</div>
        </div>`;
    if (!chapters.length) {
        return html + `<div class="icfg-detail-error">${escapeHtml(it('webui.inspection_no_chapters', '该模板暂无启用章节。'))}</div>`;
    }
    html += '<div class="icfg-chapters">' + chapters.map(ch => {
        const indicators = ch.indicators || [];
        const indicatorHtml = indicators.length
            ? indicators.map(item => `
                <span class="icfg-indicator icfg-indicator--${escapeHtml(item.mode)}" title="${escapeHtml(item.description || item.title || '')}">
                    <span class="icfg-indicator-id">${escapeHtml(item.id || '—')}</span>
                    <span class="icfg-indicator-title">${escapeHtml(item.title || it('webui.inspection_untitled_item', '未命名检查项'))}</span>
                    ${item.mode === 'sql' && item.query_sql
                        ? `<span class="icfg-indicator-mode icfg-sql-trigger" role="button" tabindex="0"
                               data-icfg-sql="${escapeHtml(item.query_sql)}"
                               data-sql-title="${escapeHtml((item.id ? '[' + item.id + '] ' : '') + (item.title || ''))}"
                               title="${escapeHtml(it('webui.inspection_view_sql', '查看 SQL'))}">SQL</span>`
                        : `<span class="icfg-indicator-mode">${item.mode === 'sql' ? 'SQL' : escapeHtml(it('webui.inspection_manual_mode', '主机'))}</span>`}
                </span>`).join('')
            : `<span class="icfg-chapter-empty">${escapeHtml(it('webui.inspection_no_items', '未配置检查项'))}</span>`;
        const note = ch.description
            ? `<details class="icfg-chapter-note"><summary>${escapeHtml(it('webui.inspection_chapter_notes', '查看章节说明与阈值'))}</summary><pre>${escapeHtml(ch.description)}</pre></details>`
            : '';
        return `
            <section class="icfg-chapter">
                <header class="icfg-chapter-head">
                    <span class="icfg-chapter-num">${escapeHtml(String(ch.chapter_number))}</span>
                    <h4>${escapeHtml(ch.chapter_title || it('webui.inspection_untitled_chapter', '未命名章节'))}</h4>
                    <span class="icfg-chapter-counts">${escapeHtml(itf('webui.inspection_sql_host', 'SQL {sql} · 主机 {host}', {sql: ch.sql_count || 0, host: ch.manual_count || 0}))}</span>
                </header>
                <div class="icfg-indicators">${indicatorHtml}</div>
                ${note}
            </section>`;
    }).join('') + '</div>';
    return html;
}

async function cloneInspectionTemplate(templateId) {
    const t = inspectionTemplateCatalog.find(item => Number(item.id) === Number(templateId));
    if (!t) return;
    const defaultName = `${t.template_name}${it('webui.inspection_clone_suffix', ' 副本')}`;
    const name = await showPromptDialog(
        it('webui.inspection_clone_prompt_title', '另存为模板'),
        it('webui.inspection_clone_prompt_message', '输入新模板名称。新模板会复制当前模板的全部章节和 SQL，原模板保持不变。'),
        defaultName
    );
    if (name === null) return;
    const trimmed = String(name).trim();
    if (!trimmed) {
        toast(it('webui.inspection_clone_failed', '另存为模板失败') + ': ' + it('webui.inspection_template_name', '模板名称') + '不能为空', 'error');
        return;
    }
    try {
        const res = await fetch(`/api/inspection/templates/${templateId}/clone`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({template_name: trimmed})
        });
        const data = await res.json();
        if (!data.success) throw new Error(data.message || it('webui.inspection_clone_failed', '另存为模板失败'));
        toastSuccess(itf('webui.inspection_clone_success', '已创建模板：{name}',
            {name: data.data?.template_name || trimmed}));
        await showInspectionTemplateList();
    } catch (e) {
        toast(it('webui.inspection_clone_failed', '另存为模板失败') + ': ' + e.message, 'error');
    }
}

async function setInspectionTemplateDefault(templateId) {
    try {
        const res = await fetch(`/api/inspection/templates/${templateId}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({is_default: 1})
        });
        const data = await res.json();
        if (!data.success) throw new Error(data.message || it('webui.inspection_set_default_failed', '设为默认模板失败'));
        toastSuccess(it('webui.inspection_set_default_success', '已设为默认模板'));
        await loadInspectionTemplates();
    } catch (e) {
        toast(it('webui.inspection_set_default_failed', '设为默认模板失败') + ': ' + e.message, 'error');
    }
}

function showInspectionSqlPreview(title, sql) {
    if (!sql) return;
    const modalId = 'icfg-sql-modal-' + Date.now();
    const titleId = modalId + '-title';
    document.documentElement.insertAdjacentHTML('beforeend', `
        <div id="${modalId}" class="icfg-sql-backdrop">
            <div class="icfg-sql-dialog" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
                <div class="icfg-sql-head">
                    <div>
                        <h3 id="${titleId}">${escapeHtml(title || it('webui.inspection_sql_preview', 'SQL 预览'))}</h3>
                        <p>${escapeHtml(it('webui.inspection_sql_preview_hint', '只读预览，不会在此执行。'))}</p>
                    </div>
                    <button type="button" class="icfg-sql-close" data-sql-close aria-label="${escapeHtml(it('webui.inspection_close', '关闭'))}">×</button>
                </div>
                <pre class="icfg-sql-body">${escapeHtml(sql)}</pre>
                <div class="icfg-sql-footer">
                    <span>${escapeHtml(it('webui.inspection_sql_preview', 'SQL 预览'))}</span>
                    <div>
                        <button type="button" class="btn btn-sm" data-sql-copy>${escapeHtml(it('webui.inspection_copy_sql', '复制 SQL'))}</button>
                        <button type="button" class="btn btn-sm btn-primary" data-sql-close>${escapeHtml(it('webui.inspection_close', '关闭'))}</button>
                    </div>
                </div>
            </div>
        </div>`);
    const backdrop = document.getElementById(modalId);
    if (!backdrop) return;
    const close = () => {
        document.removeEventListener('keydown', onKey);
        backdrop.remove();
    };
    const onKey = (event) => { if (event.key === 'Escape') { event.preventDefault(); close(); } };
    document.addEventListener('keydown', onKey);
    backdrop.addEventListener('click', (event) => { if (event.target === backdrop) close(); });
    backdrop.querySelectorAll('[data-sql-close]').forEach(btn => btn.addEventListener('click', close));
    backdrop.querySelector('[data-sql-copy]')?.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(sql);
            toastSuccess(it('webui.inspection_sql_copied', 'SQL 已复制'));
        } catch (e) {
            toast(it('webui.inspection_copy_sql', '复制 SQL') + ': ' + e.message, 'error');
        }
    });
    backdrop.querySelector('[data-sql-close]')?.focus();
}

function deleteInspectionTemplateById(templateId) {
    const t = inspectionTemplateCatalog.find(item => Number(item.id) === Number(templateId));
    if (t) deleteInspectionTemplate(t.id, t.template_name);
}

function onInspectionTemplateListClick(event) {
    const sqlTrigger = event.target.closest('[data-icfg-sql]');
    if (sqlTrigger) {
        event.preventDefault();
        event.stopPropagation();
        showInspectionSqlPreview(sqlTrigger.dataset.sqlTitle || '', sqlTrigger.dataset.icfgSql || '');
        return;
    }
    const actionButton = event.target.closest('[data-icfg-action]');
    const row = event.target.closest('.icfg-row');
    if (actionButton) {
        event.preventDefault();
        event.stopPropagation();
        const actionRow = actionButton.closest('.icfg-row')
            || actionButton.closest('.icfg-detail-row')?.previousElementSibling;
        if (!actionRow) return;
        const templateId = Number(actionRow.dataset.templateId);
        const action = actionButton.dataset.icfgAction;
        if (action === 'edit') editInspectionTemplate(templateId);
        if (action === 'export') exportInspectionTemplate(templateId);
        if (action === 'clone') cloneInspectionTemplate(templateId);
        if (action === 'default') setInspectionTemplateDefault(templateId);
        if (action === 'delete') deleteInspectionTemplateById(templateId);
        return;
    }
    if (row) toggleInspectionTemplateRow(Number(row.dataset.templateId));
}

function onInspectionTemplateListKeydown(event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const row = event.target.closest('.icfg-row');
    if (!row) return;
    event.preventDefault();
    toggleInspectionTemplateRow(Number(row.dataset.templateId));
}

(function bindInspectionTemplateListEvents() {
    const container = document.getElementById('inspection-template-list');
    if (!container) return;
    container.addEventListener('click', onInspectionTemplateListClick);
    container.addEventListener('keydown', onInspectionTemplateListKeydown);
})();


// ==================== 模板编辑 ====================

function editInspectionTemplate(templateId) {
    const listPage = document.getElementById('inspection-template-list-page');
    const editPage = document.getElementById('inspection-template-edit-page');
    if (listPage) listPage.style.display = 'none';
    if (editPage) editPage.style.display = 'block';
    currentInspectionTemplateId = templateId;
    if (templateId) {
        loadInspectionTemplateDetail(templateId);
    } else {
        clearInspectionTemplateEditForm();
    }
}

async function loadInspectionTemplateDetail(templateId) {
    try {
        const res = await fetch(`/api/inspection/templates/${templateId}`);
        const data = await res.json();
        if (data.success) {
            fillInspectionTemplateEditForm(data.data);
            // 清空右侧章节详情面板，防止状态残留
            const detailContainer = document.getElementById('inspection-chapter-detail');
            if (detailContainer) detailContainer.innerHTML = '<div class="empty-state">请点击左侧章节查看详情</div>';
            loadInspectionChapters(templateId);
        } else {
            toast(data.message || '加载模板详情失败', 'error');
        }
    } catch (e) {
        toast('加载模板详情失败: ' + e.message, 'error');
    }
}

function fillInspectionTemplateEditForm(template) {
    const dbTypeEl = document.getElementById('inspection-edit-db-type');
    const nameEl = document.getElementById('inspection-edit-name');
    const versionEl = document.getElementById('inspection-edit-version');
    const descEl = document.getElementById('inspection-edit-desc');
    const defaultEl = document.getElementById('inspection-edit-default');
    const addChapterBtn = document.getElementById('inspection-add-chapter-btn');
    const saveTemplateBtn = document.getElementById('inspection-save-template-btn');
    const exportBtn = document.querySelector('button[onclick="exportInspectionTemplate()"]');
    currentInspectionIsPreset = template.is_preset == 1;
    console.log('[DEBUG] template id=', template.id, 'is_preset=', template.is_preset, 'currentInspectionIsPreset=', currentInspectionIsPreset);
    // 预置模板也可以编辑，移除禁用限制
    if (dbTypeEl) { dbTypeEl.value = template.db_type || ''; dbTypeEl.disabled = false; }
    if (nameEl) {
        nameEl.value = template.template_name || '';
        nameEl.readOnly = false;
        nameEl.disabled = false;
    }
    if (versionEl) {
        versionEl.value = template.version || 'v1';
        versionEl.readOnly = false;
        versionEl.disabled = false;
    }
    if (descEl) { descEl.value = template.description || ''; descEl.readOnly = false; }
    if (defaultEl) { defaultEl.checked = template.is_default == 1; defaultEl.disabled = false; }
    // 按钮始终显示（预置模板也可以操作）
    if (addChapterBtn) addChapterBtn.style.display = '';
    if (saveTemplateBtn) saveTemplateBtn.style.display = '';
    if (exportBtn) exportBtn.style.display = 'none';
}

function clearInspectionTemplateEditForm() {
    const dbTypeEl = document.getElementById('inspection-edit-db-type');
    const nameEl = document.getElementById('inspection-edit-name');
    const versionEl = document.getElementById('inspection-edit-version');
    const descEl = document.getElementById('inspection-edit-desc');
    const defaultEl = document.getElementById('inspection-edit-default');
    if (dbTypeEl) dbTypeEl.value = '';
    if (nameEl) { nameEl.value = ''; nameEl.readOnly = false; nameEl.disabled = false; }
    if (versionEl) { versionEl.value = 'v1'; versionEl.readOnly = false; versionEl.disabled = false; }
    if (descEl) descEl.value = '';
    if (defaultEl) defaultEl.checked = false;
    currentInspectionIsPreset = false;
    const saveTemplateBtn = document.getElementById('inspection-save-template-btn');
    const addChapterBtn = document.getElementById('inspection-add-chapter-btn');
    const exportBtn = document.querySelector('button[onclick="exportInspectionTemplate()"]');
    if (saveTemplateBtn) saveTemplateBtn.style.display = '';
    if (addChapterBtn) addChapterBtn.style.display = '';
    if (exportBtn) exportBtn.style.display = 'none';
    const chapterList = document.getElementById('inspection-chapter-list');
    if (chapterList) chapterList.innerHTML = '<div class="empty-state">暂无章节，请点击顶部"添加章节"按钮添加。</div>';
    clearInspectionChapterDetail();
}

async function saveInspectionTemplate() {
    if (!IS_LOGGED_IN) { toast('只有登录用户才能执行此操作', 'error'); return; }
    // 预置模板也可以保存
    const dbTypeEl = document.getElementById('inspection-edit-db-type');
    const nameEl = document.getElementById('inspection-edit-name');
    const versionEl = document.getElementById('inspection-edit-version');
    const descEl = document.getElementById('inspection-edit-desc');
    const defaultEl = document.getElementById('inspection-edit-default');
    const dbType = dbTypeEl ? dbTypeEl.value : '';
    const templateName = nameEl ? nameEl.value.trim() : '';
    const version = versionEl ? versionEl.value.trim() : 'v1';
    const description = descEl ? descEl.value.trim() : '';
    const isDefault = defaultEl ? (defaultEl.checked ? 1 : 0) : 0;
    if (!dbType || !templateName) {
        toast('数据库类型和模板名称是必填项', 'error');
        return;
    }
    try {
        let res, data;
        if (currentInspectionTemplateId) {
            res = await fetch(`/api/inspection/templates/${currentInspectionTemplateId}`, {
                method: 'PUT', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({db_type: dbType, template_name: templateName, version: version, description: description, is_default: isDefault})
            });
        } else {
            res = await fetch('/api/inspection/templates', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({db_type: dbType, template_name: templateName, version: version, description: description, is_default: isDefault})
            });
        }
        data = await res.json();
        if (data.success) {
            toast('模板保存成功', 'success');
            if (!currentInspectionTemplateId && data.data && data.data.id) {
                currentInspectionTemplateId = data.data.id;
                loadInspectionChapters(currentInspectionTemplateId);
            }
        } else {
            toast(data.message || '保存模板失败', 'error');
        }
    } catch (e) {
        toast('保存模板失败: ' + e.message, 'error');
    }
}

async function deleteInspectionTemplate(templateId, templateName) {
    if (!IS_LOGGED_IN) { toast('只有登录用户才能执行此操作', 'error'); return; }
    const confirmed = await showConfirmDialog('确认删除', `确定要删除模板"${templateName}"吗？此操作不可撤销。`);
    if (!confirmed) return;
    try {
        const res = await fetch(`/api/inspection/templates/${templateId}`, {method: 'DELETE'});
        const data = await res.json();
        if (data.success) {
            toast('模板删除成功', 'success');
            showInspectionTemplateList();
        } else {
            toast(data.message || '删除模板失败', 'error');
        }
    } catch (e) {
        toast('删除模板失败: ' + e.message, 'error');
    }
}


// ==================== 章节管理 ====================

async function loadInspectionChapters(templateId) {
    try {
        const res = await fetch(`/api/inspection/templates/${templateId}/chapters`);
        const data = await res.json();
        if (data.success) {
            renderInspectionChapters(data.data);
            // 自动展开第一个章节，防止切换模板后右侧面板状态残留
            if (data.data && data.data.length > 0) {
                const firstId = data.data[0].id;
                if (currentInspectionIsPreset === true) {
                    viewInspectionChapterReadOnly(firstId);
                } else {
                    editInspectionChapter(firstId);
                }
            } else {
                // 无章节时清空右侧面板
                const detailContainer = document.getElementById('inspection-chapter-detail');
                if (detailContainer) detailContainer.innerHTML = '<div class="empty-state">暂无章节，请点击顶部"添加章节"按钮添加。</div>';
            }
        } else {
            toast(data.message || '加载章节列表失败', 'error');
        }
    } catch (e) {
        toast('加载章节列表失败: ' + e.message, 'error');
    }
}

function renderInspectionChapters(chapters) {
    const container = document.getElementById('inspection-chapter-list');
    if (!container) return;
    const isPreset = currentInspectionIsPreset === true;
    if (!chapters || chapters.length === 0) {
        container.innerHTML = '<div class="empty-state">暂无章节，请点击顶部"添加章节"按钮添加。</div>';
        filterInspectionConfig('chapter');
        return;
    }
    let html = '<ul class="chapter-sortable">';
    chapters.forEach((ch, idx) => {
        // 预置模板也可以编辑
        const clickAction = `onclick="editInspectionChapter(${ch.id})"`;
        const deleteBtn = !IS_LOGGED_IN ? '' : `
            <div class="chapter-item-actions">
                <button class="btn-delete-x" title="删除" onclick="event.stopPropagation(); deleteInspectionChapter(${ch.id}, '${escapeHtml(ch.chapter_title_zh)}')">✕</button>
            </div>`;
        html += `
        <li class="chapter-item" data-chapter-id="${ch.id}" ${clickAction}>
            <div class="chapter-info">
                <span class="chapter-number">${ch.chapter_number}</span>
                <div class="chapter-text">
                    <span class="chapter-title">${escapeHtml(ch.chapter_title_zh)}</span>
                    ${ch.chapter_title_en ? `<span class="chapter-title-en">${escapeHtml(ch.chapter_title_en)}</span>` : ''}
                </div>
            </div>
            <span class="chapter-query-count">${ch.query_count || 0}</span>
            ${deleteBtn}
        </li>`;
    });
    html += '</ul>';
    container.innerHTML = html;
    indexInspectionConfigRows('chapter', chapters, ['chapter_number','chapter_title_zh','chapter_title_en','description']);

    // 预置模板也启用拖拽排序
    if (IS_LOGGED_IN) {
        setTimeout(function() {
            const listEl = container.querySelector('.chapter-sortable');
            if (listEl && window.Sortable) {
                try {
                    window._inspectionChapterSortable = new Sortable(listEl, {
                        animation: 150,
                        disabled: Boolean(document.getElementById('inspection-chapter-search')?.value.trim()),
                        ghostClass: 'chapter-sortable-ghost',
                        onEnd: function() {
                            if (!IS_LOGGED_IN) return;  // 拖拽排序属写操作
                            const items = listEl.querySelectorAll('[data-chapter-id]');
                            const ids = Array.from(items).map(el => parseInt(el.getAttribute('data-chapter-id')));
                            fetch('/api/inspection/chapters/reorder', {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({template_id: currentInspectionTemplateId, chapter_ids: ids})
                            }).then(r => r.json()).then(d => {
                                if (!d.success) toast('排序保存失败', 'error');
                            }).catch(e => toast('排序保存失败: ' + e.message, 'error'));
                        }
                    });
                } catch(e) { /* ignore */ }
            }
        }, 100);
    }
}

function addInspectionChapter() {
    if (!IS_LOGGED_IN) { toast('只有管理员才能执行此操作', 'error'); return; }
    if (!currentInspectionTemplateId) {
        toast('请先保存模板', 'error');
        return;
    }
    showInspectionChapterEditForm(null);
}

async function editInspectionChapter(chapterId) {
    if (inspectionQueryUnsaved) {
        const confirmed = await showConfirmDialog('未保存', '当前有未保存的修改，确定要切换吗？');
        if (!confirmed) return;
    }
    try {
        const res = await fetch(`/api/inspection/chapters/${chapterId}`);
        const data = await res.json();
        if (data.success) {
            showInspectionChapterEditForm(data.data);
        } else {
            toast(data.message || '加载章节详情失败', 'error');
        }
    } catch (e) {
        toast('加载章节详情失败: ' + e.message, 'error');
    }
}

async function viewInspectionChapterReadOnly(chapterId) {
    try {
        const res = await fetch(`/api/inspection/chapters/${chapterId}`);
        const data = await res.json();
        if (data.success) {
            showInspectionChapterReadOnlyForm(data.data);
        } else {
            toast(data.message || '加载章节详情失败', 'error');
        }
    } catch (e) {
        toast('加载章节详情失败: ' + e.message, 'error');
    }
}

function showInspectionChapterReadOnlyForm(chapter) {
    const detailContainer = document.getElementById('inspection-chapter-detail');
    if (!detailContainer) return;
    currentInspectionChapterId = chapter.id;
    detailContainer.innerHTML = `
        <div class="chapter-edit-form">
            <div class="form-group"><label>章节序号</label><input type="number" class="form-input" value="${chapter.chapter_number || ''}" disabled readonly /></div>
            <div class="form-group"><label>章节标题（中文）</label><input type="text" class="form-input" value="${escapeHtml(chapter.chapter_title_zh || '')}" disabled readonly /></div>
            <div class="form-group"><label>章节标题（英文）</label><input type="text" class="form-input" value="${escapeHtml(chapter.chapter_title_en || '')}" disabled readonly /></div>
            <div class="form-group"><label>章节描述</label><textarea class="form-textarea" disabled readonly>${escapeHtml(chapter.description || '')}</textarea></div>
            <div class="form-group"><label><input type="checkbox" ${chapter.enabled != 0 ? 'checked' : ''} disabled /> 启用</label></div>
            <div class="form-actions"><span style="color:#888;font-size:12px;">预置模板，仅可查看</span></div>
        </div>
        <div id="inspection-query-list"></div>`;
    loadInspectionQueries(chapter.id);
}

function showInspectionChapterEditForm(chapter) {
    if (currentInspectionIsPreset === true && chapter) {
        showInspectionChapterReadOnlyForm(chapter);
        return;
    }
    const detailContainer = document.getElementById('inspection-chapter-detail');
    if (!detailContainer) return;
    if (chapter) {
        currentInspectionChapterId = chapter.id;
        detailContainer.innerHTML = `
            <div class="chapter-edit-form">
                <div class="form-group"><label>章节序号</label><input type="number" id="inspection-chapter-number" class="form-input" value="${chapter.chapter_number || ''}" /></div>
                <div class="form-group"><label>章节标题（中文）</label><input type="text" id="inspection-chapter-title-zh" class="form-input" value="${escapeHtml(chapter.chapter_title_zh || '')}" /></div>
                <div class="form-group"><label>章节标题（英文）</label><input type="text" id="inspection-chapter-title-en" class="form-input" value="${escapeHtml(chapter.chapter_title_en || '')}" /></div>
                <div class="form-group"><label>章节描述</label><textarea id="inspection-chapter-desc" class="form-textarea">${escapeHtml(chapter.description || '')}</textarea></div>
                <div class="form-group"><label><input type="checkbox" id="inspection-chapter-enabled" ${chapter.enabled != 0 ? 'checked' : ''} /> 启用</label></div>
                ${IS_LOGGED_IN ? '<div class="form-actions"><button class="btn btn-primary btn-sm" onclick="saveInspectionChapter()">保存章节</button></div>' : ''}
            </div>
            <div id="inspection-query-list"></div>`;
        loadInspectionQueries(chapter.id);
    } else {
        currentInspectionChapterId = null;
        detailContainer.innerHTML = `
            <div class="chapter-edit-form">
                <div class="form-group"><label>章节序号</label><input type="number" id="inspection-chapter-number" class="form-input" value="" /></div>
                <div class="form-group"><label>章节标题（中文）</label><input type="text" id="inspection-chapter-title-zh" class="form-input" value="" /></div>
                <div class="form-group"><label>章节标题（英文）</label><input type="text" id="inspection-chapter-title-en" class="form-input" value="" /></div>
                <div class="form-group"><label>章节描述</label><textarea id="inspection-chapter-desc" class="form-textarea"></textarea></div>
                <div class="form-group"><label><input type="checkbox" id="inspection-chapter-enabled" checked /> 启用</label></div>
                ${IS_LOGGED_IN ? '<div class="form-actions"><button class="btn btn-primary btn-sm" onclick="saveInspectionChapter()">保存章节</button></div>' : ''}
            </div>
            <div id="inspection-query-list"></div>`;
    }
}

async function saveInspectionChapter() {
    if (!IS_LOGGED_IN) { toast('只有管理员才能执行此操作', 'error'); return; }
    // 预置模板也可以保存章节
    const numberEl = document.getElementById('inspection-chapter-number');
    const titleZhEl = document.getElementById('inspection-chapter-title-zh');
    const titleEnEl = document.getElementById('inspection-chapter-title-en');
    const descEl = document.getElementById('inspection-chapter-desc');
    const enabledEl = document.getElementById('inspection-chapter-enabled');
    const chapterNumber = numberEl ? numberEl.value : '';
    const titleZh = titleZhEl ? titleZhEl.value.trim() : '';
    const titleEn = titleEnEl ? titleEnEl.value.trim() : '';
    const description = descEl ? descEl.value.trim() : '';
    const enabled = enabledEl ? (enabledEl.checked ? 1 : 0) : 1;
    if (!chapterNumber || !titleZh) {
        toast('章节序号和章节标题（中文）是必填项', 'error');
        return;
    }
    if (!currentInspectionTemplateId) {
        toast('请先保存模板', 'error');
        return;
    }
    try {
        let res, data;
        if (currentInspectionChapterId) {
            res = await fetch(`/api/inspection/chapters/${currentInspectionChapterId}`, {
                method: 'PUT', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({chapter_number: parseInt(chapterNumber), chapter_title_zh: titleZh, chapter_title_en: titleEn, description: description, enabled: enabled})
            });
        } else {
            res = await fetch(`/api/inspection/templates/${currentInspectionTemplateId}/chapters`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({chapter_number: parseInt(chapterNumber), chapter_title_zh: titleZh, chapter_title_en: titleEn, description: description, enabled: enabled})
            });
        }
        data = await res.json();
        if (data.success) {
            toast('章节保存成功', 'success');
            loadInspectionChapters(currentInspectionTemplateId);
            if (!currentInspectionChapterId && data.data && data.data.id) {
                currentInspectionChapterId = data.data.id;
                loadInspectionQueries(currentInspectionChapterId);
            }
        } else {
            toast(data.message || '保存章节失败', 'error');
        }
    } catch (e) {
        toast('保存章节失败: ' + e.message, 'error');
    }
}

async function deleteInspectionChapter(chapterId, chapterTitle) {
    if (!IS_LOGGED_IN) { toast('只有管理员才能执行此操作', 'error'); return; }
    // 预置模板也可以删除章节
    const confirmed = await showConfirmDialog('确认删除', `确定要删除章节"${chapterTitle}"吗？相关的 SQL 查询也会被删除。`);
    if (!confirmed) return;
    try {
        const res = await fetch(`/api/inspection/chapters/${chapterId}`, {method: 'DELETE'});
        const data = await res.json();
        if (data.success) {
            toast('章节删除成功', 'success');
            loadInspectionChapters(currentInspectionTemplateId);
            clearInspectionChapterDetail();
        } else {
            toast(data.message || '删除章节失败', 'error');
        }
    } catch (e) {
        toast('删除章节失败: ' + e.message, 'error');
    }
}

function clearInspectionChapterDetail() {
    const detailContainer = document.getElementById('inspection-chapter-detail');
    if (detailContainer) {
        detailContainer.innerHTML = '<div class="empty-state">请在左侧选择章节，或点击顶部"添加章节"创建新章节。</div>';
    }
    const queryList = document.getElementById('inspection-query-list');
    if (queryList) queryList.innerHTML = '';
    currentInspectionChapterId = null;
    inspectionQueryUnsaved = false;
}


// ==================== SQL 查询管理（内联编辑） ====================

async function loadInspectionQueries(chapterId) {
    try {
        const res = await fetch(`/api/inspection/chapters/${chapterId}/queries`);
        const data = await res.json();
        if (data.success) {
            renderInspectionQueries(data.data);
        } else {
            toast(data.message || '加载查询列表失败', 'error');
        }
    } catch (e) {
        toast('加载查询列表失败: ' + e.message, 'error');
    }
}

function renderInspectionQueries(queries) {
    const container = document.getElementById('inspection-query-list');
    if (!container) return;
    const searchValue = document.getElementById('inspection-query-search')?.value || '';
    const searchControls = `<div class="inspection-search-bar"><input id="inspection-query-search" class="form-input" type="search" aria-label="搜索 SQL 查询" placeholder="搜索查询名称、说明或完整 SQL" value="${escapeHtml(searchValue)}" oninput="filterInspectionConfig('query')"><button type="button" class="btn btn-sm" onclick="clearInspectionConfigSearch('query')">清空</button><span id="inspection-query-search-status" role="status"></span></div>`;
    // 预置模板也可以编辑
    const isPreset = currentInspectionIsPreset === true;
    if (!queries || queries.length === 0) {
        let html = '<div class="empty-state" style="margin-top:12px;">暂无 SQL 查询。</div>';
        if (IS_LOGGED_IN) {
            html += '<button class="btn btn-xs btn-primary" style="margin-top:8px;" onclick="addInspectionQuery()">添加查询</button>';
        }
        container.innerHTML = searchControls + html;
        filterInspectionConfig('query');
        return;
    }
    let html = '<h4>SQL 查询列表</h4>' + searchControls;
    if (IS_LOGGED_IN) {
        html += '<button class="btn btn-xs btn-primary" onclick="addInspectionQuery()">添加查询</button>';
    }
    html += '<ul class="query-list">';
    queries.forEach(q => {
        const sqlPreview = escapeHtmlForTemplate((q.query_sql || '').substring(0, 100)) + ((q.query_sql || '').length > 100 ? '...' : '');
        let actionsHtml = '';
        // 预置模板也可以编辑
        if (IS_LOGGED_IN) {
            actionsHtml = `
                <button class="btn btn-xs btn-ghost" onclick="editInspectionQueryInline(${q.id})">编辑</button>
                <button class="btn btn-xs btn-danger" onclick="deleteInspectionQuery(${q.id}, '${escapeHtml(q.query_key)}')">删除</button>`;
        }
        html += `
        <li class="query-item" data-query-id="${q.id}">
            <div class="query-item-header">
                <span class="query-key">${escapeHtml(q.query_key)}</span>
                <span class="query-enabled">${q.enabled ? '✅' : '❌'}</span>
                <div class="query-item-actions">${actionsHtml}</div>
            </div>
            <div class="query-sql-preview">${sqlPreview}</div>
            <div class="query-inline-edit" id="query-edit-${q.id}" style="display:none;"></div>
        </li>`;
    });
    html += '</ul>';
    container.innerHTML = html;
    indexInspectionConfigRows('query', queries, ['query_key','query_sql','query_description_zh','query_description_en','description']);
}

function addInspectionQuery() {
    if (!IS_LOGGED_IN) { toast('只有管理员才能执行此操作', 'error'); return; }
    // 预置模板也可以添加查询
    if (!currentInspectionChapterId) {
        toast('请先选择一个章节', 'error');
        return;
    }
    if (inspectionQueryUnsaved) {
        showConfirmDialog('未保存', '当前有未保存的修改，确定要放弃吗？').then(confirmed => {
            if (confirmed) {
                inspectionQueryUnsaved = false;
                showInlineQueryForm(null);
            }
        });
        return;
    }
    showInlineQueryForm(null);
}

async function editInspectionQueryInline(queryId) {
    // 预置模板也可以编辑查询
    if (inspectionQueryUnsaved) {
        const confirmed = await showConfirmDialog('未保存', '当前有未保存的修改，确定要放弃吗？');
        if (!confirmed) return;
    }
    document.querySelectorAll('.query-inline-edit').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.query-item-editing').forEach(el => el.classList.remove('query-item-editing'));
    const editContainer = document.getElementById('query-edit-' + queryId);
    if (!editContainer) return;
    try {
        const res = await fetch(`/api/inspection/queries/${queryId}`);
        const data = await res.json();
        if (data.success) {
            renderQueryInlineEditForm(data.data, editContainer, false);
            editContainer.style.display = 'block';
            const li = editContainer.closest('.query-item');
            if (li) li.classList.add('query-item-editing');
            inspectionQueryUnsaved = false;
        } else {
            toast(data.message || '加载查询详情失败', 'error');
        }
    } catch (e) {
        toast('加载查询详情失败: ' + e.message, 'error');
    }
}

function renderQueryInlineEditForm(query, container, isNew) {
    const q = query || {};
    const key    = q.query_key    || '';
    const sql    = q.query_sql    || '';
    const descZh = q.query_description_zh || '';
    const descEn = q.query_description_en || '';
    const enabled = q.enabled !== 0;
    let html = '<div class="inline-edit-form">';
    html += `<div class="form-group"><label>查询键名</label><input type="text" class="form-input inline-q-key" value="${escapeHtml(key)}" oninput="inspectionQueryUnsaved=true" /></div>`;
    html += `<div class="form-group"><label>SQL 语句</label><textarea class="form-textarea inline-q-sql" style="height:100px;font-family:monospace;" oninput="inspectionQueryUnsaved=true">${escapeHtmlForTemplate(sql)}</textarea></div>`;
    html += `<div class="form-group"><label>描述（中文）</label><input type="text" class="form-input inline-q-desc-zh" value="${escapeHtml(descZh)}" oninput="inspectionQueryUnsaved=true" /></div>`;
    html += `<div class="form-group"><label>描述（英文）</label><input type="text" class="form-input inline-q-desc-en" value="${escapeHtml(descEn)}" oninput="inspectionQueryUnsaved=true" /></div>`;
    html += `<div class="form-group"><label><input type="checkbox" class="inline-q-enabled" ${enabled ? 'checked' : ''} onchange="inspectionQueryUnsaved=true" /> 启用</label></div>`;
    html += '<div class="form-actions">';
    html += IS_LOGGED_IN ? `  <button class="btn btn-primary btn-sm" onclick="saveInspectionQueryInline(${q.id || 'null'}, ${isNew})">保存</button>` : '';
    html += `  <button class="btn btn-sm" onclick="cancelInspectionQueryInline(${q.id || 'null'})">取消</button>`;
    html += '</div></div>';
    container.innerHTML = html;
    setTimeout(function() {
        const sqlEl = container.querySelector('.inline-q-sql');
        if (sqlEl && window.CodeMirror) {
            try {
                const editor = window.CodeMirror.fromTextArea(sqlEl, {
                    mode: 'text/x-sql', theme: 'dracula',
                    lineNumbers: true, lineWrapping: true,
                    indentWithTabs: false, indentUnit: 4
                });
                editor.setSize('100%', '100px');
                editor.on('change', function() { inspectionQueryUnsaved = true; });
                container._codeMirror = editor;
            } catch (e) { /* ignore */ }
        }
    }, 100);
}

function showInlineQueryForm(query) {
    const container = document.getElementById('inspection-query-list');
    if (!container) return;
    const isNew = query === null;
    const q = query || {};
    const key    = isNew ? '' : (q.query_key    || '');
    const sql    = isNew ? '' : (q.query_sql    || '');
    const descZh = isNew ? '' : (q.query_description_zh || '');
    const descEn = isNew ? '' : (q.query_description_en || '');
    const enabled = isNew ? true : (q.enabled !== 0);
    container.innerHTML = '<div class="inline-edit-form">'
        + `<div class="form-group"><label>查询键名</label><input type="text" class="form-input inline-q-key" value="${escapeHtml(key)}" oninput="inspectionQueryUnsaved=true" /></div>`
        + `<div class="form-group"><label>SQL 语句</label><textarea class="form-textarea inline-q-sql" style="height:100px;font-family:monospace;" oninput="inspectionQueryUnsaved=true">${escapeHtmlForTemplate(sql)}</textarea></div>`
        + `<div class="form-group"><label>描述（中文）</label><input type="text" class="form-input inline-q-desc-zh" value="${escapeHtml(descZh)}" oninput="inspectionQueryUnsaved=true" /></div>`
        + `<div class="form-group"><label>描述（英文）</label><input type="text" class="form-input inline-q-desc-en" value="${escapeHtml(descEn)}" oninput="inspectionQueryUnsaved=true" /></div>`
        + `<div class="form-group"><label><input type="checkbox" class="inline-q-enabled" ${enabled ? 'checked' : ''} onchange="inspectionQueryUnsaved=true" /> 启用</label></div>`
        + '<div class="form-actions">'
        + (IS_LOGGED_IN ? `  <button class="btn btn-primary btn-sm" onclick="saveInspectionQueryInline('${isNew ? 'null' : q.id}', ${isNew})">保存</button>` : '')
        + `  <button class="btn btn-sm" onclick="cancelInspectionQueryInline('${isNew ? 'null' : q.id}')">取消</button>`
        + '</div></div>';
    const formDiv = container.querySelector('.inline-edit-form');
    if (formDiv) {
        setTimeout(function() {
            const sqlEl = formDiv.querySelector('.inline-q-sql');
            if (sqlEl && window.CodeMirror) {
                try {
                    const editor = window.CodeMirror.fromTextArea(sqlEl, {
                        mode: 'text/x-sql', theme: 'dracula',
                        lineNumbers: true, lineWrapping: true,
                        indentWithTabs: false, indentUnit: 4
                    });
                    editor.setSize('100%', '100px');
                    editor.on('change', function() { inspectionQueryUnsaved = true; });
                    formDiv._codeMirror = editor;
                } catch (e) { /* ignore */ }
            }
        }, 100);
    }
    inspectionQueryUnsaved = false;
}

async function saveInspectionQueryInline(queryId, isNew) {
    if (!IS_LOGGED_IN) { toast('只有管理员才能执行此操作', 'error'); return; }
    // 预置模板也可以保存查询
    const keyEl    = document.querySelector('.inline-q-key');
    const enabledEl = document.querySelector('.inline-q-enabled');
    const descZhEl = document.querySelector('.inline-q-desc-zh');
    const descEnEl = document.querySelector('.inline-q-desc-en');
    const cm = document.querySelector('.inline-edit-form')?._codeMirror;
    const sqlEl     = document.querySelector('.inline-q-sql');
    const queryKey   = keyEl    ? keyEl.value.trim()    : '';
    const querySql   = cm       ? cm.getValue().trim()   : (sqlEl ? sqlEl.value.trim() : '');
    const descZh     = descZhEl ? descZhEl.value.trim()  : '';
    const descEn     = descEnEl ? descEnEl.value.trim()  : '';
    const enabled     = enabledEl ? (enabledEl.checked ? 1 : 0) : 1;
    if (!queryKey || !querySql) {
        toast('查询键名和 SQL 语句是必填项', 'error');
        return;
    }
    if (!currentInspectionChapterId) {
        toast('章节 ID 丢失，请重新选择章节', 'error');
        return;
    }
    try {
        let res, data;
        if (!isNew && queryId && queryId !== 'null') {
            res = await fetch(`/api/inspection/queries/${queryId}`, {
                method: 'PUT', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({query_key: queryKey, query_sql: querySql, query_description_zh: descZh, query_description_en: descEn, enabled: enabled})
            });
        } else {
            res = await fetch(`/api/inspection/chapters/${currentInspectionChapterId}/queries`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({query_key: queryKey, query_sql: querySql, query_description_zh: descZh, query_description_en: descEn, enabled: enabled})
            });
        }
        data = await res.json();
        if (data.success) {
            toast('查询保存成功', 'success');
            inspectionQueryUnsaved = false;
            loadInspectionQueries(currentInspectionChapterId);
        } else {
            toast(data.message || '保存查询失败', 'error');
        }
    } catch (e) {
        toast('保存查询失败: ' + e.message, 'error');
    }
}

function cancelInspectionQueryInline(queryId) {
    if (inspectionQueryUnsaved) {
        showConfirmDialog('未保存', '当前有未保存的修改，确定要放弃吗？').then(confirmed => {
            if (confirmed) {
                inspectionQueryUnsaved = false;
                loadInspectionQueries(currentInspectionChapterId);
            }
        });
        return;
    }
    loadInspectionQueries(currentInspectionChapterId);
}

async function viewInspectionQuery(queryId) {
    document.querySelectorAll('.query-inline-edit').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.query-item-editing').forEach(el => el.classList.remove('query-item-editing'));
    const editContainer = document.getElementById('query-edit-' + queryId);
    if (!editContainer) return;
    try {
        const res = await fetch(`/api/inspection/queries/${queryId}`);
        const data = await res.json();
        if (data.success) {
            renderQueryInlineViewForm(data.data, editContainer);
            editContainer.style.display = 'block';
            const li = editContainer.closest('.query-item');
            if (li) li.classList.add('query-item-editing');
        } else {
            toast(data.message || '加载查询详情失败', 'error');
        }
    } catch (e) {
        toast('加载查询详情失败: ' + e.message, 'error');
    }
}

function renderQueryInlineViewForm(query, container) {
    const q = query || {};
    const key    = q.query_key    || '';
    const sql    = q.query_sql    || '';
    const descZh = q.query_description_zh || '';
    const descEn = q.query_description_en || '';
    const enabled = q.enabled !== 0;
    let html = '<div class="inline-edit-form view-mode">';
    html += `<div class="form-group"><label>查询键名</label><input type="text" class="form-input" value="${escapeHtml(key)}" disabled readonly /></div>`;
    html += `<div class="form-group"><label>SQL 语句</label><textarea class="form-textarea" style="height:160px;font-family:monospace;" disabled readonly>${escapeHtmlForTemplate(sql)}</textarea></div>`;
    html += `<div class="form-group"><label>描述（中文）</label><input type="text" class="form-input" value="${escapeHtml(descZh)}" disabled readonly /></div>`;
    html += `<div class="form-group"><label>描述（英文）</label><input type="text" class="form-input" value="${escapeHtml(descEn)}" disabled readonly /></div>`;
    html += `<div class="form-group"><label><input type="checkbox" ${enabled ? 'checked' : ''} disabled /> 启用</label></div>`;
    html += '<div class="form-actions"><button class="btn btn-sm" onclick="this.closest(\'.query-inline-edit\').style.display=\'none\'">关闭</button></div>';
    html += '</div>';
    container.innerHTML = html;
}

async function deleteInspectionQuery(queryId, queryKey) {
    if (!IS_LOGGED_IN) { toast('只有管理员才能执行此操作', 'error'); return; }
    // 预置模板也可以删除查询
    const confirmed = await showConfirmDialog('确认删除', `确定要删除查询"${queryKey}"吗？`);
    if (!confirmed) return;
    try {
        const res = await fetch(`/api/inspection/queries/${queryId}`, {method: 'DELETE'});
        const data = await res.json();
        if (data.success) {
            toast('查询删除成功', 'success');
            loadInspectionQueries(currentInspectionChapterId);
        } else {
            toast(data.message || '删除查询失败', 'error');
        }
    } catch (e) {
        toast('删除查询失败: ' + e.message, 'error');
    }
}


// ==================== 导入/导出 ====================

function downloadInspectionGapCsv(matrix) {
    const rows = (matrix.rows || []).filter(row => (row.missing || []).length);
    if (!rows.length) {
        toast(it('webui.inspection_matrix_no_gaps', '没有基线缺口'), 'success');
        return;
    }
    const escapeCsv = (value) => `"${String(value ?? '').replace(/"/g, '""')}"`;
    const lines = [['db_type', 'template_name', 'version', 'coverage', 'missing_domains'].map(escapeCsv).join(',')];
    rows.forEach(row => {
        const missing = (row.missing || []).map(key => icfgDomainLabel({key})).join('; ');
        lines.push([row.db_type, row.template_name, row.version, row.coverage, missing].map(escapeCsv).join(','));
    });
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `inspection_domain_gaps_${Date.now()}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toastSuccess(it('webui.inspection_export_gaps_success', '缺口清单已导出'));
}

async function showInspectionDomainMatrix() {
    const modalId = 'icfg-matrix-modal-' + Date.now();
    document.documentElement.insertAdjacentHTML('beforeend', `
        <div id="${modalId}" class="icfg-sql-backdrop">
            <div class="icfg-sql-dialog icfg-matrix-dialog" role="dialog" aria-modal="true">
                <div class="icfg-sql-head">
                    <div>
                        <h3>${escapeHtml(it('webui.inspection_domain_matrix_title', '标准域覆盖矩阵'))}</h3>
                        <p>${escapeHtml(it('webui.inspection_domain_matrix_desc', '仅统计通用基线模板；红色为缺口。'))}</p>
                    </div>
                    <button type="button" class="icfg-sql-close" data-matrix-close aria-label="${escapeHtml(it('webui.inspection_close', '关闭'))}">×</button>
                </div>
                <div class="icfg-matrix-body" data-matrix-body>
                    <div class="icfg-detail-loading">${escapeHtml(it('webui.inspection_loading_detail', '正在加载章节与指标明细…'))}</div>
                </div>
            </div>
        </div>`);
    const backdrop = document.getElementById(modalId);
    if (!backdrop) return;
    const close = () => { document.removeEventListener('keydown', onKey); backdrop.remove(); };
    const onKey = (event) => { if (event.key === 'Escape') { event.preventDefault(); close(); } };
    document.addEventListener('keydown', onKey);
    backdrop.addEventListener('click', (event) => { if (event.target === backdrop) close(); });
    backdrop.querySelectorAll('[data-matrix-close]').forEach(btn => btn.addEventListener('click', close));
    try {
        const res = await fetch('/api/inspection/domain-matrix');
        const data = await res.json();
        if (!data.success) throw new Error(data.message || it('webui.inspection_matrix_load_failed', '矩阵加载失败'));
        const matrix = data.data;
        const body = backdrop.querySelector('[data-matrix-body]');
        if (!body) return;
        const domains = matrix.domains || [];
        const rows = matrix.rows || [];
        body.innerHTML = `
            <div class="icfg-matrix-scroll">
                <table class="icfg-matrix-table">
                    <thead><tr>
                        <th class="icfg-matrix-template">${escapeHtml(it('webui.inspection_matrix_template', '模板'))}</th>
                        ${domains.map(domain => `<th title="${escapeHtml(icfgDomainLabel(domain))}">${escapeHtml(icfgDomainLabel(domain))}</th>`).join('')}
                        <th>${escapeHtml(it('webui.inspection_matrix_coverage', '覆盖'))}</th>
                    </tr></thead>
                    <tbody>${rows.map(row => `
                        <tr class="${row.missing?.length ? 'is-gap' : ''}">
                            <td class="icfg-matrix-template">
                                <span class="icfg-matrix-name">${escapeHtml(row.template_name)}</span>
                                <span class="icfg-matrix-meta">${escapeHtml(icfgDbLabel(row.db_type))}${row.version ? ' · ' + escapeHtml(row.version) : ''}</span>
                            </td>
                            ${(row.cells || []).map((covered, index) => `<td class="icfg-matrix-cell ${covered ? 'is-covered' : 'is-missing'}"
                                title="${escapeHtml(domains[index] ? icfgDomainLabel(domains[index]) : '')}">${covered ? '✓' : '—'}</td>`).join('')}
                            <td class="icfg-matrix-coverage">${escapeHtml(row.coverage || '')}</td>
                        </tr>`).join('')}</tbody>
                </table>
            </div>
            <div class="icfg-sql-footer">
                <span>${escapeHtml(itf('webui.inspection_matrix_gap_count', '{n} 个基线存在缺口', {n: matrix.gap_count || 0}))}</span>
                <div>
                    <button type="button" class="btn btn-sm" data-matrix-csv>${escapeHtml(it('webui.inspection_export_gaps', '导出缺口 CSV'))}</button>
                    <button type="button" class="btn btn-sm btn-primary" data-matrix-close>${escapeHtml(it('webui.inspection_close', '关闭'))}</button>
                </div>
            </div>`;
        body.querySelector('[data-matrix-csv]')?.addEventListener('click', () => downloadInspectionGapCsv(matrix));
        body.querySelectorAll('[data-matrix-close]').forEach(btn => btn.addEventListener('click', close));
    } catch (e) {
        const body = backdrop.querySelector('[data-matrix-body]');
        if (body) body.innerHTML = `<div class="icfg-detail-error">${escapeHtml(e.message || it('webui.inspection_matrix_load_failed', '矩阵加载失败'))}</div>`;
    }
}

async function exportFilteredInspectionTemplates() {
    const ids = [...document.querySelectorAll('.icfg-row')]
        .filter(row => !row.hidden)
        .map(row => Number(row.dataset.templateId))
        .filter(Boolean);
    if (!ids.length) {
        toast(it('webui.inspection_export_filtered_empty', '当前筛选结果为空'), 'error');
        return;
    }
    try {
        const res = await fetch('/api/inspection/templates/batch-export', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ids})
        });
        if (!res.ok) {
            let message = '';
            try { message = (await res.json()).message || ''; } catch (e) { /* non-JSON error */ }
            throw new Error(message || it('webui.inspection_export_filtered_failed', '批量导出失败'));
        }
        const blob = await res.blob();
        const disposition = res.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="?([^";]+)"?/i);
        const filename = match?.[1] || `inspection_templates_${Date.now()}.zip`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        toastSuccess(itf('webui.inspection_export_filtered_success', '已导出 {n} 个模板', {n: ids.length}));
    } catch (e) {
        toast(it('webui.inspection_export_filtered_failed', '批量导出失败') + ': ' + e.message, 'error');
    }
}

async function exportInspectionTemplate(templateId) {
    try {
        const res = await fetch(`/api/inspection/templates/${templateId}/export`);
        const data = await res.json();
        if (data.success) {
            const jsonStr = JSON.stringify(data.data, null, 2);
            const blob = new Blob([jsonStr], {type: 'application/json'});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `inspection_template_${templateId}.json`;
            a.click();
            URL.revokeObjectURL(url);
            toast('模板导出成功', 'success');
        } else {
            toast(data.message || '导出模板失败', 'error');
        }
    } catch (e) {
        toast('导出模板失败: ' + e.message, 'error');
    }
}

function importInspectionTemplate() {
    if (!IS_LOGGED_IN) { toast('只有登录用户才能执行此操作', 'error'); return; }
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.onchange = async function() {
        const file = input.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = async function(e) {
            try {
                const templateConfig = JSON.parse(e.target.result);
                const res = await fetch('/api/inspection/templates/import', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({template_config: templateConfig, overwrite: false})
                });
                const data = await res.json();
                if (data.success) {
                    toast('模板导入成功', 'success');
                    loadInspectionTemplates();
                } else {
                    toast(data.message || '导入模板失败', 'error');
                }
            } catch (e) {
                toast('导入模板失败: ' + e.message, 'error');
            }
        };
        reader.readAsText(file);
    };
    input.click();
}


// ==================== 工具函数 ====================

function getI18N(key) {
    return (window.I18N && window.I18N[key] !== undefined) ? window.I18N[key] : undefined;
}

function escapeHtml(str) {
    if (!str) return '';
    const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
    return str.replace(/[&<>"']/g, function(m) { return map[m]; });
}

// 安全转义：HTML 转义 + 模板字符串 $ 转义（防止 ${var} 被 JS 插值）
function escapeHtmlForTemplate(str) {
    if (!str) return '';
    const map = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
    return str.replace(/[&<>"']/g, function(m) { return map[m]; }).replace(/\$/g, '$$');
}


// ==================== 初始化 ====================
window.showInspectionConfigPage = showInspectionConfigPage;
window.showInspectionTemplateList = showInspectionTemplateList;
window.editInspectionTemplate = editInspectionTemplate;
window.saveInspectionTemplate = saveInspectionTemplate;
window.deleteInspectionTemplate = deleteInspectionTemplate;
window.addInspectionChapter = addInspectionChapter;
window.editInspectionChapter = editInspectionChapter;
window.saveInspectionChapter = saveInspectionChapter;
window.deleteInspectionChapter = deleteInspectionChapter;
window.addInspectionQuery = addInspectionQuery;
window.editInspectionQueryInline = editInspectionQueryInline;
window.deleteInspectionQuery = deleteInspectionQuery;
window.exportInspectionTemplate = exportInspectionTemplate;
window.importInspectionTemplate = importInspectionTemplate;
window.viewInspectionChapterReadOnly = viewInspectionChapterReadOnly;
window.showInspectionChapterReadOnlyForm = showInspectionChapterReadOnlyForm;
window.viewInspectionQuery = viewInspectionQuery;
