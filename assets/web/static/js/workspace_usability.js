// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
// Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck
// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck
"use strict";
(() => {
  const byId = (id) => document.getElementById(id);
  const nav = document.querySelector(".sidebar-nav");
  const search = byId("navigation-search");
  const items = [...nav.querySelectorAll(".nav-item")];
  function filterNavigation() {
    const query = search.value.trim().toLocaleLowerCase();
    let found = 0;
    for (const item of items) {
      const matches = item.textContent.toLocaleLowerCase().includes(query);
      item.classList.toggle("navigation-filtered", !matches);
      if (matches && item.style.display !== "none") found++;
    }
    for (const section of nav.querySelectorAll(".nav-section")) {
      let sibling = section.nextElementSibling;
      let visible = false;
      while (sibling && !sibling.classList.contains("nav-section")) {
        if (
          sibling.classList.contains("nav-item") &&
          !sibling.classList.contains("navigation-filtered") &&
          sibling.style.display !== "none"
        )
          visible = true;
        sibling = sibling.nextElementSibling;
      }
      section.classList.toggle("navigation-filtered", !visible);
    }
    byId("navigation-empty").hidden = found > 0;
    byId("navigation-clear").hidden = !query;
  }
  search.oninput = filterNavigation;
  byId("navigation-clear").onclick = () => {
    search.value = "";
    filterNavigation();
    search.focus();
  };
  function available() {
    return items.filter((item) => item.getClientRects().length);
  }
  search.onkeydown = (event) => {
    if (event.key === "Escape") {
      search.value = "";
      filterNavigation();
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      available()[0]?.focus();
    }
    if (event.key === "Enter") {
      event.preventDefault();
      available()[0]?.click();
    }
  };
  items.forEach((item) => {
    item.tabIndex = 0;
    item.setAttribute("role", "button");
    item.addEventListener("click", () => {
      if (search.value) {
        search.value = "";
        filterNavigation();
      }
    });
    item.onkeydown = (event) => {
      const list = available();
      const index = list.indexOf(item);
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        item.click();
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        list[(index + 1) % list.length]?.focus();
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        list[(index + list.length - 1) % list.length]?.focus();
      }
    };
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      document.querySelector(".sidebar")?.classList.remove("collapsed");
      search.focus();
      search.select();
    }
  });
  document.addEventListener("workspace-menus-ready", filterNavigation);
  new MutationObserver(filterNavigation).observe(nav, {
    subtree: true,
    attributes: true,
    attributeFilter: ["style"],
  });

  // Keep browser back/forward and refresh aligned with the selected workspace.
  let restoring = false;
  function restorePage() {
    const name = location.hash.slice(1);
    const target = items.find((item) => item.id === "nav-" + name);
    if (target && target.style.display !== "none" && byId("page-" + name)) {
      restoring = true;
      target.click();
      restoring = false;
    }
  }
  const originalShowPage = window.showPage;
  window.showPage = function (name) {
    originalShowPage(name);
    if (
      window.__workspaceAppReady &&
      !restoring &&
      location.hash !== "#" + name
    )
      history.pushState({ page: name }, "", "#" + name);
  };
  window.addEventListener("popstate", restorePage);
  document.addEventListener(
    "workspace-menus-ready",
    () => {
      if (window.__workspaceAppReady) restorePage();
    },
    { once: true },
  );
  document.addEventListener(
    "workspace-app-ready",
    () => {
      if (window.__workspaceMenusReady) restorePage();
    },
    { once: true },
  );
  if (window.__workspaceMenusReady && window.__workspaceAppReady) restorePage();

  async function quickSources() {
    const select = byId("quick-inspection-source");
    try {
      const response = await fetch("/api/pro/datasources");
      const data = await response.json();
      if (!response.ok || !data.ok)
        throw new Error(data.error || "加载数据源失败");
      const selected = select.value;
      select.replaceChildren(new Option("选择已有数据源", ""));
      for (const source of data.datasources || [])
        select.add(new Option(source.name || source.host, source.id));
      select.value = selected;
      byId("quick-inspection-hint").textContent =
        select.options.length > 1
          ? "使用已保存的连接，确认巡检设置后执行。"
          : "先添加数据源，或在下方手动配置连接。";
    } catch (error) {
      byId("quick-inspection-hint").textContent = error.message;
    }
  }
  byId("quick-inspection-open").onclick = async (event) => {
    const id = byId("quick-inspection-source").value;
    if (!id) {
      byId("quick-inspection-source").focus();
      return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const select = byId("f-datasource");
      if (![...select.options].some((option) => option.value === id))
        select.add(new Option(id, id));
      select.value = id;
      const loaded = await onDatasourceChange();
      if (loaded) gotoStep(4);
    } finally {
      button.disabled = false;
    }
  };
  byId("quick-inspection-test").onclick = async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      if (
        byId("quick-inspection-source").value !== byId("f-datasource").value ||
        !byId("f-host").value
      ) {
        await byId("quick-inspection-open").onclick({
          currentTarget: byId("quick-inspection-open"),
        });
      }
      if (!byId("f-host").value) return;
      await testDb();
      byId("quick-inspection-hint").textContent =
        byId("db-test-status").textContent;
    } finally {
      button.disabled = false;
    }
  };
  byId("quick-inspection-refresh").onclick = quickSources;
  quickSources();

  function updateSchedule() {
    const mode = byId("wf-schedule-mode").value;
    const time = byId("wf-schedule-time").value || "02:00";
    const [hour, minute] = time.split(":").map(Number);
    byId("wf-task-schedule-enabled").checked = mode !== "manual";
    if (mode === "daily")
      byId("wf-task-cron").value = `${minute} ${hour} * * *`;
    if (mode === "weekly")
      byId("wf-task-cron").value =
        `${minute} ${hour} * * ${byId("wf-schedule-weekday").value}`;
    if (mode === "monthly")
      byId("wf-task-cron").value =
        `${minute} ${hour} ${byId("wf-schedule-day").value} * *`;
    if (mode === "hourly") byId("wf-task-cron").value = `${minute} * * * *`;
    showScheduleFields();
  }
  function showScheduleFields() {
    const mode = byId("wf-schedule-mode").value;
    byId("wf-schedule-time-label").hidden = ["manual", "custom"].includes(mode);
    byId("wf-schedule-weekday-label").hidden = mode !== "weekly";
    byId("wf-schedule-day-label").hidden = mode !== "monthly";
    byId("wf-schedule-advanced").open = mode === "custom";
  }
  [
    "wf-schedule-mode",
    "wf-schedule-time",
    "wf-schedule-weekday",
    "wf-schedule-day",
  ].forEach((id) => byId(id).addEventListener("change", updateSchedule));
  function prepareTaskForm() {
    const cron = byId("wf-task-cron").value.trim();
    let mode = byId("wf-task-schedule-enabled").checked ? "custom" : "manual";
    const fields = cron.split(/\s+/);
    const [minute, hour, day, month, weekday] = fields;
    const fixedMinute = /^\d+$/.test(minute) && Number(minute) < 60;
    if (
      fields.length === 5 &&
      fixedMinute &&
      month === "*" &&
      /^\d+$/.test(hour) &&
      Number(hour) < 24
    ) {
      if (day === "*" && weekday === "*") mode = "daily";
      else if (day === "*" && /^[0-6]$/.test(weekday)) {
        mode = "weekly";
        byId("wf-schedule-weekday").value = weekday;
      } else if (/^([1-9]|[12]\d|3[01])$/.test(day) && weekday === "*") {
        mode = "monthly";
        byId("wf-schedule-day").value = day;
      }
      byId("wf-schedule-time").value =
        hour.padStart(2, "0") + ":" + minute.padStart(2, "0");
    } else if (
      fields.length === 5 &&
      fixedMinute &&
      hour === "*" &&
      day === "*" &&
      month === "*" &&
      weekday === "*"
    ) {
      mode = "hourly";
      byId("wf-schedule-time").value = "00:" + minute.padStart(2, "0");
    }
    byId("wf-schedule-mode").value = byId("wf-task-schedule-enabled").checked
      ? mode
      : "manual";
    showScheduleFields();
    byId("wf-task-source-search").value = "";
    filterTaskSources();
  }
  function filterTaskSources() {
    const query = byId("wf-task-source-search").value.toLowerCase().trim();
    for (const label of document.querySelectorAll(
      "#wf-task-batch-sources label",
    ))
      label.hidden = !label.textContent.toLowerCase().includes(query);
    byId("wf-task-selected-count").textContent =
      `已选 ${document.querySelectorAll("#wf-task-batch-sources input:checked").length} 个数据源`;
  }
  byId("wf-task-source-search").oninput = filterTaskSources;
  byId("wf-task-batch-sources").addEventListener("change", filterTaskSources);
  byId("wf-task-select-visible").onclick = () => {
    document
      .querySelectorAll("#wf-task-batch-sources label:not([hidden]) input")
      .forEach((input) => {
        input.checked = true;
      });
    filterTaskSources();
  };
  byId("wf-task-clear-sources").onclick = () => {
    document
      .querySelectorAll("#wf-task-batch-sources input")
      .forEach((input) => {
        input.checked = false;
      });
    filterTaskSources();
  };
  window.DBPulseUX = { prepareTaskForm, quickSources };
  window.addEventListener("message", async (event) => {
    const frame = byId("batch-operations-frame");
    if (
      event.origin !== location.origin ||
      event.source !== frame?.contentWindow ||
      event.data?.type !== "operations-schedule"
    )
      return;
    const task = event.data.task;
    if (!task || typeof task !== "object") return;
    showPage("workflow-task");
    await loadWorkflowTaskPage();
    wfOpenTaskModal({
      name: task.name || "数据库整体巡检",
      task_type: "batch_inspection",
      batch_spec: task.batch_spec,
      cron: "0 2 * * *",
      schedule_enabled: true,
    });
  });
  document.addEventListener("workspace-datasources-updated", quickSources);
})();
