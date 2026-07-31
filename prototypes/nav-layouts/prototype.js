/* PROTOTYPE — throwaway. Builds one of 50 layout skeletons into #stage.
   Mock content only; no backend calls. */
(function () {
  "use strict";

  var V = window.VARIANTS, PAGES = window.PAGES;
  var stage = document.getElementById("stage");
  var favs = load();

  function load() {
    try { return JSON.parse(localStorage.getItem("navproto.favs") || "[]"); }
    catch (e) { return []; }
  }
  function save() {
    try { localStorage.setItem("navproto.favs", JSON.stringify(favs)); } catch (e) {}
  }

  function current() {
    var n = parseInt(new URLSearchParams(location.search).get("v"), 10);
    return V.find(function (x) { return x.n === n; }) || V[0];
  }
  function activePage() {
    var p = new URLSearchParams(location.search).get("p");
    return PAGES.find(function (x) { return x.id === p; }) || PAGES[0];
  }
  function go(n, pageId) {
    var q = new URLSearchParams(location.search);
    q.set("v", n);
    if (pageId) q.set("p", pageId); else q.delete("p");
    location.search = q.toString();
  }

  // ---------------------------------------------------------- mock content
  function contentFor(page, sub) {
    var body = {
      light: [
        card("Power", '<button class="btn primary big">TURN OFF</button><div class="swatch"></div>'),
        card("Brightness", slider("Brightness", "82%")),
        card("Colour (HSV)", slider("Hue", "74°", "hue") + slider("Saturation", "100%")),
      ],
      audio: [
        card("Live Session", '<span class="pill on">RUNNING</span> <span class="pill">mirror_mode</span> <span class="pill">128 BPM</span>' + bars()),
        card("Mode", '<select class="sel"><option>Mirror Mode</option><option>Crescendo Ramp</option><option>Bass-Only Pulse</option></select>'),
      ],
      automation: [
        card("Sleep Timer", '<div class="row">' + ["5 min", "15 min", "30 min", "60 min"].map(function (t) { return '<button class="btn">' + t + '</button>'; }).join("") + '</div>'),
        card("Wake Timer (sunrise alarm)", field("Time", "07:00 AM") + field("Fade", "10 min")),
        card("Schedule", rows([["18:00", "Turn On"], ["23:30", "Turn Off"], ["07:00", "Scene: Sunrise"]])),
      ],
      rooms: [
        card("Groups", '<strong>All Bulbs</strong> <span class="muted">(1 device)</span><div class="row"><button class="btn">All On</button><button class="btn">All Off</button></div>'),
        card("Zones", '<strong>Upstairs</strong> <span class="pill">2 bulbs</span><br><strong>Living Room</strong> <span class="pill">1 bulb</span>'),
      ],
      system: [
        card("History", rows([["12:04", "power → on"], ["12:02", "brightness → 82%"], ["11:58", "scene → Sunset"]])),
        card("Diagnostics", '<span class="pill on">ONLINE</span> <span class="muted">186 ms · v3.5 · 192.168.0.134</span>'),
      ],
    }[page.id];
    var head = '<div class="page-head"><h1>' + page.label + (sub ? ' <span class="muted">/ ' + sub + '</span>' : "") + "</h1></div>";
    return head + body.join("");
  }

  function card(t, inner) { return '<div class="card"><h3>' + t + "</h3>" + inner + "</div>"; }
  function slider(l, v, cls) {
    return '<div class="sl"><label><span>' + l + "</span><span>" + v + '</span></label><div class="track ' + (cls || "") + '"><i></i></div></div>';
  }
  function field(l, v) { return '<div class="fld"><label>' + l + "</label><div class=inp>" + v + "</div></div>"; }
  function rows(rs) {
    return '<table class="tbl">' + rs.map(function (r) { return "<tr><td>" + r[0] + "</td><td>" + r[1] + "</td></tr>"; }).join("") + "</table>";
  }
  function bars() {
    var h = [38, 72, 55, 90, 44, 66, 30, 80, 52, 61];
    return '<div class="bars">' + h.map(function (x) { return '<i style="height:' + x + '%"></i>'; }).join("") + "</div>";
  }

  // ------------------------------------------------------- nav fragments
  function navItems(active, opts) {
    opts = opts || {};
    return PAGES.map(function (p) {
      return '<a class="nav-item' + (p.id === active.id ? " active" : "") + '" href="?v=' + current().n + "&p=" + p.id + '">' +
        (opts.icon ? '<span class="ic">' + p.icon + "</span>" : "") +
        (opts.label === false ? "" : '<span class="lb">' + p.label + "</span>") +
        (opts.desc ? '<span class="ds">' + p.subs.join(" · ") + "</span>" : "") +
        (opts.dot && (p.id === "audio" || p.id === "automation") ? '<span class="livedot"></span>' : "") +
        (opts.key ? '<kbd>' + (PAGES.indexOf(p) + 1) + "</kbd>" : "") +
        "</a>";
    }).join("");
  }
  function subTabs(page) {
    if (!page.subs.length) return "";
    return '<div class="subtabs">' + page.subs.map(function (s, i) {
      return '<button class="subtab' + (i === 0 ? " active" : "") + '">' + s + "</button>";
    }).join("") + "</div>";
  }
  function topbar(extra) {
    return '<div class="topbar"><div class="brand">Smart<span>Bulb</span></div>' +
      (extra || "") +
      '<div class="dev"><span class="pill on">LIVE</span><select class="sel"><option>Bytech A19 Bulb</option></select></div></div>';
  }

  // ------------------------------------------------------------ families
  var BUILD = {
    sidebar: function (p, o) {
      return '<div class="app fam-sidebar">' + topbar() +
        '<nav class="rail">' + navItems(p, o) + "</nav>" +
        '<main class="main">' + subTabs(p) + contentFor(p) + "</main></div>";
    },
    topnav: function (p, o) {
      return '<div class="app fam-topnav">' +
        topbar('<nav class="tnav">' + navItems(p, o) + "</nav>") +
        '<main class="main">' + subTabs(p) + contentFor(p) + "</main></div>";
    },
    bottom: function (p, o) {
      return '<div class="app fam-bottom">' +
        '<div class="minihead">' + p.label + "</div>" +
        '<main class="main">' + contentFor(p) + "</main>" +
        '<nav class="bbar">' + navItems(p, o) + "</nav></div>";
    },
    grid: function (p, o) {
      return '<div class="app fam-grid">' + topbar() +
        '<main class="main"><div class="tiles">' + PAGES.map(function (x) {
          return '<a class="tile' + (x.id === p.id ? " active" : "") + '" href="?v=' + current().n + "&p=" + x.id + '">' +
            '<span class="ic">' + x.icon + "</span><strong>" + x.label + "</strong>" +
            '<span class="muted">' + x.subs.join(" · ") + "</span></a>";
        }).join("") + "</div>" + contentFor(p) + "</main></div>";
    },
    command: function (p, o) {
      return '<div class="app fam-command">' +
        '<div class="cmdbar"><span class="cmdk">⌘K</span><input class="cmdinput" placeholder="Jump to a page or run a command…" value=""><span class="muted">' + p.label + "</span></div>" +
        '<div class="cmdlist">' + navItems(p, { key: true }) + "</div>" +
        '<main class="main">' + contentFor(p) + "</main></div>";
    },
    radial: function (p) {
      var r = PAGES.map(function (x, i) {
        var a = (i / PAGES.length) * Math.PI * 2 - Math.PI / 2;
        return '<a class="rad' + (x.id === p.id ? " active" : "") + '" style="left:calc(50% + ' + Math.cos(a) * 120 + 'px);top:calc(50% + ' + Math.sin(a) * 120 + 'px)" href="?v=' + current().n + "&p=" + x.id + '"><span class="ic">' + x.icon + "</span>" + x.label + "</a>";
      }).join("");
      return '<div class="app fam-radial">' + topbar() +
        '<main class="main"><div class="radwrap"><div class="radhub">' + p.icon + "</div>" + r + "</div>" + contentFor(p) + "</main></div>";
    },
    carousel: function (p) {
      return '<div class="app fam-carousel">' + topbar() +
        '<main class="main"><div class="cartrack">' + PAGES.map(function (x) {
          return '<section class="carpage' + (x.id === p.id ? " active" : "") + '">' + contentFor(x) + "</section>";
        }).join("") + "</div><div class='dots'>" + PAGES.map(function (x) {
          return '<a class="dot' + (x.id === p.id ? " active" : "") + '" href="?v=' + current().n + "&p=" + x.id + '"></a>';
        }).join("") + "</div></main></div>";
    },
    accordion: function (p) {
      return '<div class="app fam-accordion">' + topbar() +
        '<main class="main">' + PAGES.map(function (x) {
          var open = x.id === p.id;
          return '<section class="acc' + (open ? " open" : "") + '">' +
            '<a class="acchead" href="?v=' + current().n + "&p=" + x.id + '"><span class="ic">' + x.icon + "</span>" + x.label + "<span class='chev'>" + (open ? "▾" : "▸") + "</span></a>" +
            (open ? '<div class="accbody">' + contentFor(x) + "</div>" : "") + "</section>";
        }).join("") + "</main></div>";
    },
    split: function (p, o) {
      return '<div class="app fam-split">' + topbar() +
        '<nav class="rail">' + navItems(p, o) + "</nav>" +
        '<div class="pinned">' + card("Power", '<button class="btn primary big">TURN OFF</button>') + card("Brightness", slider("Brightness", "82%")) + "</div>" +
        '<main class="main">' + subTabs(p) + contentFor(p) + "</main></div>";
    },
    scroll: function (p) {
      return '<div class="app fam-scroll">' + topbar('<nav class="tnav">' + navItems(p) + "</nav>") +
        '<main class="main">' + PAGES.map(function (x) {
          return '<section class="scrollsec" id="s-' + x.id + '">' + contentFor(x) + "</section>";
        }).join("") + "</main></div>";
    },
  };

  // Per-variant options that change what the shared skeleton renders.
  var OPTS = {
    "v-iconrail": { icon: true }, "v-dense": { icon: true }, "v-wide": { desc: true },
    "v-raildots": { icon: true, dot: true }, "v-twotier": { icon: true },
    "v-bottom": { icon: true }, "v-bottomicon": { icon: true, label: false },
    "v-bottompill": { icon: true }, "v-arc": { icon: true }, "v-fab": { icon: true },
    "v-strip": { icon: true }, "v-adaptive": { icon: true }, "v-keys": { key: true },
    "v-launcher": { icon: true }, "v-vertical": { label: true },
    "v-bottomtitle": { icon: true }, "v-springboard": { icon: true },
  };

  function render() {
    var v = current(), p = activePage();
    var build = BUILD[v.family] || BUILD.sidebar;
    stage.className = "stage " + v.css;
    stage.innerHTML = build(p, OPTS[v.css] || {});
    document.getElementById("note").textContent = "#" + v.n + " " + v.name + " — " + v.note;
    document.getElementById("pick").value = v.n;
    document.getElementById("rate").textContent = favs.indexOf(v.n) >= 0 ? "★" : "☆";
    document.getElementById("rate").classList.toggle("on", favs.indexOf(v.n) >= 0);
  }

  // ------------------------------------------------------------- switcher
  var pick = document.getElementById("pick");
  V.forEach(function (v) {
    var o = document.createElement("option");
    o.value = v.n; o.textContent = "#" + v.n + " — " + v.name;
    pick.appendChild(o);
  });
  pick.onchange = function () { go(parseInt(pick.value, 10), activePage().id); };
  document.getElementById("prev").onclick = function () {
    var i = V.findIndex(function (x) { return x.n === current().n; });
    go(V[(i - 1 + V.length) % V.length].n, activePage().id);
  };
  document.getElementById("next").onclick = function () {
    var i = V.findIndex(function (x) { return x.n === current().n; });
    go(V[(i + 1) % V.length].n, activePage().id);
  };
  document.getElementById("rate").onclick = function () {
    var n = current().n, i = favs.indexOf(n);
    if (i >= 0) favs.splice(i, 1); else favs.push(n);
    favs.sort(function (a, b) { return a - b; });
    save(); render(); buildOverview();
  };

  document.addEventListener("keydown", function (e) {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.key === "ArrowRight") document.getElementById("next").click();
    if (e.key === "ArrowLeft") document.getElementById("prev").click();
    if (e.key === "f") document.getElementById("rate").click();
    if (e.key === "a") document.getElementById("gridview").click();
    if (e.key === "Escape") closeOver();
  });

  // ------------------------------------------------------------- overview
  var over = document.getElementById("overview");
  function openOver() { over.hidden = false; buildOverview(); }
  function closeOver() { over.hidden = true; }
  document.getElementById("gridview").onclick = openOver;
  document.getElementById("closeover").onclick = closeOver;

  function buildOverview() {
    document.getElementById("favcount").textContent = favs.length ? "· " + favs.length + " favourited" : "";
    document.getElementById("overgrid").innerHTML = V.map(function (v) {
      return '<a class="ovcard' + (favs.indexOf(v.n) >= 0 ? " fav" : "") + '" href="?v=' + v.n + '">' +
        '<div class="ovthumb ' + v.css + '"><span class="ovfam">' + v.family + "</span></div>" +
        '<div class="ovmeta"><strong>#' + v.n + " " + v.name + "</strong>" +
        (favs.indexOf(v.n) >= 0 ? '<span class="star">★</span>' : "") +
        '<span class="muted">' + v.note + "</span></div></a>";
    }).join("");
  }
  document.getElementById("copyfavs").onclick = function () {
    var txt = favs.length
      ? favs.map(function (n) { var v = V.find(function (x) { return x.n === n; }); return "#" + v.n + " " + v.name + " (" + v.family + ")"; }).join("\n")
      : "(none favourited yet)";
    navigator.clipboard.writeText(txt);
    this.textContent = "Copied ✓";
    var b = this; setTimeout(function () { b.textContent = "Copy favourites"; }, 1400);
  };

  render();
})();
