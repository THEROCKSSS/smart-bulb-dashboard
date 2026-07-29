/* Shared data layer for roadmap-active.html and roadmap-archive.html.
 * Loads docs/assets/roadmap-status.json (synced by .github/workflows/sync-roadmap-status.yml
 * from real GitHub issues — see .github/scripts/sync_roadmap_status.py) and exposes small
 * helpers both pages need: status labels/colors, week grouping, relative time, "current week".
 */
(function (global) {
  "use strict";

  var STATUS_META = {
    blocked:     { label: "Blocked",     tag: "danger",  order: 0 },
    in_progress: { label: "In Progress", tag: "warn",    order: 1 },
    planned:     { label: "Planned",     tag: "neutral", order: 2 },
    done:        { label: "Done",        tag: "success", order: 3 },
    wontfix:     { label: "Won't Fix",   tag: "neutral", order: 4 },
  };

  var WEEK_TITLES = {
    1: "Week 1 — Audio Depth & Multi-Bulb Orchestration",
    2: "Week 2 — Remote Access, Security & Infrastructure",
    3: "Week 3 — Integrations, Automation & Mobile UX",
    4: "Week 4 — Analytics, Bluetooth-Readiness, Polish & Release",
  };

  function statusMeta(status) {
    return STATUS_META[status] || { label: status, tag: "neutral", order: 9 };
  }

  function relativeTime(iso) {
    if (!iso) return "unknown";
    var then = new Date(iso).getTime();
    var now = Date.now();
    var diffMs = now - then;
    if (!isFinite(diffMs) || diffMs < 0) return "just now";
    var mins = Math.floor(diffMs / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + "m ago";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + "h ago";
    var days = Math.floor(hours / 24);
    if (days < 30) return days + "d ago";
    var months = Math.floor(days / 30);
    if (months < 12) return months + "mo ago";
    return Math.floor(months / 12) + "y ago";
  }

  function groupByWeek(sections) {
    var byWeek = { 1: [], 2: [], 3: [], 4: [] };
    sections.forEach(function (s) {
      if (!byWeek[s.week]) byWeek[s.week] = [];
      byWeek[s.week].push(s);
    });
    return byWeek;
  }

  // "Current week" = lowest-numbered week that still has open work (not done/wontfix).
  // Falls back to the highest week if every section across all weeks is closed out.
  function currentWeek(sections) {
    var weeks = [1, 2, 3, 4];
    for (var i = 0; i < weeks.length; i++) {
      var w = weeks[i];
      var hasOpen = sections.some(function (s) {
        return s.week === w && s.status !== "done" && s.status !== "wontfix";
      });
      if (hasOpen) return w;
    }
    return 4;
  }

  function fetchStatus() {
    return fetch("assets/roadmap-status.json", { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("roadmap-status.json fetch failed: " + r.status);
      return r.json();
    });
  }

  function escapeHtml(str) {
    return String(str == null ? "" : str).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  global.RoadmapData = {
    STATUS_META: STATUS_META,
    WEEK_TITLES: WEEK_TITLES,
    statusMeta: statusMeta,
    relativeTime: relativeTime,
    groupByWeek: groupByWeek,
    currentWeek: currentWeek,
    fetchStatus: fetchStatus,
    escapeHtml: escapeHtml,
  };
})(window);
