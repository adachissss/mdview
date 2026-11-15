/* mdview 前端交互：目录折叠记忆、筛选、字号/主题、阅读进度、复制、灯箱 */
(function () {
  "use strict";

  var root = document.documentElement;
  var body = document.body;
  var path = body.getAttribute("data-path") || "";
  var lastUserScroll = 0;
  var realignTimers = [];
  var documentGeneration = 0;
  var placeDocHandle = function () {};
  var store = {
    get: function (k, d) {
      try {
        var v = localStorage.getItem(k);
        return v === null ? d : v;
      } catch (e) {
        return d;
      }
    },
    set: function (k, v) {
      try { localStorage.setItem(k, v); } catch (e) {}
    }
  };
  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  /* ---------------- 顶栏折叠 ---------------- */
  var collapseTop = $("#collapse-top");
  var topTab = $("#top-tab");
  function setTopCollapsed(flag) {
    root.classList.toggle("top-collapsed", flag);
    store.set("mdview:topCollapsed", flag ? "1" : "0");
    var button = flag ? topTab : collapseTop;
    if (button) button.focus({ preventScroll: true });
  }
  if (collapseTop) collapseTop.addEventListener("click", function () { setTopCollapsed(true); });
  if (topTab) topTab.addEventListener("click", function () { setTopCollapsed(false); });

  /* ---------------- 主题 ---------------- */
  var THEME_KEY = "mdview:theme";
  var themeBtn = $("#theme");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      store.set(THEME_KEY, next);
      if (window.mermaid) renderMermaid(document);
    });
  }
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  if (mq && mq.addEventListener) {
    mq.addEventListener("change", function (ev) {
      if (!store.get(THEME_KEY, "")) {
        root.setAttribute("data-theme", ev.matches ? "dark" : "light");
      }
    });
  }

  /* ---------------- 正文宽度（可手动拖动） ---------------- */
  var DOCW_KEY = "mdview:docWidth";
  var DOCW_DEFAULT = 736;
  var docWidth = parseInt(store.get(DOCW_KEY, "0"), 10) || 0;

  function docLimit() {
    var w = document.getElementById("content") ? document.getElementById("content").clientWidth : 1024;
    return Math.max(460, w - 24);
  }
  function applyDocMax(k) {
    var base = docWidth || DOCW_DEFAULT;
    base = Math.min(base, docLimit());
    root.style.setProperty("--doc-max", Math.round(base / k) + "px");
  }

  /* ---------------- 内容缩放（右侧正文，60%–200%） ---------------- */
  var ZOOM_MIN = 60, ZOOM_MAX = 200, ZOOM_STEP = 10;
  var zoom = parseInt(store.get("mdview:zoom", "100"), 10) || 100;
  var zoomSupported = !!(window.CSS && CSS.supports && CSS.supports("zoom", "0.5"));

  function clampZoom(v, step) {
    var st = step || ZOOM_STEP;
    v = Math.round(v / st) * st;
    return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, v));
  }
  function applyZoom(v, save, step) {
    zoom = clampZoom(v, step);
    var k = zoom / 100;
    if (zoomSupported) {
      root.style.setProperty("--zoom", String(k));
      // 正文栏视觉宽度由 docWidth 决定，缩小时换到更多字，放大时换到更少字
      applyDocMax(k);
    } else {
      // 老浏览器没有 CSS zoom，退回改字号
      root.style.setProperty("--read-fs", Math.round(18 * k) + "px");
    }
    $all(".zoom-val").forEach(function (el) { el.textContent = zoom + "%"; });
    if (save) store.set("mdview:zoom", String(zoom));
  }
  applyZoom(zoom, false);
  /* 双指捏合：手势期间只做 transform 预览（不触发重排），松手才真正改 zoom */
  (function pinchZoom() {
    var el = document.getElementById("content");
    if (!el || !("ontouchstart" in window)) return;
    var startDist = 0, startZoom = 100, active = false;
    var startScroll = 0, focalY = 0;
    var raf = 0, targetZoom = 100, docEl = null;

    function dist(touches) {
      var dx = touches[0].clientX - touches[1].clientX;
      var dy = touches[0].clientY - touches[1].clientY;
      return Math.sqrt(dx * dx + dy * dy) || 1;
    }

    function paint() {
      raf = 0;
      if (!active) return;
      var ratio = targetZoom / startZoom;
      if (docEl) docEl.style.transform = "scale(" + ratio + ")";
      $all(".zoom-val").forEach(function (n) { n.textContent = Math.round(targetZoom) + "%"; });
    }

    el.addEventListener("touchstart", function (ev) {
      if (ev.touches.length === 2) {
        active = true;
        startDist = dist(ev.touches);
        startZoom = zoom;
        targetZoom = zoom;
        startScroll = window.scrollY;
        focalY = (ev.touches[0].clientY + ev.touches[1].clientY) / 2;
        docEl = document.querySelector(".doc");
        if (docEl) {
          // 以双指中心为缩放锚点，预览时手指下的内容不会乱跑
          var midY = (ev.touches[0].clientY + ev.touches[1].clientY) / 2;
          var rect = docEl.getBoundingClientRect();
          var py = rect.height ? ((midY - rect.top) / rect.height) * 100 : 0;
          py = Math.max(0, Math.min(100, py));
          docEl.style.transformOrigin = "50% " + py.toFixed(1) + "%";
          docEl.style.willChange = "transform";
        }
        root.classList.add("pinching");
        ev.preventDefault();
      }
    }, { passive: false });

    el.addEventListener("touchmove", function (ev) {
      if (!active || ev.touches.length !== 2) return;
      ev.preventDefault();
      var nz = startZoom * (dist(ev.touches) / startDist);
      targetZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, nz));
      if (!raf) raf = requestAnimationFrame(paint);
    }, { passive: false });

    function end(ev) {
      if (!active) return;
      if (ev.touches && ev.touches.length >= 2) return;
      active = false;
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      root.classList.remove("pinching");
      if (docEl) {
        docEl.style.transform = "";
        docEl.style.willChange = "";
      }
      // 手势期间使用 transform 预览，松手时更新 zoom，减少重排
      var ratio = targetZoom / startZoom;
      applyZoom(targetZoom, true, 5);
      // 让手指按住的那一点在落档后仍停在原处
      if (ratio !== 1) {
        var keep = (startScroll + focalY) * ratio - focalY;
        window.scrollTo({ top: Math.max(0, keep), behavior: "instant" });
      }
    }
    el.addEventListener("touchend", end, { passive: true });
    el.addEventListener("touchcancel", end, { passive: true });

    // Safari 私有手势事件：不拦会连带整页缩放
    document.addEventListener("gesturestart", function (ev) { ev.preventDefault(); }, { passive: false });
    document.addEventListener("gesturechange", function (ev) { ev.preventDefault(); }, { passive: false });
  })();


  function bindZoom(id, delta) {
    var b = $("#" + id);
    if (b) b.addEventListener("click", function () { applyZoom(zoom + delta, true); });
  }
  bindZoom("zoom-in", ZOOM_STEP);
  bindZoom("zoom-out", -ZOOM_STEP);
  bindZoom("fs-zoom-in", ZOOM_STEP);
  bindZoom("fs-zoom-out", -ZOOM_STEP);
  $all(".zoom-val").forEach(function (el) {
    el.addEventListener("click", function () { applyZoom(100, true); });
  });

  function docUrl(p) {
    return "/d/" + encodeURIComponent(p).replace(/%2F/g, "/");
  }

  /* ---------------- 全屏 ---------------- */
  var fsBar = $("#fs-bar");
  var fsBtn = $("#fs");
  function fsElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }
  function immersive() { return root.classList.contains("immersive"); }
  function syncFsUi() {
    var on = !!fsElement() || immersive();
    // 原生全屏时顶栏还在，浮动条只在沉浸模式出现，避免重复一套控件
    if (fsBar) fsBar.hidden = !immersive();
    if (fsBtn) {
      fsBtn.textContent = on ? "⤢" : "⛶";
      fsBtn.setAttribute("aria-pressed", on ? "true" : "false");
      fsBtn.title = on ? "退出全屏" : "全屏";
    }
  }
  function enterImmersive() {
    root.classList.add("immersive");
    syncFsUi();
  }
  function exitFullscreen() {
    fsIntent = false;
    if (fsElement()) {
      var fn = document.exitFullscreen || document.webkitExitFullscreen;
      if (fn) { try { fn.call(document); } catch (e) {} }
    }
    root.classList.remove("immersive");
    syncFsUi();
    // 全屏期间没写历史（WebKit 的 History API 会打断全屏），退出后补一条，
    // 这样之后的「后退」不会直接跳出站点
    if (scrollPath && location.pathname !== docUrl(scrollPath)) {
      try { history.pushState({ path: scrollPath }, "", docUrl(scrollPath)); } catch (e) {}
    }
  }
  function toggleFullscreen() {
    if (fsElement() || immersive()) { exitFullscreen(); return; }
    var el = document.documentElement;
    var req = el.requestFullscreen || el.webkitRequestFullscreen;
    if (!req) { enterImmersive(); return; }
    try {
      var p = req.call(el);
      if (p && typeof p.catch === "function") p.catch(function () { enterImmersive(); });
      else if (p === undefined) { /* 老实现无返回 */ }
    } catch (e) {
      enterImmersive();
    }
  }
  var fsIntent = false;
  if (fsBtn) fsBtn.addEventListener("click", function () { fsIntent = !(fsElement() || immersive()); toggleFullscreen(); });
  var fsExit = $("#fs-exit");
  if (fsExit) fsExit.addEventListener("click", exitFullscreen);
  ["fullscreenchange", "webkitfullscreenchange"].forEach(function (ev) {
    document.addEventListener(ev, function () {
      syncFsUi();
      if (fsIntent && !fsElement() && !immersive()) {
        // 浏览器自己退出了全屏（不是用户点的），切沉浸模式顶上，别打断阅读
        fsIntent = false;
        root.classList.add("immersive");
        syncFsUi();
        showToast("已切换为沉浸模式，点右上角 ⛶ 可重新全屏");
      }
    });
  });
  // 浏览器没处理 Esc 时兜底（外接键盘 / 沉浸模式）
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && (fsElement() || immersive())) exitFullscreen();
  });

  /* ---------------- 侧栏 ---------------- */
  var sidebar = $("#sidebar"), overlay = $("#overlay"), tab = $("#side-tab");
  var narrowMq = window.matchMedia("(max-width: 700px)");
  function isNarrow() { return narrowMq.matches; }
  function drawerOpen() { return sidebar.classList.contains("open"); }
  function collapsed() { return root.classList.contains("side-collapsed"); }

  function setSidebar(open) {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    if (overlay) overlay.classList.toggle("show", open);
  }
  function setCollapsed(flag) {
    root.classList.toggle("side-collapsed", flag);
    store.set("mdview:sideCollapsed", flag ? "1" : "0");
  }
  function toggleSidebar() {
    // 窄屏：抽屉 + 遮罩；宽屏：整栏完全收起 / 展开
    if (isNarrow()) { setSidebar(!drawerOpen()); return; }
    setCollapsed(!collapsed());
  }
  function showSidebar() {
    if (isNarrow()) setSidebar(true);
    else setCollapsed(false);
  }

  var menu = $("#menu");
  if (menu) menu.addEventListener("click", toggleSidebar);
  var closeSide = $(".close-side");
  if (closeSide) closeSide.addEventListener("click", toggleSidebar);
  if (overlay) overlay.addEventListener("click", function () { setSidebar(false); });
  if (tab) tab.addEventListener("click", showSidebar);
  if (narrowMq.addEventListener) {
    narrowMq.addEventListener("change", function () {
      sidebar.classList.remove("open");
      if (overlay) overlay.classList.remove("show");
    });
  }
  window.addEventListener("resize", function () {
    if (isNarrow()) return;
    var cur = parseInt(getComputedStyle(root).getPropertyValue("--side-w"), 10) || 288;
    var limit = Math.max(220, Math.min(460, window.innerWidth - 420));
    if (cur > limit) {
      root.style.setProperty("--side-w", limit + "px");
      store.set("mdview:sideWidth", String(limit));
    }
  }, { passive: true });

  /* 拖动右边缘；向左拖过阈值 = 整栏折叠，点边缘小把手再展开 */
  var resizer = $("#side-resizer");
  if (resizer) {
    // 松手时保留当前宽度；
    // 只有拖到 COLLAPSE_AT 以下才会整栏折叠（拖拽过程允许更窄，便于看到折叠意图）
    var MIN_W = 120, MAX_W = 460, MIN_DRAG = 90, COLLAPSE_AT = 120, drag = null;

    function sideWidth() { return sidebar.getBoundingClientRect().width; }
    // 正文至少留 420px，屏幕窄时上限自动收窄
    function maxWidth() {
      return Math.max(MIN_W, Math.min(MAX_W, window.innerWidth - 420));
    }
    function applyWidth(w) {
      w = Math.round(Math.max(MIN_DRAG, Math.min(maxWidth(), w)));
      root.style.setProperty("--side-w", w + "px");
      return w;
    }
    resizer.addEventListener("pointerdown", function (ev) {
      if (collapsed()) return;
      drag = { x: ev.clientX, w: sideWidth(), id: ev.pointerId, now: sideWidth() };
      root.classList.add("resizing");
      try { resizer.setPointerCapture(ev.pointerId); } catch (e) {}
      ev.preventDefault();
    });
    resizer.addEventListener("pointermove", function (ev) {
      if (!drag || ev.pointerId !== drag.id) return;
      drag.now = applyWidth(drag.w + (ev.clientX - drag.x));
      sidebar.classList.toggle("snap", drag.now < COLLAPSE_AT);
      store.set("mdview:sideWidth", String(Math.round(drag.now)));
    });
    function endDrag() {
      if (!drag) return;
      var w = drag.now, base = drag.w;
      drag = null;
      root.classList.remove("resizing");
      sidebar.classList.remove("snap");
      if (w < COLLAPSE_AT) {
        // 拖到很窄就彻底收起来，记住拖动前的宽度，下次展开还原
        var back = Math.round(Math.max(MIN_DRAG, Math.min(maxWidth(), base)));
        root.style.setProperty("--side-w", back + "px");
        store.set("mdview:sideWidth", String(back));
        setCollapsed(true);
        return;
      }
      w = Math.round(Math.max(MIN_DRAG, Math.min(maxWidth(), w)));
      root.style.setProperty("--side-w", w + "px");
      store.set("mdview:sideWidth", String(w));
    }
    resizer.addEventListener("pointerup", endDrag);
    resizer.addEventListener("pointercancel", endDrag);
    resizer.addEventListener("dblclick", function () {
      root.style.setProperty("--side-w", "288px");
      store.set("mdview:sideWidth", "288");
    });
    resizer.tabIndex = 0;
    resizer.addEventListener("keydown", function (ev) {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      ev.preventDefault();
      var w = sideWidth() + (ev.key === "ArrowRight" ? 16 : -16);
      w = Math.round(Math.max(MIN_DRAG, Math.min(maxWidth(), w)));
      root.style.setProperty("--side-w", w + "px");
      store.set("mdview:sideWidth", String(w));
    });
  }

  /* 记住展开的目录 */
  var DIR_KEY = "mdview:dirs";
  var dirState = {};
  try { dirState = JSON.parse(store.get(DIR_KEY, "{}")) || {}; } catch (e) { dirState = {}; }
  $all("#tree details[data-key]").forEach(function (d) {
    var key = d.getAttribute("data-key");
    if (key && Object.prototype.hasOwnProperty.call(dirState, key)) {
      d.open = !!dirState[key];
    }
    d.addEventListener("toggle", function () {
      if (key) { dirState[key] = d.open; store.set(DIR_KEY, JSON.stringify(dirState)); }
    });
  });

  /* ---------------- 侧栏排序（纯前端重排，不刷新页面） ---------------- */
  var SORT_KEY = "mdview:sort";
  var sortButtons = $all(".sort-btn");
  var sortMode = store.get(SORT_KEY, "name-asc");
  var SORT_MODES = sortButtons.map(function (b) { return b.getAttribute("data-sort") });
  if (SORT_MODES.indexOf(sortMode) < 0) sortMode = "name-asc";

  function sortKeyOf(li, mode) {
    var a = li.querySelector("a");
    if (!a) return null;
    var name = (a.getAttribute("data-name") || a.textContent || "").trim();
    if (mode.indexOf("time") === 0) return parseFloat(a.getAttribute("data-mtime")) || 0;
    if (mode.indexOf("size") === 0) return parseInt(a.getAttribute("data-size"), 10) || 0;
    return name;
  }

  function applySort(mode) {
    sortMode = mode;
    $all("#tree ul.tree").forEach(function (ul) {
      var dirs = [], files = [];
      Array.prototype.slice.call(ul.children).forEach(function (li) {
        if (li.classList.contains("dir")) dirs.push(li);
        else if (li.classList.contains("file")) files.push(li);
      });
      dirs.sort(function (x, y) {
        var nx = (x.querySelector(".dname") || x).textContent.trim().toLowerCase();
        var ny = (y.querySelector(".dname") || y).textContent.trim().toLowerCase();
        return nx < ny ? -1 : nx > ny ? 1 : 0;
      });
      files.sort(function (x, y) {
        var kx = sortKeyOf(x, mode), ky = sortKeyOf(y, mode);
        var cmp;
        if (typeof kx === "string") cmp = kx < ky ? -1 : kx > ky ? 1 : 0;
        else cmp = kx - ky;
        if (mode === "name-desc" || mode === "time-desc" || mode === "size-desc") cmp = -cmp;
        return cmp;
      });
      dirs.concat(files).forEach(function (li) { ul.appendChild(li); });
    });
  }

  function syncSortButtons() {
    sortButtons.forEach(function (b) {
      var on = b.getAttribute("data-sort") === sortMode;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  sortButtons.forEach(function (b) {
    b.addEventListener("click", function () {
      var mode = b.getAttribute("data-sort");
      if (mode === sortMode) return;
      store.set(SORT_KEY, mode);
      applySort(mode);
      syncSortButtons();
    });
  });
  syncSortButtons();
  if (sortMode !== "name-asc") applySort(sortMode);

  /* 标题筛选 + 回车全文搜索 */
  var filter = $("#filter");
  if (filter) {
    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      $all("#tree li.file").forEach(function (li) {
        var a = li.querySelector("a");
        if (!a) return;
        var hay = (a.textContent + " " + (a.getAttribute("title") || "")).toLowerCase();
        li.style.display = !q || hay.indexOf(q) >= 0 ? "" : "none";
      });
      $all("#tree li.dir").forEach(function (li) {
        var mine = li.querySelectorAll("li.file");
        var visible = Array.prototype.some.call(mine, function (x) { return x.style.display !== "none"; });
        li.style.display = visible ? "" : "none";
        if (q && visible) {
          var det = li.querySelector("details");
          if (det && !det.open) det.open = true;
        }
      });
    });
    filter.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        var q = filter.value.trim();
        if (q) location.href = "/search?q=" + encodeURIComponent(q);
      } else if (ev.key === "Escape") {
        filter.value = "";
        filter.dispatchEvent(new Event("input"));
        filter.blur();
      }
    });
  }

  /* ---------------- 阅读进度（切换文档复用） ---------------- */
  var scrollPath = path;
  function scrollKey(p, suffix) { return "mdview:scroll:" + p + (suffix || ""); }
  function saveScrollNow() {
    if (!scrollPath) return;
    store.set(scrollKey(scrollPath), String(Math.round(window.scrollY)));
    store.set(scrollKey(scrollPath, ":t"), String(Date.now()));
  }
  function restoreScrollFor(p) {
    var generation = documentGeneration;
    var y = parseInt(store.get(scrollKey(p), "0"), 10) || 0;
    var t = parseInt(store.get(scrollKey(p, ":t"), "0"), 10) || 0;
    var jump = function () {
      if (generation === documentGeneration && p === scrollPath) window.scrollTo(0, y);
    };
    if (y > 40 && Date.now() - t < 1000 * 60 * 60 * 24 * 7) {
      requestAnimationFrame(function () { requestAnimationFrame(jump); });
      setTimeout(jump, 160);
    } else {
      window.scrollTo(0, 0);
    }
  }
  var scrollTimer = 0;
  window.addEventListener("scroll", function () {
    if (scrollTimer) return;
    scrollTimer = setTimeout(function () { scrollTimer = 0; saveScrollNow(); }, 180);
  }, { passive: true });
  if (path) {
    store.set("mdview:last", path);
    restoreScrollFor(path);
  }
  if (location.hash) scheduleRealign();

  /* ---------------- 回到顶部 ---------------- */
  var toTop = $("#to-top");
  if (toTop) {
    window.addEventListener("scroll", function () {
      toTop.classList.toggle("show", window.scrollY > 600);
    }, { passive: true });
    toTop.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }

  /* ---------------- 正文内交互（切文档后重新绑定） ---------------- */
  var lightbox = $("#lightbox");
  var lbImg = lightbox ? lightbox.querySelector("img") : null;

  function closeLb() {
    if (!lightbox) return;
    lightbox.classList.remove("show");
    setTimeout(function () { if (lbImg) lbImg.src = ""; }, 200);
  }
  if (lightbox) lightbox.addEventListener("click", closeLb);

  function bindCopy(btn) {
    btn.addEventListener("click", function () {
      var block = btn.closest(".code-block");
      var code = block ? block.querySelector("code") : null;
      if (!code) return;
      var text = code.innerText;
      var done = function () {
        var old = btn.textContent;
        btn.textContent = "已复制";
        setTimeout(function () { btn.textContent = old; }, 1200);
      };
      function fallback() {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (e) {}
        document.body.removeChild(ta);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fallback);
      } else {
        fallback();
      }
    });
  }

  function bindImg(img) {
    img.addEventListener("click", function () {
      if (!lightbox || !lbImg) return;
      lbImg.src = img.currentSrc || img.src;
      lbImg.alt = img.alt || "";
      lightbox.classList.add("show");
    });
  }

  function renderMath(scope) {
    if (!scope || typeof window.renderMathInElement !== "function") return;
    try {
      window.renderMathInElement(scope, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false }
        ],
        throwOnError: false,
        strict: false,
        ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"]
      });
    } catch (e) {}
  }

  /* ---- Mermaid 图表：只有页面里真的出现图时才去加载 3.5MB 的库 ---- */
  var mermaidLoading = null;
  function ensureMermaid() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (mermaidLoading) return mermaidLoading;
    mermaidLoading = new Promise(function (resolve) {
      var s = document.createElement("script");
      s.src = "/static/mermaid/mermaid.min.js?v=11";
      s.onload = function () { resolve(window.mermaid || null); };
      s.onerror = function () { resolve(null); };
      document.head.appendChild(s);
    });
    return mermaidLoading;
  }

  function renderMermaid(scope) {
    if (!scope) return;
    var nodes = Array.prototype.slice.call(scope.querySelectorAll("pre.mermaid"));
    if (!nodes.length) return;
    nodes.forEach(function (n) {
      if (!n.dataset.src) n.dataset.src = n.textContent;
    });
    ensureMermaid().then(function (mm) {
      if (!mm) return;
      var theme = root.getAttribute("data-theme") === "dark" ? "dark" : "default";
      try {
        mm.initialize({
          startOnLoad: false,
          theme: theme,
          securityLevel: "strict",
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif',
          fontSize: 16,
          flowchart: { useMaxWidth: true, htmlLabels: true },
          sequence: { useMaxWidth: true },
          gantt: { useMaxWidth: true },
          journey: { useMaxWidth: true }
        });
      } catch (e) {}
      nodes.forEach(function (n) {
        n.removeAttribute("data-processed");
        n.textContent = n.dataset.src;
      });
      try {
        var p = mm.run({ nodes: nodes, suppressErrors: true });
        if (p && p.then) {
          p.then(function () {
            nodes.forEach(function (n) {
              var svg = n.querySelector("svg");
              if (svg) mountMermaidTools(n, svg);
            });
          }).catch(function () {});
        }
      } catch (e) {}
    });
  }

  /* 每张图右上角的小工具：默认撑满栏宽，可单独缩放 */
  function mountMermaidTools(pre, svg) {
    var wrap = pre.closest(".mermaid-wrap") || pre.parentElement;
    if (!wrap) return;
    var old = wrap.querySelector(".mm-tools");
    if (old) old.remove();

    // 每篇文档的每张图各自记住缩放比例
    var wraps = Array.prototype.slice.call(document.querySelectorAll(".mermaid-wrap"));
    var idx = wraps.indexOf(wrap);
    var key = "mdview:mm:" + (scrollPath || "") + ":" + idx;
    var scale = parseFloat(store.get(key, wrap.dataset.mmScale || "1")) || 1;
    var tools = document.createElement("div");
    tools.className = "mm-tools";
    tools.innerHTML =
      '<button type="button" data-mm="out" aria-label="缩小图表">−</button>' +
      '<button type="button" class="mm-val" title="点击恢复 100%">100%</button>' +
      '<button type="button" data-mm="in" aria-label="放大图表">+</button>';
    wrap.appendChild(tools);
    var val = tools.querySelector(".mm-val");

    function apply(next) {
      scale = Math.max(0.4, Math.min(2.5, Math.round(next * 10) / 10));
      wrap.dataset.mmScale = String(scale);
      svg.style.width = (scale * 100) + "%";
      svg.style.maxWidth = "none";
      svg.style.height = "auto";
      if (val) val.textContent = Math.round(scale * 100) + "%";
      store.set(key, String(scale));
    }
    apply(scale);

    tools.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button");
      if (!btn) return;
      if (btn.dataset.mm === "in") apply(scale + 0.1);
      else if (btn.dataset.mm === "out") apply(scale - 0.1);
      else apply(1);
    });
  }

  function bindContent(scope) {
    if (!scope) return;
    Array.prototype.forEach.call(scope.querySelectorAll(".code-block .copy-btn"), bindCopy);
    Array.prototype.forEach.call(scope.querySelectorAll(".doc-body img"), bindImg);
    renderMath(scope);
    renderMermaid(scope);
    if (location.hash) scheduleRealign();
  }
  bindContent(document);

  /* ---------------- 正文宽度拖拽把手 ---------------- */
  (function docWidthHandle() {
    var content = document.getElementById("content");
    if (!content) return;
    var handle = document.createElement("div");
    handle.className = "doc-resizer";
    handle.id = "doc-resizer";
    handle.title = "拖动调整正文宽度，双击恢复默认";
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    document.body.appendChild(handle);

    function place() {
      var doc = document.querySelector(".doc");
      if (!doc) { handle.style.display = "none"; return; }
      var rect = doc.getBoundingClientRect();
      handle.style.left = Math.round(rect.right - 7) + "px";
      handle.style.display = content.clientWidth < 520 ? "none" : "";
    }

    var drag = null;
    handle.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0) return;
      var doc = document.querySelector(".doc");
      if (!doc) return;
      drag = { x: ev.clientX, w: doc.getBoundingClientRect().width, id: ev.pointerId };
      handle.classList.add("dragging");
      root.classList.add("resizing");
      try { handle.setPointerCapture(ev.pointerId); } catch (e) {}
      ev.preventDefault();
    });
    handle.addEventListener("pointermove", function (ev) {
      if (!drag || ev.pointerId !== drag.id) return;
      // 正文居中，所以拖动右边缘 1px = 宽度变化 2px
      var w = drag.w + (ev.clientX - drag.x) * 2;
      w = Math.max(460, Math.min(docLimit(), w));
      docWidth = Math.round(w);
      applyDocMax(zoom / 100);
      place();
    });
    function endDrag() {
      if (!drag) return;
      drag = null;
      handle.classList.remove("dragging");
      root.classList.remove("resizing");
      store.set(DOCW_KEY, String(docWidth));
    }
    handle.addEventListener("pointerup", endDrag);
    handle.addEventListener("pointercancel", endDrag);
    handle.addEventListener("dblclick", function () {
      docWidth = 0;
      try { localStorage.removeItem(DOCW_KEY); } catch (e) {}
      applyDocMax(zoom / 100);
      place();
      showToast("正文宽度已恢复默认");
    });

    window.addEventListener("resize", place, { passive: true });
    document.addEventListener("scroll", place, { passive: true, capture: true });
    place();
    applyDocMax(zoom / 100);

    placeDocHandle = place;
    document.addEventListener("click", function () { requestAnimationFrame(place) }, true);
  })();

  /* ---------------- 站内切换：不刷新页面（全屏时才不会被踢出） ---------------- */
  var contentEl = $("#content");
  var crumbsEl = document.querySelector(".crumbs");
  var navigating = false;

  function markUserScroll() {
    lastUserScroll = Date.now();
    realignTimers.forEach(clearTimeout);
    realignTimers = [];
  }
  ["wheel", "touchmove"].forEach(function (ev) {
    window.addEventListener(ev, markUserScroll, { passive: true });
  });

  function scrollToHash(id, behavior) {
    var el = document.getElementById(id);
    if (!el) return false;
    try {
      el.scrollIntoView({ behavior: behavior || "instant", block: "start" });
    } catch (e) {
      el.scrollIntoView();
    }
    return true;
  }

  // 图片 / 公式 / 图表渲染会改变上方高度，稍后再对一次位（用户没自己滚才动）
  function scheduleRealign() {
    if (!location.hash) return;
    realignTimers.forEach(clearTimeout);
    realignTimers = [280, 700, 1400].map(function (ms) {
      return setTimeout(function () {
        if (!location.hash) return;
        if (Date.now() - lastUserScroll < 900) return;
        scrollToHash(location.hash.slice(1), "instant");
      }, ms);
    });
  }

  function setActiveLink(p) {
    $all("#tree a[data-path]").forEach(function (a) {
      var on = a.getAttribute("data-path") === p;
      var li = a.parentNode;
      if (li && li.classList) li.classList.toggle("active", on);
    });
  }

  function renderDoc(data, hash) {
    documentGeneration += 1;
    clearTimeout(refreshTimer);
    refreshTimer = 0;
    realignTimers.forEach(clearTimeout);
    realignTimers = [];
    liveVersion = data.version || "";
    lastSig = data.sig || "";
    if (contentEl) contentEl.innerHTML = data.html;
    if (crumbsEl) crumbsEl.innerHTML = data.crumbs;
    document.title = data.title + " · mdview";
    if (body) body.setAttribute("data-path", data.path);
    scrollPath = data.path;
    store.set("mdview:last", data.path);
    setActiveLink(data.path);
    bindContent(contentEl);
    requestAnimationFrame(placeDocHandle);
    if (isNarrow()) setSidebar(false);
    if (hash) {
      setTimeout(function () {
        scrollToHash(hash.replace("#", ""), "instant");
        scheduleRealign();
      }, 220);
    } else {
      restoreScrollFor(data.path);
    }
  }

  function go(href, push) {
    if (navigating) return;
    var hash = "";
    var hi = href.indexOf("#");
    if (hi >= 0) { hash = href.slice(hi); href = href.slice(0, hi); }
    navigating = true;
    var url = href + (href.indexOf("?") >= 0 ? "&" : "?") + "frag=1";
    fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.json();
      })
      .then(function (data) {
        navigating = false;
        saveScrollNow();
        // 全屏时完全不碰 History API：部分 WebKit 会因同文档导航退出全屏
        if (push && !fsElement() && !immersive()) {
          try { history.pushState({ path: data.path }, "", href + hash); } catch (e) {}
        }
        renderDoc(data, hash);
      })
      .catch(function () {
        navigating = false;
        window.location.href = href + hash;
      });
  }

  document.addEventListener("click", function (ev) {
    if (ev.defaultPrevented || ev.button !== 0) return;
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    var a = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
    if (!a) return;
    if (a.target && a.target !== "_self") return;
    var href = a.getAttribute("href") || "";

    // 页内锚点（目录跳转）：自己算位置，避免浏览器平滑滚动 + 异步渲染导致的偏差
    if (href.charAt(0) === "#" && href.length > 1 && document.getElementById(href.slice(1))) {
      ev.preventDefault();
      try { history.pushState({ hash: href }, "", href); } catch (e) {}
      scrollToHash(href.slice(1), "instant");
      scheduleRealign();
      return;
    }

    if (href.indexOf("/d/") !== 0) return;
    if (href.indexOf("raw=1") >= 0) return;   // 查看原文走普通打开
    ev.preventDefault();
    go(href, true);
  });

  window.addEventListener("popstate", function () {
    var href = location.pathname + location.search + location.hash;
    if (href.indexOf("/d/") !== 0) { location.reload(); return; }
    go(href, false);
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { closeLb(); setSidebar(false); }
  });

  try { history.replaceState({ path: path }, "", location.href); } catch (e) {}
  /* ---------------- 文档改动自动更新 ---------------- */
  var liveVersion = "";
  var refreshTimer = 0;
  var refreshInFlight = null;
  var toastEl = null;
  var toastTimer = 0;

  function showToast(text) {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "toast";
      toastEl.setAttribute("role", "status");
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 2600);
  }

  function docUrl(p) {
    return "/d/" + encodeURIComponent(p).replace(/%2F/g, "/");
  }

  var lastSig = "";
  function refreshCurrent() {
    var p = scrollPath;
    var generation = documentGeneration;
    if (!p || refreshInFlight === generation) return;
    refreshInFlight = generation;
    fetch(docUrl(p) + "?frag=1", {
      credentials: "same-origin",
      headers: { "Accept": "application/json" }
    }).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(function (data) {
      if (p !== scrollPath || generation !== documentGeneration || navigating) return;
      liveVersion = data.version || liveVersion;
      // 内容没变（只是保存时间变了）就不动 DOM，避免任何闪烁
      if (data.sig && data.sig === lastSig) {
        return;
      }
      var keepY = Math.round(window.scrollY);
      lastSig = data.sig || "";
      if (contentEl) contentEl.innerHTML = data.html;
      if (crumbsEl) crumbsEl.innerHTML = data.crumbs;
      document.title = data.title + " · mdview";
      bindContent(contentEl);
      requestAnimationFrame(placeDocHandle);
      var restore = function () {
        if (generation === documentGeneration) window.scrollTo(0, keepY);
      };
      requestAnimationFrame(restore);
      setTimeout(restore, 120);
    }).catch(function () { /* 下次轮询会重试 */ }).then(function () {
      if (refreshInFlight === generation) refreshInFlight = null;
    });
  }

  function pollDoc() {
    if (!scrollPath || /\.pdf$/i.test(scrollPath) || document.hidden || navigating) return;
    var p = scrollPath;
    var generation = documentGeneration;
    fetch("/api/meta?p=" + encodeURIComponent(p), {
      credentials: "same-origin",
      headers: { "Accept": "application/json" }
    }).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(function (meta) {
      if (!meta || !meta.ok || p !== scrollPath || generation !== documentGeneration) return;
      if (!liveVersion) { liveVersion = meta.version; return; }
      if (meta.version !== liveVersion) {
        if (refreshTimer) return;
        refreshTimer = setTimeout(function () {
          refreshTimer = 0;
          refreshCurrent();
        }, 700);
      }
    }).catch(function () {});
  }

  // 使用首屏的文件版本作为轮询基线
  var initialDoc = document.querySelector(".doc[data-version]");
  if (initialDoc) {
    liveVersion = initialDoc.getAttribute("data-version") || "";
    lastSig = initialDoc.getAttribute("data-sig") || "";
  }

  setInterval(pollDoc, 4000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) pollDoc();
  });

  /* ---------------- 添加目录：选一个目录，以软链接放进 docs ---------------- */
  var addDirBtn = $("#add-dir");
  if (addDirBtn) {
    var adModal = null, adList = null, adPath = null, adName = null, adCur = "", adParent = null, adHome = "";

    function adClose() {
      if (adModal) { adModal.remove(); adModal = null; }
    }

    function adRender(data) {
      if (!adModal) return;
      adCur = data.path;
      adParent = data.parent;
      adHome = data.home;
      adPath.value = data.path;
      adName.value = data.path.split("/").filter(Boolean).pop() || "";
      adList.textContent = "";
      if (!data.dirs.length) {
        var empty = document.createElement("p");
        empty.className = "ad-empty";
        empty.textContent = "（这里没有子目录）";
        adList.appendChild(empty);
      }
      data.dirs.forEach(function (d) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "ad-dir";
        b.textContent = d.name;
        b.addEventListener("click", function () { adLoad(d.path); });
        adList.appendChild(b);
      });
    }

    function adLoad(path) {
      fetch("/api/browse?path=" + encodeURIComponent(path), { credentials: "same-origin" })
        .then(function (r) { return r.json() })
        .then(adRender)
        .catch(function () {});
    }

    function adOpen() {
      if (adModal) return;
      adModal = document.createElement("div");
      adModal.className = "modal-mask";
      adModal.innerHTML =
        '<div class="modal" role="dialog" aria-modal="true" aria-label="添加目录">' +
        '<div class="modal-head"><b>添加目录到 docs</b>' +
        '<button type="button" class="modal-x" aria-label="关闭">✕</button></div>' +
        '<div class="modal-bar">' +
        '<button type="button" id="ad-up" title="上一级">↑</button>' +
        '<button type="button" id="ad-home">主目录</button>' +
        '<input id="ad-path" type="text" placeholder="绝对路径，回车跳转" autocomplete="off" spellcheck="false"></div>' +
        '<div class="modal-list" id="ad-list"></div>' +
        '<div class="modal-foot"><label>链接名<input id="ad-name" type="text" autocomplete="off" spellcheck="false"></label>' +
        '<div class="modal-actions"><button type="button" id="ad-cancel">取消</button>' +
        '<button type="button" id="ad-ok" class="primary">链接到 docs</button></div></div></div>';
      document.body.appendChild(adModal);
      adList = adModal.querySelector("#ad-list");
      adPath = adModal.querySelector("#ad-path");
      adName = adModal.querySelector("#ad-name");

      adModal.addEventListener("click", function (ev) {
        if (ev.target === adModal) adClose();
      });
      adModal.querySelector(".modal-x").addEventListener("click", adClose);
      adModal.querySelector("#ad-cancel").addEventListener("click", adClose);
      adModal.querySelector("#ad-home").addEventListener("click", function () { adLoad(adHome); });
      adModal.querySelector("#ad-up").addEventListener("click", function () {
        if (adParent) adLoad(adParent);
      });
      adPath.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter") return;
        ev.preventDefault();
        if (adPath.value.trim()) adLoad(adPath.value.trim());
      });
      adModal.querySelector("#ad-ok").addEventListener("click", function () {
        var okBtn = adModal.querySelector("#ad-ok");
        okBtn.disabled = true;
        fetch("/api/link", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target: adCur, name: adName.value.trim() }),
        }).then(function (r) { return r.json() }).then(function (res) {
          if (!res.ok) {
            okBtn.disabled = false;
            showToast(res.error || "创建失败");
            return;
          }
          adClose();
          location.reload();
        }).catch(function () {
          okBtn.disabled = false;
          showToast("网络错误，创建失败");
        });
      });
      adLoad("");
    }

    addDirBtn.addEventListener("click", adOpen);
  }

  /* ---------------- 快捷键 ---------------- */
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "/" && filter && document.activeElement !== filter) {
      ev.preventDefault();
      filter.focus();
    }
  });
})();
