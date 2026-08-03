const API = "";

const state = {
  deviceId: null,
  devices: [],
  presets: [],
  scenes: [],
  effects: [],
  lastStatus: null,
  hasPolledOnce: false,
  statusPollHandle: null,
  consecutiveOfflinePolls: 0,
  audioPollHandle: null,
  lastSeenAt: null,          // ms epoch timestamp of the last successful "online" status poll
  lastSeenTickHandle: null,  // 1s UI-only ticker that re-renders the "last seen Xs ago" text between real polls
  sleepCountdownHandle: null, // 1s client-side tick for the sleep-timer countdown
  wakeCountdownHandle: null,  // 1s client-side tick for the wake-timer countdown
  timersResyncHandle: null,   // ~30s real re-fetch of sleep/wake timer state
};

// Reference to the currently-attached Control-panel keydown listener (if any),
// so it can be removed the moment another panel is routed to. Kept outside
// `state` since it's a DOM wiring detail, not app data.
let controlKeyHandler = null;
// Pending debounced brightness commit from the Control panel's arrow-key
// shortcuts. Cleared alongside the listener in router(), so navigating away
// mid-debounce can't fire a POST against a slider that's no longer mounted.
let controlKeyCommitTimer = null;

// ------------------------------------------------------------ local storage --
// Remembers the last device + panel across reloads. Wrapped in try/catch
// because localStorage can throw (private browsing, disabled storage, etc.);
// degrading to "no persistence" is preferable to a crash.
const LS_KEY_DEVICE = "sbd.lastDeviceId";
const LS_KEY_ROUTE = "sbd.lastRoute";

function lsGet(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}
function lsSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* storage unavailable — ignore */ }
}

// ---------------------------------------------------------------- utils --
// `onUndo`, if given, renders an "Undo" action in the toast. Clicking it within
// the toast's lifetime runs the callback and dismisses the toast; once the
// toast auto-removes, the action is simply gone — no stale button, no error.
function toast(msg, kind, onUndo) {
  const stack = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");

  const body = document.createElement("div");
  body.className = "toast-body";
  const msgSpan = document.createElement("span");
  msgSpan.textContent = msg;
  body.appendChild(msgSpan);

  if (typeof onUndo === "function") {
    const undoBtn = document.createElement("button");
    undoBtn.type = "button";
    undoBtn.className = "toast-undo";
    undoBtn.textContent = "Undo";
    undoBtn.onclick = () => {
      el.remove();
      onUndo();
    };
    body.appendChild(undoBtn);
  }

  el.appendChild(body);
  stack.appendChild(el);
  // Give undo-able toasts a bit longer on screen so the ~5s undo window is
  // actually usable rather than the button vanishing before it's up.
  setTimeout(() => el.remove(), onUndo ? 5000 : 4200);
}

// ----------------------------------------------- copy-to-clipboard buttons --
// Mirrors the docs/assets/main.js convention: a `.copy-btn` with the literal
// text to copy in `data-copy-text`, a brief "Copied" confirmation, then revert.
function flashCopied(btn) {
  const original = btn.textContent;
  btn.textContent = "Copied";
  btn.classList.add("copied");
  clearTimeout(btn._copyResetTimer);
  btn._copyResetTimer = setTimeout(() => {
    btn.textContent = original;
    btn.classList.remove("copied");
  }, 1600);
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const text = btn.dataset.copyText || "";
  navigator.clipboard.writeText(text)
    .then(() => flashCopied(btn))
    .catch(() => toast("Could not copy to clipboard", "error"));
});

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  try {
    const res = await fetch(API + path, opts);
    if (res.status === 401 && path !== "/api/auth/login") {
      showPinGate();
      throw new Error("authentication required");
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  } catch (e) {
    if (e.message !== "authentication required") toast(`Error: ${e.message}`, "error");
    throw e;
  }
}

function get(path) { return api(path); }
function post(path, body) { return api(path, { method: "POST", body: JSON.stringify(body || {}) }); }
function patch(path, body) { return api(path, { method: "PATCH", body: JSON.stringify(body || {}) }); }
function del(path) { return api(path, { method: "DELETE" }); }

// Zone names, session-preset names and device names are user-supplied and get
// interpolated into innerHTML, so escape them. `escAttr` additionally escapes
// quotes because those values also land inside data-* attributes.
function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escAttr(s) {
  return escHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function rgbToHex(r, g, b) {
  return "#" + [r, g, b].map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("");
}

function hsvToRgb(h, s, v) {
  s /= 100; v /= 100;
  const c = v * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = v - c;
  let r, g, b;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

// -------------------------------------------------------------- routing --
// Five top-level pages instead of eleven sidebar entries. Two shapes here,
// deliberately:
//   - `tabs`   : the page's sections are each large enough to deserve their own
//                view, so they get an in-page sub-tab bar (and their own
//                bookmarkable `#/page/sub` hash).
//   - `merged` : the sections are small enough to genuinely live on ONE
//                scrolling page, stacked. No sub-tabs at all.
// Scenes+Effects and Timers+Schedule were the specific merges asked for; both
// are pairs that answer the same user question, so splitting them was noise.
const PAGES = {
  light: {
    label: "Light",
    tabs: [
      { id: "control", label: "Control", render: renderControl },
      { id: "looks", label: "Scenes & Effects", render: renderLooks },
      { id: "presets", label: "Presets & Favorites", render: renderPresets },
    ],
  },
  audio: {
    label: "Audio",
    tabs: [
      { id: "session", label: "Live Session", render: renderAudio },
      { id: "presets", label: "Session Presets", render: renderSessionPresets },
    ],
  },
  automation: { label: "Automation", merged: renderAutomation },
  rooms: { label: "Rooms", merged: renderRooms },
  system: {
    label: "System",
    tabs: [
      { id: "history", label: "History", render: renderHistory },
      // Health is the BACKEND's own health (uptime, dependencies, request
      // latency, logs); Diagnostics stays what it always was — "is this
      // one bulb reachable". Two genuinely different questions, so two
      // tabs rather than one crowded one.
      { id: "health", label: "Health", render: renderHealth },
      { id: "diagnostics", label: "Diagnostics", render: renderDiagnostics },
      // Security sits next to History deliberately: History is "what did the
      // bulbs do", Security is "what happened to this install". Same page,
      // two different questions, never merged into one noisy feed.
      { id: "security", label: "Security Log", render: renderSecurity },
      { id: "backup", label: "Backup", render: renderBackup },
      { id: "settings", label: "Settings", render: renderSettings },
      { id: "docs", label: "Docs", render: renderDocs },
    ],
  },
};

// Old one-tab-per-panel hashes still work — bookmarks, the remembered
// last-route in localStorage, and any link written before the consolidation
// all resolve to wherever that panel lives now.
const LEGACY_ROUTES = {
  control: "light/control",
  scenes: "light/looks",
  effects: "light/looks",
  presets: "light/presets",
  audio: "audio/session",
  timers: "automation",
  schedule: "automation",
  groups: "rooms",
  zones: "rooms",
  history: "system/history",
  health: "system/health",
  diagnostics: "system/diagnostics",
  settings: "system/settings",
  security: "system/security",
  backup: "system/backup",
};

const DEFAULT_ROUTE = "light/control";

// Does a stored "page/sub" (or a legacy single-panel name) still resolve to a
// real page? Used to validate the remembered route before restoring it, so a
// route saved by an older build can't send boot somewhere that no longer exists.
function routeExists(key) {
  if (!key) return false;
  if (LEGACY_ROUTES[key]) return true;
  const [pageId, subId] = String(key).split("/");
  const page = PAGES[pageId];
  if (!page) return false;
  if (page.merged) return !subId;
  return !subId || page.tabs.some(t => t.id === subId);
}

// Returns { page, sub, key } — `key` is the canonical "page/sub" string used
// for localStorage and nav highlighting.
function currentRoute() {
  let hash = location.hash.replace(/^#\/?/, "");
  if (LEGACY_ROUTES[hash]) hash = LEGACY_ROUTES[hash];
  const [pageId, subId] = hash.split("/");
  const page = PAGES[pageId];
  if (!page) {
    const [dp, ds] = DEFAULT_ROUTE.split("/");
    return { page: dp, sub: ds, key: DEFAULT_ROUTE };
  }
  if (page.merged) return { page: pageId, sub: null, key: pageId };
  const tab = page.tabs.find(t => t.id === subId) || page.tabs[0];
  return { page: pageId, sub: tab.id, key: pageId + "/" + tab.id };
}

async function router() {
  const route = currentRoute();
  lsSet(LS_KEY_ROUTE, route.key);
  if (controlKeyHandler) {
    document.removeEventListener("keydown", controlKeyHandler);
    controlKeyHandler = null;
  }
  if (controlKeyCommitTimer) {
    clearTimeout(controlKeyCommitTimer);
    controlKeyCommitTimer = null;
  }
  if (state.audioPollHandle) {
    clearInterval(state.audioPollHandle);
    state.audioPollHandle = null;
  }
  if (state.sleepCountdownHandle) {
    clearInterval(state.sleepCountdownHandle);
    state.sleepCountdownHandle = null;
  }
  if (state.wakeCountdownHandle) {
    clearInterval(state.wakeCountdownHandle);
    state.wakeCountdownHandle = null;
  }
  if (state.timersResyncHandle) {
    clearInterval(state.timersResyncHandle);
    state.timersResyncHandle = null;
  }
  document.querySelectorAll(".nav-item").forEach(n => {
    n.classList.toggle("active", n.dataset.route === route.page);
  });
  const main = document.getElementById("main");
  main.innerHTML = `<div class="empty-state loading">Loading…</div>`;
  const page = PAGES[route.page];
  try {
    if (page.merged) {
      await page.merged(main);
    } else {
      // Sub-tab bar, then the active tab's own panel rendered into a container
      // below it. Each tab keeps its own hash so it stays bookmarkable.
      main.innerHTML =
        `<div class="subtabs" role="tablist">` +
        page.tabs.map(t =>
          `<button type="button" role="tab" class="subtab${t.id === route.sub ? " active" : ""}"` +
          ` aria-selected="${t.id === route.sub}" data-subtab="${t.id}">${t.label}</button>`
        ).join("") +
        `</div><div id="subtab-panel"><div class="empty-state loading">Loading…</div></div>`;
      const panel = main.querySelector("#subtab-panel");
      const tab = page.tabs.find(t => t.id === route.sub);
      await tab.render(panel);
    }
  } catch (e) {
    const target = main.querySelector("#subtab-panel") || main;
    target.innerHTML = `<div class="empty-state">Failed to load this panel: ${e.message}</div>`;
  }
  // Every panel replaces #main's contents, so the exposure banner has to be
  // re-inserted after each render rather than living in the shell. It stays
  // deliberately un-dismissable: the condition it reports (an unauthenticated
  // dashboard on a public IP) is not something to hide behind an X.
  paintExposureBanner();
}

// ------------------------------------------------- exposure warning banner --
// Populated from GET /api/system/remote-access/status. Uses a bare fetch
// rather than api() so a failure here degrades to "no banner" instead of
// spraying error toasts over an otherwise-working dashboard.
async function refreshExposureWarnings() {
  try {
    const res = await fetch("/api/system/remote-access/status");
    state.exposureWarnings = res.ok ? (await res.json()).warnings || [] : [];
  } catch (e) {
    state.exposureWarnings = [];
  }
  paintExposureBanner();
}

function paintExposureBanner() {
  const main = document.getElementById("main");
  if (!main) return;
  const existing = main.querySelector("#exposure-banner");
  if (existing) existing.remove();
  const warnings = state.exposureWarnings || [];
  if (!warnings.length) return;
  const banner = el(
    `<div id="exposure-banner">` +
    warnings.map(w =>
      `<div class="exposure-warning ${escAttr(w.severity)}">
         <div class="ew-title">${escHtml(w.title)}</div>
         <div class="ew-detail">${escHtml(w.detail)}</div>
         <div class="ew-action">What to do: ${escHtml(w.action)}</div>
       </div>`
    ).join("") +
    `</div>`
  );
  main.insertBefore(banner, main.firstChild);
}

window.addEventListener("hashchange", router);
document.getElementById("sidebar").addEventListener("click", (e) => {
  const item = e.target.closest(".nav-item");
  if (item) location.hash = "#/" + item.dataset.route;
});
document.getElementById("main").addEventListener("click", (e) => {
  const tab = e.target.closest(".subtab");
  if (tab) location.hash = "#/" + currentRoute().page + "/" + tab.dataset.subtab;
});

// -------------------------------------------------------------- devices --
async function loadDevices() {
  state.devices = await get("/api/devices");
  const sel = document.getElementById("device-select");
  sel.innerHTML = "";
  if (state.devices.length === 0) {
    sel.innerHTML = `<option>No devices configured</option>`;
    return;
  }
  state.devices.forEach(d => {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = d.name;
    sel.appendChild(opt);
  });
  if (!state.deviceId || !state.devices.find(d => d.id === state.deviceId)) {
    state.deviceId = state.devices[0].id;
  }
  sel.value = state.deviceId;
  lsSet(LS_KEY_DEVICE, state.deviceId);
}

document.getElementById("device-select").addEventListener("change", (e) => {
  state.deviceId = e.target.value;
  lsSet(LS_KEY_DEVICE, state.deviceId);
  router();
});

// --------------------------------------------------------- status badge --
// Real Wi-Fi bulbs occasionally miss a single poll (radio contention, etc).
// Require 2 consecutive failed/offline polls before showing OFFLINE, so one
// transient blip doesn't flash a false "offline" state on every page load.
const OFFLINE_CONFIRM_THRESHOLD = 2;

// Formats a millisecond duration as a short "Xs ago" / "Xm Ys ago" / "Xh Ym ago" string
// for the live "last seen" ticker on the status badge.
function formatAgo(ms) {
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s ago`;
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  if (m < 60) return `${m}m ${s}s ago`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m ago`;
}

// Re-renders the status-badge text from whatever state pollStatus() last recorded —
// this is called every second by state.lastSeenTickHandle so "last seen Xs ago" keeps
// counting up between real poll cycles, without issuing any extra network requests.
function renderStatusText() {
  const text = document.getElementById("status-text");
  if (!text || !state.hasPolledOnce) return; // still on the initial "connecting…" text, first poll in flight
  const agoPart = state.lastSeenAt ? ` · last seen ${formatAgo(Date.now() - state.lastSeenAt)}` : "";
  // `renderControl()` (below) caches whatever /status returns into
  // state.lastStatus unconditionally, including a definitive {online: false}
  // response -- so `!state.lastStatus` alone no longer distinguishes "never
  // polled" from "polled and confirmed offline". Check `.online === false`
  // explicitly too, and skip the consecutive-miss grace period in that case:
  // an explicit "not online" answer doesn't need debouncing the way a
  // transient network blip does. Without this, a confirmed-offline device
  // could still flash "LIVE DATA · OFF" (reading `.power` off stale/absent
  // data) until enough consecutive misses piled up.
  const knownOffline = !state.lastStatus || state.lastStatus.online === false;
  if (knownOffline || state.consecutiveOfflinePolls >= OFFLINE_CONFIRM_THRESHOLD) {
    text.textContent = `OFFLINE${agoPart}`;
  } else {
    text.textContent = `LIVE DATA · ${state.lastStatus.power ? "ON" : "OFF"}${agoPart}`;
  }
}

async function pollStatus(quiet) {
  if (!state.deviceId) return;
  const badge = document.getElementById("status-badge");
  try {
    const st = await fetch(`${API}/api/devices/${state.deviceId}/status`).then(r => r.json());
    if (st.online) {
      state.consecutiveOfflinePolls = 0;
      state.lastStatus = st;
      state.lastSeenAt = Date.now();
      badge.classList.add("live");
    } else {
      state.consecutiveOfflinePolls++;
      if (state.consecutiveOfflinePolls >= OFFLINE_CONFIRM_THRESHOLD) {
        badge.classList.remove("live");
      }
    }
  } catch (e) {
    state.consecutiveOfflinePolls++;
    if (state.consecutiveOfflinePolls >= OFFLINE_CONFIRM_THRESHOLD) {
      badge.classList.remove("live");
    }
  }
  state.hasPolledOnce = true;
  renderStatusText();
  renderQuickControl();
}

function startPolling() {
  if (state.statusPollHandle) clearInterval(state.statusPollHandle);
  if (state.lastSeenTickHandle) clearInterval(state.lastSeenTickHandle);
  pollStatus();
  setTimeout(pollStatus, 1200); // quick re-check shortly after first load
  state.statusPollHandle = setInterval(pollStatus, 4000);
  // Status badge lives in the topbar (outside any panel), so this ticks for the whole
  // app lifetime rather than being torn down in router() like the panel-scoped handles.
  state.lastSeenTickHandle = setInterval(renderStatusText, 1000);
}

// ================================================================ PANELS ==

async function renderControl(main) {
  const st = state.lastStatus || await get(`/api/devices/${state.deviceId}/status`);
  state.lastStatus = st;
  const rgb = st.hue != null ? hsvToRgb(st.hue, st.saturation_pct ?? 100, st.value_pct ?? 100) : [255, 255, 255];

  main.innerHTML = `
    <h1 class="panel-title">Control</h1>
    <p class="panel-subtitle">Direct power, brightness and color control — <span class="tag ${st.online ? "on" : "error"}">${st.online ? "LIVE DATA" : "OFFLINE"}</span></p>
    <p class="panel-subtitle kbd-hint"><kbd>Space</kbd> toggles power · <kbd>&#8593;</kbd> / <kbd>&#8595;</kbd> brightness &plusmn;5%</p>

    <div class="card">
      <div class="preview-swatch" id="preview-swatch" style="background: ${rgbToHex(...rgb)}"></div>
      <button id="power-toggle" class="big-toggle ${st.power ? "primary" : ""}">${st.power ? "TURN OFF" : "TURN ON"}</button>
    </div>

    <div class="card">
      <h3>Brightness</h3>
      <div class="slider-row">
        <label><span>Brightness</span><span id="brightness-val">${st.mode === "colour" ? (st.value_pct ?? 100) : (st.brightness_pct ?? 100)}%</span></label>
        <input type="range" id="brightness-slider" min="1" max="100" value="${st.mode === "colour" ? (st.value_pct ?? 100) : (st.brightness_pct ?? 100)}">
      </div>
    </div>

    <div class="card">
      <h3>Color (HSV)</h3>
      <div class="slider-row hue-slider">
        <label><span>Hue</span><span id="hue-val">${Math.round(st.hue ?? 0)}°</span></label>
        <input type="range" id="hue-slider" min="0" max="359" value="${Math.round(st.hue ?? 0)}">
      </div>
      <div class="slider-row">
        <label><span>Saturation</span><span id="sat-val">${st.saturation_pct ?? 100}%</span></label>
        <input type="range" id="sat-slider" min="0" max="100" value="${st.saturation_pct ?? 100}">
      </div>
      <div class="row" style="margin-top:8px;">
        <label style="font-size:12px;color:var(--text-dim);">Or pick exact RGB:</label>
        <input type="color" id="rgb-picker" value="${rgbToHex(...rgb)}">
      </div>
    </div>

    <div class="card">
      <h3>White Mode</h3>
      <div class="slider-row">
        <label><span>Color Temperature (0=warm, 100=cool)</span><span id="temp-val">${st.color_temp_pct ?? 50}%</span></label>
        <input type="range" id="temp-slider" min="0" max="100" value="${st.color_temp_pct ?? 50}">
      </div>
      <button id="apply-white">Switch to White Mode</button>
    </div>

    <div class="card">
      <h3>Quick Actions</h3>
      <div class="row">
        <button id="btn-random">🎲 Random Color</button>
        <button id="btn-identify">📍 Identify Bulb (blink)</button>
        <button id="btn-flash">🚨 Flash Alert</button>
      </div>
    </div>
  `;

  main.querySelector("#power-toggle").onclick = async () => {
    await post(`/api/devices/${state.deviceId}/power`, { on: !st.power });
    state.lastStatus = null;
    router();
  };

  const brightnessSlider = main.querySelector("#brightness-slider");
  brightnessSlider.oninput = () => {
    main.querySelector("#brightness-val").textContent = brightnessSlider.value + "%";
  };
  brightnessSlider.onchange = async () => {
    await post(`/api/devices/${state.deviceId}/brightness`, { value: parseInt(brightnessSlider.value) });
    toast("Brightness updated", "success");
  };

  const hueSlider = main.querySelector("#hue-slider");
  const satSlider = main.querySelector("#sat-slider");
  const updatePreview = () => {
    const [r, g, b] = hsvToRgb(parseFloat(hueSlider.value), parseFloat(satSlider.value), 100);
    main.querySelector("#preview-swatch").style.background = rgbToHex(r, g, b);
    main.querySelector("#hue-val").textContent = Math.round(hueSlider.value) + "°";
    main.querySelector("#sat-val").textContent = satSlider.value + "%";
  };
  hueSlider.oninput = updatePreview;
  satSlider.oninput = updatePreview;
  const commitHsv = async () => {
    await post(`/api/devices/${state.deviceId}/color/hsv`, {
      h: parseFloat(hueSlider.value), s: parseFloat(satSlider.value), v: 100,
    });
    toast("Color updated", "success");
  };
  hueSlider.onchange = commitHsv;
  satSlider.onchange = commitHsv;

  main.querySelector("#rgb-picker").onchange = async (e) => {
    const hex = e.target.value;
    const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
    await post(`/api/devices/${state.deviceId}/color`, { r, g, b });
    toast("Color updated", "success");
  };

  const tempSlider = main.querySelector("#temp-slider");
  tempSlider.oninput = () => { main.querySelector("#temp-val").textContent = tempSlider.value + "%"; };
  main.querySelector("#apply-white").onclick = async () => {
    await post(`/api/devices/${state.deviceId}/white`, {
      brightness: parseInt(brightnessSlider.value), color_temp: parseInt(tempSlider.value),
    });
    toast("Switched to white mode", "success");
  };

  main.querySelector("#btn-random").onclick = async () => {
    await post(`/api/devices/${state.deviceId}/color/random`);
    toast("Random color applied", "success");
  };
  main.querySelector("#btn-identify").onclick = async () => {
    toast("Blinking bulb…");
    await post(`/api/devices/${state.deviceId}/identify`);
  };
  main.querySelector("#btn-flash").onclick = async () => {
    await post(`/api/devices/${state.deviceId}/flash-alert`, { r: 255, g: 0, b: 0, times: 3 });
    toast("Flash alert sent", "success");
  };

  // Keyboard shortcuts — only wired while the Control panel is mounted (router()
  // removes this listener the instant another panel loads, see the `controlKeyHandler`
  // cleanup there), and skipped while any input/select/textarea has focus so this
  // never hijacks typing or a slider's own native arrow-key handling elsewhere in the app.
  const BRIGHTNESS_KEY_STEP = 5; // percent per Up/Down press
  // Arrow keys fire one keydown per press — and repeat rapidly while held — so
  // committing on every one sent a POST to the bulb per keypress AND stacked a
  // "Brightness updated" toast per keypress. The slider UI still updates on
  // every press (that's just a local `input` event, no network), but the actual
  // commit is debounced so a burst of presses lands as one request and one toast.
  const KEY_COMMIT_DEBOUNCE_MS = 400;
  function handleControlKeydown(e) {
    const active = document.activeElement;
    const tag = active && active.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (active && active.isContentEditable)) return;
    if (e.code === "Space" || e.key === " ") {
      e.preventDefault();
      const toggleBtn = main.querySelector("#power-toggle");
      if (toggleBtn) toggleBtn.click();
    } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      const slider = main.querySelector("#brightness-slider");
      if (!slider) return;
      const delta = e.key === "ArrowUp" ? BRIGHTNESS_KEY_STEP : -BRIGHTNESS_KEY_STEP;
      const next = Math.max(1, Math.min(100, parseInt(slider.value, 10) + delta));
      slider.value = String(next);
      slider.dispatchEvent(new Event("input"));
      clearTimeout(controlKeyCommitTimer);
      controlKeyCommitTimer = setTimeout(function () {
        controlKeyCommitTimer = null;
        slider.dispatchEvent(new Event("change"));
      }, KEY_COMMIT_DEBOUNCE_MS);
    }
  }
  document.addEventListener("keydown", handleControlKeydown);
  controlKeyHandler = handleControlKeydown;
}

async function renderScenes(main) {
  state.scenes = state.scenes.length ? state.scenes : await get("/api/scenes");
  main.innerHTML = `
    <h1 class="panel-title">Scenes</h1>
    <p class="panel-subtitle">One-tap mood presets combining color, brightness and mode (${state.scenes.length} available)</p>
    <div class="grid" id="scene-grid"></div>
  `;
  const grid = main.querySelector("#scene-grid");
  state.scenes.forEach(s => {
    const card = el(`<div class="scene-card" title="${s.description}">
      <div class="name">${s.name}</div>
      <div class="desc">${s.description}</div>
    </div>`);
    card.onclick = async () => {
      await post(`/api/devices/${state.deviceId}/scenes/apply`, { scene_id: s.id });
      toast(`Scene "${s.name}" applied`, "success");
    };
    grid.appendChild(card);
  });
}

async function renderEffects(main) {
  state.effects = state.effects.length ? state.effects : await get("/api/effects");
  let current = null;
  try { current = (await get(`/api/devices/${state.deviceId}/effects/current`)).effect; } catch (e) {}

  main.innerHTML = `
    <h1 class="panel-title">Effects</h1>
    <p class="panel-subtitle">Animated lighting effects that run continuously until stopped</p>
    <div class="card row">
      <label style="font-size:12px;color:var(--text-dim);">Speed</label>
      <input type="range" id="effect-speed" min="0.2" max="3" step="0.1" value="1" style="width:160px;">
      <button id="stop-effect" class="danger" ${current ? "" : "disabled"}>Stop Current Effect${current ? " (" + current + ")" : ""}</button>
    </div>
    <div class="grid" id="effect-grid"></div>
  `;
  main.querySelector("#stop-effect").onclick = async () => {
    await post(`/api/devices/${state.deviceId}/effects/stop`);
    toast("Effect stopped", "success");
    renderEffects(main);
  };
  const grid = main.querySelector("#effect-grid");
  state.effects.forEach(fx => {
    const card = el(`<div class="effect-card" title="${fx.description}">
      <div class="name">${fx.name}${current === fx.id ? " ▶" : ""}</div>
      <div class="desc">${fx.description}</div>
    </div>`);
    card.onclick = async () => {
      const speed = parseFloat(main.querySelector("#effect-speed").value);
      await post(`/api/devices/${state.deviceId}/effects/start`, { effect: fx.id, speed });
      toast(`Effect "${fx.name}" started`, "success");
      renderEffects(main);
    };
    grid.appendChild(card);
  });
}

// ---------------------------------------------------------- audio tab --
const AUDIO_MODE_INFO = {
  band_fixed: { name: "Band → Color", desc: "Bass=warm red/orange, mids=green, treble=blue, blended by which is loudest.", bands: false },
  dominant_band: { name: "Dominant Band", desc: "Hue smoothly follows whichever of bass/mid/treble currently dominates.", bands: false },
  weighted_blend: { name: "Spectral Blend", desc: "Continuous hue sweep driven by the overall tonal balance, not fixed anchors.", bands: false },
  vu_meter: { name: "VU Meter", desc: "Fixed hue, brightness only — a direct volume meter.", bands: false, mono: true },
  auto_rotate_hue: { name: "Auto-Rotate", desc: "Hue slowly cycles on its own; brightness/beats still track the audio.", bands: false },
  monochrome_pulse: { name: "Monochrome Pulse", desc: "One color of your choice that pulses brighter with the beat.", bands: false, mono: true },
  strobe_on_drop: { name: "Strobe on Drop", desc: "Dim warm baseline, full white flash only on hard bass hits.", bands: false },
  palette_cycle: { name: "Palette Cycle", desc: "Steps through the built-in color presets, advancing on each detected beat.", bands: false },
  spectrum_gradient: { name: "Spectrum Gradient", desc: "Continuous hue gradient across N bands (set below) — much finer-grained than the 3-anchor Band → Color mode.", bands: true },
  band_flash_overlay: { name: "Band Flash Overlay", desc: "Ambient N-band gradient base color, with brief accent flashes whenever any individual band spikes.", bands: true },
  stereo_split: { name: "Stereo Split", desc: "Hue leans left/right-anchor based on which stereo channel is louder (needs a 2-channel input device).", bands: false },
  breathing_silence: { name: "Breathing Silence", desc: "Slow ambient breathing brightness during quiet passages instead of going flat/dark; wakes up smoothly when audio returns.", bands: false },
  harmonic_pairs: { name: "Harmonic Pairs", desc: "Finds the two most energetic non-adjacent bands each frame and blends between two complementary (180°-apart) hues based on which one dominates; more bands (below) sharpen the pairing.", bands: true },
  kick_snare_split: { name: "Kick/Snare Split", desc: "Bass drives brightness like a kick drum while a separate mid-band accent shifts the hue like a layered snare/hihat.", bands: false },
  energy_contour: { name: "Energy Contour", desc: "Hue locked to your chosen color; only saturation/brightness track a smoothed energy envelope for a slow-moving contour instead of a punchy pulse.", bands: false, mono: true },
  bass_only_pulse: { name: "Bass-Only Pulse", desc: "Brightness-only pulse driven purely by the bass band's share of the mix — hue never moves.", bands: false, mono: true },
  mirror_mode: { name: "Mirror Mode", desc: "Hue mirrors around a fixed center point as the treble/bass balance shifts, while brightness breathes independently — a breathing color effect.", bands: false },
  random_walk_hue: { name: "Random Walk Hue", desc: "Hue takes small bounded random steps instead of rotating at a fixed rate — feels organic rather than mechanical.", bands: false },
  silence_flash_recover: { name: "Silence Flash Recover", desc: "Dims through any quiet passage, then fires one bright white flash the instant audio resumes after a long pause.", bands: false, mono: true },
  crescendo_ramp: { name: "Crescendo Ramp", desc: "Detects a sustained rise in energy over a couple of seconds and ramps brightness/saturation up ahead of the peak, not just when it's already loud.", bands: false, mono: true },
};

function stopAudioPolling() {
  if (state.audioPollHandle) {
    clearInterval(state.audioPollHandle);
    state.audioPollHandle = null;
  }
}

const BAND_COLORS = ["#ff6b4a", "#ffa84a", "#e8d24a", "#7ee787", "#4ad9ff", "#7c9cff", "#b47cff", "#ff7cd1"];

function renderBandMeter(bands) {
  bands = bands || { fractions: [], rms: 0, is_beat: false };
  const fractions = bands.fractions && bands.fractions.length ? bands.fractions : [0, 0, 0];
  const pct = (v) => Math.max(2, Math.min(100, Math.round(v * 100)));
  const labels = fractions.length === 3 ? ["BASS", "MID", "TREBLE"] : fractions.map((_, i) => `B${i + 1}`);
  return `
    <div class="band-meter">
      ${fractions.map((f, i) => `
        <div class="band-col">
          <div class="band-fill" style="height:${pct(f)}%;background:${BAND_COLORS[i % BAND_COLORS.length]}"></div>
          <div class="band-label">${labels[i]}</div>
        </div>`).join("")}
    </div>
    <p class="panel-subtitle" style="margin-top:8px;">RMS ${(bands.rms || 0).toFixed(4)} <span class="beat-dot ${bands.is_beat ? "hit" : ""}"></span> beat</p>
  `;
}

function renderTempoInfo(tempo) {
  tempo = tempo || {};
  const bpmText = tempo.bpm != null ? `${tempo.bpm.toFixed(1)} BPM` : "no tempo lock yet";
  const confPct = Math.round((tempo.confidence || 0) * 100);
  const tapText = tempo.tap_bpm != null ? ` · tap tempo ${tempo.tap_bpm.toFixed(1)} BPM` : "";
  const suggestion = tempo.suggested_preset ? ` · suggests "${tempo.suggested_preset.replace(/_/g, " ")}" preset` : "";
  return `<p class="panel-subtitle" style="margin-top:8px;">${bpmText} <span style="color:var(--text-dim);">(confidence ${confPct}%)</span>${tapText}${suggestion}</p>`;
}

function renderSenderInfo(sender) {
  if (!sender) return "";
  const parts = [`dwell ${sender.min_dwell_ms}ms`];
  if (sender.last_latency_ms != null) parts.push(`last send ${sender.last_latency_ms}ms`);
  if (sender.error) parts.push(`error: ${sender.error}`);
  return `<p class="panel-subtitle" style="margin-top:4px;">${parts.join(" · ")}</p>`;
}

function fmtMs(v) { return v == null ? "—" : `${Number(v).toFixed(1)}ms`; }

// Per-stage latency, not one total. The three stages have different ceilings
// and only two of them are ours: capture and analysis are the software budget
// the ≤10ms requirement is actually about, while the bulb round-trip is a
// hardware floor no setting can move. Presenting them as one number is what
// makes someone conclude the software is slow and start tuning knobs that
// cannot possibly help — so the hardware row is labelled as such, and the
// verdict chip is scored against the software stages alone.
function renderLatencyPanel(latency, analysis) {
  if (!latency) {
    return `<div class="empty-state">No latency measured yet — start a session and the
      per-stage numbers appear here.</div>`;
  }
  const budget = latency.budget || {};
  const stages = latency.stages || {};
  const frames = latency.frames || {};
  const target = budget.target_ms;
  const soft = budget.software_p50_ms;
  const measured = soft != null;
  const ok = budget.within_target;

  const rows = ["capture", "analysis", "bulb"].map(key => {
    const s = stages[key];
    if (!s) return "";
    const hw = s.kind === "hardware";
    return `<tr class="${hw ? "lat-hw" : ""}">
        <th scope="row">${s.label}${hw ? ' <span class="lat-tag">hardware floor</span>' : ""}</th>
        <td class="num">${fmtMs(s.p50_ms)}</td>
        <td class="num">${fmtMs(s.p95_ms)}</td>
        <td class="num">${fmtMs(s.worst_ever_ms)}</td>
        <td class="num dim">${s.count || 0}</td>
      </tr>`;
  }).join("");

  const verdict = !measured
    ? `<span class="lat-chip lat-idle">awaiting frames</span>`
    : `<span class="lat-chip ${ok ? "lat-ok" : "lat-over"}">
         software ${fmtMs(soft)} ${ok ? "≤" : ">"} ${fmtMs(target)} target
       </span>`;

  const late = frames.late || 0;
  const dropped = frames.dropped || 0;
  const health = (late || dropped)
    ? `<span class="lat-warn">${late} late · ${dropped} dropped</span>`
    : `<span class="dim">no late or dropped frames</span>`;

  return `
    <div class="lat-head">
      ${verdict}
      <span class="dim">block period ${fmtMs(latency.block_period_ms)} · rolling window ${latency.window} frames</span>
    </div>
    <div class="lat-table-wrap">
      <table class="lat-table">
        <thead>
          <tr><th scope="col">Stage</th><th scope="col">Typical</th><th scope="col">p95</th>
              <th scope="col">Worst</th><th scope="col">Frames</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p class="panel-subtitle lat-note">
      Sound-to-decision is <strong>capture + analysis</strong> ${measured ? `= ${fmtMs(soft)}` : ""} —
      that is the part settings can change. The bulb round-trip
      ${stages.bulb && stages.bulb.p50_ms != null ? `(${fmtMs(stages.bulb.p50_ms)})` : ""}
      is the lamp's own hardware and no setting makes it faster.
    </p>
    ${renderDeliveryNote(stages.capture)}
    ${renderAnalysisNote(analysis)}
    <p class="panel-subtitle">${frames.processed || 0} frames processed · ${health}</p>`;
}

// The hop/window trade, stated where someone changing it can see it. These
// pull in opposite directions and the defaults are a measured compromise, not
// a guess: window 2048 resolves bass twice as finely as 1024 and loses a
// tempo doing it, because a longer window smears the transient it is trying
// to locate.
function renderAnalysisNote(analysis) {
  if (!analysis) return "";
  const hopMs = (analysis.hop / 44100) * 1000;
  const winMs = (analysis.window / 44100) * 1000;
  const binHz = 44100 / analysis.window;
  return `<p class="panel-subtitle dim">
      Hop ${analysis.hop} samples (${hopMs.toFixed(1)}ms) — sets latency; shorter costs CPU.
      Window ${analysis.window} samples (${winMs.toFixed(1)}ms, ${binHz.toFixed(0)}Hz per bin) —
      sets frequency resolution; longer smears transients and costs beat accuracy.
      Overlap ${analysis.overlap} samples.
    </p>`;
}

// Capture latency is the block period plus lateness, never the median gap
// between callbacks — a bursty audio backend delivers blocks back-to-back and
// then pauses, so half its gaps are sub-millisecond while the data is no
// fresher for it. The raw gaps are still worth showing, as delivery health.
function renderDeliveryNote(capture) {
  if (!capture || capture.interval_p50_ms == null) return "";
  const bursty = capture.interval_p50_ms < capture.floor_ms * 0.5;
  return `<p class="panel-subtitle dim">
      Capture floor ${fmtMs(capture.floor_ms)} (the block period) · delivery gaps
      typically ${fmtMs(capture.interval_p50_ms)}, p95 ${fmtMs(capture.interval_p95_ms)}${
        bursty ? " — this backend delivers in bursts, so the gaps are uneven by nature" : ""}.
    </p>`;
}

// The bridge has five states and they each need a different fix, so the chip
// distinguishes all five rather than collapsing to connected/disconnected:
//   off        - backend started with the listener disabled
//   waiting    - listening, but no bridge tool has connected yet
//   silent     - bridge attached but sending no sound (nothing playing, or
//                the wrong capture device selected)
//   ready      - audio IS arriving, but nothing is consuming it because no
//                session is running. Added because the previous four states
//                could not express it: the chip said "live", the audio really
//                was live, and the bulb sat still because nobody had pressed
//                Start. "Everything looks right and nothing happens" is the
//                worst state a status chip can fail to name.
//   live       - audio arriving AND a session consuming it
//
// `subscribers` is the signal for that distinction: a bridge session
// subscribes to the server for as long as it runs, so >0 means something is
// actually listening to the stream.
function bridgeState(b) {
  if (!b || !b.listening) return "off";
  if (!b.connected) return "waiting";
  if (!b.streaming) return "silent";
  if (!b.subscribers) return "ready";
  return "live";
}

// The capture device, chosen here rather than on a command line.
//
// This deliberately does NOT reuse the "Input device" dropdown above it: that
// one lists the CONTAINER's audio devices, which in Docker is always empty and
// is exactly the wrong list for bridge mode. The list below is reported by the
// capture tool itself over the bridge socket, so it is the host's devices.
//
// Loopback devices are grouped first because that is almost always what
// someone wants — a microphone hears the room, not what the computer is
// playing — and because picking a silent device is the single most common way
// this feature appears broken.
function renderBridgeDevicePicker(b) {
  if (!b || !b.listening) return "";
  const devices = b.devices || [];
  if (!devices.length) {
    return `<p class="bridge-launcher-foot dim">
      Connect the bridge and it will report this PC's capture devices here,
      so you can switch source without restarting it.</p>`;
  }
  const loopback = devices.filter(d => d.loopback);
  const rest = devices.filter(d => !d.loopback);
  const opt = d => `<option value="${d.index}" ${d.index === b.device_index ? "selected" : ""}>`
    + `[${d.index}] ${escapeHtml(d.name)}</option>`;
  const group = (label, list) => list.length
    ? `<optgroup label="${label}">${list.map(opt).join("")}</optgroup>` : "";

  const live = b.streaming && b.peak > 0.0005;
  return `
    <label class="bridge-field">Capture device
      <select id="bridge-device">
        ${group("Plays what the PC outputs (recommended)", loopback)}
        ${group("Microphones and other inputs", rest)}
      </select>
    </label>
    <p class="bridge-launcher-foot ${live ? "" : "dim"}">
      ${live
        ? `Signal on the current device — peak ${Number(b.peak).toFixed(3)}.`
        : `<strong>No signal on the current device.</strong> Play something; if it stays
           at zero, this device carries no audio — try another from the top group.`}
      Changing this switches the running bridge; it reconnects in a second or two.
    </p>`;
}

function bridgeChipClass(b) { return "bridge-" + bridgeState(b); }

function bridgeChipText(b) {
  return { off: "Bridge off", waiting: "Bridge waiting",
           silent: "Bridge silent", ready: "Bridge ready · no session",
           live: "Bridge live" }[bridgeState(b)];
}

function bridgeChipTitle(b) {
  const st = bridgeState(b);
  if (st === "off") return "The backend is not listening for a bridge. Restart it with SBD_AUDIO_BRIDGE enabled.";
  if (st === "waiting") return `Listening on port ${b.port}, but no bridge has connected. Run: python tools/sbd-audio-bridge.py --probe`;
  if (st === "ready") {
    return `Audio IS arriving from ${b.client} (${b.frames} frames, peak ${b.peak}) — but no audio-reactive session is running, so nothing is using it. Press Start below.`;
  }
  if (st === "silent") {
    const age = b.last_frame_age_s == null ? "never" : b.last_frame_age_s + "s ago";
    return `Bridge connected from ${b.client} but no audio is arriving (last frame ${age}). Is anything playing? Is the right capture device selected? Run --probe to find the device that actually has sound on it.`;
  }
  return `Streaming from ${b.client} — ${b.frames} frames, ${b.drops} dropped, peak ${b.peak}`;
}

// The launch command is built from a path the USER types, so it is untrusted
// input being written straight into innerHTML. Without escaping, a path
// containing a quote would break out of the value attribute -- self-inflicted
// rather than an attack, but broken either way.
function escapeHtml(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

const BRIDGE_DIR_KEY = "sbd.bridgeHostDir";
const BRIDGE_ARGS_KEY = "sbd.bridgeArgs";

// The container knows only its own /app path, which is useless in a command
// the user runs on Windows. Precedence: what the user typed here (remembered
// per browser) > the SBD_BRIDGE_HOST_DIR hint from the server > a placeholder
// they can overwrite.
function bridgeHostDir(bridge) {
  const saved = localStorage.getItem(BRIDGE_DIR_KEY);
  if (saved !== null && saved !== "") return saved;
  if (bridge && bridge.host_dir) return bridge.host_dir;
  return "C:\path\to\smart-bulb-dashboard";
}

function bridgeArgs() {
  const saved = localStorage.getItem(BRIDGE_ARGS_KEY);
  return saved === null ? "" : saved;
}

function bridgeCommand(dir, args) {
  const a = (args || "").trim();
  // Quoted: this path very often contains spaces.
  return `cd "${dir}" && tools\start-audio-bridge.cmd${a ? " " + a : ""}`;
}

async function refreshBridgeChip() {
  const chip = document.querySelector("#bridge-chip");
  if (!chip) return;
  let b;
  try { b = await get("/api/audio/bridge"); } catch (e) { return; }
  chip.className = "bridge-chip " + bridgeChipClass(b);
  chip.title = bridgeChipTitle(b);
  const text = document.querySelector("#bridge-chip-text");
  if (text) text.textContent = bridgeChipText(b);

  // Keep the device picker current — the inventory only exists once a bridge
  // has connected, so on first load there is nothing to draw. Skipped while
  // the dropdown has focus so a re-render cannot yank it shut mid-selection.
  const wrap = document.querySelector("#bridge-device-picker");
  if (wrap && document.activeElement !== document.querySelector("#bridge-device")) {
    wrap.innerHTML = renderBridgeDevicePicker(b);
    wireBridgeDevicePicker();
  }
}

function wireBridgeDevicePicker() {
  const sel = document.querySelector("#bridge-device");
  if (!sel || sel.dataset.wired) return;
  sel.dataset.wired = "1";
  sel.onchange = async (e) => {
    const index = parseInt(e.target.value, 10);
    try {
      await post("/api/audio/bridge/device", { device_index: index });
      toast(`Bridge switching to device ${index}…`);
    } catch (err) {
      toast(`Could not switch device: ${err.message}`, "error");
    }
  };
}

async function renderAudio(main) {
  stopAudioPolling();
  // Poll the bridge chip independently of the session poller: the whole point
  // is to watch it go live while starting the bridge, which happens with no
  // session running.
  if (window.__bridgeChipTimer) clearInterval(window.__bridgeChipTimer);
  window.__bridgeChipTimer = setInterval(() => {
    if (!document.querySelector("#bridge-chip")) {
      clearInterval(window.__bridgeChipTimer);
      window.__bridgeChipTimer = null;
      return;
    }
    refreshBridgeChip();
  }, 2000);
  let devicesResp;
  try {
    devicesResp = await get("/api/audio/devices");
  } catch (e) {
    main.innerHTML = `
      <h1 class="panel-title">Audio Reactive</h1>
      <div class="empty-state">Could not list audio input devices: ${e.message}</div>`;
    return;
  }
  const audioDevices = devicesResp.devices || [];
  // The container has no audio devices at all, so this list is empty there and
  // the bridge is the only way to get sound in. Fetched here (not only in the
  // poller) so the very first paint already shows the true state.
  let bridge = { listening: false, connected: false, streaming: false };
  try { bridge = await get("/api/audio/bridge"); } catch (e) {}
  const bridgeUsable = !!bridge.listening;
  // Default to whichever source can actually work right now.
  const defaultSource = (audioDevices.length === 0 && bridgeUsable) ? "bridge" : "device";
  const defaultDwell = devicesResp.default_min_dwell_ms || 90;
  const dwellFloor = devicesResp.min_dwell_floor_ms || 40;
  let sessionStatus = { active: false };
  try { sessionStatus = await get(`/api/devices/${state.deviceId}/audio-reactive/status`); } catch (e) {}
  let groupStatus = { active: false };
  try { groupStatus = await get(`/api/groups/all/audio-reactive/status`); } catch (e) {}
  const groups = await get("/api/groups").catch(() => []);
  let audioPresetsResp = { presets: [] };
  try { audioPresetsResp = await get("/api/audio/presets"); } catch (e) {}
  const beatPresets = devicesResp.beat_sensitivity_presets || ["subtle", "normal", "aggressive"];
  const defaultBeatSensitivity = devicesResp.default_beat_sensitivity || "normal";
  const tempo = sessionStatus.tempo || {};

  const preferredIdx = audioDevices.findIndex(d => /voicemeeter|cable/i.test(d.name));
  const defaultDeviceIndex = sessionStatus.device_index ?? (preferredIdx >= 0 ? audioDevices[preferredIdx].index : (audioDevices[0] ? audioDevices[0].index : null));

  const deviceOptions = audioDevices.map(d => `<option value="${d.index}" ${d.index === defaultDeviceIndex ? "selected" : ""}>${d.name}</option>`).join("");
  const sourceOptions = [
    `<option value="device" ${defaultSource === "device" ? "selected" : ""} ${audioDevices.length === 0 ? "disabled" : ""}>Local input device${audioDevices.length === 0 ? " (none on this host)" : ""}</option>`,
    `<option value="bridge" ${defaultSource === "bridge" ? "selected" : ""} ${bridgeUsable ? "" : "disabled"}>Audio bridge (from Windows host)${bridgeUsable ? "" : " — listener off"}</option>`,
  ].join("");
  const modeOptions = Object.entries(AUDIO_MODE_INFO).map(([id, info]) => `<option value="${id}" ${sessionStatus.mode === id ? "selected" : ""}>${info.name}</option>`).join("");
  const hostDir = bridgeHostDir(bridge);
  const hostArgs = bridgeArgs();

  main.innerHTML = `
    <h1 class="panel-title">Audio Reactive <span class="tag on">LIVE DATA</span>
      <span id="bridge-chip" class="bridge-chip ${bridgeChipClass(bridge)}" title="${bridgeChipTitle(bridge)}">
        <span class="bridge-dot"></span><span id="bridge-chip-text">${bridgeChipText(bridge)}</span>
      </span>
    </h1>
    <p class="panel-subtitle">
      Bulb reacts to whatever this input device hears. Route your PC's audio through
      VoiceMeeter (or pick a real microphone) — see the Audio skill/docs for setup.
      Decision latency is now sub-15ms internally; the "Min. dwell" slider controls how
      long each color actually stays on the bulb so you can still see it change.
    </p>

    <div class="bridge-launcher" id="bridge-launcher">
      <div class="bridge-launcher-head">
        <strong>Start the audio bridge</strong>
        <span class="bridge-launcher-note">Runs on Windows — the container has no audio devices of its own.</span>
      </div>
      <label class="bridge-field">Repo folder on this PC
        <input type="text" id="bridge-dir" value="${escapeAttr(hostDir)}" spellcheck="false"
               placeholder="C:\path\to\smart-bulb-dashboard">
      </label>
      <label class="bridge-field">Extra arguments <span class="bridge-hint">(--probe finds which device has sound)</span>
        <input type="text" id="bridge-args" value="${escapeAttr(hostArgs)}" spellcheck="false"
               placeholder="--device 85">
      </label>
      <div class="bridge-cmd-row">
        <code id="bridge-cmd">${escapeHtml(bridgeCommand(hostDir, hostArgs))}</code>
        <button class="btn small" id="bridge-copy" type="button">Copy</button>
        <button class="btn small ghost" id="bridge-reset" type="button" title="Forget my edits and use the server's value">Reset</button>
      </div>
      <p class="bridge-launcher-foot">Paste into a terminal, or double-click
        <code>tools\start-audio-bridge.cmd</code>. Leave it open — closing the window stops the bridge.</p>
      <div id="bridge-device-picker">${renderBridgeDevicePicker(bridge)}</div>
    </div>

    <div class="card">
      <h3>Single-Bulb Session</h3>
      <div class="form-grid">
        <label>Audio source<select id="audio-source">${sourceOptions}</select></label>
        <label id="audio-device-row" style="${defaultSource === "bridge" ? "display:none;" : ""}">Input device<select id="audio-device">${deviceOptions}</select></label>
        <label>Mode<select id="audio-mode">${modeOptions}</select></label>
        <label>Beat sensitivity
          <select id="audio-beat-sensitivity">
            ${beatPresets.map(p => `<option value="${p}" ${(tempo.beat_sensitivity || defaultBeatSensitivity) === p ? "selected" : ""}>${p[0].toUpperCase() + p.slice(1)}</option>`).join("")}
          </select>
        </label>
      </div>
      <div class="slider-row">
        <label><span>Sensitivity</span><span id="sens-val">${(sessionStatus.sensitivity ?? 1.0).toFixed(1)}x</span></label>
        <input type="range" id="audio-sensitivity" min="0.2" max="3" step="0.1" value="${sessionStatus.sensitivity ?? 1.0}">
      </div>
      <div class="slider-row">
        <label><span>Min. dwell (how long each color stays visible)</span><span id="dwell-val">${sessionStatus.sender ? sessionStatus.sender.min_dwell_ms : defaultDwell}ms</span></label>
        <input type="range" id="audio-dwell" min="${dwellFloor}" max="400" step="5" value="${sessionStatus.sender ? sessionStatus.sender.min_dwell_ms : defaultDwell}">
      </div>
      <div class="slider-row" id="nbands-row">
        <label><span>Band count (Spectrum Gradient / Band Flash Overlay)</span><span id="nbands-val">${sessionStatus.n_bands || 6}</span></label>
        <input type="range" id="audio-nbands" min="3" max="16" step="1" value="${sessionStatus.n_bands || 6}">
      </div>
      <div class="slider-row hue-slider" id="mono-hue-row">
        <label><span>Monochrome / VU hue</span><span id="mono-hue-val">280°</span></label>
        <input type="range" id="mono-hue" min="0" max="359" value="280">
      </div>
      <p class="panel-subtitle" id="mode-desc"></p>
      <div class="row">
        <button id="audio-start" class="primary" ${sessionStatus.active ? "disabled" : ""}>Start</button>
        <button id="audio-stop" class="danger" ${sessionStatus.active ? "" : "disabled"}>Stop</button>
      </div>
      ${renderSenderInfo(sessionStatus.sender)}
    </div>

    <div class="card">
      <h3>Live Input <span class="tag ${sessionStatus.active ? "on" : "off"}">${sessionStatus.active ? "LISTENING" : "IDLE"}</span></h3>
      <div id="band-meter-wrap">${renderBandMeter(sessionStatus.bands)}</div>
      <div id="tempo-wrap">${renderTempoInfo(tempo)}</div>
      <div class="row">
        <button id="tap-tempo-btn" class="primary" ${sessionStatus.active ? "" : "disabled"}>Tap Tempo</button>
      </div>
    </div>

    <div class="card">
      <h3>Latency <span class="tag ${sessionStatus.active ? "on" : "off"}">${sessionStatus.active ? "LIVE DATA" : "IDLE"}</span></h3>
      <p class="panel-subtitle">
        Measured per stage on this session — not an estimate. Typical is the median
        over the rolling window; worst is the largest spike since the session started.
      </p>
      <div id="latency-wrap">${renderLatencyPanel(sessionStatus.latency, sessionStatus.analysis)}</div>
    </div>

    <div class="card">
      <h3>Genre &amp; Mood Presets</h3>
      <p class="panel-subtitle">
        Bundles mode + sensitivity + dwell + band count + beat sensitivity + a color palette under one name.
        Applying a preset starts (or restarts) the single-bulb session above with those settings.
      </p>
      <div class="grid" id="audio-preset-grid"></div>
      <h3 style="margin-top:16px;">Save Current Settings as Custom Preset</h3>
      <div class="row">
        <input type="text" id="custom-preset-name" placeholder="Name (e.g. Friday Mix)">
        <button id="save-custom-preset" class="primary">Save</button>
      </div>
    </div>

    <div class="card">
      <h3>Multi-Bulb Orchestration</h3>
      <p class="panel-subtitle">
        Runs one shared audio analysis across every bulb in a group, so each bulb reacts
        together instead of opening its own capture. Useful now with one bulb (exercises
        the same plumbing), scales automatically once more bulbs are added — see ROADMAP.md.
      </p>
      <div class="form-grid">
        <label>Group
          <select id="group-select">${groups.map(g => `<option value="${g.id}">${g.name} (${g.device_ids.length} bulb${g.device_ids.length === 1 ? "" : "s"})</option>`).join("")}</select>
        </label>
        <label>Role mode
          <select id="role-mode">
            <option value="unison">Unison — all identical</option>
            <option value="phase_offset">Phase Offset — chase effect</option>
            <option value="band_split">Band Split — one bulb per band</option>
            <option value="wave">Wave — hue sweeps across the group</option>
            <option value="mirror">Mirror — bulbs mirror around a center hue</option>
          </select>
        </label>
      </div>
      <div class="row">
        <button id="group-audio-start" class="primary" ${groupStatus.active ? "disabled" : ""}>Start Group Session</button>
        <button id="group-audio-stop" class="danger" ${groupStatus.active ? "" : "disabled"}>Stop</button>
      </div>
      <div id="group-status">${groupStatus.active ? `<p class="panel-subtitle">Active — ${groupStatus.bulb_count} bulb(s), role: ${groupStatus.role_mode}</p>` : ""}</div>
    </div>
  `;

  const modeSelect = main.querySelector("#audio-mode");
  const monoRow = main.querySelector("#mono-hue-row");
  const nbandsRow = main.querySelector("#nbands-row");
  const modeDesc = main.querySelector("#mode-desc");
  function syncModeUI() {
    const info = AUDIO_MODE_INFO[modeSelect.value];
    modeDesc.textContent = info ? info.desc : "";
    monoRow.style.display = info && info.mono ? "" : "none";
    nbandsRow.style.display = info && info.bands ? "" : "none";
  }
  modeSelect.onchange = () => {
    syncModeUI();
    // Deliberately after syncModeUI: the mono-hue / band-count rows show or
    // hide based on the new mode, and pushing first would briefly leave the
    // panel describing the old one.
    pushLive({ mode: modeSelect.value });
  };
  syncModeUI();

  // Every control below applies LIVE to a running session and is remembered.
  //
  // Tuning by ear means moving one control and hearing the difference. The old
  // stop / change / start cycle destroyed exactly that: the bulb went dark, the
  // tempo tracker lost its lock and had to re-acquire, and — the part that
  // actually bit — the restart went through the start path, whose capture
  // source defaults to "device", so changing the mood on a bridge session
  // silently moved it to local capture the container does not have.
  //
  // `oninput` updates the label on every pixel of a drag (cheap, local);
  // `onchange` fires once when the drag ends and is what hits the network.
  // Sending on every input event would be one request per pixel.
  async function pushLive(patch, labelEl) {
    try {
      const resp = await post(`/api/devices/${state.deviceId}/audio-reactive/settings`, patch);
      if (labelEl) {
        // `live: false` means it was saved as the next session's starting
        // value but nothing is running to change. Saying "saved" is honest;
        // flashing green would imply the bulb just moved.
        labelEl.classList.toggle("val-flash", !!resp.live);
        if (resp.live) setTimeout(() => labelEl.classList.remove("val-flash"), 400);
      }
      return resp;
    } catch (err) {
      toast(`Could not apply: ${err.message}`, "error");
      return null;
    }
  }

  const sensVal = main.querySelector("#sens-val");
  main.querySelector("#audio-sensitivity").oninput = (e) => {
    sensVal.textContent = parseFloat(e.target.value).toFixed(1) + "x";
  };
  main.querySelector("#audio-sensitivity").onchange = (e) =>
    pushLive({ sensitivity: parseFloat(e.target.value) }, sensVal);

  const dwellVal = main.querySelector("#dwell-val");
  main.querySelector("#audio-dwell").oninput = (e) => {
    dwellVal.textContent = e.target.value + "ms";
  };
  main.querySelector("#audio-dwell").onchange = (e) =>
    pushLive({ min_dwell_ms: parseInt(e.target.value, 10) }, dwellVal);

  const nbandsVal = main.querySelector("#nbands-val");
  main.querySelector("#audio-nbands").oninput = (e) => {
    nbandsVal.textContent = e.target.value;
  };
  main.querySelector("#audio-nbands").onchange = (e) =>
    pushLive({ n_bands: parseInt(e.target.value, 10) }, nbandsVal);

  const hueVal = main.querySelector("#mono-hue-val");
  main.querySelector("#mono-hue").oninput = (e) => {
    hueVal.textContent = e.target.value + "°";
  };
  main.querySelector("#mono-hue").onchange = (e) =>
    pushLive({ monochrome_hue: parseFloat(e.target.value) }, hueVal);

  const beatSel = main.querySelector("#audio-beat-sensitivity");
  if (beatSel) beatSel.onchange = (e) => pushLive({ beat_sensitivity: e.target.value });

  // ---- audio bridge launcher -------------------------------------------
  const dirEl = main.querySelector("#bridge-dir");
  const argsEl = main.querySelector("#bridge-args");
  const cmdEl = main.querySelector("#bridge-cmd");

  function syncBridgeCmd() {
    if (!dirEl || !cmdEl) return;
    cmdEl.textContent = bridgeCommand(dirEl.value.trim(), argsEl ? argsEl.value : "");
  }

  if (dirEl) {
    dirEl.oninput = () => {
      // Persist per browser so the path survives a reload, and keep it as the
      // override even if the server later supplies a hint.
      localStorage.setItem(BRIDGE_DIR_KEY, dirEl.value);
      syncBridgeCmd();
    };
  }
  if (argsEl) {
    argsEl.oninput = () => {
      localStorage.setItem(BRIDGE_ARGS_KEY, argsEl.value);
      syncBridgeCmd();
    };
  }

  const copyBtn = main.querySelector("#bridge-copy");
  if (copyBtn) {
    copyBtn.onclick = async () => {
      const text = cmdEl ? cmdEl.textContent : "";
      try {
        await navigator.clipboard.writeText(text);
        toast("Command copied", "success");
      } catch (e) {
        // clipboard API needs a secure context; over plain http on a LAN
        // address it simply is not there, so fall back rather than failing.
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); toast("Command copied", "success"); }
        catch (e2) { toast("Could not copy — select the text manually", "error"); }
        document.body.removeChild(ta);
      }
    };
  }

  const resetBtn = main.querySelector("#bridge-reset");
  if (resetBtn) {
    resetBtn.onclick = () => {
      localStorage.removeItem(BRIDGE_DIR_KEY);
      localStorage.removeItem(BRIDGE_ARGS_KEY);
      if (dirEl) dirEl.value = bridgeHostDir(bridge);
      if (argsEl) argsEl.value = "";
      syncBridgeCmd();
      toast("Reset to the server's folder", "success");
    };
  }

  // Show the device picker only when it applies. With the bridge selected the
  // capture device is chosen on the Windows host, not here.
  const sourceSel = main.querySelector("#audio-source");
  if (sourceSel) {
    sourceSel.onchange = () => {
      const row = main.querySelector("#audio-device-row");
      if (row) row.style.display = sourceSel.value === "bridge" ? "none" : "";
    };
  }

  main.querySelector("#audio-start").onclick = async () => {
    const source = sourceSel ? sourceSel.value : "device";
    if (source === "device" && audioDevices.length === 0) {
      toast("No local audio input devices. Use the audio bridge instead.", "error");
      return;
    }
    if (source === "bridge" && !bridgeUsable) {
      toast("The bridge listener is not running", "error");
      return;
    }
    const deviceEl = main.querySelector("#audio-device");
    await post(`/api/devices/${state.deviceId}/audio-reactive/start`, {
      source,
      device_index: (source === "bridge" || !deviceEl) ? 0 : parseInt(deviceEl.value, 10),
      mode: main.querySelector("#audio-mode").value,
      sensitivity: parseFloat(main.querySelector("#audio-sensitivity").value),
      monochrome_hue: parseFloat(main.querySelector("#mono-hue").value),
      n_bands: parseInt(main.querySelector("#audio-nbands").value, 10),
      min_dwell_ms: parseInt(main.querySelector("#audio-dwell").value, 10),
      beat_sensitivity: main.querySelector("#audio-beat-sensitivity").value,
    });
    toast("Audio-reactive session started", "success");
    renderAudio(main);
  };

  main.querySelector("#audio-stop").onclick = async () => {
    await post(`/api/devices/${state.deviceId}/audio-reactive/stop`);
    toast("Audio-reactive session stopped");
    renderAudio(main);
  };

  main.querySelector("#group-audio-start").onclick = async () => {
    const groupId = main.querySelector("#group-select").value;
    if (!groupId) { toast("No groups configured", "error"); return; }
    await post(`/api/groups/${groupId}/audio-reactive/start`, {
      device_index: parseInt(main.querySelector("#audio-device").value, 10),
      mode: main.querySelector("#audio-mode").value,
      role_mode: main.querySelector("#role-mode").value,
      sensitivity: parseFloat(main.querySelector("#audio-sensitivity").value),
      monochrome_hue: parseFloat(main.querySelector("#mono-hue").value),
      min_dwell_ms: parseInt(main.querySelector("#audio-dwell").value, 10),
      beat_sensitivity: main.querySelector("#audio-beat-sensitivity").value,
    });
    toast("Group audio-reactive session started", "success");
    renderAudio(main);
  };

  main.querySelector("#group-audio-stop").onclick = async () => {
    const groupId = main.querySelector("#group-select").value;
    await post(`/api/groups/${groupId}/audio-reactive/stop`);
    toast("Group session stopped");
    renderAudio(main);
  };

  main.querySelector("#tap-tempo-btn").onclick = async () => {
    try {
      const resp = await post(`/api/devices/${state.deviceId}/audio-reactive/tap-tempo`, {});
      const wrap = document.getElementById("tempo-wrap");
      if (wrap && resp.tap_bpm != null) {
        wrap.innerHTML = renderTempoInfo({ ...tempo, tap_bpm: resp.tap_bpm });
      }
    } catch (e) { toast(`Tap tempo failed: ${e.message}`, "error"); }
  };

  // Which custom preset the Save box is currently aimed at. Null = save a new
  // one. Cleared on re-render, deliberately: an edit target that outlived the
  // page would silently overwrite a preset the user had stopped looking at.
  let editingPresetId = null;

  function loadPresetIntoControls(preset) {
    const set = (sel, value, labelSel, fmt) => {
      const elx = main.querySelector(sel);
      if (!elx || value === undefined || value === null) return;
      elx.value = value;
      if (labelSel) {
        const lab = main.querySelector(labelSel);
        if (lab) lab.textContent = fmt ? fmt(value) : value;
      }
    };
    set("#audio-mode", preset.mode);
    set("#audio-sensitivity", preset.sensitivity, "#sens-val", v => Number(v).toFixed(1) + "x");
    set("#audio-dwell", preset.min_dwell_ms, "#dwell-val", v => v + "ms");
    set("#audio-nbands", preset.n_bands, "#nbands-val");
    set("#mono-hue", preset.monochrome_hue, "#mono-hue-val", v => Math.round(v) + "°");
    set("#audio-beat-sensitivity", preset.beat_sensitivity);
    syncModeUI();
  }

  const presetGrid = main.querySelector("#audio-preset-grid");
  const allPresets = audioPresetsResp.presets || [];
  allPresets.forEach(preset => {
    const card = el(`<div class="effect-card" title="${preset.description || ""}">
      <div class="name">${preset.name}${preset.custom ? " ★" : ""}</div>
      <div class="desc">${preset.description || "Custom preset"}</div>
    </div>`);
    card.onclick = async () => {
      const source = main.querySelector("#audio-source").value;
      // Only LOCAL capture needs a device. Requiring one unconditionally made
      // presets unusable in bridge mode — the container reports zero audio
      // devices by design, so this check refused every preset on the one
      // deployment the bridge exists to serve.
      if (source === "device" && audioDevices.length === 0) {
        toast("No audio input devices available", "error");
        return;
      }
      const deviceEl = main.querySelector("#audio-device");
      try {
        const resp = await post(`/api/devices/${state.deviceId}/audio-reactive/apply-preset`, {
          preset_id: preset.id,
          device_index: deviceEl && deviceEl.value ? parseInt(deviceEl.value, 10) : 0,
          source,
        });
        // A live session takes a preset without restarting, so there is no
        // need to rebuild the page and lose the user's scroll position.
        toast(resp.restarted
          ? `Preset "${preset.name}" applied — session started`
          : `Preset "${preset.name}" applied live`, "success");
        loadPresetIntoControls(preset);
        if (resp.restarted) renderAudio(main);
      } catch (e) {
        toast(`Could not apply preset: ${e.message}`, "error");
      }
    };

    // Edit: pull the preset's values into the controls so they can be tweaked
    // by ear, and aim the Save box at it. Editing a custom preset overwrites
    // it (same id); editing a built-in forks a custom copy, because the
    // shipped 24 are a reference set and silently mutating them would leave
    // no way back.
    const editBtn = el(`<button class="btn small ghost" style="margin-top:6px;width:100%;">Edit${preset.custom ? "" : " a copy"}</button>`);
    editBtn.onclick = async (ev) => {
      ev.stopPropagation();
      loadPresetIntoControls(preset);
      await pushLive({
        mode: preset.mode, sensitivity: preset.sensitivity,
        monochrome_hue: preset.monochrome_hue, n_bands: preset.n_bands,
        min_dwell_ms: preset.min_dwell_ms, beat_sensitivity: preset.beat_sensitivity,
      });
      editingPresetId = preset.custom ? preset.id : null;
      const nameEl = main.querySelector("#custom-preset-name");
      nameEl.value = preset.custom ? preset.name : `${preset.name} (mine)`;
      nameEl.scrollIntoView({ behavior: "smooth", block: "center" });
      nameEl.focus();
      toast(preset.custom
        ? `Editing "${preset.name}" — tweak, then Save to overwrite it`
        : `Forking "${preset.name}" — tweak, then Save as your own copy`);
    };
    card.appendChild(editBtn);

    if (preset.custom) {
      const delBtn = el(`<button class="danger" style="margin-top:6px;width:100%;">Delete</button>`);
      delBtn.onclick = async (ev) => {
        ev.stopPropagation();
        await del(`/api/audio/presets/custom/${preset.id}`);
        toast(`Preset "${preset.name}" deleted`);
        renderAudio(main);
      };
      card.appendChild(delBtn);
    }
    presetGrid.appendChild(card);
  });

  main.querySelector("#save-custom-preset").onclick = async () => {
    const name = main.querySelector("#custom-preset-name").value.trim();
    if (!name) { toast("Enter a preset name first", "error"); return; }
    try {
      await post("/api/audio/presets/custom", {
        // Carrying the id through is what makes Save an *edit* rather than a
        // duplicate: the backend replaces by id. Null (a built-in fork, or a
        // fresh preset) lets the backend mint a new one from the name.
        id: editingPresetId || undefined,
        name,
        mode: main.querySelector("#audio-mode").value,
        sensitivity: parseFloat(main.querySelector("#audio-sensitivity").value),
        monochrome_hue: parseFloat(main.querySelector("#mono-hue").value),
        n_bands: parseInt(main.querySelector("#audio-nbands").value, 10),
        min_dwell_ms: parseInt(main.querySelector("#audio-dwell").value, 10),
        beat_sensitivity: main.querySelector("#audio-beat-sensitivity").value,
      });
      toast(`Preset "${name}" saved`, "success");
      renderAudio(main);
    } catch (e) { toast(`Could not save preset: ${e.message}`, "error"); }
  };

  if (sessionStatus.active) {
    state.audioPollHandle = setInterval(async () => {
      try {
        const st = await get(`/api/devices/${state.deviceId}/audio-reactive/status`);
        const wrap = document.getElementById("band-meter-wrap");
        if (!wrap) { stopAudioPolling(); return; }
        wrap.innerHTML = renderBandMeter(st.bands);
        const tempoWrap = document.getElementById("tempo-wrap");
        if (tempoWrap) tempoWrap.innerHTML = renderTempoInfo(st.tempo);
        const latWrap = document.getElementById("latency-wrap");
        if (latWrap) latWrap.innerHTML = renderLatencyPanel(st.latency, st.analysis);
        if (!st.active) renderAudio(main);
      } catch (e) { /* transient poll miss, ignore */ }
    }, 300);
  }
}

async function renderPresets(main) {
  state.presets = state.presets.length ? state.presets : await get("/api/presets");
  const favorites = await get(`/api/devices/${state.deviceId}/favorites`);

  main.innerHTML = `
    <h1 class="panel-title">Presets &amp; Favorites</h1>
    <p class="panel-subtitle">${state.presets.length} built-in color presets, plus your own saved favorites</p>

    <div class="card">
      <h3>Save Current Color as Favorite</h3>
      <div class="row">
        <input type="text" id="fav-name" placeholder="Name (e.g. Cozy Evening)">
        <button id="save-fav" class="primary">Save Current Color</button>
      </div>
    </div>

    <div class="card">
      <h3>Your Favorites</h3>
      <div class="grid" id="fav-grid">${favorites.length === 0 ? '<div class="empty-state">No favorites saved yet</div>' : ""}</div>
    </div>

    <div class="card">
      <h3>Built-in Presets</h3>
      <div class="grid" id="preset-grid"></div>
    </div>
  `;

  main.querySelector("#save-fav").onclick = async () => {
    const name = main.querySelector("#fav-name").value.trim() || "Untitled";
    const st = state.lastStatus;
    const rgb = st && st.hue != null ? hsvToRgb(st.hue, st.saturation_pct ?? 100, st.value_pct ?? 100) : [255, 255, 255];
    await post(`/api/devices/${state.deviceId}/favorites`, { name, r: Math.round(rgb[0]), g: Math.round(rgb[1]), b: Math.round(rgb[2]) });
    toast("Favorite saved", "success");
    renderPresets(main);
  };

  const favGrid = main.querySelector("#fav-grid");
  favorites.forEach(f => {
    const hex = rgbToHex(...f.rgb);
    const card = el(`<div class="swatch" style="background:${hex}" title="${f.name}">
      <span class="label">${f.name}</span>
    </div>`);
    card.onclick = async () => {
      await post(`/api/devices/${state.deviceId}/color`, { r: f.rgb[0], g: f.rgb[1], b: f.rgb[2] });
      toast(`Applied "${f.name}"`, "success");
    };
    card.oncontextmenu = async (e) => {
      e.preventDefault();
      await del(`/api/devices/${state.deviceId}/favorites/${f.id}`);
      toast("Favorite deleted");
      renderPresets(main);
    };
    favGrid.appendChild(card);
  });

  const presetGrid = main.querySelector("#preset-grid");
  state.presets.forEach(p => {
    const hex = rgbToHex(...p.rgb);
    const card = el(`<div class="swatch" style="background:${hex}" title="${p.name}">
      <span class="label">${p.name}</span>
    </div>`);
    card.onclick = async () => {
      await post(`/api/devices/${state.deviceId}/presets/apply`, { preset_id: p.id });
      toast(`Applied "${p.name}"`, "success");
    };
    presetGrid.appendChild(card);
  });
}

// -------------------------------------------------------------- timers --
const TIMERS_RESYNC_MS = 30000; // real re-fetch cadence; the 1s countdowns in between are client-side only

function stopTimersLiveUpdates() {
  if (state.sleepCountdownHandle) { clearInterval(state.sleepCountdownHandle); state.sleepCountdownHandle = null; }
  if (state.wakeCountdownHandle) { clearInterval(state.wakeCountdownHandle); state.wakeCountdownHandle = null; }
  if (state.timersResyncHandle) { clearInterval(state.timersResyncHandle); state.timersResyncHandle = null; }
}

function formatMinSec(totalSeconds) {
  totalSeconds = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}m ${s}s`;
}

function formatHrsMinSec(totalSeconds) {
  totalSeconds = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// Recomputes seconds-until-fire for a wake timer straight from the real "HH:MM" target
// and wall-clock time (mirrors backend/bulb_manager.py's seconds_until()), so the
// countdown can't drift the way decrementing a locally-cached copy would.
function computeWakeSecondsRemaining(hhmm) {
  const now = new Date();
  const [hh, mm] = hhmm.split(":").map(Number);
  const target = new Date(now);
  target.setHours(hh, mm, 0, 0);
  if (target <= now) target.setDate(target.getDate() + 1);
  return (target.getTime() - now.getTime()) / 1000;
}

async function renderTimers(main) {
  stopTimersLiveUpdates();
  const sleepSt = await get(`/api/devices/${state.deviceId}/timers/sleep`);
  const wakeSt = await get(`/api/devices/${state.deviceId}/timers/wake`);

  main.innerHTML = `
    <h1 class="panel-title">Timers</h1>
    <p class="panel-subtitle">Sleep timers fade the bulb out before turning off; wake timers fade it in as an alarm</p>

    <div class="card">
      <h3>Sleep Timer</h3>
      ${sleepSt.active
        ? `<p>Active — turns off in <b class="timer-countdown" id="sleep-countdown">${formatMinSec(sleepSt.seconds_remaining)}</b></p>
           <button id="cancel-sleep" class="danger">Cancel Sleep Timer</button>`
        : `<div class="row">
             <button data-min="5">5 min</button>
             <button data-min="15">15 min</button>
             <button data-min="30">30 min</button>
             <button data-min="60">60 min</button>
             <input type="number" id="custom-min" placeholder="Custom minutes" style="width:140px;">
             <button id="start-custom-sleep" class="primary">Start Custom</button>
           </div>`
      }
    </div>

    <div class="card">
      <h3>Wake Timer (Sunrise Alarm)</h3>
      ${wakeSt.active
        ? `<p>Active — will fire at <b>${wakeSt.target}</b> (in <span class="timer-countdown" id="wake-countdown">${formatHrsMinSec(computeWakeSecondsRemaining(wakeSt.target))}</span>), fading in over ${wakeSt.fade_minutes} min to ${wakeSt.brightness}%</p>
           <button id="cancel-wake" class="danger">Cancel Wake Timer</button>`
        : `<div class="form-grid">
             <label>Time<input type="time" id="wake-time" value="07:00"></label>
             <label>Target Brightness %<input type="number" id="wake-brightness" value="100" min="1" max="100"></label>
             <label>Color Temp %<input type="number" id="wake-temp" value="70" min="0" max="100"></label>
             <label>Fade Duration (min)<input type="number" id="wake-fade" value="10" min="1" max="60"></label>
           </div>
           <button id="start-wake" class="primary" style="margin-top:10px;">Set Wake Timer</button>`
      }
    </div>
  `;

  if (sleepSt.active) {
    main.querySelector("#cancel-sleep").onclick = async () => {
      stopTimersLiveUpdates();
      // Capture params before cancelling so Undo can replay them. The GET status
      // only exposes remaining seconds (not the original duration picked), so
      // "same parameters it had" is reconstructed as a fresh timer for however
      // long was left on the clock at the moment of cancellation.
      const undoMinutes = Math.max(1, Math.ceil(sleepSt.seconds_remaining / 60));
      await del(`/api/devices/${state.deviceId}/timers/sleep`);
      toast("Sleep timer cancelled", null, async () => {
        await post(`/api/devices/${state.deviceId}/timers/sleep`, { minutes: undoMinutes });
        toast(`Sleep timer restored (${undoMinutes} min)`, "success");
        renderTimers(main);
      });
      renderTimers(main);
    };
  } else {
    main.querySelectorAll("button[data-min]").forEach(b => {
      b.onclick = async () => {
        await post(`/api/devices/${state.deviceId}/timers/sleep`, { minutes: parseInt(b.dataset.min) });
        toast(`Sleep timer set for ${b.dataset.min} minutes`, "success");
        renderTimers(main);
      };
    });
    main.querySelector("#start-custom-sleep").onclick = async () => {
      const min = parseInt(main.querySelector("#custom-min").value);
      if (!min) return toast("Enter a valid number of minutes", "error");
      await post(`/api/devices/${state.deviceId}/timers/sleep`, { minutes: min });
      toast(`Sleep timer set for ${min} minutes`, "success");
      renderTimers(main);
    };
  }

  if (wakeSt.active) {
    main.querySelector("#cancel-wake").onclick = async () => {
      stopTimersLiveUpdates();
      // Capture the exact wake-timer params (time/brightness/color_temp/fade)
      // before cancelling so Undo can recreate the same timer.
      const undoParams = {
        time: wakeSt.target,
        brightness: wakeSt.brightness,
        color_temp: wakeSt.color_temp,
        fade_minutes: wakeSt.fade_minutes,
      };
      await del(`/api/devices/${state.deviceId}/timers/wake`);
      toast("Wake timer cancelled", null, async () => {
        await post(`/api/devices/${state.deviceId}/timers/wake`, undoParams);
        toast("Wake timer restored", "success");
        renderTimers(main);
      });
      renderTimers(main);
    };
  } else {
    main.querySelector("#start-wake").onclick = async () => {
      await post(`/api/devices/${state.deviceId}/timers/wake`, {
        time: main.querySelector("#wake-time").value,
        brightness: parseInt(main.querySelector("#wake-brightness").value),
        color_temp: parseInt(main.querySelector("#wake-temp").value),
        fade_minutes: parseInt(main.querySelector("#wake-fade").value),
      });
      toast("Wake timer set", "success");
      renderTimers(main);
    };
  }

  // ---- live client-side countdown ticks (1s), no re-fetch --------------
  let sleepSecondsRemaining = sleepSt.active ? sleepSt.seconds_remaining : null;
  if (sleepSt.active) {
    state.sleepCountdownHandle = setInterval(() => {
      const countdownEl = document.getElementById("sleep-countdown");
      if (!countdownEl || currentRoute() !== "timers") {
        clearInterval(state.sleepCountdownHandle);
        state.sleepCountdownHandle = null;
        return;
      }
      sleepSecondsRemaining -= 1;
      if (sleepSecondsRemaining <= 0) {
        clearInterval(state.sleepCountdownHandle);
        state.sleepCountdownHandle = null;
        renderTimers(main); // re-fetch real state once to confirm it actually turned off
        return;
      }
      countdownEl.textContent = formatMinSec(sleepSecondsRemaining);
    }, 1000);
  }

  if (wakeSt.active) {
    state.wakeCountdownHandle = setInterval(() => {
      const countdownEl = document.getElementById("wake-countdown");
      if (!countdownEl || currentRoute() !== "timers") {
        clearInterval(state.wakeCountdownHandle);
        state.wakeCountdownHandle = null;
        return;
      }
      const secondsRemaining = computeWakeSecondsRemaining(wakeSt.target);
      if (secondsRemaining <= 0) {
        clearInterval(state.wakeCountdownHandle);
        state.wakeCountdownHandle = null;
        renderTimers(main); // re-fetch real state once to confirm it actually fired
        return;
      }
      countdownEl.textContent = formatHrsMinSec(secondsRemaining);
    }, 1000);
  }

  // ---- periodic real resync (~30s) --------------------------------------
  // Actually re-fetches both timers so this view self-corrects if the timer was
  // changed/cancelled from another tab or device, rather than ticking down a
  // countdown that no longer reflects reality.
  state.timersResyncHandle = setInterval(async () => {
    if (currentRoute() !== "timers") {
      clearInterval(state.timersResyncHandle);
      state.timersResyncHandle = null;
      return;
    }
    let freshSleep, freshWake;
    try {
      freshSleep = await get(`/api/devices/${state.deviceId}/timers/sleep`);
      freshWake = await get(`/api/devices/${state.deviceId}/timers/wake`);
    } catch (e) {
      return; // transient network miss — keep the local countdown ticking, try again next cycle
    }
    if (currentRoute() !== "timers") return; // navigated away while the fetch was in flight

    const sleepChanged = freshSleep.active !== sleepSt.active ||
      (freshSleep.active && Math.abs(freshSleep.seconds_remaining - sleepSecondsRemaining) > 3);
    const wakeChanged = freshWake.active !== wakeSt.active ||
      (freshWake.active && freshWake.target !== wakeSt.target);

    if (sleepChanged || wakeChanged) {
      renderTimers(main);
      return;
    }
    if (freshSleep.active) sleepSecondsRemaining = freshSleep.seconds_remaining;
  }, TIMERS_RESYNC_MS);
}

async function renderSchedule(main) {
  const rules = await get(`/api/devices/${state.deviceId}/schedule`);
  main.innerHTML = `
    <h1 class="panel-title">Schedule</h1>
    <p class="panel-subtitle">Recurring time-of-day rules — checked every 20 seconds by the backend scheduler</p>

    <div class="card">
      <h3>New Rule</h3>
      <div class="form-grid">
        <label>Time<input type="time" id="rule-time" value="18:00"></label>
        <label>Action
          <select id="rule-action">
            <option value="power_on">Turn On</option>
            <option value="power_off">Turn Off</option>
            <option value="scene">Apply Scene</option>
            <option value="preset">Apply Preset</option>
          </select>
        </label>
        <label id="rule-param-wrap" style="display:none;">Value<select id="rule-param"></select></label>
      </div>
      <button id="add-rule" class="primary" style="margin-top:10px;">Add Rule</button>
    </div>

    <div class="card">
      <h3>Active Rules</h3>
      ${rules.length === 0 ? '<div class="empty-state">No schedule rules yet</div>' : `
      <table>
        <thead><tr><th>Time</th><th>Action</th><th>Status</th><th></th></tr></thead>
        <tbody>${rules.map(r => `
          <tr>
            <td>${r.time}</td>
            <td>${r.action}${r.params.scene_id ? " → " + r.params.scene_id : ""}${r.params.preset_id ? " → " + r.params.preset_id : ""}</td>
            <td><span class="tag ${r.enabled ? "on" : "off"}">${r.enabled ? "enabled" : "disabled"}</span></td>
            <td><button data-id="${r.id}" class="danger delete-rule">Delete</button></td>
          </tr>`).join("")}
        </tbody>
      </table>`}
    </div>
  `;

  const actionSel = main.querySelector("#rule-action");
  const paramWrap = main.querySelector("#rule-param-wrap");
  const paramSel = main.querySelector("#rule-param");
  async function refreshParamOptions() {
    if (actionSel.value === "scene") {
      const scenes = state.scenes.length ? state.scenes : await get("/api/scenes");
      paramSel.innerHTML = scenes.map(s => `<option value="${s.id}">${s.name}</option>`).join("");
      paramWrap.style.display = "";
    } else if (actionSel.value === "preset") {
      const presets = state.presets.length ? state.presets : await get("/api/presets");
      paramSel.innerHTML = presets.map(p => `<option value="${p.id}">${p.name}</option>`).join("");
      paramWrap.style.display = "";
    } else {
      paramWrap.style.display = "none";
    }
  }
  actionSel.onchange = refreshParamOptions;
  refreshParamOptions();

  main.querySelector("#add-rule").onclick = async () => {
    const action = actionSel.value;
    let params = {};
    if (action === "scene") params = { scene_id: paramSel.value };
    if (action === "preset") params = { preset_id: paramSel.value };
    await post(`/api/devices/${state.deviceId}/schedule`, {
      time: main.querySelector("#rule-time").value, days: ["daily"], action, params,
    });
    toast("Schedule rule added", "success");
    renderSchedule(main);
  };

  main.querySelectorAll(".delete-rule").forEach(b => {
    b.onclick = async () => {
      await del(`/api/schedule/${b.dataset.id}`);
      toast("Rule deleted");
      renderSchedule(main);
    };
  });
}

async function renderGroups(main) {
  const groups = await get("/api/groups");
  main.innerHTML = `
    <h1 class="panel-title">Groups</h1>
    <p class="panel-subtitle">Control multiple bulbs at once — ready for when you add more devices</p>
    ${groups.length === 0 ? '<div class="empty-state">No groups configured</div>' : ""}
    ${groups.map(g => `
      <div class="card">
        <h3>${g.name} <span style="color:var(--text-faint);font-weight:400;">(${g.device_ids.length} device${g.device_ids.length === 1 ? "" : "s"})</span></h3>
        <div class="row">
          <button data-group="${g.id}" data-on="true" class="group-power primary">Turn All On</button>
          <button data-group="${g.id}" data-on="false" class="group-power">Turn All Off</button>
          <input type="color" data-group="${g.id}" class="group-color" value="#ffffff">
        </div>
      </div>
    `).join("")}
  `;
  main.querySelectorAll(".group-power").forEach(b => {
    b.onclick = async () => {
      await post(`/api/groups/${b.dataset.group}/power`, { on: b.dataset.on === "true" });
      toast("Group updated", "success");
    };
  });
  main.querySelectorAll(".group-color").forEach(inp => {
    inp.onchange = async () => {
      const hex = inp.value;
      const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
      await post(`/api/groups/${inp.dataset.group}/color`, { r, g, b });
      toast("Group color updated", "success");
    };
  });
}

function historyRow(h) {
  return `<tr>
    <td>${escHtml(h.timestamp.replace("T", " ").slice(0, 19))}</td>
    <td>${escHtml(h.action)}</td>
    <td><code class="inline">${escHtml(JSON.stringify(h.params))}</code></td>
    <td><span class="tag ${h.ok ? "on" : "error"}">${h.ok ? "ok" : "error: " + escHtml(String(h.error))}</span></td>
  </tr>`;
}

async function renderHistory(main) {
  const hist = await get(`/api/devices/${state.deviceId}/history`);
  main.innerHTML = `
    <h1 class="panel-title">History <span class="live-dot" title="Reconnecting…"></span></h1>
    <p class="panel-subtitle">
      Every action taken on this device, newest first — streamed live as it happens
      (in-memory, resets on backend restart). <span id="hist-count">${hist.length}</span> shown.
    </p>
    <div id="hist-empty" class="empty-state" ${hist.length ? 'hidden' : ''}>
      No actions logged yet. Change a colour or flip the power and it will appear here immediately.
    </div>
    <table id="hist-table" ${hist.length ? '' : 'hidden'}>
      <thead><tr><th>Time (UTC)</th><th>Action</th><th>Params</th><th>Result</th></tr></thead>
      <tbody id="hist-body">${hist.map(historyRow).join("")}</tbody>
    </table>
  `;
  renderLiveBadge();

  // Prepend rather than re-fetch: the backend already pushed the exact entry
  // it appended, so refetching the whole list on every event would be a
  // round trip to learn something we were just told.
  if (live.unsubHistory) live.unsubHistory();
  live.unsubHistory = liveOn("history", (h) => {
    if (h.device_id !== state.deviceId) return;
    const body = document.getElementById("hist-body");
    if (!body) return;                       // panel was torn down mid-flight
    document.getElementById("hist-table").hidden = false;
    document.getElementById("hist-empty").hidden = true;
    body.insertAdjacentHTML("afterbegin", historyRow(h));
    // Match the backend's own HISTORY_LIMIT so a long session can't grow the
    // DOM without bound on a page nobody has reloaded in hours.
    while (body.children.length > 200) body.removeChild(body.lastChild);
    const c = document.getElementById("hist-count");
    if (c) c.textContent = body.children.length;
    const row = body.firstElementChild;
    if (row) {
      row.classList.add("row-new");
      setTimeout(() => row.classList.remove("row-new"), 1200);
    }
  });
}

// ------------------------------------------------------ system health ----
// The backend's own health, deliberately separate from the per-device
// Diagnostics tab. Everything here is one round trip to
// /api/system/health-summary plus an on-demand log tail.
function fmtUptime(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function fmtMs(value) {
  return value == null ? "—" : `${value} ms`;
}

async function renderHealth(main) {
  const health = await get("/api/system/health-summary");
  const levels = (await get("/api/system/log-level")).levels;
  const p = health.process;

  main.innerHTML = `
    <h1 class="panel-title">System Health</h1>
    <p class="panel-subtitle">
      How the <b>backend</b> itself is doing — uptime, dependencies, request latency and logs.
      For "is this one bulb reachable", use the Diagnostics tab.
    </p>

    <div class="card">
      <h3>
        Backend <span class="tag ${health.healthy ? "on" : "error"}">${health.healthy ? "HEALTHY" : "ATTENTION"}</span>
        <span class="tag on">LIVE DATA</span>
      </h3>
      ${health.problems.length === 0
        ? `<p class="panel-subtitle">No problems detected.</p>`
        : `<ul class="health-problems">${health.problems.map(x => `<li>${escHtml(x)}</li>`).join("")}</ul>`}
      <table><tbody>
        <tr><td>Version</td><td>${escHtml(p.version)}</td></tr>
        <tr><td>Uptime</td><td>${fmtUptime(p.uptime_seconds)}</td></tr>
        <tr><td>Started</td><td>${new Date(p.started_at).toLocaleString()}</td></tr>
        <tr><td>Python</td><td>${escHtml(p.python)}</td></tr>
        <tr><td>Log level</td><td>${escHtml(p.log_level)}</td></tr>
      </tbody></table>
    </div>

    <div class="card">
      <h3>Dependencies</h3>
      <p class="panel-subtitle">
        Checked at startup, not just imported — a package that imports fine and then fails
        on first real use is the failure mode worth catching.
      </p>
      <table>
        <thead><tr><th>Package</th><th>Required</th><th>State</th><th>Detail</th></tr></thead>
        <tbody>${health.dependencies.checks.map(c => `
          <tr>
            <td>${escHtml(c.name)}${c.version ? ` <span class="dim">${escHtml(c.version)}</span>` : ""}</td>
            <td>${c.required ? "yes" : "optional"}</td>
            <td><span class="tag ${c.ok ? "on" : (c.required ? "error" : "off")}">${c.ok ? "OK" : "UNAVAILABLE"}</span></td>
            <td class="dim">${escHtml(c.detail || c.why)}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3>Requests</h3>
      <p class="panel-subtitle">
        ${health.requests.requests} handled · ${health.requests.errors} server error(s)
        (${(health.requests.error_rate * 100).toFixed(1)}%) ·
        ${health.requests.client_errors} client error(s).
        Percentiles are over a rolling window of recent requests per endpoint, so they
        answer "is it slow now", not "was it ever slow".
      </p>
      ${health.endpoints.length === 0 ? `
        <p class="panel-subtitle">No requests recorded yet this run.</p>
      ` : `
        <table>
          <thead><tr><th>Endpoint</th><th>Calls</th><th>p50</th><th>p95</th><th>p99</th><th>Errors</th></tr></thead>
          <tbody>${health.endpoints.slice(0, 25).map(e => `
            <tr>
              <td><code class="inline">${escHtml(e.method)} ${escHtml(e.endpoint)}</code></td>
              <td>${e.requests}</td>
              <td>${fmtMs(e.p50_ms)}</td>
              <td>${fmtMs(e.p95_ms)}</td>
              <td>${fmtMs(e.p99_ms)}</td>
              <td>${e.errors ? `<span class="tag error">${e.errors}</span>` : "0"}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      `}
      <p class="panel-subtitle" style="margin-top:10px;">
        Raw Prometheus metrics: <code class="inline">GET /metrics</code>
      </p>
    </div>

    <div class="card">
      <h3>Network <span class="tag ${health.network.bulb_control_available ? "on" : "error"}">${escHtml(health.network.mode)}</span></h3>
      <p class="panel-subtitle">${escHtml(health.network.message)}</p>
      <table><tbody>${health.network.host_ips.map(h => `
        <tr><td><code class="inline">${escHtml(h.ip)}</code></td><td>${escHtml(h.class)}</td></tr>
      `).join("")}</tbody></table>
    </div>

    <div class="card">
      <h3>Bulb Latency Over Time</h3>
      ${health.bulb_latency.length === 0 ? `
        <p class="panel-subtitle">No samples yet — latency is recorded from real status calls, so this fills in as the dashboard is used.</p>
      ` : `
        <table>
          <thead><tr><th>Device</th><th>Samples</th><th>p50</th><th>p95</th><th>Max</th><th>Failures</th></tr></thead>
          <tbody>${health.bulb_latency.map(b => `
            <tr>
              <td>${escHtml(b.device_id)}</td>
              <td>${b.sample_count}</td>
              <td>${fmtMs(b.p50_ms)}</td>
              <td>${fmtMs(b.p95_ms)}</td>
              <td>${fmtMs(b.max_ms)}</td>
              <td>${b.failure_count ? `<span class="tag error">${(b.failure_rate * 100).toFixed(0)}%</span>` : "0"}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      `}
    </div>

    <div class="card">
      <h3>Logs</h3>
      <div class="row">
        <label class="inline-label">Show
          <select id="log-filter">
            <option value="">everything</option>
            ${levels.map(l => `<option value="${escAttr(l)}">${escHtml(l)} and above</option>`).join("")}
          </select>
        </label>
        <button id="log-refresh">Refresh</button>
        <button id="diag-report">Download Diagnostic Report</button>
      </div>
      <p class="panel-subtitle" style="margin-top:8px;">
        The diagnostic report bundles config shape, dependency state, metrics and recent
        logs with every secret stripped — for attaching to a bug report. Skim it before
        sharing anyway: IPs and device names are left intact on purpose.
      </p>
      <div id="log-view" class="log-view">Loading…</div>
    </div>
  `;

  async function loadLogs() {
    const level = main.querySelector("#log-filter").value;
    const body = await get(`/api/system/logs?limit=100${level ? `&level=${encodeURIComponent(level)}` : ""}`);
    const view = main.querySelector("#log-view");
    if (!body.entries.length) {
      view.innerHTML = `<div class="empty-state">No log entries buffered at this level yet.</div>`;
      return;
    }
    view.innerHTML = body.entries.map(e => `
      <div class="log-line ${escAttr(e.level.toLowerCase())}">
        <span class="log-ts">${new Date(e.timestamp).toLocaleTimeString()}</span>
        <span class="log-level">${escHtml(e.level)}</span>
        <span class="log-cid">${escHtml(e.correlation_id || "—")}</span>
        <span class="log-msg">${escHtml(e.message)}</span>
      </div>`).join("");
  }

  main.querySelector("#log-filter").onchange = loadLogs;
  main.querySelector("#log-refresh").onclick = loadLogs;
  main.querySelector("#diag-report").onclick = async (e) => {
    e.target.disabled = true;
    try {
      const report = await get("/api/system/diagnostic-report");
      const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `smart-bulb-diagnostic-${new Date().toISOString().slice(0, 19).replace(/:/g, "")}.json`;
      link.click();
      URL.revokeObjectURL(url);
      toast("Diagnostic report downloaded (secrets redacted)", "success");
    } finally {
      e.target.disabled = false;
    }
  };
  await loadLogs();
}


async function renderDiagnostics(main) {
  const device = state.devices.find(d => d.id === state.deviceId) || {};
  main.innerHTML = `
    <h1 class="panel-title">Diagnostics</h1>
    <p class="panel-subtitle">Test connectivity and troubleshoot the local connection to this bulb</p>
    <div class="card">
      <h3>Device Info</h3>
      <table>
        <tbody>
          <tr>
            <td>Device ID</td>
            <td><code class="inline">${device.device_id || "—"}</code></td>
            <td><button type="button" class="copy-btn" data-copy-text="${device.device_id || ""}">Copy</button></td>
          </tr>
          <tr>
            <td>IP Address</td>
            <td><code class="inline">${device.ip || "—"}</code></td>
            <td><button type="button" class="copy-btn" data-copy-text="${device.ip || ""}">Copy</button></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="card">
      <div class="row">
        <button id="test-conn" class="primary">Run Connection Test</button>
        <button id="rescan-btn">Rescan Network for New IP</button>
      </div>
      <div id="diag-result" style="margin-top:16px;"></div>
    </div>
    <div class="card">
      <h3>Latency Over Time <span class="tag on">LIVE DATA</span></h3>
      <p class="panel-subtitle">
        Recorded from every real status call to this bulb, so it builds up as the
        dashboard is used — no extra probe traffic. In-memory: resets on backend restart.
      </p>
      <div id="latency-history">Loading…</div>
    </div>

    <div class="card">
      <h3>Tailscale</h3>
      <p class="panel-subtitle">
        Whether Tailscale is actually running on the machine hosting this dashboard,
        and what URL that makes it reachable at from your phone. Checked on demand.
      </p>
      <button id="tailscale-check">Check Tailscale Status</button>
      <div id="tailscale-result" style="margin-top:12px;"></div>
    </div>

    <div class="card">
      <h3>System Info</h3>
      <div id="sys-info">Loading…</div>
    </div>
    <div class="card">
      <h3>Rate limiting <span class="tag on">LIVE DATA</span></h3>
      <p class="panel-subtitle">
        Per-IP limits on the public API, and the PIN gate's own lockout counters.
        Two separate mechanisms. Counters are in-memory and reset when the
        backend restarts. LAN and loopback clients are exempt by default, so
        these usually read zero on a local-only setup — that's correct, not broken.
      </p>
      <div id="rate-limit-info">Loading…</div>
    </div>
  `;
  main.querySelector("#test-conn").onclick = async () => {
    main.querySelector("#diag-result").innerHTML = "Testing…";
    const r = await post(`/api/devices/${state.deviceId}/test-connection`);
    main.querySelector("#diag-result").innerHTML = `
      <table>
        <tbody>
          <tr><td>IP</td><td>${r.ip} <button type="button" class="copy-btn" data-copy-text="${r.ip}">Copy</button></td></tr>
          <tr><td>TCP :6668 reachable</td><td><span class="tag ${r.tcp_6668_reachable ? "on" : "error"}">${r.tcp_6668_reachable}</span></td></tr>
          <tr><td>TCP latency</td><td>${r.tcp_latency_ms} ms</td></tr>
          <tr><td>Status query ok</td><td><span class="tag ${r.status_ok ? "on" : "error"}">${r.status_ok}</span></td></tr>
          <tr><td>Status latency</td><td>${r.status_latency_ms} ms</td></tr>
          ${r.status_error ? `<tr><td>Error</td><td>${r.status_error}</td></tr>` : ""}
        </tbody>
      </table>`;
  };
  main.querySelector("#rescan-btn").onclick = async () => {
    main.querySelector("#diag-result").innerHTML = "Scanning LAN (up to ~18s)…";
    const r = await post(`/api/devices/${state.deviceId}/rescan`);
    main.querySelector("#diag-result").innerHTML = r.found
      ? `<div class="empty-state">Found device at new IP: <b>${r.ip}</b> — config updated.</div>`
      : `<div class="empty-state">Device not found on network. ${r.error || "Check power/Wi-Fi."}</div>`;
  };
  main.querySelector("#tailscale-check").onclick = async (e) => {
    const target = main.querySelector("#tailscale-result");
    e.target.disabled = true;
    target.innerHTML = "Checking…";
    try {
      const ts = await get("/api/system/remote-access/tailscale");
      target.innerHTML = `
        <table><tbody>
          <tr><td>Installed on host</td><td><span class="tag ${ts.installed ? "on" : "off"}">${ts.installed}</span></td></tr>
          <tr><td>Running</td><td><span class="tag ${ts.running ? "on" : "off"}">${ts.running}</span></td></tr>
          <tr><td>Backend state</td><td>${escHtml(ts.backend_state || "—")}</td></tr>
          <tr><td>MagicDNS name</td><td><code class="inline">${escHtml(ts.magic_dns_name || "—")}</code></td></tr>
          <tr><td>Tailnet IPs</td><td><code class="inline">${escHtml(ts.tailscale_ips.join(", ") || "—")}</code></td></tr>
          <tr><td>Peers</td><td>${ts.peer_count == null ? "—" : ts.peer_count}</td></tr>
          ${ts.tailnet_url ? `
            <tr><td>Reachable at</td><td>
              <code class="inline">${escHtml(ts.tailnet_url)}</code>
              <button type="button" class="copy-btn" data-copy-text="${escAttr(ts.tailnet_url)}">Copy</button>
            </td></tr>` : ""}
          ${ts.error ? `<tr><td>Note</td><td class="dim">${escHtml(ts.error)}</td></tr>` : ""}
        </tbody></table>`;
    } finally {
      e.target.disabled = false;
    }
  };

  const latency = await get(`/api/devices/${state.deviceId}/latency-history?limit=25`);
  main.querySelector("#latency-history").innerHTML = latency.sample_count === 0 ? `
    <div class="empty-state">No samples yet. Run a connection test above, or just use the dashboard — every status call adds one.</div>
  ` : `
    <table><tbody>
      <tr><td>Samples</td><td>${latency.sample_count} (window ${latency.window})</td></tr>
      <tr><td>p50 / p95</td><td>${fmtMs(latency.p50_ms)} / ${fmtMs(latency.p95_ms)}</td></tr>
      <tr><td>Min / Max</td><td>${fmtMs(latency.min_ms)} / ${fmtMs(latency.max_ms)}</td></tr>
      <tr><td>Failures</td><td>${latency.failure_count} (${(latency.failure_rate * 100).toFixed(0)}%)</td></tr>
    </tbody></table>
    <table style="margin-top:12px;">
      <thead><tr><th>When</th><th>Latency</th><th>Result</th></tr></thead>
      <tbody>${latency.samples.map(s => `
        <tr>
          <td>${new Date(s.at).toLocaleTimeString()}</td>
          <td>${fmtMs(s.latency_ms)}</td>
          <td><span class="tag ${s.ok ? "on" : "error"}">${s.ok ? "ok" : "failed"}</span></td>
        </tr>`).join("")}
      </tbody>
    </table>`;

  const info = await get("/api/system/info");
  main.querySelector("#sys-info").innerHTML = `
    <table><tbody>
      <tr><td>Version</td><td>${info.version}</td></tr>
      <tr><td>Uptime</td><td>${Math.round(info.uptime_seconds)}s</td></tr>
      <tr><td>Presets</td><td>${info.presets_count}</td></tr>
      <tr><td>Scenes</td><td>${info.scenes_count}</td></tr>
      <tr><td>Effects</td><td>${info.effects_count}</td></tr>
    </tbody></table>`;

  const rl = await get("/api/system/diagnostics/rate-limit");
  const limits = rl.api.config.limits;
  const busiest = rl.api.current_window_usage;
  main.querySelector("#rate-limit-info").innerHTML = `
    <table><tbody>
      <tr><td>Enforcement</td><td><span class="tag ${rl.api.config.enabled ? "on" : "off"}">${rl.api.config.enabled ? "ON" : "OFF"}</span></td></tr>
      <tr><td>LAN/loopback exempt</td><td>${rl.api.config.exempt_local ? "yes" : "no"}</td></tr>
      <tr><td>Per-minute allowance</td><td>poll ${limits.poll} · read ${limits.read} · write ${limits.write} · expensive ${limits.expensive}</td></tr>
      <tr><td>Requests counted / exempted</td><td>${rl.api.allowed} / ${rl.api.exempt}</td></tr>
      <tr><td>Requests rejected (429)</td><td><span class="tag ${rl.api.blocked ? "error" : "on"}">${rl.api.blocked}</span></td></tr>
      <tr><td>Last rejection</td><td>${rl.api.last_blocked_at ? `${new Date(rl.api.last_blocked_at * 1000).toLocaleString()} — <code class="inline">${escHtml(rl.api.last_blocked_path || "")}</code>` : "never"}</td></tr>
      <tr><td>Failed logins</td><td>${rl.auth.login_failure}</td></tr>
      <tr><td>PIN lockouts triggered</td><td><span class="tag ${rl.auth.lockouts_triggered ? "error" : "on"}">${rl.auth.lockouts_triggered}</span></td></tr>
      <tr><td>IPs locked out right now</td><td>${rl.auth.locked_out_now}</td></tr>
      <tr><td>Login endpoint throttles</td><td>${rl.auth.login_rate_limit_blocks}</td></tr>
    </tbody></table>
    ${busiest.length ? `
      <p class="panel-subtitle" style="margin-top:14px;">Busiest clients in the current 60s window</p>
      <table>
        <thead><tr><th>Client / tier</th><th>Requests</th></tr></thead>
        <tbody>${busiest.map(u => `<tr><td><code class="inline">${escHtml(u.client)}</code></td><td>${u.requests}</td></tr>`).join("")}</tbody>
      </table>` : `<p class="panel-subtitle" style="margin-top:14px;">No non-exempt clients have made a request in the last 60 seconds.</p>`}
  `;
}

// Grades the PIN the user is typing against the backend's own rules, so the
// Settings form says why a PIN will be refused before the request is sent.
// Debounced because it fires per keystroke; the server applies the same
// rules regardless of what this said, so it is advisory only.
function wirePinStrengthMeter(input, output) {
  if (!input || !output) return;
  let timer = null;
  input.addEventListener("input", () => {
    const pin = input.value;
    clearTimeout(timer);
    if (!pin) { output.innerHTML = ""; return; }
    timer = setTimeout(async () => {
      let verdict;
      try {
        verdict = await post("/api/system/remote-auth/pin-strength", { pin });
      } catch (e) { return; }
      if (input.value !== pin) return;  // typed on while in flight
      const notes = verdict.issues.length ? verdict.issues : verdict.hints;
      output.innerHTML =
        `<span class="tag ${verdict.ok ? "on" : "error"}">${verdict.ok ? verdict.strength.toUpperCase() : "REJECTED"}</span>` +
        (notes.length ? ` ${escHtml(notes.map(n => (verdict.ok ? n : "PIN " + n)).join("; "))}` : "");
    }, 250);
  });
}

// ---------------------------------------------------------- security log --
// Distinct from History (per-device actions). This is the install's own
// security record: auth events, config changes, backups, tamper checks.

const SEVERITY_ORDER = ["info", "notice", "warning", "critical"];

// Remembered across re-renders so a filter survives hitting Refresh — the
// whole point of a search UI is iterating on the query.
const securityFilters = { q: "", event: "", min_severity: "", limit: 100, include_rotated: false };

function severityTag(sev) {
  const cls = { info: "off", notice: "on", warning: "warn", critical: "error" }[sev] || "off";
  return `<span class="tag ${cls}">${escHtml(sev)}</span>`;
}

function fmtTs(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "—";
}

async function renderSecurity(main) {
  const [cfg, verification, alertsBody] = await Promise.all([
    get("/api/security/config"),
    get("/api/security/verify"),
    get("/api/security/alerts?limit=20"),
  ]);
  const params = new URLSearchParams({
    limit: String(securityFilters.limit),
    include_rotated: String(securityFilters.include_rotated),
  });
  if (securityFilters.q) params.set("q", securityFilters.q);
  if (securityFilters.event) params.set("event", securityFilters.event);
  if (securityFilters.min_severity) params.set("min_severity", securityFilters.min_severity);
  const body = await get(`/api/security/events?${params.toString()}`);
  const alerts = alertsBody.alerts || [];

  // Three states, not two: a chain that verifies but can't reach the start
  // (because retention pruned an old segment) is normal housekeeping, and
  // showing it as a failure would train you to ignore a real one.
  const chainState = !verification.ok ? "error" : (verification.complete ? "on" : "off");
  const chainLabel = !verification.ok ? "TAMPERING DETECTED"
    : (verification.complete ? "VERIFIED" : "VERIFIED (PARTIAL)");

  main.innerHTML = `
    <h1 class="panel-title">Security Log</h1>
    <p class="panel-subtitle">
      Security-relevant events for this install — logins, lockouts, config changes,
      devices appearing, backups and restores. Separate from the per-device
      <b>History</b> tab, which records what the bulbs did.
    </p>

    ${!verification.ok ? `
      <div class="callout danger">
        <b>The security log failed its integrity check.</b>
        ${escHtml(verification.reason || "")}
        <br>Treat this as an incident: see <code class="inline">docs/security-secrets.md</code>
        for the response checklist. Do not "fix" it by clearing the log — the
        broken chain is the evidence.
      </div>` : ""}

    <div class="card">
      <h3>Tamper Check <span class="tag ${chainState}">${chainLabel}</span></h3>
      <p class="panel-subtitle">
        Every entry is HMAC-chained to the one before it, and the head is
        recorded separately — so an edited, removed or truncated entry shows up
        here. Checked ${verification.entries} entr${verification.entries === 1 ? "y" : "ies"}.
        ${verification.reason && verification.ok ? escHtml(verification.reason) : ""}
      </p>
      <div class="row">
        <button id="sec-verify" class="primary">Re-verify Now</button>
        <button id="sec-selftest">Run Self-Test</button>
        <button id="sec-rotate">Rotate &amp; Apply Retention</button>
        <button id="sec-export-json">Export JSON</button>
        <button id="sec-export-csv">Export CSV</button>
      </div>
      <div id="sec-selftest-result" style="margin-top:12px;"></div>
    </div>

    <div class="card">
      <h3>Alerts ${alerts.length ? `<span class="tag warn">${alerts.length}</span>` : `<span class="tag off">none</span>`}</h3>
      <p class="panel-subtitle">
        Raised at <b>${escHtml(cfg.alert_min_severity)}</b> and above. Ordinary daily
        use (logging in, changing a colour, taking a backup) is below that line on
        purpose — an alert you see every day is an alert you stop reading.
      </p>
      ${alerts.length === 0 ? `<div class="empty-state">No alerts. Nothing has crossed the threshold.</div>` : `
        <table>
          <thead><tr><th>When</th><th>Severity</th><th>Event</th><th>Detail</th><th></th></tr></thead>
          <tbody>${alerts.map(a => `
            <tr>
              <td>${fmtTs(a.ts)}</td>
              <td>${severityTag(a.severity)}</td>
              <td>${escHtml(a.event)}</td>
              <td>${escHtml(a.message || "")}</td>
              <td>${a.acknowledged ? `<span class="tag off">seen</span>` : `<span class="tag warn">new</span>`}</td>
            </tr>`).join("")}
          </tbody>
        </table>
        <div class="row" style="margin-top:12px;">
          <button id="sec-ack">Mark All Seen</button>
          <button id="sec-notify">Enable Browser Notifications</button>
        </div>
      `}
    </div>

    <div class="card">
      <h3>Search</h3>
      <div class="form-grid">
        <label>Text search
          <input type="text" id="sec-q" value="${escAttr(securityFilters.q)}" placeholder="an IP, a device id, anything">
        </label>
        <label>Event type
          <select id="sec-event">
            <option value="">All events</option>
            ${body.known_events.map(e => `
              <option value="${escAttr(e)}" ${securityFilters.event === e ? "selected" : ""}>${escHtml(e)}</option>
            `).join("")}
          </select>
        </label>
        <label>Minimum severity
          <select id="sec-sev">
            <option value="">Any</option>
            ${SEVERITY_ORDER.map(s => `
              <option value="${s}" ${securityFilters.min_severity === s ? "selected" : ""}>${s}</option>
            `).join("")}
          </select>
        </label>
        <label>Show
          <select id="sec-limit">
            ${[50, 100, 250, 1000].map(n => `
              <option value="${n}" ${securityFilters.limit === n ? "selected" : ""}>last ${n}</option>
            `).join("")}
          </select>
        </label>
      </div>
      <div class="row" style="margin-top:10px;">
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-dim);">
          <input type="checkbox" id="sec-rotated" ${securityFilters.include_rotated ? "checked" : ""}>
          include rotated (older) segments
        </label>
        <button id="sec-search" class="primary">Search</button>
        <button id="sec-clear">Clear Filters</button>
      </div>
    </div>

    <div class="card">
      <h3>Events <span class="tag on">LIVE DATA</span></h3>
      <p class="panel-subtitle">${body.count} matching entr${body.count === 1 ? "y" : "ies"}, newest first. Rows marked <b>action</b> are the ones worth looking at.</p>
      ${body.count === 0 ? `<div class="empty-state">No events match these filters.</div>` : `
        <table>
          <thead><tr><th>#</th><th>When</th><th>Severity</th><th>Event</th><th>Outcome</th><th>Source</th><th>Detail</th></tr></thead>
          <tbody>${body.events.map(e => `
            <tr class="${e.actionable ? "sec-actionable" : ""}">
              <td>${e.seq}</td>
              <td>${fmtTs(e.ts)}</td>
              <td>${severityTag(e.severity)} ${e.actionable ? `<span class="tag warn">action</span>` : ""}</td>
              <td>${escHtml(e.event)}</td>
              <td>${escHtml(e.outcome || "")}</td>
              <td>${escHtml(e.source || "")}</td>
              <td><code class="inline">${escHtml(JSON.stringify(e.detail || {}))}</code></td>
            </tr>`).join("")}
          </tbody>
        </table>`}
    </div>

    <div class="card">
      <h3>Alerting &amp; Retention</h3>
      <p class="panel-subtitle">
        A webhook is the only way anything leaves this machine, and it is off by
        default. Local alerts need no external service at all.
      </p>
      <div class="form-grid">
        <label>Log everything at or above
          <select id="cfg-min-sev">
            ${SEVERITY_ORDER.map(s => `<option value="${s}" ${cfg.min_severity === s ? "selected" : ""}>${s}</option>`).join("")}
          </select>
        </label>
        <label>Raise an alert at or above
          <select id="cfg-alert-sev">
            ${SEVERITY_ORDER.map(s => `<option value="${s}" ${cfg.alert_min_severity === s ? "selected" : ""}>${s}</option>`).join("")}
          </select>
        </label>
        <label>Webhook URL (optional)
          <input type="text" id="cfg-webhook-url" value="${escAttr(cfg.webhook_url || "")}" placeholder="https://…">
        </label>
        <label>Keep rotated segments
          <input type="number" id="cfg-rotate-keep" min="1" value="${cfg.rotate_keep}">
        </label>
        <label>Rotate at (bytes)
          <input type="number" id="cfg-max-bytes" min="1024" value="${cfg.max_log_bytes}">
        </label>
        <label>Retention (days)
          <input type="number" id="cfg-retention" min="1" value="${cfg.retention_days}">
        </label>
      </div>
      <div class="row" style="margin-top:10px;">
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-dim);">
          <input type="checkbox" id="cfg-webhook-enabled" ${cfg.webhook_enabled ? "checked" : ""}>
          send alerts to the webhook
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-dim);">
          <input type="checkbox" id="cfg-local-alerts" ${cfg.local_alerts_enabled ? "checked" : ""}>
          keep alerts locally (no external service)
        </label>
        <button id="cfg-save" class="primary">Save</button>
      </div>
    </div>
  `;

  const applyFilters = () => {
    securityFilters.q = main.querySelector("#sec-q").value.trim();
    securityFilters.event = main.querySelector("#sec-event").value;
    securityFilters.min_severity = main.querySelector("#sec-sev").value;
    securityFilters.limit = parseInt(main.querySelector("#sec-limit").value, 10);
    securityFilters.include_rotated = main.querySelector("#sec-rotated").checked;
    renderSecurity(main);
  };
  main.querySelector("#sec-search").onclick = applyFilters;
  main.querySelector("#sec-q").onkeydown = (e) => { if (e.key === "Enter") applyFilters(); };
  main.querySelector("#sec-clear").onclick = () => {
    Object.assign(securityFilters,
      { q: "", event: "", min_severity: "", limit: 100, include_rotated: false });
    renderSecurity(main);
  };

  main.querySelector("#sec-verify").onclick = async () => {
    const r = await get("/api/security/verify");
    toast(r.ok ? (r.complete ? "Chain verified — no tampering" : "Chain verified (older segments pruned)")
               : `Integrity check FAILED: ${r.reason}`, r.ok ? "success" : "error");
    renderSecurity(main);
  };
  main.querySelector("#sec-selftest").onclick = async () => {
    const r = await post("/api/security/self-test");
    main.querySelector("#sec-selftest-result").innerHTML = `
      <table><tbody>
        <tr><td>Wrote canary event</td><td><span class="tag ${r.wrote_event ? "on" : "error"}">${r.wrote_event}</span></td></tr>
        <tr><td>Chain verified</td><td><span class="tag ${r.verification.ok ? "on" : "error"}">${r.verification.ok}</span></td></tr>
        <tr><td>Local alerts</td><td><span class="tag ${r.alerting.local_alerts_enabled ? "on" : "off"}">${r.alerting.local_alerts_enabled}</span></td></tr>
        <tr><td>Webhook</td><td><span class="tag ${r.alerting.webhook_enabled ? "on" : "off"}">${r.alerting.webhook_enabled ? (r.alerting.webhook_configured ? "enabled" : "enabled but no URL set") : "off"}</span></td></tr>
      </tbody></table>`;
  };
  main.querySelector("#sec-rotate").onclick = async () => {
    const r = await post("/api/security/events/rotate");
    toast(`Rotated. Removed ${r.removed.length} old segment(s).`, "success");
    renderSecurity(main);
  };
  const exportUrl = (fmt) => {
    const p = new URLSearchParams({ format: fmt, include_rotated: "true", limit: "0" });
    if (securityFilters.q) p.set("q", securityFilters.q);
    if (securityFilters.event) p.set("event", securityFilters.event);
    if (securityFilters.min_severity) p.set("min_severity", securityFilters.min_severity);
    return `${API}/api/security/events/export?${p.toString()}`;
  };
  main.querySelector("#sec-export-json").onclick = () => { window.location = exportUrl("json"); };
  main.querySelector("#sec-export-csv").onclick = () => { window.location = exportUrl("csv"); };

  const ackBtn = main.querySelector("#sec-ack");
  if (ackBtn) {
    ackBtn.onclick = async () => {
      const r = await post("/api/security/alerts/ack");
      toast(`${r.acknowledged} alert(s) marked seen`);
      renderSecurity(main);
    };
  }
  const notifyBtn = main.querySelector("#sec-notify");
  if (notifyBtn) {
    // W2-149: browser notifications, so alerting works for someone who
    // wants nothing leaving the machine. Permission is only ever requested
    // from this explicit click — never on page load.
    notifyBtn.onclick = async () => {
      if (!("Notification" in window)) { toast("This browser has no notification API", "error"); return; }
      const permission = await Notification.requestPermission();
      if (permission !== "granted") { toast("Notifications not permitted", "error"); return; }
      const unseen = alerts.filter(a => !a.acknowledged);
      new Notification("Smart Bulb Dashboard — security", {
        body: unseen.length
          ? `${unseen.length} unseen alert(s). Most recent: ${unseen[0].message}`
          : "Notifications enabled. Nothing outstanding.",
      });
      toast("Browser notifications enabled", "success");
    };
  }

  main.querySelector("#cfg-save").onclick = async () => {
    await post("/api/security/config", {
      min_severity: main.querySelector("#cfg-min-sev").value,
      alert_min_severity: main.querySelector("#cfg-alert-sev").value,
      webhook_enabled: main.querySelector("#cfg-webhook-enabled").checked,
      webhook_url: main.querySelector("#cfg-webhook-url").value.trim() || null,
      local_alerts_enabled: main.querySelector("#cfg-local-alerts").checked,
      rotate_keep: parseInt(main.querySelector("#cfg-rotate-keep").value, 10),
      max_log_bytes: parseInt(main.querySelector("#cfg-max-bytes").value, 10),
      retention_days: parseInt(main.querySelector("#cfg-retention").value, 10),
    });
    toast("Security settings saved", "success");
    renderSecurity(main);
  };
}

// ------------------------------------------------------- backup & restore --
async function renderBackup(main) {
  const [listing, options] = await Promise.all([
    get("/api/backups"),
    get("/api/backups/options"),
  ]);
  const backups = listing.backups || [];

  main.innerHTML = `
    <h1 class="panel-title">Backup &amp; Restore</h1>
    <p class="panel-subtitle">
      One archive holding <code class="inline">config.json</code> and everything under
      <code class="inline">data/</code> — favourites, schedules, lightshows, audio
      presets, discovery state.
    </p>

    <div class="callout danger">
      <b>A backup contains every bulb's <code class="inline">local_key</code>.</b>
      That key is permanent local control of the bulb for anyone on your network,
      and it cannot be revoked without re-pairing the bulb. Encrypt the archive
      unless you are certain of where it is going to live.
    </div>

    <div class="card">
      <h3>Create a Backup</h3>
      <div class="form-grid">
        <label>Encryption
          <select id="bk-mode">
            <option value="encrypted">Encrypt with a password (recommended)</option>
            <option value="plain">No encryption</option>
          </select>
        </label>
        <label id="bk-pass-wrap">Password
          <input type="password" id="bk-password" autocomplete="new-password" placeholder="you cannot recover this later">
        </label>
        <label>Note (optional)
          <input type="text" id="bk-note" placeholder="before moving to the new NUC">
        </label>
      </div>

      <div id="bk-plain-warning" style="display:none;">
        <div class="callout danger" style="margin-top:12px;">
          <b>Unencrypted archive.</b> Anyone who gets this file can control your bulbs
          from your LAN — indefinitely. Only reasonable if it stays on an
          encrypted disk you control.
          <label style="display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--text);">
            <input type="checkbox" id="bk-ack">
            I understand this file will contain my device keys in plaintext.
          </label>
        </div>
      </div>

      <p class="panel-subtitle" style="margin-top:14px;">Leave out (optional, keeps the archive small):</p>
      <div class="row">
        ${options.exclusions.map(e => `
          <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-dim);">
            <input type="checkbox" class="bk-exclude" value="${escAttr(e.name)}">
            ${escHtml(e.name)} <span class="tag off">${Math.round(e.bytes / 1024)} KB</span>
          </label>`).join("")}
      </div>

      <p class="panel-subtitle" style="margin-top:14px;">
        Never included, whatever you pick:
        ${options.never_included.map(n => `<code class="inline">${escHtml(n)}</code>`).join(" ")}
        — the PIN hash and session signing key, and the security log's own
        tamper-evidence chain. Restoring those would let a restore rewrite your
        access controls and erase the audit trail.
      </p>

      <button id="bk-create" class="primary" style="margin-top:10px;">Create Backup</button>
    </div>

    <div class="card">
      <h3>Backups <span class="tag on">${backups.length}</span></h3>
      <p class="panel-subtitle">
        Keeping the newest <b>${listing.settings.keep}</b>; older ones are deleted
        automatically. A restore never changes whether the PIN gate is on or off.
      </p>
      ${backups.length === 0 ? `<div class="empty-state">No backups yet.</div>` : `
        <table>
          <thead><tr><th>Name</th><th>When</th><th>Size</th><th>Encrypted</th><th>Note</th><th></th></tr></thead>
          <tbody>${backups.map(b => `
            <tr>
              <td><code class="inline">${escHtml(b.name)}</code></td>
              <td>${new Date(b.modified_at * 1000).toLocaleString()}</td>
              <td>${Math.round(b.bytes / 1024)} KB</td>
              <td><span class="tag ${b.encrypted ? "on" : "error"}">${b.encrypted ? "yes" : "NO — plaintext keys"}</span></td>
              <td>${escHtml((b.manifest && b.manifest.note) || "—")}</td>
              <td>
                <button class="bk-download" data-name="${escAttr(b.name)}">Download</button>
                <button class="bk-restore" data-name="${escAttr(b.name)}" data-enc="${b.encrypted}">Restore…</button>
                <button class="danger bk-delete" data-name="${escAttr(b.name)}">Delete</button>
              </td>
            </tr>`).join("")}
          </tbody>
        </table>`}
      <div class="form-grid" style="margin-top:14px;">
        <label>Keep this many backups
          <input type="number" id="bk-keep" min="1" value="${listing.settings.keep}">
        </label>
      </div>
      <button id="bk-save-settings" style="margin-top:10px;">Save</button>
    </div>

    <div class="card" id="bk-restore-panel" style="display:none;">
      <h3>Restore — <span id="bk-restore-name"></span></h3>
      <div id="bk-restore-body"></div>
    </div>
  `;

  const modeSelect = main.querySelector("#bk-mode");
  const syncMode = () => {
    const plain = modeSelect.value === "plain";
    main.querySelector("#bk-plain-warning").style.display = plain ? "" : "none";
    main.querySelector("#bk-pass-wrap").style.display = plain ? "none" : "";
  };
  modeSelect.onchange = syncMode;
  syncMode();

  main.querySelector("#bk-create").onclick = async () => {
    const plain = modeSelect.value === "plain";
    const password = main.querySelector("#bk-password").value;
    if (plain && !main.querySelector("#bk-ack").checked) {
      toast("Tick the acknowledgement, or choose an encrypted backup", "error");
      return;
    }
    if (!plain && password.length < 8) {
      toast("Use a password of at least 8 characters", "error");
      return;
    }
    const exclude = [...main.querySelectorAll(".bk-exclude:checked")].map(c => c.value);
    const result = await post("/api/backups", {
      password: plain ? null : password,
      exclude,
      note: main.querySelector("#bk-note").value.trim() || null,
    });
    toast(result.warning ? "Backup created — UNENCRYPTED, contains device keys"
                         : "Encrypted backup created", result.warning ? "error" : "success");
    renderBackup(main);
  };

  main.querySelector("#bk-save-settings").onclick = async () => {
    await post("/api/backups/settings", { keep: parseInt(main.querySelector("#bk-keep").value, 10) });
    toast("Retention updated", "success");
    renderBackup(main);
  };

  main.querySelectorAll(".bk-download").forEach(b => {
    b.onclick = () => { window.location = `${API}/api/backups/${encodeURIComponent(b.dataset.name)}/download`; };
  });

  main.querySelectorAll(".bk-delete").forEach(b => {
    b.onclick = async () => {
      if (!window.confirm(`Delete ${b.dataset.name}? The file is overwritten before removal.`)) return;
      await del(`/api/backups/${encodeURIComponent(b.dataset.name)}`);
      toast("Backup deleted");
      renderBackup(main);
    };
  });

  main.querySelectorAll(".bk-restore").forEach(b => {
    b.onclick = () => showRestorePanel(main, b.dataset.name, b.dataset.enc === "true", options);
  });
}

// Restore is a two-step flow on purpose: nothing is written until the
// archive has passed its integrity check, you've seen what would change, and
// you've explicitly ticked the overwrite box.
async function showRestorePanel(main, name, encrypted, options) {
  const panel = main.querySelector("#bk-restore-panel");
  const bodyEl = main.querySelector("#bk-restore-body");
  main.querySelector("#bk-restore-name").textContent = name;
  panel.style.display = "";
  panel.scrollIntoView({ behavior: "smooth", block: "start" });

  let password = null;
  if (encrypted) {
    password = window.prompt(`"${name}" is encrypted. Enter its password:`);
    if (password === null) { panel.style.display = "none"; return; }
  }

  bodyEl.innerHTML = `<div class="empty-state loading">Checking integrity…</div>`;
  const pre = await post(`/api/backups/${encodeURIComponent(name)}/preflight`, { password });

  if (!pre.verification.ok) {
    bodyEl.innerHTML = `
      <div class="callout danger">
        <b>This backup did not pass its integrity check, so it will not be offered
        for restore.</b><br>${escHtml(pre.verification.reason || "")}
      </div>`;
    return;
  }

  const diff = pre.diff || {};
  const devices = diff.devices || { added: [], removed: [], changed: [] };
  bodyEl.innerHTML = `
    <p class="panel-subtitle">
      Integrity check passed (${pre.verification.files_checked} files).
      Taken ${pre.verification.manifest ? new Date(pre.verification.manifest.created_at * 1000).toLocaleString() : "—"}.
    </p>

    <h3>What would change</h3>
    ${!diff.config_in_backup ? `<div class="empty-state">This archive has no config.json.</div>` : `
      <table>
        <tbody>
          <tr><td>Devices added by this restore</td><td>${devices.added.length ? escHtml(devices.added.join(", ")) : "—"}</td></tr>
          <tr><td>Devices removed by this restore</td><td>${devices.removed.length ? escHtml(devices.removed.join(", ")) : "—"}</td></tr>
          <tr><td>Devices changed</td><td>${devices.changed.length ? devices.changed.map(c =>
            `${escHtml(c.id)}${c.local_key_changed ? " <span class=\"tag warn\">key differs</span>" : ""}`).join(", ") : "—"}</td></tr>
          <tr><td>Groups added / removed</td><td>${escHtml((diff.groups.added.join(", ") || "—") + " / " + (diff.groups.removed.join(", ") || "—"))}</td></tr>
          <tr><td>Remote access (PIN gate)</td><td><span class="tag on">unchanged by any restore</span></td></tr>
        </tbody>
      </table>
      <p class="panel-subtitle">Key values are never shown here — only whether they differ.</p>`}

    <h3 style="margin-top:18px;">What to restore</h3>
    <div class="row">
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-dim);">
        <input type="radio" name="bk-scope" value="all" checked> Everything
      </label>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-dim);">
        <input type="radio" name="bk-scope" value="some"> Only what I pick
      </label>
    </div>
    <div id="bk-sections" style="display:none;margin-top:10px;">
      ${pre.sections.map(s => `
        <label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--text-dim);padding:3px 0;">
          <input type="checkbox" class="bk-section" value="${escAttr(s.id)}">
          ${escHtml(s.label)}
          ${s.touches_credentials ? `<span class="tag error">overwrites device keys</span>` : ""}
        </label>`).join("")}
    </div>

    <div class="callout danger" style="margin-top:16px;">
      <b>This overwrites your current configuration and data.</b>
      A safety backup of the current state is taken automatically first, so this
      is undoable — but only if you can find that file afterwards.
      <label style="display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--text);">
        <input type="checkbox" id="bk-confirm">
        Yes, overwrite the current configuration with this backup.
      </label>
    </div>
    <div class="row" style="margin-top:12px;">
      <button id="bk-do-restore" class="danger">Restore Now</button>
      <button id="bk-cancel-restore">Cancel</button>
    </div>
    <div id="bk-restore-result" style="margin-top:14px;"></div>
  `;

  const sectionsBox = bodyEl.querySelector("#bk-sections");
  bodyEl.querySelectorAll('input[name="bk-scope"]').forEach(r => {
    r.onchange = () => { sectionsBox.style.display = r.value === "some" && r.checked ? "" : "none"; };
  });
  bodyEl.querySelector("#bk-cancel-restore").onclick = () => { panel.style.display = "none"; };

  bodyEl.querySelector("#bk-do-restore").onclick = async () => {
    if (!bodyEl.querySelector("#bk-confirm").checked) {
      toast("Tick the overwrite confirmation first", "error");
      return;
    }
    const scoped = bodyEl.querySelector('input[name="bk-scope"]:checked').value === "some";
    const sections = [...bodyEl.querySelectorAll(".bk-section:checked")].map(c => c.value);
    if (scoped && sections.length === 0) { toast("Pick at least one section", "error"); return; }

    const result = await post(`/api/backups/${encodeURIComponent(name)}/restore`, {
      password, confirm: true, sections: scoped ? sections : null,
    });
    bodyEl.querySelector("#bk-restore-result").innerHTML = `
      <table><tbody>
        <tr><td>Files restored</td><td>${result.restored_files.length}</td></tr>
        <tr><td>Device credentials touched</td><td><span class="tag ${result.touched_device_credentials ? "warn" : "on"}">${result.touched_device_credentials}</span></td></tr>
        <tr><td>Safety backup taken first</td><td><code class="inline">${escHtml(result.safety_backup || "—")}</code></td></tr>
        <tr><td>Remote access changed</td><td><span class="tag ${result.remote_access.changed ? "error" : "on"}">${result.remote_access.changed}</span></td></tr>
      </tbody></table>`;
    toast("Restore complete — reload to pick up the restored devices", "success");
    await loadDevices();
  };
}

async function renderSettings(main) {
  const disco = await get("/api/system/discovery");
  const remoteAuth = await get("/api/system/remote-auth/status");
  const guestPins = remoteAuth.enabled ? (await get("/api/system/remote-auth/pins")).pins : [];
  const remoteAccess = await get("/api/system/remote-access/status");
  const logLevel = await get("/api/system/log-level");
  const never = `<span class="dim">never</span>`;

  main.innerHTML = `
    <h1 class="panel-title">Settings</h1>
    <p class="panel-subtitle">Manage configured devices. Local keys are never shown once saved.</p>

    <div class="card">
      <h3>Add a Device</h3>
      <div class="form-grid">
        <label>Internal ID<input type="text" id="new-id" placeholder="bulb-2"></label>
        <label>Display Name<input type="text" id="new-name" placeholder="Bedroom Bulb"></label>
        <label>Tuya Device ID<input type="text" id="new-devid"></label>
        <label>Local Key<input type="text" id="new-key"></label>
        <label>IP Address<input type="text" id="new-ip" placeholder="192.168.1.101"></label>
        <label>Protocol Version<input type="text" id="new-version" value="3.3"></label>
      </div>
      <button id="add-device" class="primary" style="margin-top:10px;">Add Device</button>
      <p class="panel-subtitle" style="margin-top:10px;">See SETUP.md for how to obtain the Device ID and Local Key.</p>
    </div>

    <div class="card">
      <h3>Configured Devices</h3>
      <table>
        <thead><tr><th>Name</th><th>Device ID</th><th>IP</th><th>Version</th><th></th></tr></thead>
        <tbody>${state.devices.map(d => `
          <tr>
            <td>${d.name}</td>
            <td><code class="inline">${d.device_id}</code></td>
            <td>${d.ip}</td>
            <td>${d.version}</td>
            <td><button data-id="${d.id}" class="danger remove-device">Remove</button></td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3>Network Discovery <span class="tag on">LIVE DATA</span></h3>
      <p class="panel-subtitle">
        Scans this LAN (local UDP broadcast only — nothing leaves the network) for Tuya
        devices not yet added to this dashboard.
        Last scan: ${disco.last_scan ? new Date(disco.last_scan).toLocaleString() : "never"}
      </p>
      <div class="form-grid">
        <label>Auto-scan interval
          <select id="disco-interval">
            <option value="24" ${disco.interval_hours === 24 ? "selected" : ""}>Daily</option>
            <option value="168" ${disco.interval_hours === 168 ? "selected" : ""}>Weekly</option>
            <option value="720" ${disco.interval_hours === 720 ? "selected" : ""}>Monthly</option>
          </select>
        </label>
      </div>
      <button id="scan-now" class="primary" style="margin-top:10px;">Scan Now</button>

      ${disco.discovered.length === 0 ? `
        <p class="panel-subtitle" style="margin-top:14px;">No undiscovered devices found on last scan.</p>
      ` : `
        <table style="margin-top:14px;">
          <thead><tr><th>Device ID</th><th>IP</th><th>Version</th><th>First Seen</th><th></th></tr></thead>
          <tbody>${disco.discovered.map(d => `
            <tr>
              <td><code class="inline">${d.device_id}</code></td>
              <td>${d.ip || "unknown"}</td>
              <td>${d.version || "?"}</td>
              <td>${new Date(d.first_seen).toLocaleDateString()}</td>
              <td>
                <button data-id="${d.device_id}" data-ip="${d.ip || ""}" data-version="${d.version || ""}" class="use-discovered">Add</button>
                <button data-id="${d.device_id}" class="danger ignore-discovered">Ignore</button>
              </td>
            </tr>`).join("")}
          </tbody>
        </table>
      `}

      ${disco.ignored.length > 0 ? `
        <p class="panel-subtitle" style="margin-top:14px;">Ignored: ${disco.ignored.map(id => `
          <code class="inline">${id}</code> <button data-id="${id}" class="unignore-discovered" style="margin-right:8px;">Unignore</button>
        `).join("")}</p>
      ` : ""}
    </div>

    <div class="card">
      <h3>Remote Access — PIN Gate <span class="tag ${remoteAuth.enabled ? "on" : "off"}">${remoteAuth.enabled ? "ENABLED" : "DISABLED"}</span></h3>
      <p class="panel-subtitle">
        Required if you expose this dashboard beyond your LAN (DuckDNS, port
        forwarding, etc.) — see <code class="inline">docs/remote-access-security.md</code>.
        Local-only setups can safely leave this disabled. After
        ${remoteAuth.lockout_max_attempts} wrong attempts an IP is locked out for
        ${Math.round(remoteAuth.lockout_base_s / 60)} minutes, doubling on each
        repeat lockout up to ${Math.round(remoteAuth.lockout_max_s / 3600)}h.
      </p>
      ${remoteAuth.enabled ? `
        <div class="form-grid">
          <label>Change household PIN<input type="password" id="remote-auth-new-pin" autocomplete="new-password"></label>
        </div>
        <p class="panel-subtitle" id="remote-auth-new-pin-strength" style="min-height:20px;"></p>
        <p class="panel-subtitle">Changing the PIN signs every device out, including this one — you stay signed in here, everything else re-authenticates.</p>
        <button id="remote-auth-change-pin" class="primary">Change PIN</button>
        <button id="remote-auth-disable" class="danger" style="margin-left:8px;">Disable PIN Gate</button>
      ` : `
        <div class="form-grid">
          <label>New PIN (6+ characters, not a common or test PIN)<input type="password" id="remote-auth-pin" autocomplete="new-password"></label>
        </div>
        <p class="panel-subtitle" id="remote-auth-pin-strength" style="min-height:20px;"></p>
        <button id="remote-auth-enable" class="primary" style="margin-top:10px;">Enable PIN Gate</button>
      `}
    </div>

    ${remoteAuth.enabled ? `
    <div class="card">
      <h3>Guest PINs</h3>
      <p class="panel-subtitle">
        A second way into the same dashboard that you can take back on its own.
        Revoking one signs out only the devices that used it — the household PIN
        and its sessions are untouched. Up to 5 active at a time.
      </p>
      ${guestPins.filter(p => p.kind === "guest").length === 0 ? `
        <p class="panel-subtitle">No guest PINs issued.</p>
      ` : `
        <table>
          <thead><tr><th>Label</th><th>Issued</th><th>Expires</th><th>Last used</th><th></th></tr></thead>
          <tbody>${guestPins.filter(p => p.kind === "guest").map(p => `
            <tr>
              <td>${escHtml(p.label || "Guest")}</td>
              <td>${p.created_at ? new Date(p.created_at * 1000).toLocaleDateString() : "—"}</td>
              <td>${p.expires_at ? new Date(p.expires_at * 1000).toLocaleString() : "never"}</td>
              <td>${p.last_used_at ? new Date(p.last_used_at * 1000).toLocaleString() : "never"}</td>
              <td><button data-id="${escAttr(p.id)}" class="danger revoke-guest-pin">Revoke</button></td>
            </tr>`).join("")}
          </tbody>
        </table>
      `}
      <div class="form-grid" style="margin-top:14px;">
        <label>Guest PIN<input type="password" id="guest-pin" autocomplete="new-password"></label>
        <label>Label<input type="text" id="guest-pin-label" placeholder="Dog sitter"></label>
        <label>Expires after
          <select id="guest-pin-expiry">
            <option value="">Never</option>
            <option value="86400">1 day</option>
            <option value="604800">1 week</option>
            <option value="2592000">30 days</option>
          </select>
        </label>
      </div>
      <p class="panel-subtitle" id="guest-pin-strength" style="min-height:20px;"></p>
      <button id="add-guest-pin" class="primary">Issue Guest PIN</button>
    </div>
    ` : ""}

    <div class="card">
      <h3>Session &amp; Lockout Policy</h3>
      <p class="panel-subtitle">
        Applies to the PIN gate. A shorter session means more PIN prompts but a
        smaller window for a stolen cookie. Changing the session length affects
        new sign-ins only — use Sign Out All Devices to cut existing ones short.
      </p>
      <div class="form-grid">
        <label>Session length
          <select id="session-ttl">
            ${[["3600", "1 hour"], ["28800", "8 hours"], ["86400", "1 day"], ["604800", "1 week"], ["2592000", "30 days"]].map(([v, l]) =>
              `<option value="${v}" ${String(remoteAuth.session_ttl_s) === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
        </label>
        <label>Wrong attempts before lockout<input type="number" id="lockout-attempts" min="1" value="${remoteAuth.lockout_max_attempts}"></label>
        <label>First lockout (seconds)<input type="number" id="lockout-base" min="1" value="${remoteAuth.lockout_base_s}"></label>
        <label>Longest lockout (seconds)<input type="number" id="lockout-max" min="1" value="${remoteAuth.lockout_max_s}"></label>
      </div>
      <button id="save-lockout-policy" class="primary" style="margin-top:10px;">Save Policy</button>
      <button id="revoke-all-sessions" class="danger" style="margin-left:8px;">Sign Out All Devices</button>
    </div>

    <div class="card">
      <h3>Remote Access — Exposure</h3>
      <p class="panel-subtitle">
        What this install currently looks like from outside. The public-IP lookup is the
        <b>only</b> outbound internet request this project makes anywhere, and it only
        happens when you press the button below — see <code class="inline">SECURITY.md</code>.
      </p>
      <table><tbody>
        <tr>
          <td>Detected public IP</td>
          <td>
            <code class="inline">${escHtml(remoteAccess.public_ip.ip || "not checked")}</code>
            ${remoteAccess.public_ip.checked_at
              ? `<span class="dim">— checked ${new Date(remoteAccess.public_ip.checked_at).toLocaleString()}</span>`
              : ""}
            ${remoteAccess.public_ip.error ? `<span class="tag error">lookup failed</span>` : ""}
          </td>
        </tr>
        <tr><td>DuckDNS domain</td><td><code class="inline">${escHtml(remoteAccess.duckdns.domain || "—")}</code></td></tr>
        <tr>
          <td>Last DuckDNS sync</td>
          <td>${remoteAccess.duckdns.last_sync_at
                ? `${new Date(remoteAccess.duckdns.last_sync_at).toLocaleString()}
                   <span class="tag ${remoteAccess.duckdns.last_sync_ok ? "on" : "error"}">${remoteAccess.duckdns.last_sync_ok ? "ok" : "failed"}</span>`
                : never}</td>
        </tr>
        <tr>
          <td>Public exposure</td>
          <td><span class="tag ${remoteAccess.exposure.configured ? "error" : "off"}">${remoteAccess.exposure.configured ? "CONFIGURED" : "not configured"}</span>
            ${remoteAccess.exposure.source ? `<span class="dim">${escHtml(remoteAccess.exposure.source)}</span>` : ""}</td>
        </tr>
        <tr>
          <td>Seen from a public IP</td>
          <td>${remoteAccess.exposure.public_client_seen_at
                ? `<code class="inline">${escHtml(remoteAccess.exposure.public_client_ip)}</code>
                   <span class="dim">${new Date(remoteAccess.exposure.public_client_seen_at).toLocaleString()}</span>`
                : never}</td>
        </tr>
      </tbody></table>
      <div class="row" style="margin-top:12px;">
        <button id="detect-public-ip">Detect Public IP Now</button>
        ${remoteAccess.exposure.configured
          ? `<button id="exposure-clear" class="danger">Retract Exposure Declaration</button>`
          : `<button id="exposure-set">I have a port forward set up</button>`}
      </div>
      <p class="panel-subtitle" style="margin-top:10px;">
        Your DuckDNS updater can report its sync time here with
        <code class="inline">POST /api/system/remote-access/duckdns-sync</code>. This project
        deliberately does not run an updater of its own.
      </p>
    </div>

    <div class="card">
      <h3>Logging</h3>
      <p class="panel-subtitle">
        Backend log verbosity — no code edit, no restart. Persisted. View the resulting
        lines under System → Health.
      </p>
      <div class="form-grid">
        <label>Log level
          <select id="log-level-select">
            ${logLevel.levels.map(l =>
              `<option value="${escAttr(l)}" ${l === logLevel.log_level ? "selected" : ""}>${escHtml(l)}</option>`
            ).join("")}
          </select>
        </label>
      </div>
    </div>
  `;

  main.querySelector("#add-device").onclick = async () => {
    await post("/api/devices", {
      id: main.querySelector("#new-id").value.trim(),
      name: main.querySelector("#new-name").value.trim(),
      device_id: main.querySelector("#new-devid").value.trim(),
      local_key: main.querySelector("#new-key").value.trim(),
      ip: main.querySelector("#new-ip").value.trim(),
      version: parseFloat(main.querySelector("#new-version").value) || 3.3,
    });
    toast("Device added", "success");
    await loadDevices();
    renderSettings(main);
  };

  main.querySelectorAll(".remove-device").forEach(b => {
    b.onclick = async () => {
      await del(`/api/devices/${b.dataset.id}`);
      toast("Device removed");
      await loadDevices();
      renderSettings(main);
    };
  });

  main.querySelector("#disco-interval").onchange = async (e) => {
    await post("/api/system/discovery/interval", { hours: parseInt(e.target.value, 10) });
    toast("Scan interval updated", "success");
  };

  main.querySelector("#scan-now").onclick = async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Scanning…";
    const result = await post("/api/system/scan");
    e.target.disabled = false;
    e.target.textContent = "Scan Now";
    if (result.already_scanning) {
      toast("A scan is already in progress", "error");
    } else if (result.ok === false) {
      toast(`Scan failed: ${result.error}`, "error");
    } else {
      toast(`Scan complete — ${result.new_count} new device(s) found`, "success");
    }
    renderSettings(main);
  };

  main.querySelectorAll(".use-discovered").forEach(b => {
    b.onclick = () => {
      main.querySelector("#new-devid").value = b.dataset.id;
      main.querySelector("#new-ip").value = b.dataset.ip;
      if (b.dataset.version) main.querySelector("#new-version").value = b.dataset.version;
      main.querySelector("#new-id").focus();
      toast("Device ID/IP filled in above — local_key still needs to be obtained via SETUP.md, then click Add Device", "success");
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
  });

  main.querySelectorAll(".ignore-discovered").forEach(b => {
    b.onclick = async () => {
      await post(`/api/system/discovery/${b.dataset.id}/ignore`);
      toast("Device ignored");
      renderSettings(main);
    };
  });

  main.querySelectorAll(".unignore-discovered").forEach(b => {
    b.onclick = async () => {
      await post(`/api/system/discovery/${b.dataset.id}/unignore`);
      toast("Device un-ignored — it will reappear on the next scan");
      renderSettings(main);
    };
  });

  wirePinStrengthMeter(
    main.querySelector("#remote-auth-pin"), main.querySelector("#remote-auth-pin-strength"));
  wirePinStrengthMeter(
    main.querySelector("#remote-auth-new-pin"), main.querySelector("#remote-auth-new-pin-strength"));
  wirePinStrengthMeter(
    main.querySelector("#guest-pin"), main.querySelector("#guest-pin-strength"));

  const enableBtn = main.querySelector("#remote-auth-enable");
  if (enableBtn) {
    enableBtn.onclick = async () => {
      const pin = main.querySelector("#remote-auth-pin").value;
      // The server enforces the real rules; this only avoids a pointless
      // round-trip on an obviously-empty field.
      if (!pin) { toast("Enter a PIN first", "error"); return; }
      await post("/api/system/remote-auth/enable", { pin });
      toast("PIN gate enabled — you'll need it on your next visit", "success");
      renderSettings(main);
    };
  }
  const disableBtn = main.querySelector("#remote-auth-disable");
  if (disableBtn) {
    disableBtn.onclick = async () => {
      await post("/api/system/remote-auth/disable");
      toast("PIN gate disabled");
      // Disabling the gate is exactly the moment the fail-safe warning is
      // supposed to reappear if exposure is configured, so re-check now
      // rather than waiting for the next page load.
      await refreshExposureWarnings();
      renderSettings(main);
    };
  }

  main.querySelector("#detect-public-ip").onclick = async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Checking…";
    const result = await post("/api/system/remote-access/detect-public-ip");
    toast(result.public_ip ? `Public IP: ${result.public_ip}` : `Lookup failed: ${result.error}`,
          result.public_ip ? "success" : "error");
    await refreshExposureWarnings();
    renderSettings(main);
  };

  const exposureSet = main.querySelector("#exposure-set");
  if (exposureSet) {
    exposureSet.onclick = async () => {
      await post("/api/system/remote-access/exposure",
                 { configured: true, source: "declared in Settings" });
      toast("Exposure recorded — the PIN gate warning now stays until the gate is on", "success");
      await refreshExposureWarnings();
      renderSettings(main);
    };
  }
  const exposureClear = main.querySelector("#exposure-clear");
  if (exposureClear) {
    exposureClear.onclick = async () => {
      await post("/api/system/remote-access/exposure", { configured: false });
      toast("Exposure retracted — only do this once the port forward is actually removed");
      await refreshExposureWarnings();
      renderSettings(main);
    };
  }

  const changePinBtn = main.querySelector("#remote-auth-change-pin");
  if (changePinBtn) {
    changePinBtn.onclick = async () => {
      const pin = main.querySelector("#remote-auth-new-pin").value;
      if (!pin) { toast("Enter a new PIN first", "error"); return; }
      const result = await post("/api/system/remote-auth/pin", { pin });
      toast(`PIN changed — ${result.revoked_sessions} other session(s) signed out`, "success");
      renderSettings(main);
    };
  }

  const addGuestBtn = main.querySelector("#add-guest-pin");
  if (addGuestBtn) {
    addGuestBtn.onclick = async () => {
      const pin = main.querySelector("#guest-pin").value;
      if (!pin) { toast("Enter a guest PIN first", "error"); return; }
      const expiry = main.querySelector("#guest-pin-expiry").value;
      await post("/api/system/remote-auth/pins", {
        pin,
        label: main.querySelector("#guest-pin-label").value.trim() || null,
        expires_in_s: expiry ? parseInt(expiry, 10) : null,
      });
      toast("Guest PIN issued", "success");
      renderSettings(main);
    };
  }

  main.querySelectorAll(".revoke-guest-pin").forEach(b => {
    b.onclick = async () => {
      const result = await del(`/api/system/remote-auth/pins/${b.dataset.id}`);
      toast(`Guest PIN revoked — ${result.revoked_sessions} session(s) signed out`);
      renderSettings(main);
    };
  });

  main.querySelector("#session-ttl").onchange = async (e) => {
    await post("/api/system/remote-auth/session-ttl", { session_ttl_s: parseInt(e.target.value, 10) });
    toast("Session length updated — applies to new sign-ins", "success");
  };

  main.querySelector("#save-lockout-policy").onclick = async () => {
    await post("/api/system/remote-auth/lockout-policy", {
      max_attempts: parseInt(main.querySelector("#lockout-attempts").value, 10),
      base_seconds: parseInt(main.querySelector("#lockout-base").value, 10),
      max_seconds: parseInt(main.querySelector("#lockout-max").value, 10),
    });
    toast("Lockout policy saved", "success");
    renderSettings(main);
  };

  main.querySelector("#revoke-all-sessions").onclick = async () => {
    const result = await post("/api/auth/sessions/revoke-all");
    toast(`${result.revoked} session(s) signed out — every device must re-enter the PIN`, "success");
  };

  main.querySelector("#log-level-select").onchange = async (e) => {
    await post("/api/system/log-level", { level: e.target.value });
    toast(`Log level set to ${e.target.value}`, "success");
  };
}

// ---------------------------------------------------------- live stream --
// One EventSource for the whole page. Panels register a handler for the
// topics they care about and unregister when they're torn down, so switching
// tabs never leaves an orphan connection or a handler writing into DOM that
// no longer exists.
//
// Deliberately NOT one connection per panel: browsers cap concurrent
// connections per origin, and three live views on one page would spend that
// budget on plumbing.
const live = {
  source: null,
  handlers: {},        // topic -> Set of fn
  connected: false,
  retryMs: 1000,
};

function liveOn(topic, fn) {
  (live.handlers[topic] || (live.handlers[topic] = new Set())).add(fn);
  liveConnect();
  return () => { const s = live.handlers[topic]; if (s) s.delete(fn); };
}

function liveConnect() {
  if (live.source) return;
  // Same-origin, cookie-authenticated: EventSource can't set headers, but it
  // does send cookies, which is what the PIN gate reads.
  const es = new EventSource(`${API}/api/stream`);
  live.source = es;

  ["ready", "bulb", "log", "history"].forEach(topic => {
    es.addEventListener(topic, ev => {
      if (topic === "ready") {
        live.connected = true;
        live.retryMs = 1000;
        renderLiveBadge();
        return;
      }
      let data;
      try { data = JSON.parse(ev.data); } catch (e) { return; }
      (live.handlers[topic] || []).forEach(fn => {
        // One panel throwing must not stop the others receiving the event.
        try { fn(data); } catch (e) { /* keep other handlers alive */ }
      });
    });
  });

  es.onerror = () => {
    // EventSource reconnects on its own, but only while the connection is
    // merely dropped. A 401 (session expired) closes it for good, so back off
    // and retry rather than silently going dead.
    live.connected = false;
    renderLiveBadge();
    if (es.readyState === EventSource.CLOSED) {
      live.source = null;
      setTimeout(liveConnect, live.retryMs);
      live.retryMs = Math.min(live.retryMs * 2, 30000);
    }
  };
}

function renderLiveBadge() {
  document.querySelectorAll(".live-dot").forEach(el => {
    el.classList.toggle("is-live", live.connected);
    el.title = live.connected ? "Live — streaming from the server" : "Reconnecting…";
  });
}

// ------------------------------------------------ always-visible control --
// Rendered once per status poll rather than per route, so it survives page
// navigation — the whole point is that power/brightness are reachable no
// matter which page you're on.
let quickCtlBusy = false;

function renderQuickControl() {
  const el = document.getElementById("quickctl");
  if (!el) return;
  const st = state.lastStatus;
  const dev = state.devices.find(d => d.id === state.deviceId);
  const name = dev ? dev.name : "No device";

  if (!st || st.online === false) {
    el.innerHTML =
      `<h4>Quick control</h4>` +
      `<div class="qc-device">${escHtml(name)}</div>` +
      `<p class="qc-offline">Offline — controls hidden until the bulb answers again.</p>`;
    return;
  }

  // Don't stomp the slider the user is currently dragging.
  if (quickCtlBusy) return;

  const pct = st.mode === "colour" ? (st.value_pct ?? 100) : (st.brightness_pct ?? 100);
  const rgb = st.hue != null ? hsvToRgb(st.hue, st.saturation_pct ?? 100, st.value_pct ?? 100) : [255, 255, 255];

  el.innerHTML = `
    <h4>Quick control</h4>
    <div class="qc-device">${escHtml(name)}</div>
    <div class="qc-swatch" id="qc-swatch" style="background:${rgbToHex(...rgb)}"></div>
    <div class="qc-row"><span class="live-dot"></span><span id="qc-live-hex" class="qc-live-hex">idle</span></div>
    <button id="qc-power" class="qc-power ${st.power ? "primary" : ""}">${st.power ? "TURN OFF" : "TURN ON"}</button>
    <div class="qc-row"><span>Brightness</span><span id="qc-bval">${pct}%</span></div>
    <input type="range" id="qc-bright" min="1" max="100" value="${pct}">
  `;

  el.querySelector("#qc-power").onclick = async () => {
    await post(`/api/devices/${state.deviceId}/power`, { on: !st.power });
    state.lastStatus = null;
    pollStatus();
    // The Control panel mirrors this state, so re-render it if it's on screen.
    if (currentRoute().key === "light/control") router();
  };

  // Live colour. The swatch above shows polled status; this overlays what the
  // audio sender is ACTUALLY pushing to the bulb, which during a session
  // changes far faster than the poll interval. Unsubscribes on next render so
  // handlers don't pile up every time the poll re-renders this panel.
  if (live.unsubBulb) live.unsubBulb();
  live.unsubBulb = liveOn("bulb", (ev) => {
    if (ev.device_id !== state.deviceId || !ev.hex) return;
    const sw = document.getElementById("qc-swatch");
    if (!sw) return;
    sw.style.background = ev.hex;
    const lbl = document.getElementById("qc-live-hex");
    if (lbl) lbl.textContent = `${ev.hex} · ${ev.latency_ms}ms`;
  });

  // This panel re-renders on every status poll, which replaces its .live-dot
  // with a fresh grey one. `ready` only fires at connect, so without this the
  // indicator would go stale within seconds and claim the stream was down
  // while it was happily delivering events.
  renderLiveBadge();

  const slider = el.querySelector("#qc-bright");
  slider.oninput = () => {
    quickCtlBusy = true;
    el.querySelector("#qc-bval").textContent = slider.value + "%";
  };
  slider.onchange = async () => {
    await post(`/api/devices/${state.deviceId}/brightness`, { value: parseInt(slider.value, 10) });
    quickCtlBusy = false;
    toast("Brightness updated", "success");
    state.lastStatus = null;
    pollStatus();
  };
}

// ------------------------------------------------------- merged panels --
// Each of these composes existing panel renderers into one page rather than
// reimplementing them. The sub-renderers already scope their DOM lookups to
// the container they're handed, so handing each a section <div> instead of
// #main works unchanged — and any future fix to (say) renderTimers applies
// here automatically.

function panelSection(id) {
  return `<section class="panel-section"><div class="panel-section__body" id="${id}"></div></section>`;
}

async function renderLooks(main) {
  main.innerHTML =
    `<h1 class="panel-title">Scenes &amp; Effects</h1>` +
    `<p class="panel-subtitle">Both ways to put a look on the bulb, together — scenes are fixed multi-color moods, effects animate until you stop them.</p>` +
    panelSection("sec-scenes") + panelSection("sec-effects");
  await renderScenes(main.querySelector("#sec-scenes"));
  await renderEffects(main.querySelector("#sec-effects"));
}

async function renderAutomation(main) {
  main.innerHTML =
    `<h1 class="panel-title">Automation</h1>` +
    `<p class="panel-subtitle">Everything that happens on a clock — one-off sleep/wake timers, and the recurring weekly schedule.</p>` +
    panelSection("sec-timers") + panelSection("sec-schedule");
  await renderTimers(main.querySelector("#sec-timers"));
  await renderSchedule(main.querySelector("#sec-schedule"));
}

async function renderRooms(main) {
  main.innerHTML =
    `<h1 class="panel-title">Rooms</h1>` +
    `<p class="panel-subtitle">Groups are a flat set of bulbs you control together. Zones sit above them — a zone can contain bulbs, whole groups, or both.</p>` +
    panelSection("sec-groups") + panelSection("sec-zones");
  await renderGroups(main.querySelector("#sec-groups"));
  await renderZones(main.querySelector("#sec-zones"));
}

// ------------------------------------------------------------- zones --
async function renderZones(main) {
  let zones = [];
  try { zones = await get("/api/zones"); } catch (e) { zones = []; }
  const groups = await get("/api/groups").catch(() => []);
  const devices = state.devices || [];

  // A zone's real membership is bulbs + every bulb inside its groups, deduped.
  // Resolve it client-side for the list so this doesn't fire N extra requests;
  // the server does the same resolution on GET /api/zones/{id}.
  const resolveCount = (z) => {
    const seen = new Set(z.device_ids || []);
    (z.group_ids || []).forEach(gid => {
      const g = groups.find(x => x.id === gid);
      if (g) (g.device_ids || []).forEach(d => seen.add(d));
    });
    return seen.size;
  };

  main.innerHTML = `
    <h1 class="panel-title">Zones</h1>
    <p class="panel-subtitle">A zone sits above groups — it can hold individual bulbs, whole groups, or a mix of both.</p>
    <div class="card">
      <h3>Create a zone</h3>
      <div class="form-grid">
        <label>Zone ID<input id="zone-new-id" placeholder="upstairs"></label>
        <label>Display name<input id="zone-new-name" placeholder="Upstairs"></label>
      </div>
      <div class="row" style="margin-top:8px;">
        <button id="zone-create" class="primary">Create Zone</button>
      </div>
    </div>
    ${zones.length === 0
      ? `<div class="empty-state">No zones yet. A zone is a higher-level room — it can hold individual bulbs, whole groups, or a mix of both.</div>`
      : zones.map(z => `
        <div class="card" data-zone="${escAttr(z.id)}">
          <div class="row" style="justify-content:space-between;align-items:center;">
            <h3 style="margin:0;">${escHtml(z.name || z.id)} <span class="tag">${resolveCount(z)} bulb(s)</span></h3>
            <button class="zone-delete danger" data-zone-id="${escAttr(z.id)}">Delete</button>
          </div>
          <p class="panel-subtitle" style="margin-top:4px;">ID <code>${escHtml(z.id)}</code></p>
          <div class="slider-row">
            <label><span>Bulbs in this zone</span></label>
            <div class="row" style="flex-wrap:wrap;gap:6px;">
              ${(z.device_ids || []).length === 0
                ? `<span class="panel-subtitle">none directly assigned</span>`
                : (z.device_ids || []).map(did => {
                    const d = devices.find(x => x.id === did);
                    return `<span class="tag">${escHtml(d ? d.name : did)}
                      <button class="zone-remove-device" data-zone-id="${escAttr(z.id)}" data-device-id="${escAttr(did)}" title="Remove from zone">×</button></span>`;
                  }).join("")}
            </div>
          </div>
          <div class="row" style="margin-top:8px;">
            <select class="zone-add-select" data-zone-id="${escAttr(z.id)}">
              ${devices.filter(d => !(z.device_ids || []).includes(d.id))
                .map(d => `<option value="${escAttr(d.id)}">${escHtml(d.name)}</option>`).join("")
                || `<option value="">(every bulb already in this zone)</option>`}
            </select>
            <button class="zone-add-device" data-zone-id="${escAttr(z.id)}">Add bulb</button>
          </div>
          <div class="slider-row" style="margin-top:8px;">
            <label><span>Groups in this zone</span></label>
            <div class="row" style="flex-wrap:wrap;gap:6px;">
              ${(z.group_ids || []).length === 0
                ? `<span class="panel-subtitle">none</span>`
                : (z.group_ids || []).map(gid => {
                    const g = groups.find(x => x.id === gid);
                    return `<span class="tag">${escHtml(g ? g.name : gid)}</span>`;
                  }).join("")}
            </div>
          </div>
        </div>`).join("")}
  `;

  main.querySelector("#zone-create").onclick = async () => {
    const id = main.querySelector("#zone-new-id").value.trim();
    const name = main.querySelector("#zone-new-name").value.trim();
    if (!id || !name) { toast("Zone needs both an ID and a name", "error"); return; }
    try {
      await post("/api/zones", { id, name, device_ids: [], group_ids: [] });
      toast(`Zone "${name}" created`, "success");
      renderZones(main);
    } catch (e) { toast(e.message || "Could not create zone", "error"); }
  };

  main.querySelectorAll(".zone-delete").forEach(btn => {
    btn.onclick = async () => {
      await del(`/api/zones/${btn.dataset.zoneId}`);
      toast("Zone deleted");
      renderZones(main);
    };
  });

  main.querySelectorAll(".zone-add-device").forEach(btn => {
    btn.onclick = async () => {
      const sel = main.querySelector(`.zone-add-select[data-zone-id="${btn.dataset.zoneId}"]`);
      if (!sel || !sel.value) return;
      await post(`/api/zones/${btn.dataset.zoneId}/devices`, { device_id: sel.value });
      toast("Bulb added to zone", "success");
      renderZones(main);
    };
  });

  main.querySelectorAll(".zone-remove-device").forEach(btn => {
    btn.onclick = async () => {
      await del(`/api/zones/${btn.dataset.zoneId}/devices/${btn.dataset.deviceId}`);
      toast("Bulb removed from zone");
      renderZones(main);
    };
  });
}

// --------------------------------------------------- audio session presets --
async function renderSessionPresets(main) {
  let presets = [];
  try { presets = await get("/api/audio/session-presets"); } catch (e) { presets = []; }
  let live = { active: false };
  try { live = await get(`/api/devices/${state.deviceId}/audio-reactive/status`); } catch (e) {}

  main.innerHTML = `
    <h1 class="panel-title">Session Presets</h1>
    <p class="panel-subtitle">A snapshot of a whole audio-reactive session — mode, sensitivity, band count, dwell, capture device and safety limits — saved under a name so you can drop straight back into it. Distinct from the genre presets on the Live Session tab, which are curated starting points rather than saved state.</p>

    <div class="card">
      <h3>Save the running session</h3>
      ${live.active
        ? `<p class="panel-subtitle">Currently running: <span class="tag on">${escHtml(live.mode)}</span>
             sensitivity ${live.sensitivity}, ${live.n_bands} band(s), input #${live.device_index}</p>
           <div class="row">
             <input id="sp-name" placeholder="e.g. Evening chill" style="flex:1;">
             <button id="sp-save" class="primary">Save as preset</button>
           </div>`
        : `<div class="empty-state">No audio-reactive session is running on this bulb right now. Start one on the <a href="#/audio/session">Live Session</a> tab, get it sounding right, then come back here to save it.</div>`}
    </div>

    <h3>Saved presets</h3>
    ${presets.length === 0
      ? `<div class="empty-state">Nothing saved yet.</div>`
      : presets.map(p => {
          const c = p.config || {};
          return `
          <div class="card">
            <div class="row" style="justify-content:space-between;align-items:center;">
              <h3 style="margin:0;">${escHtml(p.name)}</h3>
              <div class="row">
                <button class="sp-apply primary" data-preset-id="${escAttr(p.id)}">Apply</button>
                <button class="sp-delete danger" data-preset-id="${escAttr(p.id)}">Delete</button>
              </div>
            </div>
            <p class="panel-subtitle" style="margin-top:6px;">
              <span class="tag">${escHtml(c.mode || "?")}</span>
              sensitivity ${c.sensitivity ?? "?"} ·
              ${c.n_bands ?? "?"} band(s) ·
              dwell ${c.min_dwell_ms ?? "?"}ms ·
              input #${c.device_index ?? "?"}
              ${c.max_duration_s ? ` · stops after ${c.max_duration_s}s` : ""}
              ${c.max_flash_rate_hz ? ` · flash cap ${c.max_flash_rate_hz}Hz` : ""}
            </p>
          </div>`;
        }).join("")}
  `;

  const saveBtn = main.querySelector("#sp-save");
  if (saveBtn) {
    saveBtn.onclick = async () => {
      const name = main.querySelector("#sp-name").value.trim();
      if (!name) { toast("Give the preset a name first", "error"); return; }
      try {
        await post(`/api/devices/${state.deviceId}/audio-reactive/session-presets`, {
          name,
          device_index: live.device_index,
          mode: live.mode,
          sensitivity: live.sensitivity,
          n_bands: live.n_bands,
          min_dwell_ms: (live.sender && live.sender.min_dwell_ms) || undefined,
          max_duration_s: live.max_duration_s,
          warmup_s: live.warmup_s,
          max_flash_rate_hz: live.max_flash_rate_hz,
        });
        toast(`Saved "${name}"`, "success");
        renderSessionPresets(main);
      } catch (e) { toast(e.message || "Could not save preset", "error"); }
    };
  }

  main.querySelectorAll(".sp-apply").forEach(btn => {
    btn.onclick = async () => {
      try {
        await post(`/api/devices/${state.deviceId}/audio-reactive/session-presets/apply`, {
          preset_id: btn.dataset.presetId,
        });
        toast("Session started from preset", "success");
        renderSessionPresets(main);
      } catch (e) { toast(e.message || "Could not apply preset", "error"); }
    };
  });

  main.querySelectorAll(".sp-delete").forEach(btn => {
    btn.onclick = async () => {
      await del(`/api/audio/session-presets/${btn.dataset.presetId}`);
      toast("Preset deleted");
      renderSessionPresets(main);
    };
  });
}

// ------------------------------------------------------------------ docs --
// The project's own documentation, in-app. Deliberately a System tab rather
// than a link out: the docs describe the thing you're looking at, and a link
// to GitHub is useless on a phone on the tailnet with no internet.

// A small Markdown renderer. Written rather than pulled in because the page
// has no build step and no CDN access, and these docs use a known, narrow
// subset: headings, tables, fenced code, lists, blockquotes, rules, links,
// bold/italic/inline-code.
//
// Everything is HTML-escaped FIRST and inline formatting applied after, so a
// doc can never inject markup -- these files are local today, but a renderer
// that trusts its input is a bad habit to leave lying around.
function mdInline(text) {
  return escHtml(text)
    .replace(/`([^`]+)`/g, '<code class="inline">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
    // Only http(s) and in-app #/ links become anchors; anything else stays
    // as plain text so a doc can't produce a javascript: URL.
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+|#\/[^)\s]*)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

function renderMarkdown(md) {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  let listType = null;

  const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };

  while (i < lines.length) {
    const line = lines[i];

    // fenced code -- emitted verbatim (escaped), no inline formatting inside
    if (/^```/.test(line)) {
      closeList();
      const lang = line.slice(3).trim();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre class="md-code"${lang ? ` data-lang="${escAttr(lang)}"` : ""}><code>${escHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    // table: a header row followed by a |---|---| separator
    if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      closeList();
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) body.push(cells(lines[i++]));
      out.push(
        '<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
        head.map(h => `<th>${mdInline(h)}</th>`).join("") +
        "</tr></thead><tbody>" +
        body.map(r => "<tr>" + r.map(c => `<td>${mdInline(c)}</td>`).join("") + "</tr>").join("") +
        "</tbody></table></div>"
      );
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      // id so the outline can jump to it
      const id = "h-" + heading[2].toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      out.push(`<h${level} id="${escAttr(id)}" class="md-h md-h${level}">${mdInline(heading[2])}</h${level}>`);
      i++;
      continue;
    }

    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) { closeList(); out.push('<hr class="md-hr">'); i++; continue; }

    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? "ul" : "ol";
      if (listType !== want) { closeList(); out.push(`<${want} class="md-list">`); listType = want; }
      out.push(`<li>${mdInline((ul || ol)[1])}</li>`);
      i++;
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      closeList();
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote class="md-quote">${mdInline(buf.join(" "))}</blockquote>`);
      continue;
    }

    if (!line.trim()) { closeList(); i++; continue; }

    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*[-*]\s|\s*\d+\.\s|\s*>|\s*\|)/.test(lines[i])) {
      buf.push(lines[i++]);
    }
    if (buf.length) { closeList(); out.push(`<p class="md-p">${mdInline(buf.join(" "))}</p>`); }
    else i++;
  }
  closeList();
  return out.join("\n");
}

const docsState = { list: null, slug: null, query: "" };

async function renderDocs(main) {
  if (!docsState.list) docsState.list = await get("/api/docs");
  const cats = docsState.list.categories;

  main.innerHTML = `
    <h1 class="panel-title">Documentation</h1>
    <p class="panel-subtitle">
      All ${docsState.list.total} project documents, readable here rather than on GitHub —
      which matters on a phone that can reach the tailnet but not the internet.
    </p>
    <div class="docs-search">
      <input type="search" id="docs-q" placeholder="Search all documentation…"
             autocomplete="off" value="${escAttr(docsState.query)}">
      <span class="docs-hint" id="docs-hint"></span>
    </div>
    <div class="docs-layout">
      <nav class="docs-nav" id="docs-nav">
        ${cats.map(c => `
          <div class="docs-cat">
            <h4>${escHtml(c.name)}</h4>
            ${c.docs.map(d => `
              <a class="docs-link" href="#/system/docs" data-slug="${escAttr(d.slug)}"
                 title="${escAttr(d.summary || d.title)}">${escHtml(d.title)}</a>`).join("")}
          </div>`).join("")}
      </nav>
      <article class="docs-body" id="docs-body">
        <div class="empty-state">Pick a document, or search above.</div>
      </article>
    </div>
  `;

  const body = main.querySelector("#docs-body");
  const hint = main.querySelector("#docs-hint");

  async function openDoc(slug) {
    docsState.slug = slug;
    main.querySelectorAll(".docs-link").forEach(a =>
      a.classList.toggle("active", a.dataset.slug === slug));
    body.innerHTML = `<div class="empty-state loading">Loading…</div>`;
    try {
      const doc = await get(`/api/docs/${encodeURIComponent(slug)}`);
      body.innerHTML =
        `<div class="docs-meta">${escHtml(doc.category)} · ${doc.words} words ·
           <code class="inline">${escHtml(doc.path)}</code></div>` +
        renderMarkdown(doc.content);
      body.scrollTop = 0;
    } catch (e) {
      body.innerHTML = `<div class="empty-state">Couldn't load that document: ${escHtml(e.message)}</div>`;
    }
  }

  main.querySelector("#docs-nav").addEventListener("click", (e) => {
    const a = e.target.closest(".docs-link");
    if (!a) return;
    e.preventDefault();
    openDoc(a.dataset.slug);
  });

  // Debounced so typing doesn't fire a request per keystroke -- the search is
  // a full scan of every doc server-side.
  let searchTimer = null;
  const input = main.querySelector("#docs-q");
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    docsState.query = input.value;
    searchTimer = setTimeout(runSearch, 220);
  });

  async function runSearch() {
    const q = input.value.trim();
    if (q.length < 2) {
      hint.textContent = q ? "Keep typing…" : "";
      if (docsState.slug) openDoc(docsState.slug);
      else body.innerHTML = `<div class="empty-state">Pick a document, or search above.</div>`;
      return;
    }
    const res = await get(`/api/docs/search?q=${encodeURIComponent(q)}`);
    hint.textContent = res.results.length
      ? `${res.results.length} document${res.results.length === 1 ? "" : "s"}`
      : "no matches";
    body.innerHTML = res.results.length === 0
      ? `<div class="empty-state">Nothing matches “${escHtml(q)}”.</div>`
      : res.results.map(r => `
          <div class="docs-result">
            <a class="docs-result__title" href="#/system/docs" data-slug="${escAttr(r.slug)}">
              ${escHtml(r.title)}</a>
            <span class="tag">${escHtml(r.category)}</span>
            <span class="docs-result__hits">${r.hits} match${r.hits === 1 ? "" : "es"}</span>
            ${r.snippets.map(s => `<p class="docs-snippet">${highlight(s.text, q)}</p>`).join("")}
          </div>`).join("");
    body.querySelectorAll(".docs-result__title").forEach(a => {
      a.onclick = (e) => { e.preventDefault(); openDoc(a.dataset.slug); };
    });
  }

  // Highlight by splitting on the escaped needle, so the <mark> is inserted
  // around already-escaped text rather than into raw doc content.
  function highlight(text, q) {
    const safe = escHtml(text);
    const needle = escHtml(q);
    if (!needle) return safe;
    const parts = safe.split(new RegExp(`(${needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig"));
    return parts.map((p, i) => (i % 2 ? `<mark>${p}</mark>` : p)).join("");
  }

  if (docsState.query.length >= 2) runSearch();
  else if (docsState.slug) openDoc(docsState.slug);
}

// ------------------------------------------------------------ pin gate --
function showPinGate() {
  document.getElementById("pin-gate-overlay").style.display = "flex";
  document.getElementById("pin-input").focus();
}
function hidePinGate() {
  document.getElementById("pin-gate-overlay").style.display = "none";
}

async function submitPin() {
  const input = document.getElementById("pin-input");
  const errEl = document.getElementById("pin-error");
  const submitBtn = document.getElementById("pin-submit");
  errEl.textContent = "";
  // Guard against double-submit (easy to trigger with a clumsy tap on a phone
  // keyboard) and give real visual feedback that the tap registered.
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  const originalLabel = submitBtn.textContent;
  submitBtn.textContent = "Unlocking…";
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin: input.value }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      errEl.textContent = body.detail || "incorrect PIN";
      input.value = "";
      input.focus();
      return;
    }
    hidePinGate();
    input.value = "";
    await bootDashboard();
  } catch (e) {
    errEl.textContent = "could not reach the server";
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = originalLabel;
  }
}

document.getElementById("pin-submit").addEventListener("click", submitPin);
document.getElementById("pin-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitPin();
});

async function bootDashboard() {
  // Seed from the last-remembered device/panel (if any). loadDevices() already
  // falls back to the first real device when state.deviceId doesn't match any
  // configured device (e.g. it was removed since the last visit), so no extra
  // validation is needed here.
  state.deviceId = lsGet(LS_KEY_DEVICE);
  await loadDevices();
  startPolling();
  if (!location.hash) {
    // `ROUTES` was the pre-consolidation one-panel-per-tab map and no longer
    // exists; referencing it here threw a ReferenceError that aborted boot
    // before router() ran, leaving a completely blank dashboard. It only fired
    // for a *returning* visitor — with an empty localStorage `savedRoute` is
    // falsy and `&&` short-circuits before touching the missing global, which
    // is why a first visit looked fine and this survived review.
    // routeExists() validates against the current PAGES map instead.
    const savedRoute = lsGet(LS_KEY_ROUTE);
    location.hash = "#/" + (savedRoute && routeExists(savedRoute) ? savedRoute : DEFAULT_ROUTE);
  }
  router();
  // Deliberately not awaited: a banner about remote exposure must not hold up
  // first paint of the dashboard itself.
  refreshExposureWarnings();
}

// ---------------------------------------------------------------- init --
(async function init() {
  try {
    const authStatus = await fetch("/api/auth/status").then(r => r.json());
    if (authStatus.enabled && !authStatus.authenticated) {
      showPinGate();
      return;
    }
  } catch (e) { /* auth-status check failed open — same as PIN gate disabled */ }
  await bootDashboard();
})();
