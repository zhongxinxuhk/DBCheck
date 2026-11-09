// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
// Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck

// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

'use strict';

const state = {catalog: null, assets: [], workflows: [], overview: null, editWorkflow: null};
const labels = {pass:'通过', fail:'不符合', blocked:'安全阻断', error:'采集失败', not_collected:'未采集', needs_review:'待复核', not_applicable:'不适用'};
const element = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));

function message(text, error = false) {
  element('message').textContent = text;
  element('message').className = error ? 'error' : '';
}

async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const response = await fetch(path, options);
  if (response.status === 401) {
    location.href = '/um/login';
    throw new Error('请先登录');
  }
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(result.error || '请求未成功');
  return result;
}

function choices(id, options, empty = false) {
  const target = element(id);
  const selected = target.value;
  target.innerHTML = (empty ? '<option value="">新增资产</option>' : '') + options.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join('');
  if ([...target.options].some(option => option.value === selected)) target.value = selected;
}

function items(kind) {
  return state.catalog.requirements.flatMap(requirement => requirement.items.map(item => ({...item, module_id:requirement.module_id}))).filter(item => !kind || item.kind === kind);
}

function renderCoverage() {
  const module = element('module-filter').value;
  const rows = state.overview.rows.filter(row => !module || row.module_id === module);
  element('coverage-rows').innerHTML = rows.map(row => `<tr><td><input type="checkbox" disabled ${row.status === 'pass' ? 'checked' : ''} aria-label="${escapeHtml(row.item_id)}验收状态"> ${escapeHtml(row.item_id)}</td><td>${escapeHtml(row.asset_name)}</td><td>${escapeHtml(row.title)}</td><td class="status ${escapeHtml(row.status)}">${escapeHtml(labels[row.status])}</td><td>${escapeHtml(row.reason)} ${escapeHtml(row.missing.join('、'))}</td></tr>`).join('') || '<tr><td colspan="5">尚无绑定资产。请先建立台账，需求目录已保留全部服务条目。</td></tr>';
  choices('review-record', rows.filter(row => row.status === 'needs_review' && row.evidence_id && row.missing.length === 0).map(row => ({id:row.evidence_id,title:row.item_id + ' ' + row.asset_name})));
  const data = state.overview;
  element('metrics').innerHTML = [
    ['资产', data.asset_count], ['应检项', data.required_checks], ['通过', data.counts.pass],
    ['待补证据', data.counts.not_collected + data.counts.needs_review],
    ['失败与阻断', data.counts.fail + data.counts.error + data.counts.blocked],
    ['完成率', data.completion_rate === null ? '—' : data.completion_rate + '%']
  ].map(([title,value]) => `<div class="metric"><span>${escapeHtml(title)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
  element('scope').textContent = `${state.catalog.modules.length} 个模块，${state.catalog.requirements.length} 条来源需求，${items().length} 个功能检查项。统计周期 ${data.period_start.slice(0,10)} 至 ${data.period_end.slice(0,10)}，结束日不计入。`;
}

function renderAssets() {
  const options = state.assets.map(record => ({id:record.id, title:record.payload.asset_name}));
  choices('asset-id', options, true);
  choices('workflow-asset', options);
  choices('evidence-asset', options);
  element('asset-list').innerHTML = state.assets.map(record => `<div class="item"><strong>${escapeHtml(record.payload.asset_name)}</strong><div>${escapeHtml(record.payload.db_type)} ${escapeHtml(record.payload.version)}</div><div>${escapeHtml(record.payload.configuration_id)}</div><div>监控 ${record.payload.monitoring_enabled ? '已启用' : '未启用'}</div></div>`).join('') || '<p>尚无资产记录</p>';
}

function renderWorkflows() {
  const options = state.workflows.map(record => ({id:record.id, title:record.payload.title + ' · ' + record.payload.state}));
  choices('workflow-id', options);
  choices('evidence-workflow', options);
  element('workflow-list').innerHTML = state.workflows.map(record => `<div class="item"><strong>${escapeHtml(record.payload.title)}</strong><div>${escapeHtml(record.item_id)} · ${escapeHtml(record.payload.state)} · 版本 ${record.version}</div><div>申请人 ${record.payload.requester}，审批人 ${record.payload.approved_by || '待审批'}</div></div>`).join('') || '<p>尚无工作流记录</p>';
}

function query() {
  return new URLSearchParams({period:element('period').value, ...(element('date').value ? {date:element('date').value} : {})});
}

async function refresh() {
  const [assets, workflows, overview] = await Promise.all([
    api('/api/services/assets'), api('/api/services/workflows'), api('/api/services/overview?' + query())
  ]);
  state.assets = assets.assets;
  state.workflows = workflows.records;
  state.overview = overview.overview;
  renderAssets();
  renderWorkflows();
  renderCoverage();
}

function detailsTemplate(id, kind) {
  const item = items(kind).find(item => item.id === element(id).value);
  return Object.fromEntries((item?.fields || []).map(field => [field, '']));
}

function newAsset() {
  element('asset-id').value = '';
  element('asset-data').value = JSON.stringify({
    configuration_id:'', asset_name:'', db_type:'oracle', version:'', owner:'', business:'', system:'', lifecycle:'active',
    instance_id:'', reviewers:[], project_id:'', cluster_id:'', cluster_type:'', node_count:null, node_names:[],
    architecture:'', cluster_network:'', deployment:'', hardware:'', host_model:'', location:'', os_version:'',
    storage_platform:'', database:'', instance_name:'', charset:'', patch_level:'', archive_mode:'', archive_path:'',
    backup_policy_id:'', backup_window:'', backup_frequency:'', backup_retention:'', disaster_recovery:'',
    least_privilege_reference:'', tls_ca_file:'', ssh_host_role:'', core_files:[], log_paths:{},
    transport:'tls', oracle_client_dir:'', oracle_config_dir:'', oracle_server_dn:'',
    monitoring_enabled:false, schedule_seconds:300, service_schedule_enabled:false, service_start_date:'', customer_calendar:null
  }, null, 2);
}

function handle(action) {
  return async event => {
    event?.preventDefault();
    try {
      await action();
    } catch (error) {
      message(error.message, true);
    }
  };
}

async function init() {
  state.catalog = (await api('/api/services/catalog')).catalog;
  choices('module-filter', [{id:'',title:'全部模块'}, ...state.catalog.modules.map(module => ({id:module.id,title:module.title}))]);
  choices('deliverable', [{id:'数据库运维服务履约报告',title:'数据库运维服务履约报告'}, ...state.catalog.modules.flatMap(module => module.deliverables.map(title => ({id:title,title})))]);
  choices('workflow-item', items('workflow').map(item => ({id:item.id,title:item.id + ' ' + item.title})));
  choices('evidence-item', items().filter(item => item.kind !== 'inspection').map(item => ({id:item.id,title:item.id + ' ' + item.title})));
  element('workflow-data').value = JSON.stringify(detailsTemplate('workflow-item','workflow'),null,2);
  newAsset();
  await refresh();
  element('refresh').onclick = handle(refresh);
  element('module-filter').onchange = renderCoverage;
  element('period').onchange = handle(refresh);
  element('date').onchange = handle(refresh);
  element('new-asset').onclick = newAsset;
  element('asset-id').onchange = () => {
    const record = state.assets.find(record => record.id === element('asset-id').value);
    if (record) element('asset-data').value = JSON.stringify(record.payload,null,2);
    else newAsset();
  };
  element('asset-form').onsubmit = handle(async () => {
    const old = state.assets.find(record => record.id === element('asset-id').value);
    await api('/api/services/assets', {payload:JSON.parse(element('asset-data').value), ...(old ? {id:old.id,version:old.version} : {})});
    message('资产台账已保存，变更已留存审计记录');
    await refresh();
  });
  element('inspect').onclick = handle(async () => {
    const id = element('asset-id').value;
    if (!id) throw new Error('请选择已有资产');
    message('正在执行固定只读采集');
    const result = await api('/api/services/assets/' + encodeURIComponent(id) + '/inspect', {});
    message(`采集完成，已记录 ${result.result.records} 项结果，异常及阻断 ${result.result.alerts} 项`);
    await refresh();
  });
  element('workflow-item').onchange = () => { element('workflow-data').value = JSON.stringify(detailsTemplate('workflow-item','workflow'),null,2); };
  element('workflow-form').onsubmit = handle(async () => {
    const details = JSON.parse(element('workflow-data').value);
    const result = await api('/api/services/workflows', {asset_id:element('workflow-asset').value,item_id:element('workflow-item').value,
      details, reviewers:element('reviewers').value.split(',').map(value => value.trim()).filter(Boolean).map(Number),
      ...(state.editWorkflow ? {id:state.editWorkflow.id, version:state.editWorkflow.version} : {})});
    state.editWorkflow = null;
    message('工作流草稿已保存，编号 ' + result.record.id);
    await refresh();
  });
  for (const button of document.querySelectorAll('[data-state]')) {
    button.onclick = handle(async () => {
      const record = state.workflows.find(record => record.id === element('workflow-id').value);
      if (!record) throw new Error('请选择工作流');
      await api('/api/services/workflows/' + record.id + '/transition', {state:button.dataset.state,version:record.version,evidence:JSON.parse(element('transition-data').value)});
      message('状态迁移已登记');
      await refresh();
    });
  }
  element('evidence-item').onchange = () => { element('evidence-data').value = JSON.stringify(detailsTemplate('evidence-item'),null,2); };
  element('evidence-form').onsubmit = handle(async () => {
    await api('/api/services/evidence', {asset_id:element('evidence-asset').value,item_id:element('evidence-item').value,
      workflow_id:element('evidence-workflow').value,details:JSON.parse(element('evidence-data').value)});
    message('验收证据已追加');
    await refresh();
  });
  element('load-draft').onclick = handle(async () => {
    const record = state.workflows.find(record => record.id === element('workflow-id').value);
    if (!record || record.payload.state !== 'draft') throw new Error('请选择可编辑的草稿');
    state.editWorkflow = record;
    element('workflow-asset').value = record.asset_id;
    element('workflow-item').value = record.item_id;
    element('workflow-data').value = JSON.stringify(record.payload.details,null,2);
    element('reviewers').value = (record.payload.reviewers || []).join(',');
    message('已载入草稿，保存时校验当前版本');
  });
  element('new-workflow').onclick = () => {
    state.editWorkflow = null;
    element('workflow-data').value = JSON.stringify(detailsTemplate('workflow-item','workflow'),null,2);
  };
  element('show-source').onclick = handle(async () => {
    const recordId = element('review-record').value;
    if (!recordId) throw new Error('暂无证据完整的待复核检查');
    const result = await api('/api/services/records/' + encodeURIComponent(recordId));
    const run = await api('/api/services/runs/' + encodeURIComponent(result.record.payload.source_reference));
    element('source-data').textContent = JSON.stringify(run.record.payload,null,2);
  });
  element('submit-review').onclick = handle(async () => {
    await api('/api/services/reviews', {
      record_id:element('review-record').value, verdict:element('review-verdict').value,
      baseline_reference:element('review-baseline').value, review_reason:element('review-reason').value
    });
    message('独立复核已登记，原始采集证据保留');
    await refresh();
  });
  element('record-event').onclick = handle(async () => {
    const record = state.workflows.find(record => record.id === element('workflow-id').value);
    if (!record) throw new Error('请选择工作流');
    await api('/api/services/workflows/' + record.id + '/events', {
      version:record.version, event:element('event-type').value, reference:element('event-reference').value
    });
    message('服务事件与实际登记时间已留存');
    await refresh();
  });
  element('deliverable').onchange = handle(async () => {
    const title = element('deliverable').value;
    for (const [word,period] of [['季度','quarter'],['年报','year'],['月报','month'],['周报','week']]) {
      if (title.includes(word)) element('period').value = period;
    }
    await refresh();
  });
  for (const format of ['docx','html']) {
    element('export-' + format).onclick = () => {
      const params = query();
      params.set('format',format);
      params.set('deliverable',element('deliverable').value);
      window.open('/api/services/report?' + params,'_blank','noopener');
    };
  }
  for (const button of document.querySelectorAll('[data-tab]')) {
    button.onclick = () => {
      for (const pane of document.querySelectorAll('.pane')) pane.hidden = pane.id !== button.dataset.tab;
      for (const tab of document.querySelectorAll('[data-tab]')) tab.classList.toggle('selected',tab === button);
    };
  }
}

init().catch(error => message(error.message,true));
