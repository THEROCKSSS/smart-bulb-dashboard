(function () {
  "use strict";
  var RD = window.RoadmapData;
  var WEEK_COLORS = {
    1: "oklch(70% 0.15 253)",
    2: "oklch(70% 0.15 320)",
    3: "oklch(75% 0.15 145)",
    4: "oklch(75% 0.15 75)",
  };
  var STATUS_COLORS = {
    blocked: "oklch(68% 0.17 25)",
    in_progress: "oklch(75% 0.15 75)",
    planned: "oklch(56% 0.01 250)",
    done: "oklch(76% 0.14 145)",
    wontfix: "oklch(40% 0.01 250)",
  };

  var allSections = [];
  var showAllRef = false;

  RD.fetchStatus().then(render).catch(function (err) {
    document.getElementById("archive-error").hidden = false;
    document.getElementById("archive-error").textContent =
      "Couldn't load roadmap-status.json (" + err.message + "). This page needs to be served over http(s), not opened as a file://.";
    document.getElementById("archive-loading").hidden = true;
  });

  function render(data) {
    document.getElementById("archive-loading").hidden = true;
    allSections = data.sections || [];

    var closed = allSections.filter(isClosed);
    var badge = document.getElementById("archive-sync-badge");
    if (closed.length === 0) {
      badge.classList.add("is-empty");
      badge.textContent = "NON-LIVE DATA · 0 of " + allSections.length + " sections closed so far — synced from GitHub Issues " + RD.relativeTime(data.generatedAt);
    } else {
      badge.textContent = "NON-LIVE DATA · " + closed.length + " of " + allSections.length + " sections closed — synced " + RD.relativeTime(data.generatedAt);
    }

    renderAnalytics(allSections);
    renderSunburst(allSections);
    renderTimeline();
    renderDatabase();

    document.getElementById("ref-toggle").addEventListener("change", function (e) {
      showAllRef = e.target.checked;
      renderTimeline();
      renderDatabase();
    });
  }

  function isClosed(s) { return s.status === "done" || s.status === "wontfix"; }

  // ------------------------------------------------------------- analytics
  function renderAnalytics(sections) {
    var counts = {};
    sections.forEach(function (s) { counts[s.status] = (counts[s.status] || 0) + 1; });
    var order = ["done", "in_progress", "blocked", "planned", "wontfix"];
    var total = sections.length;

    var barWrap = document.getElementById("bar-chart");
    barWrap.innerHTML = "";
    order.forEach(function (status) {
      var n = counts[status] || 0;
      if (n === 0 && status !== "planned" && status !== "done") return;
      var meta = RD.statusMeta(status);
      var pct = total ? (n / total) * 100 : 0;
      var row = document.createElement("div");
      row.className = "bar-chart__row";
      row.innerHTML =
        '<span class="bar-chart__label">' + RD.escapeHtml(meta.label) + '</span>' +
        '<span class="bar-chart__track"><span class="bar-chart__fill" style="width:' + pct.toFixed(1) + '%; background:' + STATUS_COLORS[status] + '"></span></span>' +
        '<span class="bar-chart__num">' + n + '</span>';
      barWrap.appendChild(row);
    });

    renderDonut(counts, total, order);
  }

  function renderDonut(counts, total, order) {
    var svg = document.getElementById("donut-svg");
    svg.innerHTML = "";
    var cx = 90, cy = 90, rInner = 45, rOuter = 85;
    var angle = 0;
    order.forEach(function (status) {
      var n = counts[status] || 0;
      if (!n) return;
      var span = (n / total) * 360;
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", arcPath(cx, cy, rInner, rOuter, angle, angle + span));
      path.setAttribute("style", "fill:" + STATUS_COLORS[status]);
      var meta = RD.statusMeta(status);
      var title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = meta.label + ": " + n + " (" + Math.round((n / total) * 100) + "%)";
      path.appendChild(title);
      svg.appendChild(path);
      angle += span;
    });
    var label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", cx); label.setAttribute("y", cy);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("dominant-baseline", "middle");
    label.setAttribute("style", "font-family:var(--font-display); font-weight:600; fill:var(--color-ink); font-size:1.4rem;");
    label.textContent = total;
    svg.appendChild(label);

    var legend = document.getElementById("donut-legend");
    legend.innerHTML = "";
    order.forEach(function (status) {
      var n = counts[status] || 0;
      if (!n) return;
      var meta = RD.statusMeta(status);
      var li = document.createElement("li");
      li.innerHTML = '<span class="swatch" style="background:' + STATUS_COLORS[status] + '"></span>' + RD.escapeHtml(meta.label) + " — " + n;
      legend.appendChild(li);
    });
  }

  // -------------------------------------------------------------- sunburst
  function polar(cx, cy, r, angleDeg) {
    var rad = ((angleDeg - 90) * Math.PI) / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
  }
  function arcPath(cx, cy, rInner, rOuter, a0, a1) {
    var largeArc = a1 - a0 > 180 ? 1 : 0;
    var p0 = polar(cx, cy, rOuter, a0), p1 = polar(cx, cy, rOuter, a1);
    var q1 = polar(cx, cy, rInner, a1), q0 = polar(cx, cy, rInner, a0);
    return ["M", p0.x, p0.y, "A", rOuter, rOuter, 0, largeArc, 1, p1.x, p1.y,
            "L", q1.x, q1.y, "A", rInner, rInner, 0, largeArc, 0, q0.x, q0.y, "Z"].join(" ");
  }

  function renderSunburst(sections) {
    var svg = document.getElementById("sunburst-svg");
    svg.innerHTML = "";
    var cx = 120, cy = 120;
    var totalItems = sections.reduce(function (a, s) { return a + s.itemCount; }, 0);
    var byWeek = RD.groupByWeek(sections);
    var angle = 0;

    [1, 2, 3, 4].forEach(function (w) {
      var list = (byWeek[w] || []).slice().sort(function (a, b) { return a.startNum - b.startNum; });
      var weekTotal = list.reduce(function (a, s) { return a + s.itemCount; }, 0);
      var weekSpan = totalItems ? (weekTotal / totalItems) * 360 : 0;

      var innerPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
      innerPath.setAttribute("d", arcPath(cx, cy, 42, 72, angle, angle + weekSpan));
      innerPath.setAttribute("style", "fill:" + WEEK_COLORS[w]);
      var t1 = document.createElementNS("http://www.w3.org/2000/svg", "title");
      t1.textContent = "Week " + w + ": " + weekTotal + " items (" + Math.round((weekTotal / totalItems) * 100) + "%)";
      innerPath.appendChild(t1);
      svg.appendChild(innerPath);

      var sub = angle;
      list.forEach(function (s) {
        var span = totalItems ? (s.itemCount / totalItems) * 360 : 0;
        var outerPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
        outerPath.setAttribute("d", arcPath(cx, cy, 74, 110, sub, sub + span));
        outerPath.setAttribute("style", "fill:" + STATUS_COLORS[s.status] + "; opacity:0.9");
        var t2 = document.createElementNS("http://www.w3.org/2000/svg", "title");
        t2.textContent = s.section + " — " + s.itemCount + " items — " + RD.statusMeta(s.status).label;
        outerPath.appendChild(t2);
        svg.appendChild(outerPath);
        sub += span;
      });

      angle += weekSpan;
    });

    var legend = document.getElementById("sunburst-legend");
    legend.innerHTML = "";
    [1, 2, 3, 4].forEach(function (w) {
      var li = document.createElement("li");
      li.innerHTML = '<span class="swatch" style="background:' + WEEK_COLORS[w] + '"></span>Week ' + w + " — 250 items (" + ((byWeek[w] || []).filter(isClosed).length) + " of " + (byWeek[w] || []).length + " sections closed)";
      legend.appendChild(li);
    });
  }

  // --------------------------------------------------------------- timeline
  function renderTimeline() {
    var wrap = document.getElementById("timeline");
    var emptyNote = document.getElementById("timeline-empty");
    var closed = allSections.filter(isClosed);
    var list = (showAllRef ? allSections : closed).slice().sort(function (a, b) { return (a.week - b.week) || (a.startNum - b.startNum); });

    wrap.innerHTML = "";
    if (closed.length === 0) {
      emptyNote.hidden = false;
    } else {
      emptyNote.hidden = true;
    }

    list.forEach(function (s) {
      var item = document.createElement("div");
      item.className = "timeline-item";
      var meta = RD.statusMeta(s.status);
      item.innerHTML =
        '<div class="timeline-item__dot" style="background:' + (isClosed(s) ? STATUS_COLORS[s.status] : "var(--color-rule-2)") + '"></div>';
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "timeline-item__card";
      btn.innerHTML =
        '<div class="timeline-item__title">' + RD.escapeHtml(s.section) + '</div>' +
        '<div class="timeline-item__meta">Week ' + s.week + ' · <span class="tag ' + meta.tag + '">' + RD.escapeHtml(meta.label) + '</span> · ' + s.itemCount + ' items</div>';
      btn.addEventListener("click", function () { openModal(s); });
      item.appendChild(btn);
      wrap.appendChild(item);
    });
  }

  function openModal(s) {
    var overlay = document.getElementById("detail-modal");
    var meta = RD.statusMeta(s.status);
    document.getElementById("modal-title").textContent = s.section;
    document.getElementById("modal-body").innerHTML =
      '<p><span class="tag ' + meta.tag + '">' + RD.escapeHtml(meta.label) + '</span> · Week ' + s.week + ' · ' + s.itemCount + ' items (W' + s.week + '-' + String(s.startNum).padStart(3, "0") + '–' + String(s.endNum).padStart(3, "0") + ')</p>' +
      (s.description ? '<p>' + RD.escapeHtml(s.description) + '</p>' : '') +
      '<p>Last updated ' + RD.relativeTime(s.updatedAt) + '.</p>' +
      '<p><a href="' + s.url + '" target="_blank" rel="noopener">View issue #' + s.issueNumber + ' on GitHub →</a></p>';
    overlay.hidden = false;
  }
  document.getElementById("modal-close").addEventListener("click", function () {
    document.getElementById("detail-modal").hidden = true;
  });
  document.getElementById("detail-modal").addEventListener("click", function (e) {
    if (e.target.id === "detail-modal") e.target.hidden = true;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.getElementById("detail-modal").hidden = true;
  });

  // --------------------------------------------------------------- database
  var dbSort = { col: "week", dir: 1 };
  var dbSearch = "";

  function renderDatabase() {
    var closed = allSections.filter(isClosed);
    var source = showAllRef ? allSections : closed;
    var emptyNote = document.getElementById("db-empty");
    emptyNote.hidden = closed.length !== 0;

    var q = dbSearch.toLowerCase();
    var filtered = source.filter(function (s) {
      return !q || s.section.toLowerCase().indexOf(q) !== -1 || (s.description || "").toLowerCase().indexOf(q) !== -1;
    });
    filtered.sort(function (a, b) {
      var av = a[dbSort.col], bv = b[dbSort.col];
      if (typeof av === "string") return av.localeCompare(bv) * dbSort.dir;
      return (av - bv) * dbSort.dir;
    });

    var tbody = document.getElementById("db-body");
    tbody.innerHTML = "";
    filtered.forEach(function (s) {
      var meta = RD.statusMeta(s.status);
      var tr = document.createElement("tr");
      tr.innerHTML =
        '<td>' + RD.escapeHtml(s.section) + '</td>' +
        '<td><span class="tag accent">Week ' + s.week + '</span> <span class="tag ' + meta.tag + '">' + RD.escapeHtml(meta.label) + '</span></td>' +
        '<td class="num">' + s.itemCount + '</td>' +
        '<td>' + RD.relativeTime(s.updatedAt) + '</td>' +
        '<td><a href="' + s.url + '" target="_blank" rel="noopener">#' + s.issueNumber + ' →</a></td>';
      tbody.appendChild(tr);
    });
  }

  document.getElementById("db-search").addEventListener("input", function (e) {
    dbSearch = e.target.value;
    renderDatabase();
  });
  document.querySelectorAll("#db-table th[data-sort]").forEach(function (th) {
    th.style.cursor = "pointer";
    th.addEventListener("click", function () {
      var col = th.dataset.sort;
      if (dbSort.col === col) dbSort.dir *= -1; else { dbSort.col = col; dbSort.dir = 1; }
      renderDatabase();
    });
  });
})();
