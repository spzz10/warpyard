/* Warpyard metrics charts — dependency-free SVG time-series (server page + admin
   Host page). Renders area charts from a metrics.json endpoint with a crosshair
   tooltip, timeframe presets and 30s auto-refresh (visible tab only). */
(function () {
  "use strict";

  var SERIES = { indigo: "#6a7dff", teal: "#1d9a8e", ochre: "#c9822e" }; // validated trio on --surface
  var H = 150; // plot height (px); width is responsive

  function fmtBytes(v, perSec) {
    if (v == null || isNaN(v)) return "—";
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (v >= 10 || v === 0 ? Math.round(v) : v.toFixed(1)) + " " + u[i] + (perSec ? "/s" : "");
  }
  function fmtPct(v) { return v == null || isNaN(v) ? "—" : (v >= 10 ? Math.round(v) : v.toFixed(1)) + "%"; }

  // clean axis ceiling: 1/2/5 × 10^n above max
  function niceCeil(v) {
    if (!(v > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v)));
    for (var i = 0; i < 4; i++) { var c = [1, 2, 5, 10][i] * p; if (c >= v) return c; }
    return 10 * p;
  }

  // clean ceiling in the unit the axis will display (1024-based), so byte ticks read 33/67/100 KB not 98
  function niceCeilBytes(v) {
    if (!(v > 0)) return 1024;
    var u = 1;
    while (v / u >= 1024) u *= 1024;
    return niceCeil(v / u) * u;
  }

  function timeLabel(t, tf) {
    var d = new Date(t * 1000);
    var hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    if (tf === "hour") return hm;
    if (tf === "day") return hm;
    return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()] + " " + hm;
  }

  function el(tag, attrs, parent) {
    var n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  /* One chart card: opts = {node, title, series:[{key,label,color}], fmt, fixedMax} */
  function Chart(opts) {
    this.o = opts;
    this.node = opts.node;
    this.node.classList.add("mx-card");
    this.node.innerHTML =
      '<div class="mx-head"><span class="mx-title"></span><span class="mx-now"></span></div>' +
      '<div class="mx-plot"></div><div class="mx-legend"></div><div class="mx-tip" hidden></div>';
    this.node.querySelector(".mx-title").textContent = opts.title;
    this.plot = this.node.querySelector(".mx-plot");
    this.tip = this.node.querySelector(".mx-tip");
    var lg = this.node.querySelector(".mx-legend");
    if (opts.series.length > 1) {
      opts.series.forEach(function (s) {
        var it = document.createElement("span");
        it.className = "mx-key";
        var sw = document.createElement("i");
        sw.style.background = s.color;
        it.appendChild(sw);
        it.appendChild(document.createTextNode(s.label));
        lg.appendChild(it);
      });
    } else lg.remove();
    this.points = [];
  }

  Chart.prototype.setNow = function (text) {
    this.node.querySelector(".mx-now").textContent = text;
  };

  Chart.prototype.render = function (points, tf) {
    this.points = points; this.tf = tf;
    var o = this.o, plot = this.plot;
    plot.textContent = "";
    var W = Math.max(plot.clientWidth, 240);
    var padL = 62, padR = 10, padT = 8, padB = 20;
    var iw = W - padL - padR, ih = H - padT - padB;

    var xs = points.map(function (p) { return p.t; });
    var x0 = Math.min.apply(null, xs), x1 = Math.max.apply(null, xs);
    if (!(x1 > x0)) { plot.innerHTML = '<div class="mx-empty">no data yet</div>'; return; }

    var maxV = 0;
    points.forEach(function (p) {
      o.series.forEach(function (s) { var v = p[s.key]; if (v != null && v > maxV) maxV = v; });
    });
    var yMax = o.fixedMax || (o.bytes ? niceCeilBytes(maxV * 1.08) : niceCeil(maxV * 1.08));

    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img" }, plot);
    var X = function (t) { return padL + ((t - x0) / (x1 - x0)) * iw; };
    var Y = function (v) { return padT + ih - (Math.min(v, yMax) / yMax) * ih; };

    // hairline grid + y ticks (3 lines: 0 is the baseline)
    for (var g = 1; g <= 3; g++) {
      var vy = Y(yMax * g / 3);
      el("line", { x1: padL, x2: W - padR, y1: vy, y2: vy, stroke: "var(--line)", "stroke-width": 1 }, svg);
      var ty = el("text", { x: padL - 8, y: vy + 3.5, "text-anchor": "end", class: "mx-tick" }, svg);
      ty.textContent = o.fmt(yMax * g / 3);
    }
    el("line", { x1: padL, x2: W - padR, y1: Y(0), y2: Y(0), stroke: "var(--line-2)", "stroke-width": 1 }, svg);

    // x time labels (4)
    for (var q = 0; q <= 3; q++) {
      var tt = x0 + (x1 - x0) * q / 3;
      var tx = el("text", {
        x: Math.min(Math.max(X(tt), padL + 12), W - padR - 12), y: H - 5,
        "text-anchor": q === 0 ? "start" : q === 3 ? "end" : "middle", class: "mx-tick"
      }, svg);
      tx.textContent = timeLabel(tt, tf);
    }

    // series: area wash (10%) + 2px line; null values break the path (no interpolation)
    o.series.forEach(function (s) {
      var line = "", area = "", open = false, lastX = null;
      points.forEach(function (p) {
        var v = p[s.key];
        if (v == null || isNaN(v)) {
          if (open && area) area += "L" + lastX + " " + Y(0) + "Z";
          open = false; return;
        }
        var px = X(p.t).toFixed(1), py = Y(v).toFixed(1);
        if (!open) { line += "M" + px + " " + py; area += "M" + px + " " + Y(0) + "L" + px + " " + py; open = true; }
        else { line += "L" + px + " " + py; area += "L" + px + " " + py; }
        lastX = px;
      });
      if (open && area) area += "L" + lastX + " " + Y(0) + "Z";
      el("path", { d: area, fill: s.color, "fill-opacity": 0.1 }, svg);
      el("path", { d: line, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    });

    // crosshair + hover dots (2px surface ring)
    var cross = el("line", { y1: padT, y2: padT + ih, stroke: "var(--line-2)", "stroke-width": 1, visibility: "hidden" }, svg);
    var dots = o.series.map(function (s) {
      return el("circle", { r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, svg);
    });

    var self = this;
    function nearest(clientX) {
      var r = svg.getBoundingClientRect();
      var t = x0 + ((clientX - r.left) / r.width * W - padL) / iw * (x1 - x0);
      var best = null, bd = Infinity;
      points.forEach(function (p) { var d = Math.abs(p.t - t); if (d < bd) { bd = d; best = p; } });
      return best;
    }
    function show(ev) {
      var p = nearest(ev.clientX);
      if (!p) return;
      var cx = X(p.t);
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
      cross.setAttribute("visibility", "visible");
      var rows = [];
      o.series.forEach(function (s, i) {
        var v = p[s.key];
        if (v == null || isNaN(v)) { dots[i].setAttribute("visibility", "hidden"); return; }
        dots[i].setAttribute("cx", cx); dots[i].setAttribute("cy", Y(v));
        dots[i].setAttribute("visibility", "visible");
        rows.push({ label: s.label, color: s.color, val: o.fmt(v) });
      });
      self.tip.textContent = "";
      var when = document.createElement("div");
      when.className = "mx-tip-t";
      when.textContent = timeLabel(p.t, tf);
      self.tip.appendChild(when);
      rows.forEach(function (row) {
        var d = document.createElement("div");
        d.className = "mx-tip-row";
        var key = document.createElement("i"); key.style.background = row.color;
        var val = document.createElement("b"); val.textContent = row.val;
        d.appendChild(key); d.appendChild(val);
        d.appendChild(document.createTextNode(o.series.length > 1 ? " " + row.label : ""));
        self.tip.appendChild(d);
      });
      self.tip.hidden = false;
      // anchor relative to the card (the tip's offset parent), flipping side near the right edge
      var cr = self.node.getBoundingClientRect(), pr = svg.getBoundingClientRect();
      var tw = self.tip.offsetWidth;
      var lx = pr.left - cr.left + (cx / W) * pr.width;
      var left = lx + 12 + tw > cr.width - 8 ? lx - tw - 12 : lx + 12;
      self.tip.style.left = Math.max(left, 4) + "px";
      self.tip.style.top = pr.top - cr.top + 6 + "px";
    }
    function hide() {
      cross.setAttribute("visibility", "hidden");
      dots.forEach(function (d) { d.setAttribute("visibility", "hidden"); });
      self.tip.hidden = true;
    }
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerleave", hide);
  };

  /* Bootstrapping: wyMetrics(rootEl) — rootEl carries data-url; children carry data-chart. */
  window.wyMetrics = function (root) {
    var url = root.dataset.url;
    var charts = {
      cpu: new Chart({
        node: root.querySelector('[data-chart="cpu"]'), title: "CPU",
        series: [{ key: "cpu", label: "CPU", color: SERIES.indigo }], fmt: fmtPct, fixedMax: 100
      }),
      mem: new Chart({
        node: root.querySelector('[data-chart="mem"]'), title: "Memory",
        series: [{ key: "mem", label: "Used", color: SERIES.indigo }], fmt: fmtBytes, bytes: true
      }),
      net: new Chart({
        node: root.querySelector('[data-chart="net"]'), title: "Network",
        series: [
          { key: "netin", label: "In", color: SERIES.indigo },
          { key: "netout", label: "Out", color: SERIES.teal }
        ],
        fmt: function (v) { return fmtBytes(v, true); }, bytes: true
      })
    };
    var tf = "hour", timer = null, inflight = false;

    function load() {
      if (inflight) return;
      inflight = true;
      root.classList.add("mx-loading"); // previous render held at reduced opacity
      fetch(url + "?timeframe=" + tf, { headers: { Accept: "application/json" } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) {
          var pts = data.points || [];
          var now = data.now || {};
          // memory reads best against the VM's total RAM, not an auto ceiling
          if (now.maxmem) charts.mem.o.fixedMax = now.maxmem;
          charts.cpu.render(pts, tf); charts.mem.render(pts, tf); charts.net.render(pts, tf);
          charts.cpu.setNow(fmtPct(now.cpu));
          charts.mem.setNow(now.mem != null && now.maxmem ? fmtBytes(now.mem) + " of " + fmtBytes(now.maxmem) : "—");
          charts.net.setNow((now.netin != null ? "↓ " + fmtBytes(now.netin, true) : "—") +
            (now.netout != null ? "  ↑ " + fmtBytes(now.netout, true) : ""));
        })
        .catch(function () {
          Object.keys(charts).forEach(function (k) {
            charts[k].plot.innerHTML = '<div class="mx-empty">metrics unavailable</div>';
          });
        })
        .then(function () { root.classList.remove("mx-loading"); inflight = false; });
    }

    root.querySelectorAll("[data-tf]").forEach(function (b) {
      b.addEventListener("click", function () {
        tf = b.dataset.tf;
        root.querySelectorAll("[data-tf]").forEach(function (x) {
          x.setAttribute("aria-pressed", x === b ? "true" : "false");
        });
        load();
      });
    });

    var ro = new ResizeObserver(function () {
      Object.keys(charts).forEach(function (k) {
        if (charts[k].points.length) charts[k].render(charts[k].points, tf);
      });
    });
    ro.observe(root);

    function tick() { if (document.visibilityState === "visible") load(); }
    timer = setInterval(tick, 30000);
    document.addEventListener("visibilitychange", tick);
    load();
    return { stop: function () { clearInterval(timer); } };
  };

  /* Admin Host page: node-wide charts from /host/metrics.json — same contract as
     wyMetrics (rootEl carries data-url; children carry data-chart; [data-tf] presets). */
  window.wyHost = function (root) {
    var url = root.dataset.url;
    function fmtLoad(v) { return v == null || isNaN(v) ? "—" : (v >= 10 ? v.toFixed(1) : v.toFixed(2)); }
    var charts = {
      cpu: new Chart({
        node: root.querySelector('[data-chart="cpu"]'), title: "CPU",
        series: [
          { key: "cpu", label: "CPU", color: SERIES.indigo },
          { key: "iowait", label: "IO wait", color: SERIES.teal }
        ],
        fmt: fmtPct, fixedMax: 100
      }),
      load: new Chart({
        node: root.querySelector('[data-chart="load"]'), title: "Load average",
        series: [{ key: "load", label: "1m load", color: SERIES.indigo }], fmt: fmtLoad
      }),
      mem: new Chart({
        node: root.querySelector('[data-chart="mem"]'), title: "Memory",
        series: [
          { key: "mem", label: "Used", color: SERIES.indigo },
          { key: "arc", label: "ZFS ARC", color: SERIES.teal }
        ],
        fmt: fmtBytes, bytes: true
      }),
      pressure: new Chart({
        node: root.querySelector('[data-chart="pressure"]'), title: "Pressure (PSI, some)",
        series: [
          { key: "cpupsi", label: "CPU", color: SERIES.indigo },
          { key: "mempsi", label: "Memory", color: SERIES.teal },
          { key: "iopsi", label: "IO", color: SERIES.ochre }
        ],
        fmt: fmtPct
      }),
      net: new Chart({
        node: root.querySelector('[data-chart="net"]'), title: "Network",
        series: [
          { key: "netin", label: "In", color: SERIES.indigo },
          { key: "netout", label: "Out", color: SERIES.teal }
        ],
        fmt: function (v) { return fmtBytes(v, true); }, bytes: true
      })
    };
    var tf = "hour", timer = null, inflight = false;

    function load() {
      if (inflight) return;
      inflight = true;
      root.classList.add("mx-loading");
      fetch(url + "?timeframe=" + tf, { headers: { Accept: "application/json" } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) {
          var pts = data.points || [];
          var now = data.now || {};
          if (now.memtotal) charts.mem.o.fixedMax = now.memtotal; // read against the host's real RAM
          Object.keys(charts).forEach(function (k) { charts[k].render(pts, tf); });
          charts.cpu.setNow(fmtPct(now.cpu) + (now.iowait != null ? " · io " + fmtPct(now.iowait) : ""));
          charts.load.setNow(fmtLoad(now.load));
          charts.mem.setNow(now.mem != null && now.memtotal
            ? fmtBytes(now.mem) + " of " + fmtBytes(now.memtotal) + (now.arc != null ? " · ARC " + fmtBytes(now.arc) : "")
            : "—");
          charts.pressure.setNow("io " + fmtPct(now.iopsi));
          charts.net.setNow((now.netin != null ? "↓ " + fmtBytes(now.netin, true) : "—") +
            (now.netout != null ? "  ↑ " + fmtBytes(now.netout, true) : ""));
        })
        .catch(function () {
          Object.keys(charts).forEach(function (k) {
            charts[k].plot.innerHTML = '<div class="mx-empty">metrics unavailable</div>';
          });
        })
        .then(function () { root.classList.remove("mx-loading"); inflight = false; });
    }

    root.querySelectorAll("[data-tf]").forEach(function (b) {
      b.addEventListener("click", function () {
        tf = b.dataset.tf;
        root.querySelectorAll("[data-tf]").forEach(function (x) {
          x.setAttribute("aria-pressed", x === b ? "true" : "false");
        });
        load();
      });
    });

    var ro = new ResizeObserver(function () {
      Object.keys(charts).forEach(function (k) {
        if (charts[k].points.length) charts[k].render(charts[k].points, tf);
      });
    });
    ro.observe(root);

    function tick() { if (document.visibilityState === "visible") load(); }
    timer = setInterval(tick, 30000);
    document.addEventListener("visibilitychange", tick);
    load();
    return { stop: function () { clearInterval(timer); } };
  };

  /* Uptime + response-time from PoppaPing — rootEl carries data-url, holds
     [data-stat="uptime"], [data-stat="avg"], [data-chart="rt"], [data-tf] period buttons. */
  window.wyMonitor = function (root) {
    var url = root.dataset.url;
    var TF_LABEL = { "24h": "hour", "7d": "day", "30d": "week" };  // reuse the time-axis labelers
    var rt = new Chart({
      node: root.querySelector('[data-chart="rt"]'), title: "Response time",
      series: [{ key: "ms", label: "ms", color: SERIES.indigo }],
      fmt: function (v) { return v == null ? "—" : Math.round(v) + " ms"; }
    });
    var upEl = root.querySelector('[data-stat="uptime"]');
    var avgEl = root.querySelector('[data-stat="avg"]');
    var period = "24h", inflight = false;

    function load() {
      if (inflight) return;
      inflight = true;
      root.classList.add("mx-loading");
      fetch(url + "?period=" + period, { headers: { Accept: "application/json" } })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (d) {
          rt.render(d.points || [], TF_LABEL[period] || "day");
          rt.setNow(d.avg_ms != null ? Math.round(d.avg_ms) + " ms avg" : "—");
          if (upEl) {
            var pct = d.uptime_pct;
            upEl.textContent = pct == null ? "—" : (pct >= 99.95 ? "100" : pct.toFixed(2)) + "%";
            upEl.style.color = pct == null ? "" : pct >= 99 ? "var(--green)" : pct >= 95 ? "var(--amber)" : "var(--red)";
          }
          if (avgEl) avgEl.textContent = (d.up || 0) + " up · " + (d.down || 0) + " down (" + (d.total || 0) + " checks)";
        })
        .catch(function () {
          rt.plot.innerHTML = '<div class="mx-empty">no data yet — checks run every few minutes</div>';
          if (upEl) upEl.textContent = "—";
        })
        .then(function () { root.classList.remove("mx-loading"); inflight = false; });
    }

    root.querySelectorAll("[data-tf]").forEach(function (b) {
      b.addEventListener("click", function () {
        period = b.dataset.tf;
        root.querySelectorAll("[data-tf]").forEach(function (x) {
          x.setAttribute("aria-pressed", x === b ? "true" : "false");
        });
        load();
      });
    });
    new ResizeObserver(function () { if (rt.points.length) rt.render(rt.points, TF_LABEL[period] || "day"); }).observe(root);
    load();
    return { reload: load };
  };
})();
