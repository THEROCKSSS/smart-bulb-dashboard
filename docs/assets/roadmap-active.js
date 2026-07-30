(function () {
  "use strict";
  var RD = window.RoadmapData;

  RD.fetchStatus().then(render).catch(function (err) {
    document.getElementById("active-error").hidden = false;
    document.getElementById("active-error").textContent =
      "Couldn't load roadmap-status.json (" + err.message + "). This page needs to be served over http(s), not opened as a file://.";
    document.getElementById("active-loading").hidden = true;
  });

  function render(data) {
    document.getElementById("active-loading").hidden = true;
    var sections = data.sections || [];
    var curWeek = RD.currentWeek(sections);

    renderSyncBadge(data.generatedAt);
    renderRings(sections, curWeek);
    renderKanban(sections, curWeek);
    renderTracker(sections);
  }

  function renderSyncBadge(generatedAt) {
    var el = document.getElementById("sync-badge");
    el.textContent = "NON-LIVE DATA · synced from GitHub Issues " + RD.relativeTime(generatedAt);
    el.title = "Last synced: " + generatedAt;
  }

  // ---------------------------------------------------------------- rings
  function renderRings(sections, curWeek) {
    var byWeek = RD.groupByWeek(sections);
    var wrap = document.getElementById("ring-row");
    wrap.innerHTML = "";
    [1, 2, 3, 4].forEach(function (w) {
      var list = byWeek[w] || [];
      var total = list.length;
      var closed = list.filter(function (s) { return s.status === "done" || s.status === "wontfix"; }).length;
      var started = list.filter(function (s) { return s.status === "in_progress"; }).length;
      var pct = total ? closed / total : 0;
      // The dim outer arc spans closed + in-progress, so a week that's fully
      // underway but nothing closed yet doesn't render as a flat, misleading
      // empty ring. The accent arc (closed only) still sits on top of it.
      var startedPct = total ? (closed + started) / total : 0;
      var r = 50, c = 2 * Math.PI * r;
      var offset = c * (1 - pct);
      var startedOffset = c * (1 - startedPct);

      var sub = closed + ' / ' + total + ' sections closed';
      if (started > 0) sub += ' · ' + started + ' in progress';

      var card = document.createElement("div");
      card.className = "ring-card" + (w === curWeek ? " is-current" : "");
      card.innerHTML =
        '<svg class="ring-svg" viewBox="0 0 120 120">' +
          '<circle class="ring-track" cx="60" cy="60" r="' + r + '"></circle>' +
          '<circle class="ring-progress" cx="60" cy="60" r="' + r + '" stroke-dasharray="' + c.toFixed(1) + '" stroke-dashoffset="' + startedOffset.toFixed(1) + '"></circle>' +
          '<circle class="ring-fill" cx="60" cy="60" r="' + r + '" stroke-dasharray="' + c.toFixed(1) + '" stroke-dashoffset="' + offset.toFixed(1) + '"></circle>' +
          '<text x="60" y="60">' + Math.round(pct * 100) + '%</text>' +
        '</svg>' +
        '<span class="ring-card__title">' + (w === curWeek ? "▸ " : "") + "Week " + w + '</span>' +
        '<span class="ring-card__sub">' + sub + '</span>';
      wrap.appendChild(card);
    });
  }

  // --------------------------------------------------------------- kanban
  var KANBAN_COLUMNS = ["blocked", "in_progress", "planned"];

  function renderKanban(sections, curWeek) {
    var board = document.getElementById("kanban-board");
    board.innerHTML = "";
    KANBAN_COLUMNS.forEach(function (status) {
      var meta = RD.statusMeta(status);
      var all = sections.filter(function (s) { return s.status === status; });
      var current = all.filter(function (s) { return s.week === curWeek; });
      var later = all.filter(function (s) { return s.week !== curWeek; });

      var col = document.createElement("div");
      col.className = "kanban-col";
      var head = document.createElement("div");
      head.className = "kanban-col__head";
      head.innerHTML =
        '<span class="kanban-col__title">' + RD.escapeHtml(meta.label) + '</span>' +
        '<span class="kanban-col__count">' + all.length + '</span>';
      col.appendChild(head);

      var cardsWrap = document.createElement("div");
      cardsWrap.className = "kanban-cards";

      if (all.length === 0) {
        var empty = document.createElement("p");
        empty.className = "empty-note";
        empty.style.margin = "0";
        empty.textContent = status === "blocked"
          ? "Nothing blocked right now."
          : status === "in_progress"
            ? "Nothing in progress right now — everything's still in planning."
            : "Nothing planned in this column.";
        cardsWrap.appendChild(empty);
      } else {
        current.forEach(function (s) { cardsWrap.appendChild(kanbanCard(s, false)); });
        if (current.length === 0 && later.length > 0) {
          var note = document.createElement("p");
          note.className = "empty-note";
          note.style.margin = "0";
          note.textContent = "Nothing in Week " + curWeek + " yet — see other weeks below.";
          cardsWrap.appendChild(note);
        }
      }
      col.appendChild(cardsWrap);

      if (later.length > 0) {
        var laterWrap = document.createElement("div");
        laterWrap.className = "kanban-cards";
        laterWrap.style.display = "none";
        laterWrap.style.marginTop = "0.75rem";
        later.forEach(function (s) { laterWrap.appendChild(kanbanCard(s, true)); });
        col.appendChild(laterWrap);

        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "kanban-see-more";
        btn.textContent = "See " + later.length + " more (other weeks)";
        btn.addEventListener("click", function () {
          var open = laterWrap.style.display !== "none";
          laterWrap.style.display = open ? "none" : "flex";
          laterWrap.style.flexDirection = "column";
          laterWrap.style.gap = "0.75rem";
          btn.textContent = open ? "See " + later.length + " more (other weeks)" : "Show less";
        });
        col.appendChild(btn);
      }

      board.appendChild(col);
    });
  }

  function kanbanCard(s, dim) {
    var a = document.createElement("a");
    a.href = s.url;
    a.target = "_blank";
    a.rel = "noopener";
    a.className = "kanban-card" + (dim ? " is-dim" : "");
    a.innerHTML =
      '<div class="kanban-card__title">' + RD.escapeHtml(s.section) + '</div>' +
      '<div class="kanban-card__meta">' +
        '<span>Week ' + s.week + '</span><span>·</span>' +
        '<span>' + (s.itemCount || (s.endNum - s.startNum + 1)) + ' items</span><span>·</span>' +
        '<span>#' + s.issueNumber + '</span>' +
      '</div>' +
      (s.description ? '<div class="kanban-card__desc">' + RD.escapeHtml(s.description) + '</div>' : '');
    return a;
  }

  // -------------------------------------------------------------- tracker
  var activeFilter = { status: "all", week: "all" };

  function renderTracker(sections) {
    var tbody = document.getElementById("tracker-body");
    tbody.innerHTML = "";
    sections
      .slice()
      .sort(function (a, b) { return (RD.statusMeta(a.status).order - RD.statusMeta(b.status).order) || (a.week - b.week) || (a.startNum - b.startNum); })
      .forEach(function (s) {
        var tr = document.createElement("tr");
        tr.dataset.status = s.status;
        tr.dataset.week = s.week;
        var meta = RD.statusMeta(s.status);
        tr.innerHTML =
          '<td><span class="tag ' + meta.tag + '">' + RD.escapeHtml(meta.label) + '</span></td>' +
          '<td>Week ' + s.week + '</td>' +
          '<td>' + RD.escapeHtml(s.section) + '</td>' +
          '<td class="num">' + (s.itemCount || (s.endNum - s.startNum + 1)) + '</td>' +
          '<td>' + RD.relativeTime(s.updatedAt) + '</td>' +
          '<td><a href="' + s.url + '" target="_blank" rel="noopener">#' + s.issueNumber + ' →</a></td>';
        tbody.appendChild(tr);
      });
    applyFilter();
  }

  function applyFilter() {
    var rows = document.querySelectorAll("#tracker-body tr");
    rows.forEach(function (tr) {
      var statusOk = activeFilter.status === "all" || tr.dataset.status === activeFilter.status;
      var weekOk = activeFilter.week === "all" || tr.dataset.week === activeFilter.week;
      tr.classList.toggle("is-hidden", !(statusOk && weekOk));
    });
  }

  document.addEventListener("click", function (e) {
    var chip = e.target.closest(".filter-chip");
    if (!chip) return;
    var group = chip.dataset.filterGroup;
    var value = chip.dataset.filterValue;
    document.querySelectorAll('.filter-chip[data-filter-group="' + group + '"]').forEach(function (c) {
      c.classList.toggle("is-active", c === chip);
    });
    activeFilter[group] = value;
    applyFilter();
  });
})();
