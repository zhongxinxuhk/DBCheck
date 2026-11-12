// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"use strict";
(() => {
  const esc = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (char) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[char],
    );
  async function api(url, options = {}) {
    const headers =
      options.body instanceof FormData
        ? {}
        : { "Content-Type": "application/json" };
    const response = await fetch(url, {
      ...options,
      headers: { ...headers, ...options.headers },
    });
    const data = await response.json();
    if (!response.ok || !data.ok)
      throw new Error(data.error || data.msg || "操作失败");
    return data;
  }
  function dialog(title, content) {
    const box = document.createElement("dialog");
    box.style.cssText =
      "width:min(1000px,94vw);max-height:88vh;overflow:auto;padding:24px;border:1px solid var(--border,#ccd6e1);border-radius:12px;background:var(--surface,#fff);color:var(--text,#213146)";
    box.innerHTML = `<div style="display:flex;align-items:center;gap:16px;margin-bottom:16px"><h2 style="margin:0;flex:1">${esc(title)}</h2><button data-close type="button">关闭</button></div><p data-feedback role="status"></p>${content}`;
    box.querySelector("[data-close]").onclick = () => box.close();
    box.addEventListener("close", () => box.remove(), { once: true });
    document.body.append(box);
    box.showModal();
    return box;
  }
  const table = (headers, body) =>
    `<div style="overflow:auto"><table style="width:100%;text-align:left;border-collapse:collapse"><thead><tr>${headers.map((header) => `<th style="padding:10px">${esc(header)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
  function feedback(box, error) {
    box.querySelector("[data-feedback]").textContent = error.message || error;
  }
  async function importInventory(onComplete) {
    const box = dialog(
      "Excel 批量导入台账",
      `<p><a href="/api/operations/inventory/import-template" download>下载标准导入模板</a> · 也可修改已导出的台账后上传。相同配置项更新已有记录，密码留空沿用现有值。</p><input data-file type="file" accept=".xlsx"><button data-preview>检查并预览</button><div data-preview-body></div><button data-apply disabled>确认导入并同步数据源</button>`,
    );
    let token = null;
    box.querySelector("[data-file]").onchange = () => {
      token = null;
      box.querySelector("[data-apply]").disabled = true;
    };
    box.querySelector("[data-preview]").onclick = async (event) => {
      event.target.disabled = true;
      token = null;
      box.querySelector("[data-apply]").disabled = true;
      try {
        const file = box.querySelector("[data-file]").files[0];
        if (!file) throw new Error("请选择 Excel 文件");
        const form = new FormData();
        form.append("file", file);
        const result = await api("/api/operations/inventory/import-preview", {
          method: "POST",
          body: form,
        });
        token = result.token;
        box.querySelector("[data-preview-body]").innerHTML = table(
          ["处理方式", "配置项编号", "资产名称", "地址"],
          result.rows
            .map(
              (row) =>
                `<tr><td>${esc(row.action)}</td><td>${esc(row.configuration_id)}</td><td>${esc(row.name)}</td><td>${esc(row.host)}</td></tr>`,
            )
            .join(""),
        );
        box.querySelector("[data-apply]").disabled = false;
        feedback(box, `检查通过，共 ${result.rows.length} 条，确认后保存。`);
      } catch (error) {
        feedback(box, error);
      } finally {
        event.target.disabled = false;
      }
    };
    box.querySelector("[data-apply]").onclick = async (event) => {
      event.target.disabled = true;
      try {
        const result = await api("/api/operations/inventory/import-apply", {
          method: "POST",
          body: JSON.stringify({ token }),
        });
        token = null;
        feedback(
          box,
          `导入完成，新增 ${result.created} 条，更新 ${result.updated} 条。数据源已同步。`,
        );
        await onComplete?.();
      } catch (error) {
        feedback(box, error);
        event.target.disabled = !token;
      }
    };
  }
  async function testConnections(ids, onEdit) {
    if (!ids.length) return alert("请先勾选需要检查的数据源");
    const box = dialog(
      "批量测试连接",
      `<button data-stop>停止后续检查</button><p>使用已保存的数据库、驱动与 SSH 连接配置。</p><div data-results></div>`,
    );
    let stopped = false,
      cursor = 0;
    box.querySelector("[data-stop]").onclick = () => {
      stopped = true;
      feedback(box, "已停止后续检查，正在进行的连接检查完成后结束。");
    };
    box.addEventListener("close", () => {
      stopped = true;
    });
    try {
      const records = (await api("/api/operations/inventory")).records.filter(
        (record) => ids.includes(record.id),
      );
      const results = records.map((record) => ({
        record,
        status: "等待检查",
        message: "",
      }));
      function render() {
        box.querySelector("[data-results]").innerHTML = table(
          ["数据源", "地址", "状态", "检查结果", "操作"],
          results
            .map(
              (item) =>
                `<tr><td>${esc(item.record.source.name)}</td><td>${esc(item.record.source.host)}:${esc(item.record.source.port)}</td><td>${esc(item.status)}</td><td>${esc(item.message)}</td><td><button data-edit="${esc(item.record.id)}">修改配置</button></td></tr>`,
            )
            .join(""),
        );
        box.querySelectorAll("[data-edit]").forEach(
          (button) =>
            (button.onclick = () => {
              box.close();
              onEdit?.(button.dataset.edit);
            }),
        );
      }
      render();
      await Promise.all(
        Array.from({ length: Math.min(2, results.length) }, async () => {
          while (!stopped && cursor < results.length) {
            const item = results[cursor++];
            item.status = "检查中";
            render();
            try {
              const result = await api(
                `/api/operations/inventory/${encodeURIComponent(item.record.id)}/test-connection`,
                { method: "POST", body: "{}" },
              );
              item.status = result.connected ? "连接成功" : "连接失败";
              item.message = result.message;
            } catch (error) {
              item.status = "检查失败";
              item.message = error.message;
            }
            render();
          }
        }),
      );
      for (const item of results)
        if (item.status === "等待检查") item.status = "已跳过";
      render();
      box.querySelector("[data-stop]").disabled = true;
      feedback(
        box,
        `检查结束，成功 ${results.filter((item) => item.status === "连接成功").length} 条，失败 ${results.filter((item) => item.status.includes("失败")).length} 条，跳过 ${results.filter((item) => item.status === "已跳过").length} 条。`,
      );
    } catch (error) {
      feedback(box, error);
    }
  }
  const states = {
    pending: "待处理",
    working: "处理中",
    review: "待复检",
    closed: "已关闭",
  };
  async function editFinding(record, onChange, onRun) {
    const box = dialog(
      "维护巡检整改",
      `<h3>${esc(record.name)} · ${esc(record.description)}</h3><p>${esc(record.suggestion)}</p><form data-form style="display:grid;gap:12px"><label>状态 <select name="state">${Object.entries(
        states,
      )
        .map(
          ([key, label]) =>
            `<option value="${key}" ${record.state === key ? "selected" : ""}>${label}</option>`,
        )
        .join(
          "",
        )}</select></label><label>负责人 <input name="assignee" required value="${esc(record.assignee)}"></label><label>计划完成日期 <input type="date" name="deadline" value="${esc(record.deadline)}"></label><label>处理措施 <textarea name="action" rows="4" style="width:100%">${esc(record.action)}</textarea></label><label>复检任务 <select name="verification_run"><option value="">请选择复检任务</option></select></label><label>本次备注 <input name="note" style="width:100%"></label><div><button type="submit">保存整改记录</button> <button type="button" data-recheck>发起同模板复检</button></div></form><details><summary>处理记录</summary>${record.history.map((item) => `<p>${esc(new Date(item.at * 1000).toLocaleString())} · ${esc(item.actor)} · ${esc(states[item.state])}<br>${esc(item.note)}</p>`).join("")}</details>`,
    );
    const form = box.querySelector("form");
    try {
      const runs = (await api("/api/operations/batches")).runs.filter(
        (run) =>
          run.created > record.source_created &&
          run.payload.members.some(
            (member) => member.instance_id === record.instance_id,
          ),
      );
      form.elements.verification_run.innerHTML += runs
        .map(
          (run) =>
            `<option value="${esc(run.id)}" ${record.verification_run === run.id ? "selected" : ""}>${esc(run.payload.name)} · ${esc(new Date(run.created * 1000).toLocaleString())}</option>`,
        )
        .join("");
    } catch (error) {
      feedback(box, error);
    }
    form.onsubmit = async (event) => {
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        await api(`/api/operations/findings/${record.id}`, {
          method: "PUT",
          body: JSON.stringify({
            ...Object.fromEntries(new FormData(form)),
            version: record.version,
          }),
        });
        box.close();
        await onChange?.();
      } catch (error) {
        feedback(box, error);
      } finally {
        button.disabled = false;
      }
    };
    box.querySelector("[data-recheck]").onclick = async (event) => {
      event.target.disabled = true;
      try {
        const result = await api(
          `/api/operations/findings/${record.id}/recheck`,
          {
            method: "POST",
            body: JSON.stringify({ request_key: crypto.randomUUID() }),
          },
        );
        box.close();
        await onChange?.();
        await onRun?.(result.run.id);
      } catch (error) {
        feedback(box, error);
        event.target.disabled = false;
      }
    };
  }
  async function findings(onRun) {
    const box = dialog(
      "巡检问题整改",
      `<div style="display:flex;gap:12px;flex-wrap:wrap"><input data-search placeholder="搜索数据库、问题或负责人"><select data-state><option value="">全部状态</option>${Object.entries(
        states,
      )
        .map(([key, label]) => `<option value="${key}">${label}</option>`)
        .join(
          "",
        )}</select><a href="/api/operations/findings/export" download>导出整改清单 Excel</a><button data-refresh>刷新</button></div><div data-list></div>`,
    );
    let records = [];
    function render() {
      const query = box.querySelector("[data-search]").value.toLowerCase(),
        state = box.querySelector("[data-state]").value;
      const rows = records.filter(
        (record) =>
          (!state || record.state === state) &&
          [record.name, record.description, record.assignee]
            .join(" ")
            .toLowerCase()
            .includes(query),
      );
      box.querySelector("[data-list]").innerHTML = table(
        ["数据库 / 问题", "状态", "负责人", "计划完成日期", "操作"],
        rows
          .map(
            (record) =>
              `<tr><td>${esc(record.name)}<br>${esc(record.description)}</td><td>${esc(states[record.state])}</td><td>${esc(record.assignee)}</td><td>${esc(record.deadline)}</td><td><button data-finding="${esc(record.id)}">查看 / 维护</button></td></tr>`,
          )
          .join("") ||
          '<tr><td colspan="5">暂无整改记录，可从巡检异常项创建。</td></tr>',
      );
      box.querySelectorAll("[data-finding]").forEach(
        (button) =>
          (button.onclick = () =>
            editFinding(
              records.find((record) => record.id === button.dataset.finding),
              load,
              onRun,
            )),
      );
    }
    async function load() {
      try {
        records = (await api("/api/operations/findings")).records;
        render();
      } catch (error) {
        feedback(box, error);
      }
    }
    box.querySelector("[data-search]").oninput = render;
    box.querySelector("[data-state]").onchange = render;
    box.querySelector("[data-refresh]").onclick = load;
    await load();
  }
  async function createFinding(runId, instanceId, fingerprint, onRun) {
    const result = await api("/api/operations/findings", {
      method: "POST",
      body: JSON.stringify({
        run_id: runId,
        instance_id: instanceId,
        fingerprint,
      }),
    });
    await editFinding(result.record, null, onRun);
  }
  function scopePicker(element, options, selected = {}, onChange = () => {}) {
    const groups = options.records || options.groups || [],
      fields = options.fields || options.grouping_fields;
    const filters = selected.selection_filters || {};
    const selectedGroups = new Set(selected.group_ids || []);
    element.innerHTML = `<p>保存的分组会在每次执行时按最新台账和当前数据源权限匹配成员。</p><div data-groups style="display:flex;gap:12px;flex-wrap:wrap">${groups.map((group) => `<label><input type="checkbox" data-scope-group="${esc(group.id)}" ${selectedGroups.has(group.id) ? "checked" : ""}>${esc(group.name)}（当前可见 ${group.count} 个）</label>`).join("") || "<span>暂无保存的分组，可点击“维护巡检分组”创建。</span>"}</div><details><summary>按业务条件纳入</summary><p>同一字段可多选，不同字段同时匹配。条件留空时使用已选数据源、集群或保存的分组。</p><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px">${Object.entries(
      fields,
    )
      .filter(([key]) => key !== "cluster_id" && key !== "db_type")
      .map(
        ([key, label]) =>
          `<fieldset data-scope-filter="${esc(key)}"><legend>${esc(label)}</legend><div style="max-height:130px;overflow:auto">${facetChecks(options.facets[key] || [], filters[key] || [])}</div></fieldset>`,
      )
      .join("")}</div></details><p data-scope-summary role="status"></p>`;
    function value() {
      return {
        group_ids: [
          ...element.querySelectorAll("[data-scope-group]:checked"),
        ].map((input) => input.dataset.scopeGroup),
        selection_filters: Object.fromEntries(
          [...element.querySelectorAll("[data-scope-filter]")]
            .map((input) => [
              input.dataset.scopeFilter,
              [...input.querySelectorAll("input:checked")].map(
                (option) => option.value,
              ),
            ])
            .filter(([, values]) => values.length),
        ),
      };
    }
    function changed() {
      const selection = value();
      element.querySelector("[data-scope-summary]").textContent =
        `已选 ${selection.group_ids.length} 个分组，${Object.keys(selection.selection_filters).length} 项业务条件。`;
      onChange(selection);
    }
    element.onchange = changed;
    changed();
    return { value };
  }
  function facetChecks(options, selected) {
    const rows = [...options];
    for (const value of selected)
      if (!rows.some((item) => item.value === value))
        rows.push({ value, label: value || "未登记", count: 0 });
    return rows
      .map(
        (item) =>
          `<label style="display:block;padding:4px"><input type="checkbox" value="${esc(item.value)}" ${selected.includes(item.value) ? "checked" : ""}>${esc(item.label)}（${item.count}）</label>`,
      )
      .join("");
  }
  async function bulkGrouping(records, onComplete) {
    if (!records.length) return alert("请先勾选需要维护的数据源");
    const fields = {
      system: "业务系统",
      environment: "运行环境",
      owner: "运维责任人",
      group: "数据源分组",
    };
    const box = dialog(
      "批量维护业务分组",
      `<p>已选择 ${records.length} 个数据源。勾选要修改的字段，勾选后留空会清除该项登记。</p><form data-form style="display:grid;gap:14px">${Object.entries(
        fields,
      )
        .map(
          ([key, label]) =>
            `<label><input type="checkbox" data-apply-field="${key}">${label} <input name="${key}" placeholder="${label}"></label>`,
        )
        .join("")}<button type="submit">保存到所选台账及数据源</button></form>`,
    );
    box.querySelector("form").onsubmit = async (event) => {
      event.preventDefault();
      const button = box.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        const metadata = {},
          source = {};
        box.querySelectorAll("[data-apply-field]:checked").forEach((input) => {
          const key = input.dataset.applyField;
          (key === "group" ? source : metadata)[key] = box.querySelector(
            `[name="${key}"]`,
          ).value;
        });
        const result = await api("/api/operations/inventory/grouping", {
          method: "PUT",
          body: JSON.stringify({
            instance_ids: records.map((record) => record.id),
            revisions: Object.fromEntries(
              records.map((record) => [record.id, record.revision]),
            ),
            metadata,
            source,
          }),
        });
        await onComplete?.();
        box.close();
      } catch (error) {
        feedback(box, error);
      } finally {
        button.disabled = false;
      }
    };
  }
  async function manageGroups(onComplete) {
    const box = dialog(
      "维护巡检分组",
      `<button data-new>新建分组</button><button data-refresh>刷新</button><div data-list></div>`,
    );
    let options;
    async function load() {
      try {
        options = await api("/api/operations/groups");
        box.querySelector("[data-list]").innerHTML = table(
          ["分组名称", "当前可见成员", "使用范围", "操作"],
          options.records
            .map(
              (group) =>
                `<tr><td>${esc(group.name)}</td><td>${group.count}</td><td>${group.shared ? "本租户共享" : "仅创建人及管理员"}</td><td><button data-edit-group="${group.id}">${group.can_edit ? "维护" : "查看"}</button>${group.can_edit ? ` <button data-delete-group="${group.id}">删除</button>` : ""}</td></tr>`,
            )
            .join("") || '<tr><td colspan="4">暂无巡检分组。</td></tr>',
        );
        box.querySelectorAll("[data-edit-group]").forEach(
          (button) =>
            (button.onclick = () =>
              editGroup(
                options,
                options.records.find(
                  (group) => group.id === button.dataset.editGroup,
                ),
                changed,
              )),
        );
        box.querySelectorAll("[data-delete-group]").forEach(
          (button) =>
            (button.onclick = async () => {
              const group = options.records.find(
                (item) => item.id === button.dataset.deleteGroup,
              );
              if (
                !confirm(
                  `删除“${group.name}”分组？引用此分组的定时任务需要重新选择范围。`,
                )
              )
                return;
              try {
                await api("/api/operations/groups/" + group.id, {
                  method: "DELETE",
                  body: JSON.stringify({ version: group.version }),
                });
                await changed();
              } catch (error) {
                feedback(box, error);
              }
            }),
        );
      } catch (error) {
        feedback(box, error);
      }
    }
    async function changed() {
      await load();
      await onComplete?.();
    }
    box.querySelector("[data-new]").onclick = () =>
      editGroup(options, null, changed);
    box.querySelector("[data-refresh]").onclick = load;
    await load();
  }
  async function editGroup(options, group, onComplete) {
    const canEdit = !group || group.can_edit;
    const box = dialog(
      group ? "维护巡检分组" : "新建巡检分组",
      `<form data-form style="display:grid;gap:12px"><label>分组名称 <input name="name" required maxlength="100" value="${esc(group?.name || "")}"></label><label>使用范围 <select name="shared"><option value="false" ${group?.shared ? "" : "selected"}>仅创建人及管理员</option><option value="true" ${group?.shared ? "selected" : ""}>本租户共享</option></select></label><p>同一字段内任一值匹配即可，不同字段同时匹配。条件全部留空会纳入全部当前可见且已启用的数据源。</p><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px">${Object.entries(
        options.fields,
      )
        .map(
          ([key, label]) =>
            `<fieldset data-group-filter="${key}"><legend>${esc(label)}</legend><div style="max-height:150px;overflow:auto">${facetChecks(options.facets[key] || [], group?.filters[key] || [])}</div></fieldset>`,
        )
        .join(
          "",
        )}</div><div><button type="button" data-preview>预览匹配成员</button> <button type="submit" ${canEdit ? "" : "disabled"}>保存分组</button></div><div data-members></div></form><details><summary>分组修改记录</summary>${(group?.history || []).map((item) => `<p>${esc(new Date(item.at * 1000).toLocaleString())} · ${esc(item.actor)} · ${esc(item.name)}</p>`).join("")}</details>`,
    );
    const form = box.querySelector("form");
    const filters = () =>
      Object.fromEntries(
        [...box.querySelectorAll("[data-group-filter]")]
          .map((input) => [
            input.dataset.groupFilter,
            [...input.querySelectorAll("input:checked")].map(
              (option) => option.value,
            ),
          ])
          .filter(([, values]) => values.length),
      );
    box.querySelector("[data-preview]").onclick = async (event) => {
      event.target.disabled = true;
      try {
        const result = await api("/api/operations/groups/preview", {
          method: "POST",
          body: JSON.stringify({ filters: filters() }),
        });
        box.querySelector("[data-members]").innerHTML = table(
          ["数据库", "业务系统", "环境", "负责人"],
          result.records
            .map(
              (record) =>
                `<tr><td>${esc(record.name)}</td><td>${esc(record.system)}</td><td>${esc(record.environment)}</td><td>${esc(record.owner)}</td></tr>`,
            )
            .join("") ||
            '<tr><td colspan="4">当前没有匹配且已启用的数据源。</td></tr>',
        );
        feedback(box, `当前可见且已启用 ${result.records.length} 个数据源。`);
      } catch (error) {
        feedback(box, error);
      } finally {
        event.target.disabled = false;
      }
    };
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (!canEdit) return;
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        await api("/api/operations/groups" + (group ? "/" + group.id : ""), {
          method: group ? "PUT" : "POST",
          body: JSON.stringify({
            name: form.elements.name.value,
            shared: form.elements.shared.value === "true",
            filters: filters(),
            ...(group ? { version: group.version } : {}),
          }),
        });
        await onComplete?.();
        box.close();
      } catch (error) {
        feedback(box, error);
      } finally {
        button.disabled = false;
      }
    };
    if (!canEdit)
      form
        .querySelectorAll("input,select")
        .forEach((input) => (input.disabled = true));
  }

  window.DBPulseTools = {
    importInventory,
    testConnections,
    findings,
    createFinding,
    scopePicker,
    bulkGrouping,
    manageGroups,
  };
})();
