// The lighting surface paints synchronously. Hardware status arrives independently.
let powerBusy = false;
let commandEpoch = 0;
let controlEditing = false;
let temperatureDirty = false;

function bulbMarkup() {
  return `<div class="bulb-art" aria-hidden="true"><div class="light-halo"></div>
    <div class="bulb-glass"><i></i></div><div class="bulb-neck"></div><div class="bulb-base"></div>
    <div class="bulb-shadow"></div></div>`;
}

function paintControlStatus() {
  const st = state.lastStatus;
  const known = st?.online === true && state.consecutiveOfflinePolls < OFFLINE_CONFIRM_THRESHOLD;
  const stage = document.getElementById("light-stage");
  if (!stage) return;
  const on = known && st.power;
  const mode = st?.mode === "colour" ? "Color" : "White";
  stage.classList.toggle("is-on", Boolean(on));
  stage.classList.toggle("is-pending", powerBusy);
  const caption = document.getElementById("light-state");
  caption.textContent = powerBusy ? "Sending command…" : known ? (on ? "The light is on." : "A moment of quiet.") : state.hasPolledOnce ? "Your light is unreachable." : "Finding your light…";
  document.getElementById("light-state-detail").textContent = powerBusy
    ? "Waiting for the bulb to confirm."
    : known ? (on ? `${mode} light · make it your own.` : "Ready whenever you are.")
    : state.hasPolledOnce ? (/key|version/i.test(st?.error || "") ? "The bulb rejected its saved connection settings. Check them in System settings." : "Check its power and Wi-Fi, then retry the connection.") : "Controls will be ready when the bulb responds.";
  const tag = document.getElementById("control-source");
  tag.textContent = powerBusy ? "PENDING" : known ? "LIVE DATA" : state.hasPolledOnce ? "OFFLINE" : "CONNECTING";
  tag.className = `tag ${known ? "on" : ""}`;
  document.getElementById("control-retry").hidden = known || !state.hasPolledOnce;
  document.querySelectorAll("#power-toggle, #qc-power").forEach(btn => {
    btn.disabled = !known || powerBusy;
    btn.classList.toggle("primary", Boolean(on));
    btn.setAttribute("aria-pressed", String(Boolean(on)));
    btn.setAttribute("aria-busy", String(powerBusy));
    btn.textContent = powerBusy ? "Confirming…" : on ? "Turn off" : "Turn on";
  });
  document.querySelectorAll("#studio-controls input, #studio-controls button, .studio-actions button").forEach(input => {
    input.disabled = !known || powerBusy;
  });
  if (!known) {
    for (const id of ["brightness-display", "brightness-val", "hue-val", "sat-val", "temp-val", "color-hex"]) document.getElementById(id).textContent = "—";
    return;
  }
  if (controlEditing || powerBusy) return;
  const values = { brightness: st.mode === "colour" ? st.value_pct : st.brightness_pct, hue: st.hue, sat: st.saturation_pct, temp: st.color_temp_pct };
  for (const [key, value] of Object.entries(values)) {
    if (key === "temp" && temperatureDirty) continue;
    const slider = document.getElementById(`${key === "sat" ? "sat" : key}-slider`);
    if (slider && value != null && document.activeElement !== slider) slider.value = value;
  }
  paintControlPreview();
}

function paintControlPreview() {
  const brightness = document.getElementById("brightness-slider");
  if (!brightness) return;
  const hue = document.getElementById("hue-slider");
  const sat = document.getElementById("sat-slider");
  const temp = document.getElementById("temp-slider");
  const color = rgbToHex(...hsvToRgb(Number(hue.value), Number(sat.value), 100));
  const stage = document.getElementById("light-stage");
  const white = state.lastStatus?.mode !== "colour" && !controlEditing;
  stage.style.setProperty("--light-color", white ? "#ffe2aa" : color);
  stage.style.setProperty("--light-level", Number(brightness.value) / 100);
  document.getElementById("brightness-val").textContent = brightness.value + "%";
  document.getElementById("brightness-display").textContent = brightness.value;
  document.getElementById("hue-val").textContent = hue.value + "°";
  document.getElementById("sat-val").textContent = sat.value + "%";
  document.getElementById("temp-val").textContent = temp.value + "%";
  document.getElementById("rgb-picker").value = color;
  document.getElementById("color-hex").textContent = color.toUpperCase();
  for (const slider of [brightness, hue, sat, temp]) {
    slider.style.setProperty("--fill", (Number(slider.value) - Number(slider.min)) / (Number(slider.max) - Number(slider.min)) * 100 + "%");
  }
}

async function setPower(on) {
  if (powerBusy || !state.lastStatus?.online) return;
  const device = state.deviceId;
  const focusedId = document.activeElement.id;
  const previous = { ...state.lastStatus };
  powerBusy = true;
  commandEpoch++;
  state.lastStatus = { ...previous, power: on };
  renderQuickControl();
  paintControlStatus();
  try {
    await post(`/api/devices/${device}/power`, { on });
    // A successful HTTP response is not proof that the physical light changed.
    const verified = await get(`/api/devices/${device}/status`);
    if (!verified.online || verified.power !== on) throw new Error("The bulb did not confirm the power change. Check its connection and retry.");
    if (state.deviceId === device) {
      state.lastStatus = verified;
      state.lastSeenAt = Date.now();
      toast(on ? "Light turned on" : "Light turned off", "success");
    }
  } catch (error) {
    if (state.deviceId === device) state.lastStatus = previous;
    if (!error.apiNotified) toast(error.message, "error");
  } finally {
    powerBusy = false;
    commandEpoch++;
    renderStatusText();
    renderQuickControl();
    paintControlStatus();
    if (document.activeElement === document.body && focusedId) document.getElementById(focusedId)?.focus({ preventScroll: true });
  }
}

function renderControl(main) {
  temperatureDirty = false;
  const dev = state.devices.find(d => d.id === state.deviceId);
  main.innerHTML = `
    <header class="studio-heading"><div><p class="eyebrow">YOUR LIGHT, YOUR ATMOSPHERE</p>
      <h1 class="panel-title">Set the mood<span class="accent">.</span></h1>
      <p class="panel-subtitle">Small adjustments. A whole different room.</p></div>
      <span class="studio-device"><span class="local-mark"></span>${escHtml(dev?.name || "No bulb selected")}</span></header>
    <div class="studio-grid">
      <section id="light-stage" class="light-stage" aria-label="Light preview">
        <div class="stage-top"><span class="eyebrow">LIGHT PREVIEW</span><span id="control-source" class="tag">CONNECTING</span></div>
        ${bulbMarkup()}
        <div class="stage-caption"><h2 id="light-state">Finding your light…</h2><p id="light-state-detail">Waiting for a live reading.</p></div>
        <button id="power-toggle" class="stage-power" disabled>Turn on</button>
        <button id="control-retry" class="text-button" hidden>Retry connection</button>
        <p class="stage-shortcut"><kbd>Space</kbd> power <span>·</span> <kbd>↑</kbd> <kbd>↓</kbd> brightness</p>
      </section>
      <div id="studio-controls" class="studio-controls">
        <section class="card brightness-card"><div class="control-section-head"><h3>Brightness</h3><span class="control-symbol" aria-hidden="true">☼</span></div>
          <div class="brightness-reading"><span id="brightness-display">50</span><span>%</span><small>Make room for the right light.</small></div>
          <div class="slider-row"><label for="brightness-slider"><span>Intensity</span><span id="brightness-val">50%</span></label><input aria-label="Brightness" type="range" id="brightness-slider" min="1" max="100" value="50" disabled></div>
          <div class="range-endpoints"><span>Subtle</span><span>Brilliant</span></div>
        </section>
        <section class="card color-card"><div class="control-section-head"><h3>Color palette</h3><span id="color-hex" class="mono">#FFAA55</span></div>
          <div class="color-dots" aria-label="Quick colors">${[["Amber",32,"#f5ba70"],["Coral",12,"#ef896f"],["Rose",330,"#e693be"],["Violet",265,"#b59bdf"],["Ocean",205,"#83bde5"],["Mint",155,"#8bd4b0"]].map(([name,hue,color]) => `<button type="button" class="color-dot" data-hue="${hue}" style="--dot-color:${color}" aria-label="Set ${name.toLowerCase()} color" title="${name}" disabled></button>`).join("")}</div>
          <div class="slider-row hue-slider"><label for="hue-slider"><span>Hue</span><span id="hue-val">32°</span></label><input type="range" id="hue-slider" min="0" max="359" value="32" disabled></div>
          <div class="slider-row"><label for="sat-slider"><span>Saturation</span><span id="sat-val">70%</span></label><input type="range" id="sat-slider" min="0" max="100" value="70" disabled></div>
          <label class="exact-color" for="rgb-picker">Find your own color <input type="color" id="rgb-picker" value="#ffbb77" disabled></label>
        </section>
        <section class="card white-card"><div class="control-section-head"><h3>White balance</h3><span class="control-symbol" aria-hidden="true">◑</span></div>
          <div class="slider-row temperature-slider"><label for="temp-slider"><span>Temperature</span><span id="temp-val">50%</span></label><input type="range" id="temp-slider" min="0" max="100" value="50" disabled></div>
          <div class="range-endpoints"><span>Candle warm</span><span>Daylight cool</span></div>
          <button id="apply-white" class="white-apply" disabled>Use white light <span aria-hidden="true">↗</span></button>
        </section>
      </div>
    </div>
    <section class="studio-bottom"><div><p class="eyebrow">A CHANGE OF SCENE</p><h2>Let the room follow you.</h2></div><a href="#/light/looks" class="scene-link">Explore scenes & effects <span aria-hidden="true">↗</span></a></section>
    <div class="studio-actions"><span>Try something different</span><button id="btn-random" disabled>Random color</button><button id="btn-identify" disabled>Identify bulb</button><button id="btn-flash" disabled>Flash alert</button></div>`;
  main.querySelector("#power-toggle").onclick = () => setPower(!state.lastStatus?.power);
  main.querySelector("#control-retry").onclick = () => pollStatus();
  const device = state.deviceId;
  // Latest-value queue: slider gestures never create a growing command backlog.
  const pending = new Map();
  let draining = false;
  async function commit(path, body) {
    pending.delete(path);
    pending.set(path, body);
    controlEditing = true;
    if (draining) return;
    draining = true;
    commandEpoch++;
    try {
      while (pending.size) {
        const [next, value] = pending.entries().next().value;
        pending.delete(next);
        await post(`/api/devices/${device}/${next}`, value);
      }
    } catch (error) {
      pending.clear();
    } finally {
      draining = false;
      controlEditing = false;
      commandEpoch++;
      if (state.deviceId === device) pollStatus();
    }
  }
  const brightness = main.querySelector("#brightness-slider");
  const hue = main.querySelector("#hue-slider");
  const sat = main.querySelector("#sat-slider");
  const temp = main.querySelector("#temp-slider");
  [brightness, hue, sat, temp].forEach(slider => {
    slider.oninput = () => { controlEditing = true; if (slider === temp) temperatureDirty = true; paintControlPreview(); };
    slider.onblur = () => { if (!draining) controlEditing = false; };
  });
  brightness.onchange = () => commit("brightness", { value: Number(brightness.value) });
  const colorChange = () => commit("color/hsv", { h: Number(hue.value), s: Number(sat.value), v: Number(brightness.value) });
  hue.onchange = sat.onchange = colorChange;
  temp.onchange = () => { controlEditing = false; };
  main.querySelectorAll(".color-dot").forEach(btn => btn.onclick = () => {
    hue.value = btn.dataset.hue;
    sat.value = 75;
    controlEditing = true;
    paintControlPreview();
    colorChange();
  });
  main.querySelector("#rgb-picker").onchange = e => {
    const hex = e.target.value;
    commit("color", {r: parseInt(hex.slice(1,3),16), g: parseInt(hex.slice(3,5),16), b: parseInt(hex.slice(5,7),16)});
  };
  main.querySelector("#apply-white").onclick = async () => {
    await commit("white", { brightness: Number(brightness.value), color_temp: Number(temp.value) });
    temperatureDirty = false;
  };
  main.querySelector("#btn-random").onclick = () => commit("color/random", {});
  main.querySelector("#btn-identify").onclick = () => commit("identify", {});
  main.querySelector("#btn-flash").onclick = () => commit("flash-alert", { r:255, g:0, b:0, times:3 });
  controlKeyHandler = e => {
    if (e.repeat && e.code === "Space") return;
    if (e.target.closest("input, textarea, select, button, a, [contenteditable=true]")) return;
    if (e.code === "Space") { e.preventDefault(); main.querySelector("#power-toggle").click(); }
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !brightness.disabled) {
      e.preventDefault();
      brightness.value = Math.max(1, Math.min(100, Number(brightness.value) + (e.key === "ArrowUp" ? 5 : -5)));
      brightness.oninput();
      clearTimeout(controlKeyCommitTimer);
      controlKeyCommitTimer = setTimeout(() => { controlKeyCommitTimer = null; brightness.onchange(); }, 250);
    }
  };
  document.addEventListener("keydown", controlKeyHandler);
  paintControlStatus();
}
