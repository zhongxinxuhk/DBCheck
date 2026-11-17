// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
"use strict";
(() => {
  const $ = (id) => document.getElementById("rc-" + id);
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
    published: "已发布",
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
    const response = await fetch("/api/services/" + path, options);
    if (response.status === 401) {
      location.href = "/um/login";
      throw new Error("请先登录");
    }
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "请求失败");
    return result;
  }
  function tab(name) {
    document
      .querySelectorAll("[data-tab]")
      .forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document
      .querySelectorAll(".rc-pane")
      .forEach((p) => (p.hidden = p.id !== "rc-" + name));
  }
  document
    .querySelectorAll("[data-tab]")
    .forEach((b) => (b.onclick = () => tab(b.dataset.tab)));
  function checks(container, values, selected) {
    container.replaceChildren();
    values.forEach((value) => {
      const label = node("label");
      const input = node("input");
      input.type = "checkbox";
      input.value = value.id;
      input.checked = selected.includes(value.id);
      label.append(input, node("span", value.title));
      container.append(label);
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
    if (choice === "custom") return;
    const selected = state.combinations[choice] || [];
    $("deliverables")
      .querySelectorAll("input")
      .forEach((i) => (i.checked = selected.includes(i.value)));
    [...$("deliverables").children]
      .sort(
        (a, b) =>
          Number(b.querySelector("input").checked) -
          Number(a.querySelector("input").checked),
      )
      .forEach((item) => $("deliverables").append(item));
    if (choice !== "register") $("period").value = choice;
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
      asset_ids: checked($("assets")),
      deliverables: checked($("deliverables")),
      period: $("period").value,
      date: $("date").value,
      cutoff: state.cutoffEdited
        ? new Date($("cutoff").value).toISOString()
        : undefined,
      formats: [...document.querySelectorAll("input[name=format]:checked")].map(
        (i) => i.value,
      ),
      run_id: $("run").value,
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
      "/api/services/deliverables/" +
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
    $("revision-label").textContent = "修订报告 " + job.report_id;
    $("period").value = job.request.period;
    $("date").value = job.request.date;
    $("combination").value = "custom";
    $("layout").value = job.request.layout || "separate";
    $("assets")
      .querySelectorAll("input")
      .forEach((i) => (i.checked = job.request.asset_ids.includes(i.value)));
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
      $("task-list"),
      ["报告 / 周期", "交付物", "状态", "操作"],
      state.jobs.map((job) => {
        const actions = node("div");
        if (
          job.state === "failed" ||
          ["queued", "generating"].includes(job.state)
        )
          actions.append(
            button("重试中断任务", async () => {
              await api("deliverables/jobs/" + job.id + "/retry", {});
              await refresh();
            }),
          );
        if (job.state === "draft")
          actions.append(
            button("复核并发布", async () => {
              await api("deliverables/jobs/" + job.id + "/publish", {
                version: job.version,
              });
              await refresh();
              message("正式报告已签名归档");
            }),
          );
        if (["draft", "published"].includes(job.state))
          actions.append(downloadLink(job));
        return [
          job.report_id + " / " + job.request.date,
          job.request.deliverables.map(title).join("、"),
          (labels[job.state] || job.state) +
            (job.error ? " · " + job.error : ""),
          actions,
        ];
      }),
    );
    table(
      $("archive-list"),
      ["报告 / 版本", "统计周期", "文件与 SHA-256", "签名与操作"],
      jobs
        .filter((j) => ["draft", "published"].includes(j.state))
        .map((job) => {
          const files = node("div");
          job.artifacts.forEach((a) => {
            const block = node("div");
            block.append(downloadLink(job, a), node("code", a.sha256));
            files.append(block);
          });
          const actions = node("div");
          actions.append(
            node("div", "签名公钥指纹"),
            node("code", job.manifest.key_id),
            downloadLink(job),
            button("生成修订版", () => revision(job)),
          );
          actions.append(
            button("核验此报告", () => {
              $("verify-job").value = job.id;
              verifyArtifacts();
              tab("verify");
            }),
          );
          const superseded = state.jobs.some(
            (v) =>
              v.report_id === job.report_id &&
              v.revision > job.revision &&
              v.state === "published",
          );
          return [
            job.report_id +
              " / v" +
              job.revision +
              " · " +
              (superseded ? "历史发布版本" : labels[job.state]),
            job.request.start + " 至 " + job.request.end,
            files,
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
    table(
      $("plans"),
      ["服务计划", "周期", "状态", "资产"],
      (data.plans || []).map((p) => [p.title, p.period, p.state, p.asset_name]),
    );
    clearTimeout(state.timer);
    if (state.jobs.some((j) => ["queued", "generating"].includes(j.state)))
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
  $("refresh").onclick = () => refresh().catch((e) => message(e.message, true));
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
        open_issues: "未结问题",
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
  $("collect").onclick = async () => {
    const b = $("collect");
    b.disabled = true;
    try {
      const ids = checked($("assets"));
      if (!ids.length) throw new Error("请选择资产");
      const problems = [];
      for (const id of ids) {
        message("正在全量采集 " + id);
        const value = await api(
          "deliverables/assets/" + encodeURIComponent(id) + "/collect",
          {},
        );
        Object.entries(value.statuses)
          .filter(([, v]) => v.status !== "collected")
          .forEach(([key, v]) =>
            problems.push(key + " " + (v.reason || v.status)),
          );
      }
      $("cutoff").value = new Date(
        Date.now() - new Date().getTimezoneOffset() * 60000,
      )
        .toISOString()
        .slice(0, 19);
      message(
        problems.length
          ? "采集已归档，需核对 " + problems.join("、")
          : "全量采集已归档，可以预览并导出",
        problems.length > 0,
      );
    } catch (e) {
      message(e.message, true);
    } finally {
      b.disabled = false;
    }
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
      tab("tasks");
    } catch (e) {
      message(e.message, true);
    } finally {
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
    const [catalog, assets, observations] = await Promise.all([
      api("deliverables/catalog"),
      api("assets"),
      api("deliverables/observations"),
    ]);
    state.catalog = catalog.deliverables;
    state.combinations = catalog.combinations;
    state.assets = assets.assets;
    checks(
      $("assets"),
      state.assets.map((a) => ({
        id: a.id,
        title: [
          a.payload.asset_name,
          a.payload.system,
          a.payload.project_id,
          a.payload.db_type,
        ]
          .filter(Boolean)
          .join(" · "),
      })),
      state.assets.map((a) => a.id),
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
    if (!state.assets.length) message("请先从运维服务登记资产，再选择报告范围");
    await refresh();
  }
  window.addEventListener("pagehide", () => clearTimeout(state.timer));
  init().catch((e) => message(e.message, true));
})();
