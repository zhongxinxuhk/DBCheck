// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

'use strict';
(() => {
  const state = {clusters: [], kinds: {}, sources: [], selected: null, editing: null};
  const root = document.getElementById('cluster-workspace');
  const $ = id => root.querySelector('#' + CSS.escape(id));
  const labels = {healthy:'正常',warning:'关注',critical:'异常',incomplete:'采集不完整',stale:'样本过期',
    dispatching:'准备中',running:'执行中',done:'已完成',failed:'失败',error:'失败',partial_failed:'部分失败',
    interrupted:'已中断',not_started:'未启动',unconfirmed:'待复核',pending:'待执行',primary:'主库',standby:'备库',member:'成员',unknown:'未知'};
  const text = value => value === null || value === undefined || value === '' ? '—' : String(value);
  const escape = value => text(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const time = value => value ? new Date(value*1000).toLocaleString('zh-CN') : '尚未采集';
  const family = type => ['dm','dm8','dameng'].includes(type) ? 'dm' : String(type).startsWith('oracle') ? 'oracle' : '';
  const badge = status => `<span class="pill ${escape(status)}">${escape(labels[status] || status || '尚未采集')}</span>`;
  const sourceName = iid => state.sources.find(row => row.id === iid)?.name || iid;

  async function api(url, options={}) {
    const response = await fetch(url, {headers:{'Content-Type':'application/json'}, ...options});
    const value = await response.json();
    if (!response.ok || !value.ok) throw new Error(value.error || '请求失败');
    return value;
  }
  function message(value, success=false) {
    $('page-message').textContent = value;
    $('page-message').className = 'message'+(success ? ' success' : '');
  }
  async function loadOptions() {
    const result = await api('/api/clusters/options');
    state.kinds = result.kinds;
    state.sources = result.datasources;
    $('cluster-kind').replaceChildren();
    for (const [value,kind] of Object.entries(state.kinds)) {
      const option = new Option(kind.label, value);
      $('cluster-kind').add(option);
    }
  }
  function list() {
    $('cluster-list').replaceChildren();
    if (!state.clusters.length) $('cluster-list').textContent = '尚未添加集群';
    for (const cluster of state.clusters) {
      const button = document.createElement('button');
      button.className = 'cluster-card'+(state.selected === cluster.id ? ' selected' : '');
      button.innerHTML = `<strong>${escape(cluster.name)}</strong><div class="muted">${escape(state.kinds[cluster.spec.kind]?.label)}</div>${badge(cluster.restricted ? 'unknown' : cluster.monitor?.status)}`;
      button.onclick = () => { state.selected = cluster.id; list(); detail(cluster); };
      $('cluster-list').append(button);
    }
  }
  function detail(cluster) {
    if (cluster.restricted) {
      $('cluster-detail').innerHTML = '<div class="panel">部分成员的数据源权限已变化，请由集群管理员复核绑定关系。</div>';
      return;
    }
    const monitor = cluster.monitor;
    const issues = monitor?.issues || [];
    const members = cluster.spec.members;
    const nodeRows = members.map(member => {
      const node = monitor?.nodes[member.instance_id];
      return `<tr><td>${escape(sourceName(member.instance_id))}<div class="muted">${escape(node?.instance_name)}</div></td>
        <td>${member.enabled ? node?.available === true ? '在线' : node ? '不可用' : '待采集' : '已停用'}</td>
        <td>${escape(labels[member.expected_role])} / ${escape(labels[node?.role] || '未知')}</td>
        <td>${escape(node?.qps)} / ${escape(node?.tps)}</td><td>${escape(node?.sessions)}</td>
        <td>${escape(node?.repl_lag_s)}<div class="muted">传输 ${escape(node?.transport_lag_s)} / 重演 ${escape(node?.apply_lag_s)}</div></td><td>${escape(node?.version)}</td></tr>`;
    }).join('');
    $('cluster-detail').innerHTML = `<div class="panel"><div class="toolbar"><div><h2>${escape(cluster.name)}</h2>
      <div class="muted">${escape(state.kinds[cluster.spec.kind]?.label)} · ${time(monitor?.ts)} · ${cluster.spec.monitor_enabled ? '持续监控已启用' : '持续监控已暂停'}</div></div>${badge(monitor?.status)}</div>
      <div class="actions"><button id="edit-cluster">编辑集群</button><button id="refresh-cluster">立即采集</button>
      <button id="inspect-cluster" class="primary">巡检全部启用节点</button><button id="delete-cluster" class="danger">删除集群</button></div>
      <div class="metrics"><div class="metric">预期 / 登记<strong>${cluster.spec.expected_nodes} / ${members.length}</strong></div>
      <div class="metric">在线节点<strong>${escape(monitor?.online_nodes)}</strong></div>
      <div class="metric">独立实例覆盖率<strong>${monitor ? escape(monitor.coverage_pct)+'%' : '—'}</strong></div>
      <div class="metric">当前关注项<strong>${monitor ? issues.length : '—'}</strong></div></div>
      ${issues.length ? '<ul class="issues">'+issues.map(row=>`<li>${escape(row.message)}${row.instance_id ? ' · '+escape(sourceName(row.instance_id)) : ''}</li>`).join('')+'</ul>' : '<p class="muted">'+(monitor ? '本轮未发现已定义的集群异常。' : '等待首轮采集。')+'</p>'}
      <div class="scroll"><table><thead><tr><th>成员</th><th>连接</th><th>预期 / 实际角色</th><th>QPS / TPS</th><th>会话</th><th>复制延迟（秒）</th><th>版本</th></tr></thead><tbody>${nodeRows}</tbody></table></div>
      ${cluster.spec.service_instance_id ? '<p class="muted">业务入口 '+escape(sourceName(cluster.spec.service_instance_id))+' · '+(monitor?.service?.available ? '连接成功' : '等待采集或连接失败')+'</p>' : ''}
      <details><summary>节点采集证据与视图错误</summary><pre id="node-evidence"></pre></details></div>
      <div class="panel"><h2>监控历史与事件</h2><div id="monitor-trend" class="trend"></div><div id="cluster-events" class="muted">加载中…</div></div>
      <div class="panel"><h2>统一巡检任务</h2><div id="cluster-runs">加载中…</div></div>`;
    $('node-evidence').textContent = JSON.stringify(monitor?.nodes || {},null,2);
    $('edit-cluster').onclick = () => edit(cluster);
    $('refresh-cluster').onclick = async () => {
      try { await api(`/api/clusters/${cluster.id}/refresh`,{method:'POST',body:'{}'}); message('采集请求已提交，监控结果会自动刷新。',true); }
      catch(error) { message(error.message); }
    };
    $('inspect-cluster').onclick = async event => {
      event.target.disabled = true;
      try {
        await api(`/api/clusters/${cluster.id}/inspections`,{method:'POST',body:JSON.stringify({request_key:crypto.randomUUID()})});
        message('集群巡检已启动，将逐节点生成报告并汇总。',true);
        await loadRuns(cluster.id);
      } catch(error) { message(error.message); }
      finally { if (event.target.isConnected) event.target.disabled = false; }
    };
    $('delete-cluster').onclick = async () => {
      if (!confirm('删除集群配置及停止该集群监控？成员数据源和已有报告会保留。')) return;
      try { await api(`/api/clusters/${cluster.id}`,{method:'DELETE',body:JSON.stringify({version:cluster.version})});state.selected=null;await load(); }
      catch(error) { message(error.message); }
    };
    loadRuns(cluster.id).catch(error=>message(error.message));
    loadHistory(cluster.id).catch(error=>message(error.message));
  }
  async function loadRuns(id) {
    const result = await api(`/api/clusters/${id}/inspections`);
    if (state.selected !== id || !$('cluster-runs')) return;
    $('cluster-runs').innerHTML = result.runs.length ? result.runs.map(run=>`<div class="event">${time(run.created)} ${badge(run.status)}
      <a href="/cluster-inspections/${encodeURIComponent(run.id)}/report" target="_blank" rel="noopener">汇总报告</a>
      <div class="muted">${run.payload.members.map(member=>escape(sourceName(member.instance_id))+' '+escape(labels[member.status] || member.status)).join(' · ')}</div></div>`).join('') : '<p class="muted">尚未执行集群巡检。</p>';
  }
  async function loadHistory(id) {
    const result = await api(`/api/clusters/${id}/history`);
    if (state.selected !== id || !$('cluster-events')) return;
    $('monitor-trend').innerHTML = result.samples.slice(-60).map(sample=>`<span title="${escape(time(sample.ts))} · 覆盖率 ${sample.payload.coverage_pct}%" style="height:${Math.max(4,sample.payload.coverage_pct/2)}px;background:${sample.payload.status==='critical' ? '#c94b5b' : sample.payload.status==='healthy' ? '#39885c' : '#d3a34d'}"></span>`).join('');
    const events = {monitor_started:'首次采集',health_changed:'健康状态变化',role_changed:'主备角色变化',availability_changed:'节点连接状态变化',service_target_changed:'业务入口目标变化',alert_opened:'新增关注项',alert_resolved:'关注项恢复'};
    $('cluster-events').innerHTML = result.events.slice(-12).reverse().map(row=>`<div class="event">${escape(time(row.ts))} · ${escape(events[row.kind] || row.kind)} ${escape(row.payload.instance_id ? sourceName(row.payload.instance_id) : '')}
      ${row.payload.before !== undefined ? escape(labels[row.payload.before] || row.payload.before)+' → '+escape(labels[row.payload.after] || row.payload.after) : ''}${row.payload.code ? ' · '+escape(row.payload.code) : ''}</div>`).join('') || '尚无状态变化事件。';
  }
  function memberOptions(existing={}) {
    const chosenFamily = state.kinds[$('cluster-kind').value]?.family;
    const sources = state.sources.filter(row=>family(row.db_type)===chosenFamily);
    $('member-options').replaceChildren();
    $('service-instance').replaceChildren(new Option('逐节点采集',''));
    for(const source of sources) {
      const row = document.createElement('div');
      row.className = 'member-row';
      const label = document.createElement('label');
      label.className = 'member-label';
      const check = document.createElement('input');
      check.type='checkbox';check.className='member-check';check.dataset.instanceId=source.id;
      check.checked=!!existing[source.id];
      const info = document.createElement('span');
      info.className = 'member-info';
      const name = document.createElement('strong');
      name.textContent = source.name;
      const meta = document.createElement('small');
      meta.textContent = `${source.host}:${source.port} · ${source.service_name || source.database || '—'}`;
      info.append(name,meta);
      label.append(check,info);
      const controls = document.createElement('div');
      controls.className = 'member-controls';
      const role = document.createElement('select');role.dataset.roleFor=source.id;
      for(const value of ['member','primary','standby','unknown']) role.add(new Option(labels[value],value));
      role.value = existing[source.id]?.expected_role || 'member';
      const enabled = document.createElement('input');enabled.type='checkbox';enabled.dataset.enabledFor=source.id;enabled.checked=existing[source.id]?.enabled !== false;
      const enabledLabel = document.createElement('label');
      enabledLabel.className = 'member-enabled';
      enabledLabel.append(enabled,document.createTextNode('启用'));
      controls.append(role,enabledLabel);
      row.append(label,controls);
      row.classList.toggle('selected',check.checked);
      check.onchange=()=>row.classList.toggle('selected',check.checked);
      $('member-options').append(row);
      $('service-instance').add(new Option(source.name,source.id));
    }
    if (!sources.length) {
      const empty = document.createElement('div');
      empty.className = 'member-empty';
      empty.textContent = '请先在数据源管理中添加此系列的数据库节点。';
      $('member-options').append(empty);
    }
  }
  function edit(cluster=null) {
    state.editing = cluster;
    $('form-title').textContent = cluster ? '编辑集群' : '添加集群';
    $('cluster-name').value = cluster?.name || '';
    $('cluster-kind').value = cluster?.spec.kind || 'oracle_rac';
    $('expected-nodes').value = cluster?.spec.expected_nodes || 2;
    $('interval-seconds').value = cluster?.spec.interval_seconds || 30;
    $('lag-seconds').value = cluster?.spec.lag_alert_seconds || 30;
    $('monitor-enabled').checked = cluster?.spec.monitor_enabled !== false;
    const members = Object.fromEntries((cluster?.spec.members || []).map(row=>[row.instance_id,row]));
    memberOptions(members);
    $('service-instance').value = cluster?.spec.service_instance_id || '';
    $('form-message').textContent='';$('cluster-dialog').showModal();$('cluster-name').focus();
  }
  $('cluster-kind').onchange=()=>memberOptions();
  $('new-cluster').onclick=()=>edit();
  $('cancel-cluster').onclick=()=>$('cluster-dialog').close();
  $('close-cluster').onclick=()=>$('cluster-dialog').close();
  $('cluster-form').onsubmit=async event=>{
    event.preventDefault();$('save-cluster').disabled=true;$('form-message').textContent='';
    const members=[...root.querySelectorAll('#member-options input[data-instance-id]:checked')].map(check=>{
      const id=check.dataset.instanceId;
      const row=check.closest('.member-row');
      return {instance_id:id,expected_role:row.querySelector('select').value,enabled:row.querySelector('input[data-enabled-for]').checked};
    });
    const body={name:$('cluster-name').value,kind:$('cluster-kind').value,expected_nodes:Number($('expected-nodes').value),
      interval_seconds:Number($('interval-seconds').value),lag_alert_seconds:Number($('lag-seconds').value),
      monitor_enabled:$('monitor-enabled').checked,service_instance_id:$('service-instance').value,members,
      version:state.editing?.version};
    try{
      const result=await api('/api/clusters'+(state.editing ? '/'+state.editing.id : ''),{method:state.editing ? 'PUT' : 'POST',body:JSON.stringify(body)});
      state.selected=result.cluster.id;$('cluster-dialog').close();message('集群配置已保存。',true);await load();
    }catch(error){$('form-message').textContent=error.message;}
    finally{$('save-cluster').disabled=false;}
  };
  let loading=false;
  async function load(){
    if(loading || $('cluster-dialog').open) return;
    loading=true;
    try{
      const result=await api('/api/clusters');state.clusters=result.clusters;
      if(!state.clusters.some(row=>row.id===state.selected)) state.selected=state.clusters[0]?.id || null;
      list();const current=state.clusters.find(row=>row.id===state.selected);
      if(current) detail(current);else $('cluster-detail').innerHTML='<div class="panel">添加集群后查看成员、监控和巡检结果。</div>';
    }finally{loading=false;}
  }
  let initialized = false;
  window.initClusterPage = function() {
    if (initialized) return load().catch(error=>message(error.message));
    initialized = true;
    loadOptions().then(load).catch(error=>{initialized=false;message(error.message);});
  };
  setInterval(()=>{
    const page = root.closest('.page');
    if (!document.hidden && initialized && (!page || page.classList.contains('active'))) {
      load().catch(error=>message(error.message));
    }
  },5000);
  if (root.dataset.embedded !== 'true') window.initClusterPage();
})();
