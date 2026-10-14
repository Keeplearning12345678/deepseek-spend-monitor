/* ============================================================
 * DeepSeek 花费浮窗 —— 嵌入 pi-web 页面内部
 *
 *  · 默认三列：剩（余额） / 今日（花费） / 总（累计），末尾还有一个「峰 / 谷」时段标识
 *    （悬停在峰/谷上会显示空闲/高峰两个时段的完整三档单价，与 DeepSeek 官网价格表同序：
 *      输入缓存命中 / 输入缓存未命中 / 输出）
 *  · 配色直接取 pi-web 的 CSS 变量（--bg-panel/--border/--text/--accent），
 *    浅色深色主题自动跟随
 *  · position:fixed，滚动时固定在视口，不随内容滚动
 *  · 可拖动，位置记在 localStorage；双击复位
 *  · **右键菜单：锁定位置 / 解锁位置 / 复位到左上角**
 *    —— 锁定后完全不可拖动（防止误碰），想换位置时右键解锁即可
 *  · **锁定态 = 深度融合模式**：去掉阴影/边框/圆角/背景，只留文字落在 pi-web 面板上，
 *    看起来就是页面原生的一行数据（不是“挂件”）；拖动后自动贴边吸附
 *  · 数据来自本地服务 http://127.0.0.1:__PORT__/state
 *    （__PORT__ 由数据服务在返回本文件时替换成实际端口）
 *
 *  想改显示的指标：编辑下面的 FIELDS 数组即可。
 * ============================================================ */
(function () {
  "use strict";
  if (window.__dsSpendWidgetLoaded) return;
  window.__dsSpendWidgetLoaded = true;

  var API = "http://127.0.0.1:__PORT__/state";
  var POS_KEY = "dsSpendWidget.pos.v1";
  var LOCK_KEY = "dsSpendWidget.locked.v1";
  var ICON_KEY = "dsSpendWidget.alertIcon.v1";   // 压缩提醒图标开关（v1.9.0）
  var POLL_MS = 5000;
  var DEF = { left: 12, top: 64 };
  var MARK = "ds-spend-widget";

  // 显示的指标：label = 小标签，key = /state 字段名，cls = 配色类名
  // 可用字段：balance(剩余余额) today(今日) session(本会话) all(累计)
  var FIELDS = [
    { label: "剩", key: "balance", cls: "ds-balance" },
    { label: "今日", key: "today", cls: "ds-today" },
    { label: "总", key: "all", cls: "ds-all" }
  ];

  var CSS = [
    "#" + MARK + "{position:fixed;z-index:2147483000;display:flex;align-items:baseline;gap:11px;",
    "padding:5px 10px 6px;border-radius:8px;box-sizing:border-box;",
    "background:var(--bg-panel,#f5f5f5);border:1px solid var(--border,#e0e0e0);",
    "box-shadow:0 2px 10px rgba(0,0,0,.13);",
    'font-family:var(--font-sans,ui-sans-serif,system-ui,"Microsoft YaHei",sans-serif);',
    "cursor:grab;user-select:none;-webkit-user-select:none;opacity:.97;",
    "transition:opacity .15s ease}",
    "#" + MARK + ":hover{opacity:1}",
    "#" + MARK + ".dragging{cursor:grabbing}",
    "#" + MARK + ".ds-locked{cursor:default;",
    "background:var(--bg-panel,#f5f5f5);border-color:transparent;",
    "box-shadow:none;border-radius:0;opacity:1}",
    "#" + MARK + ".ds-locked:hover{opacity:1}",
    "#" + MARK + ".ds-locked .ds-sep{background:var(--border,#e0e0e0);opacity:.6}",
    ".ds-spend-menu{position:fixed;z-index:2147483001;min-width:124px;padding:4px;border-radius:7px;",
    "background:var(--bg-panel,#f5f5f5);border:1px solid var(--border,#e0e0e0);",
    "box-shadow:0 4px 16px rgba(0,0,0,.18);",
    'font-family:var(--font-sans,ui-sans-serif,system-ui,"Microsoft YaHei",sans-serif);font-size:11px}',
    ".ds-spend-menu > div{padding:5px 9px;border-radius:5px;cursor:pointer;color:var(--text,#1a1a1a);white-space:nowrap}",
    ".ds-spend-menu > div:hover{background:var(--bg-hover,#eee)}",
    ".ds-spend-menu > div.ds-mi-dim{color:var(--text-muted,#6b7280);font-size:10px;padding:3px 9px;cursor:default}",
    ".ds-spend-menu > div.ds-mi-dim:hover{background:transparent}",
    "#" + MARK + " .ds-cell{display:flex;align-items:baseline;gap:3px;white-space:nowrap}",
    "#" + MARK + " .ds-lb{font-size:9px;line-height:1;color:var(--text-muted,#6b7280)}",
    "#" + MARK + " .ds-v{font-size:15px;line-height:1;font-weight:700;letter-spacing:-.3px;",
    "color:var(--text,#1a1a1a);font-variant-numeric:tabular-nums}",
    "#" + MARK + " .ds-cell.ds-today .ds-v{color:var(--accent,#2563eb)}",
    "#" + MARK + " .ds-cell.ds-all .ds-v{opacity:.72}",
    "#" + MARK + " .ds-cell.ds-session .ds-v{opacity:.85}",
    "#" + MARK + " .ds-sep{width:1px;align-self:stretch;background:var(--border,#e0e0e0);margin:1px 0}",
    "#" + MARK + " .ds-peak{font-size:11px;line-height:1;font-weight:700;cursor:help;",
    "padding:1px 3px;border-radius:3px;letter-spacing:0}",
    "#" + MARK + " .ds-peak.ds-peak-on{color:#c2410c}",
    "html.dark #" + MARK + " .ds-peak.ds-peak-on{color:#ffb454}",
    "#" + MARK + " .ds-peak.ds-peak-off{color:#0f7a45}",
    "html.dark #" + MARK + " .ds-peak.ds-peak-off{color:#48d597}",
    "#" + MARK + ".ds-locked .ds-peak.ds-peak-on{color:#c2410c}",
    "html.dark #" + MARK + ".ds-locked .ds-peak.ds-peak-on{color:#ffb454}",
    "#" + MARK + " .ds-v.ds-bad{color:var(--text-dim,#9ca3af)}",
    // 计费规则弹层：长行允许折行
    ".ds-spend-menu.ds-rules{max-width:330px;white-space:normal;line-height:1.75}"
  ].join("");

  function kfmt(n) {
    n = Number(n) || 0;
    if (n >= 1000000) { return (Math.round(n / 100000) / 10) + "M"; }
    return n >= 1000 ? Math.round(n / 1000) + "K" : String(n);
  }

  // 今日成本构成：上下文重发 / 模型输出 / 新增输入（三者相加 = 今日花费）
  function costBar(d) {
    var c = d.todayCtxCost || 0, o = d.todayOutCost || 0, n = d.todayNewCost || 0;
    var t = c + o + n;
    if (!t) return "今日成本构成：—";
    var pct = function (v) { return Math.round(v / t * 100); };
    return "今日成本构成：上下文 ¥" + money(c) + "（" + pct(c) + "%）· 输出 ¥" + money(o) +
           "（" + pct(o) + "%）· 新增 ¥" + money(n) + "（" + pct(n) + "%）";
  }

  // 命中率显示：null/undefined → “—”，否则 “71%”
  function pct(v) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return "—";
    return (Math.round(Number(v) * 10) / 10) + "%";
  }

  function money(v) {
    if (v === null || v === undefined || isNaN(v)) return null;
    v = Number(v);
    if (v >= 1000) return v.toFixed(0);
    if (v >= 100) return v.toFixed(1);
    if (v >= 1) return v.toFixed(2);
    if (v >= 0.01) return v.toFixed(3);
    return v.toFixed(4);
  }

  // ---- 位置锁定状态 ----
  function readLock() {
    try { return localStorage.getItem(LOCK_KEY) === "1"; } catch (e) { return false; }
  }
  function readIcon() {
    // 本地没设过 → 跟服务端配置默认（config.json 的 alert_icon）
    try {
      var v = localStorage.getItem(ICON_KEY);
      if (v === null) { return !(lastData && lastData.showAlertIcon === false); }
      return v !== "0";
    } catch (e) { return true; }
  }
  function writeIcon(v) {
    try { localStorage.setItem(ICON_KEY, v ? "1" : "0"); } catch (e) { /* ignore */ }
  }
  function writeLock(v) {
    try { localStorage.setItem(LOCK_KEY, v ? "1" : "0"); } catch (e) { /* ignore */ }
  }
  var locked = readLock();
  var lastData = null;

  function applyLockUI(el) {
    var node = el || document.getElementById(MARK);
    if (node) node.classList.toggle("ds-locked", locked);
  }

  function updateTitle() {
    var el = document.getElementById(MARK);
    if (!el) return;
    var head;
    if (lastData) {
      head = "DeepSeek 账户\n" +
        "剩余余额 ¥" + (money(lastData.balance) || "-") + "（更新 " + (lastData.balanceAt || "-") + "）\n" +
        "今日花费 ¥" + (money(lastData.today) || "-") + "（" + (lastData.todayCalls || 0) + " 次调用）\n" +
        "本会话 ¥" + (money(lastData.session) || "-") + "\n" +
        "累计花费 ¥" + (money(lastData.all) || "-") + "（pi 会话合计，" + (lastData.sessions || 0) + " 个会话 / " + (lastData.allCalls || 0) + " 次调用）\n" +
        "当前时段：" + (lastData.peak ? "高峰价" : "闲时价");
    } else {
      head = "DeepSeek 余额 / 花费";
    }
    el.setAttribute("title", head + "\n" +
      (locked ? "🔒 位置已锁定、已融入页面（右键可解锁 / 换位置）"
              : "拖动可移动（松手贴边）· 双击复位 · 右键可锁定位置"));
  }

  function loadPos() {
    try {
      var p = JSON.parse(localStorage.getItem(POS_KEY) || "null");
      if (p && typeof p.left === "number" && typeof p.top === "number") return p;
    } catch (e) { /* ignore */ }
    return { left: DEF.left, top: DEF.top };
  }

  function savePos(el) {
    try {
      localStorage.setItem(POS_KEY, JSON.stringify({ left: el.offsetLeft, top: el.offsetTop }));
    } catch (e) { /* ignore */ }
  }

  function place(el, p) {
    var maxX = Math.max(0, window.innerWidth - el.offsetWidth - 4);
    var maxY = Math.max(0, window.innerHeight - el.offsetHeight - 4);
    el.style.left = Math.max(0, Math.min(p.left, maxX)) + "px";
    el.style.top = Math.max(0, Math.min(p.top, maxY)) + "px";
  }

  function applyAlertIcon(d) {
    try {
    var ic = document.getElementById("ds-alert-icon");
    if (!ic) { return; }
    var ca = (d && d.compactAdvice) || {};
    var on = (ca.action === "compact") && readIcon();
    ic.style.display = on ? "inline" : "none";
    ic.style.color = "#d9a441";
    ic.setAttribute("title", on ? (ca.text || "") : "");
    if (!ic.dataset.bound) {
      ic.dataset.bound = "1";
      ic.addEventListener("click", function (e) {
        e.stopPropagation();
        if (typeof window.__dsOpenPanel === "function") { window.__dsOpenPanel(alertHtml()); }
      });
    }
    } catch (err) { try { console.error("[ds-spend-widget] 提醒图标出错（不影响金额显示）", err); } catch (_) {} }
  }

  function alertHtml() {
    var ca = (lastData && lastData.compactAdvice) || {};
    if (!ca.text) { return '<div class="ds-mi-dim">暂无判据数据</div>'; }
    var rows = ['<div style="max-width:330px;white-space:normal">' + ca.text + "</div>", '<div class="ds-mi-dim">　</div>'];
    rows.push('<div class="ds-mi-dim">上下文 ' + ((lastData && lastData.ctxNowText) || "—") +
              " · 命中率 " + ((ca.hitPct === null || ca.hitPct === undefined) ? "—" : Math.round(ca.hitPct) + "%") + "</div>");
    if (ca.waste !== null && ca.waste !== undefined) {
      rows.push('<div class="ds-mi-dim">每次因未命中多花 ¥' + Number(ca.waste).toFixed(3) +
                " · 重发一次 ¥" + Number(ca.resend || 0).toFixed(3) + "</div>");
    }
    if (ca.oneTime !== null && ca.oneTime !== undefined) {
      rows.push('<div class="ds-mi-dim">压缩一次性成本 ¥' + Number(ca.oneTime).toFixed(3) + "（重建缓存 + 摘要）</div>");
    }
    rows.push('<div class="ds-mi-dim">判据：C×(1−h)×价差 vs 压缩一次性成本；阈值见 config.json 的 compact_rule</div>');
    rows.push('<div class="ds-mi-dim">（点空白处关闭）</div>');
    return rows.join("");
  }



  function build() {
    if (!document.body) return null;
    var st = document.createElement("style");
    st.textContent = CSS;
    document.head.appendChild(st);

    var el = document.createElement("div");
    el.id = MARK;
    el.setAttribute("title", "DeepSeek 余额 / 花费 · 拖动可移动 · 双击复位");
    var html = [];
    FIELDS.forEach(function (f, i) {
      if (i) html.push('<span class="ds-sep"></span>');
      html.push(
        '<span class="ds-cell ' + f.cls + '">' +
        '<span class="ds-lb">' + f.label + '</span>' +
        '<span class="ds-v" id="ds-v-' + f.key + '">—</span></span>'
      );
    });
    // 末尾追加「峰 / 谷」时段标识（悬停查看峰谷时间段与单价）
    html.push('<span class="ds-sep"></span>');
    html.push('<span class="ds-peak ds-peak-off" id="ds-peak-mark">--</span>');
    html.push('<span id="ds-alert-icon" style="display:none;font-size:11px;line-height:1;font-weight:700;cursor:pointer">' + String.fromCharCode(9986, 65039) + '</span>');
    el.innerHTML = html.join("");
    document.body.appendChild(el);
    place(el, loadPos());

    var drag = null;
    el.addEventListener("mousedown", function (e) {
      if (locked || e.button !== 0) return;          // 已锁定 → 不可拖动
      drag = { x: e.clientX - el.offsetLeft, y: e.clientY - el.offsetTop };
      el.classList.add("dragging");
      e.preventDefault();
    });
    window.addEventListener("mousemove", function (e) {
      if (!drag) return;
      place(el, { left: e.clientX - drag.x, top: e.clientY - drag.y });
    });
    window.addEventListener("mouseup", function () {
      if (!drag) return;
      drag = null;
      el.classList.remove("dragging");
      // 贴边吸附：靠近左右边缘时自动靠到 8px，让位置更像页面原生对齐
      var maxX = window.innerWidth - el.offsetWidth;
      var x = el.offsetLeft;
      if (x <= 16) x = 8;
      else if (x >= maxX - 16) x = maxX - 8;
      place(el, { left: x, top: el.offsetTop });
      savePos(el);
    });
    el.addEventListener("dblclick", function () {
      if (locked) return;                            // 已锁定 → 防误触
      try { localStorage.removeItem(POS_KEY); } catch (e) { /* ignore */ }
      place(el, DEF);
    });
    window.addEventListener("resize", function () {
      place(el, { left: el.offsetLeft, top: el.offsetTop });
    });

    // ---- 右键菜单：锁定/解锁位置 · 复位 ----
    var menu = null;
    function closeMenu() {
      if (menu) { menu.remove(); menu = null; }
    }
    el.addEventListener("contextmenu", function (e) {
      e.preventDefault();
      e.stopPropagation();
      closeMenu();
      menu = document.createElement("div");
      menu.className = "ds-spend-menu";
      menu.innerHTML =
        '<div data-act="lock">' + (locked ? "🔓 解锁位置（可拖动）" : "🔒 锁定位置（融入页面）") + "</div>" +
        '<div data-act="reset">↖ 复位到左上角</div>' +
        '<div data-act="rules">📋 计费规则与单价</div>' +
        '<div data-act="cost">🧩 今日成本构成</div>' +
        '<div data-act="icon">' + (readIcon() ? "✓ " : "　") + '显示压缩提醒图标</div>' +
        '<div class="ds-mi-dim">' + (locked ? "已锁定：不可拖动、阴影已去掉" : "当前：可拖动（松手会贴边对齐）") + "</div>";
      document.body.appendChild(menu);
      menu.style.left = Math.min(e.clientX, window.innerWidth - menu.offsetWidth - 6) + "px";
      menu.style.top = Math.min(e.clientY, window.innerHeight - menu.offsetHeight - 6) + "px";
      menu.addEventListener("click", function (ev) {
        var act = ev.target && ev.target.getAttribute ? ev.target.getAttribute("data-act") : null;
        if (act === "lock") {
          locked = !locked;
          writeLock(locked);
          applyLockUI(el);
          updateTitle();
        } else if (act === "reset") {
          try { localStorage.removeItem(POS_KEY); } catch (err) { /* ignore */ }
          place(el, DEF);
        } else if (act === "rules") {
          showPanel(rulesHtml());
        } else if (act === "cost") {
          showPanel(costHtml());
        } else if (act === "icon") {
          writeIcon(!readIcon());
          applyAlertIcon(lastData);
        }
        closeMenu();
      });
    });
    document.addEventListener("click", closeMenu, true);
    window.addEventListener("blur", closeMenu);

    // ---- 信息弹层（单价/时段规则、成本构成等细节从悬停提示里移出来）----
    var panel = null;
    function closePanel() {
      if (panel) { panel.remove(); panel = null; }
      document.removeEventListener("click", closePanel, true);
    }
    function showPanel(html) {
      window.__dsOpenPanel = showPanel;      // 供顶层图标点击调用（作用域桥）
      if (panel) { closePanel(); return; }      // 再点一次同一个菜单项就关掉
      panel = document.createElement("div");
      panel.className = "ds-spend-menu ds-rules";
      panel.innerHTML = html;
      document.body.appendChild(panel);
      var r = el.getBoundingClientRect();
      panel.style.left = Math.max(8, Math.min(r.left + 20, window.innerWidth - panel.offsetWidth - 8)) + "px";
      panel.style.top = Math.max(8, Math.min(r.bottom + 6, window.innerHeight - panel.offsetHeight - 8)) + "px";
      setTimeout(function () { document.addEventListener("click", closePanel, true); }, 0);
    }


    function costHtml() {
      // 环形饼图（conic-gradient 手绘，不引入依赖）+ 图例
      var d = lastData || {};
      var c = Number(d.todayCtxCost) || 0, o = Number(d.todayOutCost) || 0, n = Number(d.todayNewCost) || 0;
      var tot = c + o + n;
      if (!tot) { return '<div class="ds-mi-dim">今日暂无花费记录</div>'; }
      var pc = function (v) { return 100 * v / tot; };
      var a1 = pc(c), a2 = a1 + pc(o);
      var ring = "conic-gradient(#48d597 0 " + a1.toFixed(2) + "%, #d9a441 " + a1.toFixed(2) + "% " +
                 a2.toFixed(2) + "%, #e06c6c " + a2.toFixed(2) + "% 100%)";
      var legend = [["重发上下文（命中）", c, "#48d597"], ["模型输出", o, "#d9a441"],
                    ["新增输入（未命中）", n, "#e06c6c"]].map(function (x) {
        return '<div style="white-space:nowrap"><span style="display:inline-block;width:10px;height:10px;' +
               "background:" + x[2] + ';border-radius:2px;margin-right:6px"></span>' +
               x[0] + "　<strong>¥" + money(x[1]) + "</strong>　" + pc(x[1]).toFixed(0) + "%</div>";
      }).join("");
      return '<div class="ds-mi-dim">今日成本构成</div>' +
        '<div style="display:flex;align-items:center;gap:14px;padding:6px 2px">' +
          '<div style="position:relative;width:104px;height:104px;flex:0 0 auto">' +
            '<div style="width:104px;height:104px;border-radius:50%;background:' + ring + '"></div>' +
            '<div style="position:absolute;left:22px;top:22px;width:60px;height:60px;border-radius:50%;' +
              'background:var(--bg-panel,#f5f5f5);display:flex;flex-direction:column;align-items:center;' +
              'justify-content:center">' +
              '<div style="font-size:12px;font-weight:700;color:var(--text,#1a1a1a)">¥' + money(tot) + "</div>" +
              '<div class="ds-mi-dim" style="font-size:9px">今日合计</div>' +
            "</div>" +
          "</div>" +
          "<div>" + legend + "</div>" +
        "</div>" +
        '<div class="ds-mi-dim">"新增输入" = 没吃到缓存、按未命中单价计费的部分</div>' +
        '<div class="ds-mi-dim">其单价是命中的 50×（空闲 1 vs 0.02 元/百万）</div>' +
        '<div class="ds-mi-dim">占比高时说明上下文一直在按未命中价重发</div>' +
        '<div class="ds-mi-dim">（点空白处关闭）</div>';
    }
    function rulesHtml() {
      var h = (lastData && lastData.hours) || {};
      return '<div class="ds-mi-dim">计费规则（改 config.json 可换模型价格）</div>' +
        '<div>空闲时段：' + (h.offpeakRate || h.offpeakRateShort || "-") + "</div>" +
        '<div>高峰时段：' + (h.peakRate || h.peakRateShort || "-") + "</div>" +
        '<div>单价单位：元 / 百万 tokens</div>' +
        '<div class="ds-mi-dim">高峰：' + (h.peakText || "-") + "（北京时间）</div>" +
        '<div class="ds-mi-dim">空闲：' + (h.offpeakText || "-") + "</div>" +
        (h.holidayNote ? '<div class="ds-mi-dim">节假日：' + h.holidayNote + (h.holidayYears && h.holidayYears.length ? "（已收录 " + h.holidayYears.join("、") + " 年）" : "") + "</div>" : "") +
        (h.holidayStaleHint ? '<div class="ds-mi-dim">' + h.holidayStaleHint + "</div>" : "") +
        '<div class="ds-mi-dim">（点空白处关闭）</div>';
    }

    applyLockUI(el);
    updateTitle();
    return el;
  }

  function setVal(id, text, bad) {
    var n = document.getElementById(id);
    if (!n) return;
    n.textContent = text === null ? "—" : "¥" + text;
    if (bad) n.classList.add("ds-bad"); else n.classList.remove("ds-bad");
  }

  function poll() {
    fetch(API, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (d) {
        // UI 细节出错不该把已有金额清成"—"：渲染异常只记日志，网络错误才走下面的 catch
        try {
        FIELDS.forEach(function (f) {
          var bad = f.key === "balance" && (d.balance === null || d.balance === undefined);
          setVal("ds-v-" + f.key, money(d[f.key]), bad);
        });
        // 「峰 / 谷」标识 + 悬停提示（时段、三档单价、今日/本周/本月峰谷与命中率、成本构成、倒计时、压缩建议）
        var mk = document.getElementById("ds-peak-mark");
        if (mk) {
          var h = d.hours || {};
          var hn = d.holidayToday || "";
          mk.textContent = (hn && !d.peak) ? ("谷·" + (d.holidayShort || "节")) : (d.peak ? "峰" : "谷");
          mk.className = "ds-peak " + (d.peak ? "ds-peak-on" : "ds-peak-off");
          var L = [];
          L.push(d.peak ? "当前处于【高峰时段】—— 单价较高（空闲时段只要一半）"
                        : "当前处于【空闲时段】—— 单价为高峰时段的一半");
          if (h.priceLine) { L.push(h.priceLine); }
          if (hn && !d.peak) { L.push(d.holidayTodayText || ("📅 今天是【" + hn + "】—— 法定节假日全天按空闲价")); }
          if (d.holidayEveText) { L.push(d.holidayEveText); }
          if (d.holidayLastText) { L.push(d.holidayLastText); }
          L.push("");
          L.push("今日：高峰 ¥" + (money(d.todayPeak) || "0") + "（命中 " + pct(d.todayPeakHitRate) +
                 "）· 空闲 ¥" + (money(d.todayOffpeak) || "0") + "（命中 " + pct(d.todayOffpeakHitRate) +
                 "）· 全天 " + pct(d.todayHitRate));
          L.push("本周：高峰 ¥" + (money(d.weekPeak) || "0") + " · 空闲 ¥" + (money(d.weekOffpeak) || "0") + " · 平均命中 " + pct(d.weekHitRate));
          L.push("本月：高峰 ¥" + (money(d.monthPeak) || "0") + " · 空闲 ¥" + (money(d.monthOffpeak) || "0") + " · 平均命中 " + pct(d.monthHitRate));
          if (d.savingTip) { L.push(d.savingTip); }
          if (d.adviceTip) { L.push(d.adviceTip); }
          if (d.compactAdvice && d.compactAdvice.text) { L.push(d.compactAdvice.text); }
          if (d.ctxHint) { L.push(d.ctxHint); }
          if (d.nextSwitchText) { L.push(""); L.push(d.nextSwitchText); }
          L.push("");
          L.push("右键 → 规则 · 成本构成 · 锁定/复位");
          mk.setAttribute("title", L.join("\n"));
          // 压缩提醒图标（v1.9.0）：判据自己跑，值得压缩时才亮；点一下看结论
          var ic = document.getElementById("ds-alert-icon");
          if (ic) {
            applyAlertIcon(d);
          }
        }
        lastData = d;
        updateTitle();
        } catch (err) {
          try { console.error("[ds-spend-widget] 渲染出错（金额已保留）", err); } catch (_) {}
        }
      })
      .catch(function () {
        FIELDS.forEach(function (f) { setVal("ds-v-" + f.key, null, true); });
        var el = document.getElementById(MARK);
        if (el) {
          el.setAttribute("title",
            "本地数据服务未运行（见 skill 的 install.py start）\n" +
            (locked ? "🔒 位置已锁定（右键可解锁 / 换位置）"
                    : "拖动可移动 · 双击复位 · 右键可锁定位置"));
        }
      });
  }

  function start() {
    if (build()) {
      poll();
      // 挂到 window：热加载替换版本时能清掉旧定时器，避免两个轮询同时跑
      window.__dsSpendWidgetTimer = setInterval(poll, POLL_MS);
    } else {
      document.addEventListener("DOMContentLoaded", function () { build(); poll(); });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
