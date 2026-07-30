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
const ROUTES = {
  control: renderControl,
  scenes: renderScenes,
  effects: renderEffects,
  audio: renderAudio,
  presets: renderPresets,
  timers: renderTimers,
  schedule: renderSchedule,
  groups: renderGroups,
  history: renderHistory,
  diagnostics: renderDiagnostics,
  settings: renderSettings,
};

function currentRoute() {
  const hash = location.hash.replace(/^#\/?/, "");
  return ROUTES[hash] ? hash : "control";
}

async function router() {
  const route = currentRoute();
  lsSet(LS_KEY_ROUTE, route);
  if (controlKeyHandler) {
    document.removeEventListener("keydown", controlKeyHandler);
    controlKeyHandler = null;
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
    n.classList.toggle("active", n.dataset.route === route);
  });
  const main = document.getElementById("main");
  main.innerHTML = `<div class="empty-state loading">Loading…</div>`;
  try {
    await ROUTES[route](main);
  } catch (e) {
    main.innerHTML = `<div class="empty-state">Failed to load this panel: ${e.message}</div>`;
  }
}

window.addEventListener("hashchange", router);
document.getElementById("sidebar").addEventListener("click", (e) => {
  const item = e.target.closest(".nav-item");
  if (item) location.hash = "#/" + item.dataset.route;
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
      slider.dispatchEvent(new Event("change"));
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

async function renderAudio(main) {
  stopAudioPolling();
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
  const modeOptions = Object.entries(AUDIO_MODE_INFO).map(([id, info]) => `<option value="${id}" ${sessionStatus.mode === id ? "selected" : ""}>${info.name}</option>`).join("");

  main.innerHTML = `
    <h1 class="panel-title">Audio Reactive <span class="tag on">LIVE DATA</span></h1>
    <p class="panel-subtitle">
      Bulb reacts to whatever this input device hears. Route your PC's audio through
      VoiceMeeter (or pick a real microphone) — see the Audio skill/docs for setup.
      Decision latency is now sub-15ms internally; the "Min. dwell" slider controls how
      long each color actually stays on the bulb so you can still see it change.
    </p>

    <div class="card">
      <h3>Single-Bulb Session</h3>
      <div class="form-grid">
        <label>Input device<select id="audio-device">${deviceOptions}</select></label>
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
  modeSelect.onchange = syncModeUI;
  syncModeUI();

  main.querySelector("#audio-sensitivity").oninput = (e) => {
    main.querySelector("#sens-val").textContent = parseFloat(e.target.value).toFixed(1) + "x";
  };
  main.querySelector("#audio-dwell").oninput = (e) => {
    main.querySelector("#dwell-val").textContent = e.target.value + "ms";
  };
  main.querySelector("#audio-nbands").oninput = (e) => {
    main.querySelector("#nbands-val").textContent = e.target.value;
  };
  main.querySelector("#mono-hue").oninput = (e) => {
    main.querySelector("#mono-hue-val").textContent = e.target.value + "°";
  };

  main.querySelector("#audio-start").onclick = async () => {
    if (audioDevices.length === 0) {
      toast("No audio input devices available", "error");
      return;
    }
    await post(`/api/devices/${state.deviceId}/audio-reactive/start`, {
      device_index: parseInt(main.querySelector("#audio-device").value, 10),
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

  const presetGrid = main.querySelector("#audio-preset-grid");
  const allPresets = audioPresetsResp.presets || [];
  allPresets.forEach(preset => {
    const card = el(`<div class="effect-card" title="${preset.description || ""}">
      <div class="name">${preset.name}${preset.custom ? " ★" : ""}</div>
      <div class="desc">${preset.description || "Custom preset"}</div>
    </div>`);
    card.onclick = async () => {
      if (audioDevices.length === 0) {
        toast("No audio input devices available", "error");
        return;
      }
      await post(`/api/devices/${state.deviceId}/audio-reactive/apply-preset`, {
        preset_id: preset.id,
        device_index: parseInt(main.querySelector("#audio-device").value, 10),
      });
      toast(`Preset "${preset.name}" applied`, "success");
      renderAudio(main);
    };
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

async function renderHistory(main) {
  const hist = await get(`/api/devices/${state.deviceId}/history`);
  main.innerHTML = `
    <h1 class="panel-title">History</h1>
    <p class="panel-subtitle">Last ${hist.length} actions for this device (in-memory, resets on backend restart)</p>
    ${hist.length === 0 ? '<div class="empty-state">No actions logged yet</div>' : `
    <table>
      <thead><tr><th>Time (UTC)</th><th>Action</th><th>Params</th><th>Result</th></tr></thead>
      <tbody>${hist.map(h => `
        <tr>
          <td>${h.timestamp.replace("T", " ").slice(0, 19)}</td>
          <td>${h.action}</td>
          <td><code class="inline">${JSON.stringify(h.params)}</code></td>
          <td><span class="tag ${h.ok ? "on" : "error"}">${h.ok ? "ok" : "error: " + h.error}</span></td>
        </tr>`).join("")}
      </tbody>
    </table>`}
  `;
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
      <h3>System Info</h3>
      <div id="sys-info">Loading…</div>
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
  const info = await get("/api/system/info");
  main.querySelector("#sys-info").innerHTML = `
    <table><tbody>
      <tr><td>Version</td><td>${info.version}</td></tr>
      <tr><td>Uptime</td><td>${Math.round(info.uptime_seconds)}s</td></tr>
      <tr><td>Presets</td><td>${info.presets_count}</td></tr>
      <tr><td>Scenes</td><td>${info.scenes_count}</td></tr>
      <tr><td>Effects</td><td>${info.effects_count}</td></tr>
    </tbody></table>`;
}

async function renderSettings(main) {
  const disco = await get("/api/system/discovery");
  const remoteAuth = await get("/api/system/remote-auth/status");

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
        Local-only setups can safely leave this disabled. Locks out an IP for
        5 minutes after 5 wrong PIN attempts.
      </p>
      ${remoteAuth.enabled ? `
        <button id="remote-auth-disable" class="danger">Disable PIN Gate</button>
      ` : `
        <div class="form-grid">
          <label>New PIN (min. 4 characters)<input type="password" id="remote-auth-pin" autocomplete="off"></label>
        </div>
        <button id="remote-auth-enable" class="primary" style="margin-top:10px;">Enable PIN Gate</button>
      `}
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

  const enableBtn = main.querySelector("#remote-auth-enable");
  if (enableBtn) {
    enableBtn.onclick = async () => {
      const pin = main.querySelector("#remote-auth-pin").value;
      if (pin.length < 4) { toast("PIN must be at least 4 characters", "error"); return; }
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
      renderSettings(main);
    };
  }
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
    const savedRoute = lsGet(LS_KEY_ROUTE);
    location.hash = "#/" + (savedRoute && ROUTES[savedRoute] ? savedRoute : "control");
  }
  router();
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
