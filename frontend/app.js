const API = "";

const state = {
  deviceId: null,
  devices: [],
  presets: [],
  scenes: [],
  effects: [],
  lastStatus: null,
  statusPollHandle: null,
  consecutiveOfflinePolls: 0,
};

// ---------------------------------------------------------------- utils --
function toast(msg, kind) {
  const stack = document.getElementById("toast-stack");
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  stack.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  try {
    const res = await fetch(API + path, opts);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res.text();
  } catch (e) {
    toast(`Error: ${e.message}`, "error");
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
  document.querySelectorAll(".nav-item").forEach(n => {
    n.classList.toggle("active", n.dataset.route === route);
  });
  const main = document.getElementById("main");
  main.innerHTML = `<div class="empty-state">Loading…</div>`;
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
}

document.getElementById("device-select").addEventListener("change", (e) => {
  state.deviceId = e.target.value;
  router();
});

// --------------------------------------------------------- status badge --
// Real Wi-Fi bulbs occasionally miss a single poll (radio contention, etc).
// Require 2 consecutive failed/offline polls before showing OFFLINE, so one
// transient blip doesn't flash a false "offline" state on every page load.
const OFFLINE_CONFIRM_THRESHOLD = 2;

async function pollStatus(quiet) {
  if (!state.deviceId) return;
  const badge = document.getElementById("status-badge");
  const text = document.getElementById("status-text");
  try {
    const st = await fetch(`${API}/api/devices/${state.deviceId}/status`).then(r => r.json());
    if (st.online) {
      state.consecutiveOfflinePolls = 0;
      state.lastStatus = st;
      badge.classList.add("live");
      text.textContent = `LIVE DATA · ${st.power ? "ON" : "OFF"}`;
    } else {
      state.consecutiveOfflinePolls++;
      if (state.consecutiveOfflinePolls >= OFFLINE_CONFIRM_THRESHOLD) {
        badge.classList.remove("live");
        text.textContent = "OFFLINE";
      }
    }
  } catch (e) {
    state.consecutiveOfflinePolls++;
    if (state.consecutiveOfflinePolls >= OFFLINE_CONFIRM_THRESHOLD) {
      badge.classList.remove("live");
      text.textContent = "OFFLINE";
    }
  }
}

function startPolling() {
  if (state.statusPollHandle) clearInterval(state.statusPollHandle);
  pollStatus();
  setTimeout(pollStatus, 1200); // quick re-check shortly after first load
  state.statusPollHandle = setInterval(pollStatus, 4000);
}

// ================================================================ PANELS ==

async function renderControl(main) {
  const st = state.lastStatus || await get(`/api/devices/${state.deviceId}/status`);
  state.lastStatus = st;
  const rgb = st.hue != null ? hsvToRgb(st.hue, st.saturation_pct ?? 100, st.value_pct ?? 100) : [255, 255, 255];

  main.innerHTML = `
    <h1 class="panel-title">Control</h1>
    <p class="panel-subtitle">Direct power, brightness and color control — <span class="tag ${st.online ? "on" : "error"}">${st.online ? "LIVE DATA" : "OFFLINE"}</span></p>

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

async function renderTimers(main) {
  const sleepSt = await get(`/api/devices/${state.deviceId}/timers/sleep`);
  const wakeSt = await get(`/api/devices/${state.deviceId}/timers/wake`);

  main.innerHTML = `
    <h1 class="panel-title">Timers</h1>
    <p class="panel-subtitle">Sleep timers fade the bulb out before turning off; wake timers fade it in as an alarm</p>

    <div class="card">
      <h3>Sleep Timer</h3>
      ${sleepSt.active
        ? `<p>Active — turns off in <b>${Math.floor(sleepSt.seconds_remaining / 60)}m ${sleepSt.seconds_remaining % 60}s</b></p>
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
        ? `<p>Active — will fire at <b>${wakeSt.target}</b>, fading in over ${wakeSt.fade_minutes} min to ${wakeSt.brightness}%</p>
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
      await del(`/api/devices/${state.deviceId}/timers/sleep`);
      toast("Sleep timer cancelled");
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
      await del(`/api/devices/${state.deviceId}/timers/wake`);
      toast("Wake timer cancelled");
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
  main.innerHTML = `
    <h1 class="panel-title">Diagnostics</h1>
    <p class="panel-subtitle">Test connectivity and troubleshoot the local connection to this bulb</p>
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
          <tr><td>IP</td><td>${r.ip}</td></tr>
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
}

// ---------------------------------------------------------------- init --
(async function init() {
  await loadDevices();
  startPolling();
  if (!location.hash) location.hash = "#/control";
  router();
})();
