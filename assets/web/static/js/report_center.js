// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"use strict";
(() => {
  const root = document.getElementById("report-center");
  if (!root) return;
  const $ = (id) => root.querySelector("#rc-" + id);
  const state = {
    assets: [],
    catalog: [],
    combinations: {},
    jobs: [],
    timer: null,
    cutoffEdited: false,
  };
  const labels = {
    queued: "待生成",
    generating: "生成中",
    failed: "生成失败",
    draft: "待复核",
    published: "已完成",
  };
  const title = (id) => state.catalog.find((v) => v.id === id)?.title || id;
  const message = (value, error = false) => {
    $("message").textContent = value;
    $("message").className = error ? "error" : "";
  };
  const node = (tag, value) => {
    const item = document.createElement(tag);
    if (value !== undefined) item.textContent = value;
    return item;
  };
  const display = (value) =>
    value == null
      ? "—"
      : typeof value === "boolean"
        ? value
          ? "是"
          : "否"
        : typeof value === "object"
          ? JSON.stringify(value)
          : String(value);
  async function api(path, body) {
    const options =
      body === undefined
        ? {}
        : body instanceof FormData
          ? { method: "POST", body }
          : {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            };
    const response = await fetch(
      "/api/report-center/" + path.replace(/^deliverables\//, ""),
      options,
    );
    if (response.status === 401) {
      location.href = "/um/login";
      throw new Error("请先登录");
    }
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "请求失败");
    return result;
  }
  function tab(name) {
    root
      .querySelectorAll("[data-tab]")
      .forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    root
      .querySelectorAll(".rc-pane")
      .forEach((p) => (p.hidden = p.id !== "rc-" + name));
  }
  root
    .querySelectorAll("[data-tab]")
    .forEach((b) => (b.onclick = () => tab(b.dataset.tab)));
  function checks(container, values, selected) {
    container.replaceChildren();
    const groups = new Map();
    if (container === $("deliverables")) {
      ["台账", "巡检与周期报告", "专项报告"].forEach((name) => {
        const group = node("details");
        group.open = true;
        group.append(node("summary", name));
        container.append(group);
        groups.set(name, group);
      });
    }
    values.forEach((value) => {
      const label = node("label");
      const input = node("input");
      input.type = "checkbox";
      input.value = value.id;
      input.checked = selected.includes(value.id);
      label.append(input, node("span", value.title));
      const category = /^D0[1-7]$/.test(value.id)
        ? "台账"
        : [
              "RUN",
              "D08",
              "D09",
              "D10",
              "D11",
              "D12",
              "D13",
              "D18",
              "D22",
            ].includes(value.id)
          ? "巡检与周期报告"
          : "专项报告";
      (groups.get(category) || container).append(label);
    });
  }
  const checked = (container) =>
    [...container.querySelectorAll("input:checked")].map((i) => i.value);
  function sheets() {
    const names = [
      ...new Set(
        state.catalog
          .filter((v) => checked($("deliverables")).includes(v.id))
          .flatMap((v) => v.sheets),
      ),
    ];
    checks(
      $("sheets"),
      names.map((name) => ({ id: name, title: name })),
      [],
    );
  }
  function combination() {
    const choice = $("combination").value;
    $("period").closest("label").hidden = ["run", "register"].includes(choice);
    $("date").closest("label").hidden = ["run", "register"].includes(choice);
    if (choice === "custom") return;
    const selected =
      choice === "run" ? ["RUN"] : state.combinations[choice] || [];
    $("deliverables")
      .querySelectorAll("input")
      .forEach((i) => (i.checked = selected.includes(i.value)));
    $("deliverables")
      .querySelectorAll("details")
      .forEach((group) => {
        group.open = Boolean(group.querySelector("input:checked"));
        [...group.querySelectorAll(":scope > label")]
          .sort(
            (left, right) =>
              Number(right.querySelector("input").checked) -
              Number(left.querySelector("input").checked),
          )
          .forEach((label) => group.append(label));
      });
    if (["week", "month", "quarter", "year"].includes(choice))
      $("period").value = choice;
    sheets();
  }
  function options() {
    if (!state.cutoffEdited)
      $("cutoff").value = new Date(
        Date.now() - new Date().getTimezoneOffset() * 60000,
      )
        .toISOString()
        .slice(0, 19);
    return {
      source_ids: checked($("assets")),
      collect_first: $("collect-first").checked,
      split_sources: $("split-sources").checked,
      deliverables: checked($("deliverables")),
      period: $("period").value,
      date: $("date").value,
      cutoff: state.cutoffEdited
        ? new Date($("cutoff").value).toISOString()
        : undefined,
      formats: [...root.querySelectorAll("input[name=format]:checked")].map(
        (i) => i.value,
      ),
      run_id: $("run").value,
      workflow_ids: $("event").value ? [$("event").value] : [],
      sheets: checked($("sheets")),
      layout: $("layout").value,
      previous_id: $("previous").value || undefined,
      reason: $("reason").value,
    };
  }
  function table(container, headers, rows) {
    const wrapper = node("div");
    wrapper.className = "rc-table-wrap";
    const table = node("table");
    const head = node("tr");
    headers.forEach((h) => head.append(node("th", h)));
    const thead = node("thead");
    thead.append(head);
    table.append(thead);
    const body = node("tbody");
    rows.forEach((values) => {
      const tr = node("tr");
      values.forEach((value) => {
        const td = node("td");
        td.append(value instanceof Node ? value : node("span", display(value)));
        tr.append(td);
      });
      body.append(tr);
    });
    if (!rows.length) {
      const td = node("td", "暂无记录");
      td.colSpan = headers.length;
      const tr = node("tr");
      tr.append(td);
      body.append(tr);
    }
    table.append(body);
    wrapper.append(table);
    container.replaceChildren(wrapper);
  }
  function button(label, action) {
    const b = node("button", label);
    b.type = "button";
    b.onclick = async () => {
      b.disabled = true;
      try {
        await action();
      } catch (error) {
        message(error.message, true);
      } finally {
        b.disabled = false;
      }
    };
    return b;
  }
  function downloadLink(job, artifact) {
    const a = node("a", artifact ? artifact.name : "下载交付包");
    a.href =
      "/api/report-center/" +
      encodeURIComponent(job.id) +
      "/download" +
      (artifact
        ? "?artifact_id=" + encodeURIComponent(artifact.artifact_id)
        : "");
    return a;
  }
  function revision(job) {
    $("previous").value = job.id;
    $("reason").value = "";
    $("reason").closest("details").open = true;
    $("revision-label").textContent = "修订报告 " + job.report_id;
    $("period").value = job.request.period;
    $("date").value = job.request.date;
    $("combination").value = "custom";
    $("layout").value = job.request.layout || "separate";
    $("event").value = job.request.workflow_ids?.[0] || "";
    $("assets")
      .querySelectorAll("input")
      .forEach(
        (i) =>
          (i.checked = (
            job.source_ids ||
            job.request.source_ids ||
            job.request.asset_ids
          ).includes(i.value)),
      );
    $("deliverables")
      .querySelectorAll("input")
      .forEach((i) => (i.checked = job.request.deliverables.includes(i.value)));
    sheets();
    tab("generate");
  }
  function renderJobs() {
    const search = $("archive-filter").value.toLowerCase();
    const jobs = state.jobs.filter((j) =>
      JSON.stringify([
        j.report_id,
        j.request.date,
        j.request.deliverables.map(title),
      ])
        .toLowerCase()
        .includes(search),
    );
    table(
      $("archive-list"),
      ["报告 / 周期", "交付物", "状态", "文件与操作"],
      jobs.map((job) => {
        const actions = node("div");
        if (["queued", "generating", "failed"].includes(job.state))
          actions.append(
            button("重试中断任务", async () => {
              await api("deliverables/jobs/" + job.id + "/retry", {});
              await refresh();
            }),
          );
        if (job.artifacts.length) {
          const details = node("details");
          details.append(node("summary", "文件、摘要与签名"));
          job.artifacts.forEach((artifact) => {
            const line = node("div");
            line.append(
              downloadLink(job, artifact),
              node("code", artifact.sha256),
            );
            details.append(line);
          });
          details.append(
            node("p", "签名公钥指纹"),
            node("code", job.manifest.key_id),
          );
          actions.append(
            downloadLink(job),
            button("生成修订版", () => revision(job)),
            button("校验", () => {
              $("verify-job").value = job.id;
              verifyArtifacts();
              tab("verify");
            }),
            details,
          );
        }
        return [
          job.report_id + " / v" + job.revision + " / " + job.request.date,
          job.request.deliverables.map(title).join("、"),
          (labels[job.state] || job.state) +
            (job.error ? " · " + job.error : ""),
          actions,
        ];
      }),
    );
    const selected = $("verify-job").value;
    $("verify-job").replaceChildren(node("option", "请选择报告"));
    $("verify-job").firstChild.value = "";
    state.jobs
      .filter((j) => j.artifacts.length)
      .forEach((j) => {
        const o = node(
          "option",
          j.report_id + " / v" + j.revision + " / " + j.request.date,
        );
        o.value = j.id;
        $("verify-job").append(o);
      });
    $("verify-job").value = selected;
    verifyArtifacts();
  }
  function verifyArtifacts() {
    const job = state.jobs.find((j) => j.id === $("verify-job").value);
    $("verify-artifact").replaceChildren();
    const o = node("option", "自动识别水印或交付包");
    o.value = "";
    $("verify-artifact").append(o);
    (job?.artifacts || []).forEach((a) => {
      const option = node("option", a.name);
      option.value = a.artifact_id;
      $("verify-artifact").append(option);
    });
  }
  async function refresh() {
    const data = await api("deliverables/jobs");
    state.jobs = data.jobs;
    renderJobs();
    clearTimeout(state.timer);
    if (
      active() &&
      state.jobs.some((j) => ["queued", "generating"].includes(j.state))
    )
      state.timer = setTimeout(
        () => refresh().catch((e) => message(e.message, true)),
        5000,
      );
  }
  $("combination").onchange = combination;
  $("period").onchange = () => {
    $("combination").value = $("period").value;
    combination();
  };
  $("cutoff").onchange = () => {
    state.cutoffEdited = true;
  };
  $("deliverables").onchange = () => {
    $("combination").value = "custom";
    sheets();
  };
  $("asset-filter").oninput = () => {
    const query = $("asset-filter").value.toLowerCase();
    $("assets")
      .querySelectorAll("label")
      .forEach(
        (l) => (l.hidden = !l.textContent.toLowerCase().includes(query)),
      );
  };
  $("archive-filter").oninput = renderJobs;
  $("verify-job").onchange = verifyArtifacts;

  $("preview").onclick = async () => {
    try {
      const data = await api("deliverables/preview", options());
      const stats = node("div");
      stats.className = "rc-stats";
      const labels = {
        assets: "资产",
        runs: "采集批次",
        checks: "检查结果",
        uncollected_days: "缺采集天数",
        workflows: "服务记录",
        open_issues: "未结整改记录",
      };
      Object.entries(data.coverage).forEach(([k, v]) => {
        const item = node("div", labels[k] || k);
        item.append(node("strong", v));
        stats.append(item);
      });
      $("preview-result").replaceChildren(stats);
      message("覆盖预览已生成，缺失证据将在报告中保留状态");
    } catch (e) {
      message(e.message, true);
    }
  };
  $("collect").onclick = () => {
    $("collect-first").checked = true;
    $("form").requestSubmit($("form").querySelector('button[type="submit"]'));
  };
  $("form").onsubmit = async (event) => {
    event.preventDefault();
    const b = event.submitter;
    b.disabled = true;
    try {
      const result = await api("deliverables/jobs", options());
      message("已创建报告任务 " + result.job.id);
      $("previous").value = "";
      $("revision-label").textContent = "";
      await refresh();
      tab("archive");
    } catch (e) {
      message(e.message, true);
    } finally {
      $("collect-first").checked = false;
      b.disabled = false;
    }
  };
  $("verify-form").onsubmit = async (event) => {
    event.preventDefault();
    const b = event.submitter;
    b.disabled = true;
    try {
      const data = new FormData();
      data.append("job_id", $("verify-job").value);
      data.append("artifact_id", $("verify-artifact").value);
      data.append("file", $("file").files[0]);
      const { result } = await api("deliverables/verify", data);
      const card = node("div");
      card.className = "rc-card";
      card.append(node("h2", result.message));
      const dl = node("dl");
      [
        ["文件SHA-256", result.sha256],
        ["归档原件匹配", result.matches_original],
        ["签名验证", result.signature_valid],
        ["报告版本", result.revision],
        ["文件签发状态", labels[result.signed_state || result.state]],
        ["后续发布版本", result.superseded_by?.revision],
        ["水印匹配", result.watermark_matches],
        ["内容差异数", result.total],
        ["比对说明", result.diff_reason],
      ].forEach(([k, v]) => {
        dl.append(node("dt", k), node("dd", display(v)));
      });
      card.append(dl);
      $("verify-result").replaceChildren(card);
      if (result.differences?.length) {
        const diff = node("div");
        table(
          diff,
          ["位置", "原值", "当前值"],
          result.differences.map((v) => [v.位置, v.原值, v.当前值]),
        );
        card.append(diff);
      }
    } catch (e) {
      message(e.message, true);
    } finally {
      b.disabled = false;
    }
  };
  async function init() {
    const local = new Date(
      Date.now() - new Date().getTimezoneOffset() * 60000,
    ).toISOString();
    $("date").value = local.slice(0, 10);
    $("cutoff").value = local.slice(0, 19);
    const [catalog, assets, observations, workflows] = await Promise.all([
      api("deliverables/catalog"),
      api("sources"),
      api("deliverables/observations"),
      api("events"),
    ]);
    state.catalog = catalog.deliverables;
    state.combinations = catalog.combinations;
    $("event").replaceChildren(new Option("本周期相关事件", ""));
    $("run").replaceChildren(new Option("按周期汇总", ""));
    $("cluster").replaceChildren(new Option("选择集群", ""));
    state.assets = assets.sources.map((source) => ({
      id: source.id,
      payload: source,
    }));
    state.observations = observations.observations;
    state.eventFields = workflows.fields;
    state.eventLabels = workflows.labels;
    (assets.clusters || []).forEach((cluster) => {
      const option = node("option", cluster.name);
      option.value = JSON.stringify(cluster.source_ids);
      $("cluster").append(option);
    });
    $("cluster").hidden = !(assets.clusters || []).length;
    $("expand-cluster").hidden = !(assets.clusters || []).length;
    workflows.records.forEach((record) => {
      const option = node("option", record.payload.title + " / " + record.id);
      option.value = record.id;
      $("event").append(option);
    });
    checks(
      $("assets"),
      state.assets.map((a) => ({
        id: a.id,
        title: [
          a.payload.asset_name,
          a.payload.system,
          a.payload.project_id,
          a.payload.db_type,
          a.payload.database,
          a.payload.environment,
          ...(a.payload.tags || []),
        ]
          .filter(Boolean)
          .join(" · "),
      })),
      [],
    );
    checks(
      $("deliverables"),
      state.catalog.map((d) => ({ id: d.id, title: d.title })),
      [],
    );
    combination();
    observations.observations.forEach((r) => {
      const o = node("option", r.observed_at + " / " + r.id);
      o.value = r.id;
      $("run").append(o);
    });
    const query = new URLSearchParams(location.search);
    if (query.get("run_id")) {
      const run = observations.observations.find(
        (r) => r.id === query.get("run_id"),
      );
      if (run) {
        $("run").value = run.id;
        $("date").value = run.observed_at.slice(0, 10);
        $("assets")
          .querySelectorAll("input")
          .forEach((i) => (i.checked = i.value === run.asset_id));
      }
      $("combination").value = "custom";
      $("deliverables")
        .querySelectorAll("input")
        .forEach((i) => (i.checked = i.value === "RUN"));
      sheets();
    }
    if (!state.assets.length) message("请在资产台账中添加数据源");
    updateSelection();
    await refresh();
  }
  window.addEventListener("pagehide", () => clearTimeout(state.timer));

  function updateSelection() {
    $("selected-count").textContent = "已选 " + checked($("assets")).length;
  }
  $("assets").addEventListener("change", updateSelection);
  $("select-visible").onclick = () => {
    $("assets")
      .querySelectorAll("label:not([hidden]) input")
      .forEach((input) => (input.checked = true));
    updateSelection();
  };
  $("clear").onclick = () => {
    $("assets")
      .querySelectorAll("input")
      .forEach((input) => (input.checked = false));
    updateSelection();
  };
  $("expand-cluster").onclick = () => {
    const ids = JSON.parse($("cluster").value || "[]");
    $("assets")
      .querySelectorAll("input")
      .forEach((input) => {
        if (ids.includes(input.value)) input.checked = true;
      });
    updateSelection();
  };
  $("refresh").onclick = () => {
    const source_ids = checked($("assets"));
    initialization = null;
    window.initReportCenterPage({ source_ids });
  };
  function eventFields() {
    $("event-fields").replaceChildren();
    [
      ...new Set([
        ...(state.eventFields?.[$("event-key").value] || []),
        "verification_reference",
      ]),
    ].forEach((key) => {
      const label = node("label", state.eventLabels?.[key] || key);
      const isTime =
        key.endsWith("_at") ||
        ["restore_started", "restore_finished"].includes(key);
      const input = node(
        isTime || key.endsWith("_minutes") || key === "deadline"
          ? "input"
          : "textarea",
      );
      if (isTime) input.type = "datetime-local";
      else if (key.endsWith("_minutes")) {
        input.type = "number";
        input.min = "0";
      } else if (key === "deadline") input.type = "date";
      else input.rows = 2;
      input.dataset.field = key;
      label.append(input);
      $("event-fields").append(label);
    });
  }
  $("event-key").onchange = eventFields;
  $("event-cancel").onclick = () => $("event-dialog").close();
  $("add-event").onclick = () => {
    $("event-source").replaceChildren();
    state.assets
      .filter((asset) => checked($("assets")).includes(asset.id))
      .forEach((asset) => {
        const option = node("option", asset.payload.asset_name);
        option.value = asset.id;
        $("event-source").append(option);
      });
    eventFields();
    $("event-dialog").showModal();
  };
  $("event-form").onsubmit = async (event) => {
    event.preventDefault();
    try {
      const details = Object.fromEntries(
        [...$("event-fields").querySelectorAll("[data-field]")]
          .filter((input) => input.value.trim())
          .map((input) => [
            input.dataset.field,
            input.type === "datetime-local"
              ? new Date(input.value).toISOString()
              : input.type === "number"
                ? Number(input.value)
                : input.value.trim(),
          ]),
      );
      details.major_incident = $("event-major").checked;
      const data = await api("events", {
        source_id: $("event-source").value,
        key: $("event-key").value,
        title: $("event-title").value,
        state: $("event-state").value,
        details,
      });
      const option = node("option", data.record.payload.title);
      option.value = data.record.id;
      $("event").append(option);
      $("event").value = option.value;
      $("event-dialog").close();
      message("事件资料已保存");
    } catch (error) {
      message(error.message, true);
    }
  };

  let initialization;
  function active() {
    return root.closest(".page")?.classList.contains("active") ?? true;
  }
  window.initReportCenterPage = async (context = {}) => {
    try {
      initialization ||= init();
      await initialization;
      if (context.source_ids || context.run_id) tab("generate");
      if (context.source_ids || context.run_id) {
        $("asset-filter").value = "";
        $("asset-filter").oninput();
      }
      if (context.source_ids) {
        $("assets")
          .querySelectorAll("input")
          .forEach(
            (input) =>
              (input.checked = context.source_ids.includes(input.value)),
          );
        updateSelection();
      }
      if (context.run_id) {
        const selected = state.observations.find(
          (observation) => observation.id === context.run_id,
        );
        if (selected && !context.source_ids) {
          $("assets")
            .querySelectorAll("input")
            .forEach(
              (input) => (input.checked = input.value === selected.asset_id),
            );
          updateSelection();
        }
        let option = [...$("run").options].find(
          (o) => o.value === context.run_id,
        );
        if (!option) {
          option = node("option", context.run_id);
          option.value = context.run_id;
          $("run").append(option);
        }
        $("run").value = context.run_id;
        $("combination").value = "run";
        combination();
      }
      await refresh();
    } catch (error) {
      initialization = null;
      message(error.message, true);
    }
  };
  window.addEventListener("workspace-page-change", (event) => {
    if (event.detail.page !== "report-center") clearTimeout(state.timer);
  });
  window.addEventListener("workspace-datasources-updated", () => {
    initialization = null;
  });
})();
