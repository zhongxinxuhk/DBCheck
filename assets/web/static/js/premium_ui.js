// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

/* ══════════════════════════════════════════════════════════════
   Premium UI Layer — 命令面板 / Toast 2.0 / 密度 / 快捷交互
   纯原生实现，无外部依赖；所有增强均可降级。
   ══════════════════════════════════════════════════════════════ */
"use strict";

(() => {
  const doc = document;
  const $ = (sel, root = doc) => root.querySelector(sel);
  const $$ = (sel, root = doc) => Array.from(root.querySelectorAll(sel));

  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem("px." + key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (err) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem("px." + key, JSON.stringify(value));
      } catch (err) {
        /* 隐私模式下静默降级 */
      }
    },
  };

  const isEn = () =>
    (doc.documentElement.lang || "zh").toLowerCase().startsWith("en");

  /* ── 1. 紧凑密度 ─────────────────────────────────────────── */
  const density = {
    apply(compact) {
      doc.documentElement.classList.toggle("px-compact", !!compact);
    },
    toggle() {
      const next = !doc.documentElement.classList.contains("px-compact");
      store.set("density", next);
      density.apply(next);
      return next;
    },
    isCompact() {
      return doc.documentElement.classList.contains("px-compact");
    },
  };
  density.apply(store.get("density", false));

  /* ── 2. Toast 2.0：进度条 + 关闭按钮 + 堆叠上限 ──────────── */
  const nativeToast = window.toast;
  if (typeof nativeToast === "function") {
    window.toast = function premiumToast(msg, type = "info", duration = 4000) {
      const el = nativeToast(msg, type, duration);
      try {
        decorateToast(el, duration);
      } catch (err) {
        /* 装饰失败不影响通知本身 */
      }
      return el;
    };
  }

  function decorateToast(el, duration) {
    if (!el || el.dataset.pxToast === "1") return;
    el.dataset.pxToast = "1";
    el.classList.add("px-toast");

    const life = Number(duration) > 0 ? Number(duration) : 4000;

    const bar = doc.createElement("span");
    bar.className = "px-toast__bar";
    bar.style.animationDuration = life + "ms";
    el.appendChild(bar);

    const close = doc.createElement("button");
    close.type = "button";
    close.className = "px-toast__close";
    close.setAttribute("aria-label", isEn() ? "Dismiss" : "关闭通知");
    close.textContent = "\u00d7";
    close.addEventListener("click", (event) => {
      event.stopPropagation();
      if (typeof window.dismiss === "function") window.dismiss(el);
      else el.remove();
    });
    el.appendChild(close);

    const box = doc.getElementById("toast-container");
    if (box && box.children.length > 4) {
      const oldest = box.firstElementChild;
      if (oldest) oldest.remove();
    }
  }

  /* ── 3. 命令面板 ─────────────────────────────────────────── */
  const palette = {
    el: null,
    input: null,
    list: null,
    trigger: null,
    results: [],
    active: -1,
    returnFocus: null,
    composing: false,
  };

  function navPages() {
    return $$(".nav-item")
      .filter((item) => {
        if (!item.getClientRects().length) return false;
        if (getComputedStyle(item).display === "none") return false;
        return !!item.getAttribute("onclick");
      })
      .map((item) => {
        const title = (item.textContent || "").replace(/\s+/g, " ").trim();
        return {
          title,
          hint: sectionOf(item),
          group: isEn() ? "Pages" : "页面",
          kind: "page",
          icon: "\u25a7",
          run: () => item.click(),
        };
      })
      .filter((entry) => entry.title);
  }

  function sectionOf(item) {
    let node = item.previousElementSibling;
    while (node) {
      if (node.classList && node.classList.contains("nav-section")) {
        return (node.textContent || "").replace(/\s+/g, " ").trim();
      }
      node = node.previousElementSibling;
    }
    return isEn() ? "Navigate" : "功能导航";
  }

  function quickActions() {
    const actions = [
      {
        title: isEn() ? "Toggle theme" : "切换深浅主题",
        hint:
          (getTheme() === "light"
            ? isEn()
              ? "Currently light"
              : "当前：浅色"
            : isEn()
              ? "Currently dark"
              : "当前：深色"),
        icon: "\u25d0",
        run: () => {
          if (typeof window.toggleTheme === "function") window.toggleTheme();
        },
      },
      {
        title: isEn() ? "Toggle compact density" : "切换紧凑密度",
        hint: density.isCompact()
          ? isEn()
            ? "Currently compact"
            : "当前：紧凑"
          : isEn()
            ? "Currently comfortable"
            : "当前：标准",
        icon: "\u2630",
        run: () => {
          const compact = density.toggle();
          if (typeof window.toast === "function") {
            window.toast(
              compact
                ? isEn()
                  ? "Compact density enabled"
                  : "已切换为紧凑密度"
                : isEn()
                  ? "Comfortable density restored"
                  : "已恢复标准密度",
              "success",
              2200
            );
          }
        },
      },
      {
        title: isEn() ? "Reload current page" : "重新加载当前页",
        hint: isEn() ? "Refresh data" : "刷新最新数据",
        icon: "\u21bb",
        run: () => location.reload(),
      },
      {
        title: isEn() ? "Back to top" : "回到页面顶部",
        hint: isEn() ? "Scroll to top" : "快速定位",
        icon: "\u2191",
        run: () => window.scrollTo({ top: 0, behavior: "smooth" }),
      },
    ];
    return actions.map((action) => ({
      ...action,
      kind: "action",
      group: isEn() ? "Actions" : "快捷操作",
    }));
  }

  function getTheme() {
    return doc.documentElement.getAttribute("data-theme") || "dark";
  }

  function fuzzyScore(text, query) {
    const haystack = String(text).toLocaleLowerCase();
    const needle = String(query).toLocaleLowerCase();
    if (!needle) return 1;
    const direct = haystack.indexOf(needle);
    if (direct >= 0) return 1000 - direct;

    let cursor = 0;
    let score = 0;
    let previous = -2;
    for (const ch of needle) {
      const idx = haystack.indexOf(ch, cursor);
      if (idx < 0) return -1;
      score += idx === previous + 1 ? 6 : 1;
      previous = idx;
      cursor = idx + 1;
    }
    return score;
  }

  function recentTitles() {
    const list = store.get("recent", []);
    return Array.isArray(list) ? list.filter((item) => typeof item === "string") : [];
  }

  function remember(title) {
    const list = recentTitles().filter((item) => item !== title);
    list.unshift(title);
    store.set("recent", list.slice(0, 6));
  }

  function buildResults(query) {
    const pages = navPages();
    const actions = quickActions();
    const recent = recentTitles();
    const byTitle = new Map(pages.map((page) => [page.title, page]));

    if (!query) {
      const recentPages = recent
        .map((title) => byTitle.get(title))
        .filter(Boolean)
        .map((page) => ({ ...page, group: isEn() ? "Recent" : "最近访问" }));
      const recentTitlesSet = new Set(recentPages.map((page) => page.title));
      const otherPages = pages.filter((page) => !recentTitlesSet.has(page.title));
      return [...recentPages, ...otherPages, ...actions];
    }

    const scored = [];
    const push = (entry) => {
      const score = fuzzyScore(entry.title + " " + (entry.hint || ""), query);
      if (score >= 0) scored.push({ entry, score });
    };
    pages.forEach(push);
    actions.forEach(push);
    scored.sort((a, b) => b.score - a.score);
    return scored.slice(0, 40).map((row) => row.entry);
  }

  function buildPalette() {
    const wrap = doc.createElement("div");
    wrap.className = "px-cmdk";
    wrap.id = "px-cmdk";
    wrap.hidden = true;
    wrap.innerHTML = `
      <div class="px-cmdk__backdrop" data-px-close="1"></div>
      <div class="px-cmdk__panel" role="dialog" aria-modal="true"
           aria-label="${isEn() ? "Command palette" : "命令面板"}">
        <div class="px-cmdk__inputwrap">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="11" cy="11" r="7"></circle>
            <path d="m20 20-3.2-3.2"></path>
          </svg>
          <input id="px-cmdk-input" class="px-cmdk__input" type="text"
                 autocomplete="off" spellcheck="false"
                 placeholder="${isEn() ? "Search pages or actions…" : "搜索页面或操作…"}"
                 aria-controls="px-cmdk-list" aria-autocomplete="list" />
          <span class="px-cmdk__esc">ESC</span>
        </div>
        <div class="px-cmdk__list" id="px-cmdk-list" role="listbox"></div>
        <div class="px-cmdk__foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> ${isEn() ? "select" : "选择"}</span>
          <span><kbd>↵</kbd> ${isEn() ? "open" : "打开"}</span>
          <span><kbd>esc</kbd> ${isEn() ? "close" : "关闭"}</span>
          <span class="px-cmdk__brand">DBPulse</span>
        </div>
      </div>`;
    doc.body.appendChild(wrap);

    palette.el = wrap;
    palette.input = $("#px-cmdk-input", wrap);
    palette.list = $("#px-cmdk-list", wrap);

    wrap.addEventListener("click", (event) => {
      if (event.target.dataset && event.target.dataset.pxClose) closePalette();
    });
    palette.input.addEventListener("input", () => {
      if (!palette.composing) renderPalette(palette.input.value);
    });
    palette.input.addEventListener("compositionstart", () => {
      palette.composing = true;
    });
    palette.input.addEventListener("compositionend", () => {
      palette.composing = false;
      renderPalette(palette.input.value);
    });
    palette.input.addEventListener("keydown", onPaletteKeydown);
    palette.list.addEventListener("mousemove", (event) => {
      const item = event.target.closest(".px-cmdk__item");
      if (!item) return;
      const idx = Number(item.dataset.idx);
      if (Number.isInteger(idx) && idx !== palette.active) setActive(idx);
    });
    palette.list.addEventListener("click", (event) => {
      const item = event.target.closest(".px-cmdk__item");
      if (!item) return;
      const idx = Number(item.dataset.idx);
      if (Number.isInteger(idx)) runResult(idx);
    });
  }

  function injectTrigger() {
    const topbar = $(".topbar");
    if (!topbar || $("#px-cmdk-trigger")) return;
    const btn = doc.createElement("button");
    btn.type = "button";
    btn.id = "px-cmdk-trigger";
    btn.className = "px-cmdk-trigger";
    btn.setAttribute("aria-label", isEn() ? "Open command palette" : "打开命令面板");
    btn.setAttribute("aria-haspopup", "dialog");
    btn.title = isEn() ? "Open command palette (⌘/Ctrl K)" : "打开命令面板（⌘/Ctrl K）";
    btn.innerHTML = `
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
        <circle cx="11" cy="11" r="7"></circle>
        <path d="m20 20-3.2-3.2"></path>
      </svg>
      <span class="px-cmdk-trigger__label">${isEn() ? "Search" : "搜索"}</span>
      <kbd>${/Mac|iPhone|iPad/.test(navigator.platform) ? "⌘K" : "Ctrl K"}</kbd>`;
    btn.addEventListener("click", openPalette);
    const anchor = $("#theme-toggle", topbar) || topbar.lastElementChild;
    topbar.insertBefore(btn, anchor);
    palette.trigger = btn;
  }

  function openPalette() {
    if (!palette.el) buildPalette();
    palette.returnFocus = doc.activeElement;
    palette.el.hidden = false;
    doc.body.classList.add("px-cmdk-open");
    if (palette.trigger) palette.trigger.setAttribute("aria-expanded", "true");
    palette.input.value = "";
    renderPalette("");
    requestAnimationFrame(() => palette.input.focus());
  }

  function closePalette() {
    if (!palette.el || palette.el.hidden) return;
    palette.el.hidden = true;
    doc.body.classList.remove("px-cmdk-open");
    if (palette.trigger) palette.trigger.setAttribute("aria-expanded", "false");
    if (palette.returnFocus && typeof palette.returnFocus.focus === "function") {
      palette.returnFocus.focus({ preventScroll: true });
    }
  }

  function renderPalette(query) {
    const results = buildResults(query.trim());
    palette.results = results;
    palette.active = results.length ? 0 : -1;
    palette.list.innerHTML = "";

    if (!results.length) {
      palette.input.removeAttribute("aria-activedescendant");
      const empty = doc.createElement("div");
      empty.className = "px-cmdk__empty";
      empty.textContent = isEn()
        ? `No match for “${query}”`
        : `未找到与“${query}”匹配的页面或操作`;
      palette.list.appendChild(empty);
      return;
    }

    const fragment = doc.createDocumentFragment();
    let currentGroup = "";
    results.forEach((entry, idx) => {
      if (entry.group !== currentGroup) {
        currentGroup = entry.group;
        const group = doc.createElement("div");
        group.className = "px-cmdk__group";
        group.textContent = currentGroup;
        fragment.appendChild(group);
      }

      const item = doc.createElement("div");
      item.className = "px-cmdk__item" + (idx === 0 ? " is-active" : "");
      item.dataset.idx = String(idx);
      item.id = "px-cmdk-opt-" + idx;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", idx === 0 ? "true" : "false");

      const icon = doc.createElement("span");
      icon.className = "px-cmdk__icon";
      icon.textContent = entry.icon || "\u203a";

      const text = doc.createElement("span");
      text.className = "px-cmdk__text";
      const title = doc.createElement("span");
      title.className = "px-cmdk__title";
      title.textContent = entry.title;
      text.appendChild(title);
      if (entry.hint) {
        const hint = doc.createElement("span");
        hint.className = "px-cmdk__hint";
        hint.textContent = entry.hint;
        text.appendChild(hint);
      }

      item.appendChild(icon);
      item.appendChild(text);
      fragment.appendChild(item);
    });
    palette.list.appendChild(fragment);
    palette.input.setAttribute("aria-activedescendant", "px-cmdk-opt-0");
  }

  function setActive(idx) {
    if (idx < 0 || idx >= palette.results.length) return;
    palette.active = idx;
    $$(".px-cmdk__item", palette.list).forEach((item) => {
      const active = Number(item.dataset.idx) === idx;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-selected", active ? "true" : "false");
    });
    const active = $(`.px-cmdk__item[data-idx="${idx}"]`, palette.list);
    if (active) {
      active.scrollIntoView({ block: "nearest" });
      palette.input.setAttribute("aria-activedescendant", active.id);
    }
  }

  function runResult(idx) {
    const entry = palette.results[idx];
    if (!entry) return;
    if (entry.kind === "page") remember(entry.title);
    closePalette();
    try {
      entry.run();
    } catch (err) {
      console.error("[premium-ui] action failed:", err);
    }
  }

  function onPaletteKeydown(event) {
    if (event.isComposing || palette.composing) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!palette.results.length) return;
      setActive((palette.active + 1) % palette.results.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!palette.results.length) return;
      setActive((palette.active - 1 + palette.results.length) % palette.results.length);
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (palette.active >= 0) runResult(palette.active);
    } else if (event.key === "Tab") {
      event.preventDefault();
      palette.input.focus();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closePalette();
    }
  }

  function onGlobalKeydown(event) {
    if (event.isComposing) return;
    const key = (event.key || "").toLowerCase();

    if ((event.metaKey || event.ctrlKey) && key === "k") {
      event.preventDefault();
      if (palette.el && !palette.el.hidden) closePalette();
      else openPalette();
      return;
    }

    if (key === "escape") {
      if (palette.el && !palette.el.hidden) {
        event.preventDefault();
        closePalette();
        return;
      }
      closeTopModal();
      return;
    }

    if (key === "/" && !event.metaKey && !event.ctrlKey && !event.altKey) {
      const target = event.target;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable);
      if (!typing) {
        event.preventDefault();
        openPalette();
      }
    }
  }

  /* 关闭最上层弹窗：优先复用其背景点击逻辑，其次直接隐藏 */
  function closeTopModal() {
    const overlays = $$(".modal-overlay, .modal-backdrop-fix").filter((el) => {
      const style = getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" &&
        el.getClientRects().length > 0;
    });
    const top = overlays[overlays.length - 1];
    if (!top) return;
    top.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    if (getComputedStyle(top).display !== "none") {
      top.style.display = "none";
    }
  }

  /* ── 4. 加载态骨架屏（仅替换纯“加载中…”占位） ──────────── */
  const LOADING_RE = /^(加载中[.…]*|loading[.…]*)$/i;

  function polishLoadingStates(root = doc) {
    $$('tbody td[colspan]', root).forEach((td) => {
      if (td.dataset.pxSkeleton === "1") return;
      if (!LOADING_RE.test((td.textContent || "").trim())) return;
      td.dataset.pxSkeleton = "1";
      td.textContent = "";
      const box = doc.createElement("span");
      box.className = "px-skeleton-box";
      for (let i = 0; i < 3; i += 1) {
        const bar = doc.createElement("span");
        bar.className = "px-skeleton";
        bar.style.width = 72 - i * 14 + "%";
        bar.style.margin = "6px auto";
        box.appendChild(bar);
      }
      td.appendChild(box);
    });
  }

  function watchLoadingStates() {
    polishLoadingStates();
    const pages = $$(".page");
    if (!pages.length) return;
    const observer = new MutationObserver((mutations) => {
      if (mutations.some((m) => m.attributeName === "class")) {
        window.setTimeout(() => polishLoadingStates(), 60);
      }
    });
    pages.forEach((page) => observer.observe(page, { attributes: true, attributeFilter: ["class"] }));
  }

  /* ── 5. 回到顶部 ─────────────────────────────────────────── */
  function initScrollTop() {
    const btn = doc.createElement("button");
    btn.type = "button";
    btn.className = "px-scrolltop";
    btn.setAttribute("aria-label", isEn() ? "Back to top" : "回到顶部");
    btn.title = isEn() ? "Back to top" : "回到顶部";
    btn.textContent = "\u2191";
    btn.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
    doc.body.appendChild(btn);

    let ticking = false;
    const sync = () => {
      btn.classList.toggle("is-visible", window.scrollY > 680);
      ticking = false;
    };
    window.addEventListener(
      "scroll",
      () => {
        if (ticking) return;
        ticking = true;
        requestAnimationFrame(sync);
      },
      { passive: true }
    );
    sync();
  }

  /* ── 6. 初始化 ───────────────────────────────────────────── */
  function init() {
    injectTrigger();
    initScrollTop();
    watchLoadingStates();
    doc.addEventListener("keydown", onGlobalKeydown, true);

    // 语言切换后重建面板文案
    const langSelect = doc.getElementById("lang-select");
    if (langSelect) {
      langSelect.addEventListener("change", () => {
        if (palette.el) {
          palette.el.remove();
          palette.el = null;
        }
        if (palette.trigger) {
          palette.trigger.remove();
          palette.trigger = null;
        }
        injectTrigger();
      });
    }
  }

  if (doc.readyState === "loading") {
    doc.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }

  // 暴露少量能力，便于页面按需复用
  window.pxUI = {
    openPalette,
    closePalette,
    toggleDensity: () => density.toggle(),
    polishLoadingStates,
    skeleton: (rows = 3) =>
      Array.from({ length: rows }, () => '<span class="px-skeleton"></span>').join(""),
  };
})();
