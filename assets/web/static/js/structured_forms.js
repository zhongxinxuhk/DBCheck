// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
// Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck
// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck
"use strict";
window.DBPulseForms = {
  mount(textarea, options = {}) {
    const original = textarea.closest("label");
    const panel = document.createElement("div");
    panel.className = "structured-fields";
    const advanced = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "高级配置（完整 JSON）";
    original.before(panel, advanced);
    advanced.append(summary, original);
    let model;
    const labelFor = (key) => options.labels?.[key] || key;
    function save() {
      textarea.value = JSON.stringify(model, null, 2);
    }
    function renderObject(value, container, depth = 0) {
      for (const [key, current] of Object.entries(value)) {
        const choices = options.choices?.[key];
        const field = document.createElement("label");
        field.className = "structured-field";
        const title = document.createElement("span");
        title.textContent = labelFor(key);
        field.append(title);
        let input;
        if (choices) {
          input = document.createElement("select");
          for (const choice of choices)
            input.add(new Option(choice.label, choice.value));
          if (
            current !== null &&
            ![...input.options].some(
              (option) => option.value === String(current),
            )
          ) {
            input.add(new Option(String(current), String(current)));
          }
          input.value = current ?? "";
          input.onchange = () => {
            value[key] = input.value;
            save();
          };
        } else if (typeof current === "boolean") {
          input = document.createElement("input");
          input.type = "checkbox";
          input.checked = current;
          input.onchange = () => {
            value[key] = input.checked;
            save();
          };
        } else if (
          current &&
          typeof current === "object" &&
          !Array.isArray(current) &&
          depth < 3
        ) {
          const group = document.createElement("fieldset");
          const legend = document.createElement("legend");
          legend.textContent = labelFor(key);
          group.append(legend);
          renderObject(current, group, depth + 1);
          container.append(group);
          continue;
        } else if (
          Array.isArray(current) &&
          current.every(
            (item) => typeof item === "string" || typeof item === "number",
          )
        ) {
          input = document.createElement("textarea");
          input.rows = 3;
          input.value = current.join("\n");
          input.placeholder = "每行填写一项";
          input.oninput = () => {
            const lines = input.value
              .split("\n")
              .map((line) => line.trim())
              .filter(Boolean);
            const numeric =
              options.numberArrays?.includes(key) ||
              (current.length > 0 &&
                current.every((item) => typeof item === "number"));
            input.setCustomValidity(
              numeric && lines.some((line) => !Number.isFinite(Number(line)))
                ? "请每行填写一个数字"
                : "",
            );
            if (input.validity.valid) {
              value[key] = numeric ? lines.map(Number) : lines;
              save();
            }
          };
        } else if (current && typeof current === "object") {
          input = document.createElement("textarea");
          input.rows = 4;
          input.value = JSON.stringify(current, null, 2);
          input.oninput = () => {
            try {
              value[key] = JSON.parse(input.value);
              input.setCustomValidity("");
              save();
            } catch {
              input.setCustomValidity("请填写有效的 JSON，或使用下方完整配置");
            }
          };
        } else {
          input = document.createElement("input");
          const numeric =
            typeof current === "number" || options.numbers?.includes(key);
          input.type = numeric ? "number" : "text";
          if (numeric) input.step = "any";
          input.value = current ?? "";
          input.oninput = () => {
            value[key] = numeric
              ? input.value === ""
                ? null
                : Number(input.value)
              : input.value;
            save();
          };
        }
        input.dataset.field = key;
        input.setAttribute("aria-label", labelFor(key));
        input.required = options.required?.includes(key) || false;
        field.append(input);
        container.append(field);
      }
    }
    function refresh() {
      try {
        model = JSON.parse(textarea.value || "{}");
        if (!model || Array.isArray(model) || typeof model !== "object")
          throw new Error();
        panel.replaceChildren();
        if (options.primary) {
          const common = Object.fromEntries(
            Object.entries(model).filter(([key]) =>
              options.primary.includes(key),
            ),
          );
          // Use accessors so editing either view updates the original object.
          for (const key of Object.keys(common))
            Object.defineProperty(common, key, {
              enumerable: true,
              get: () => model[key],
              set: (value) => {
                model[key] = value;
              },
            });
          renderObject(common, panel);
          const more = document.createElement("details");
          const heading = document.createElement("summary");
          heading.textContent = "更多资产与采集配置";
          const other = {};
          for (const key of Object.keys(model).filter(
            (key) => !options.primary.includes(key),
          ))
            Object.defineProperty(other, key, {
              enumerable: true,
              get: () => model[key],
              set: (value) => {
                model[key] = value;
              },
            });
          more.append(heading);
          renderObject(other, more);
          panel.append(more);
        } else renderObject(model, panel);
        textarea.setCustomValidity("");
      } catch {
        textarea.setCustomValidity("完整配置须为有效的 JSON 对象");
        advanced.open = true;
      }
    }
    textarea.addEventListener("input", refresh);
    refresh();
    return { refresh };
  },
};
