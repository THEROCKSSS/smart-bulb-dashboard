/* Persistent lighting-mixer controls. Transport is injected for browser/native parity. */
(function (root) {
  function threeBandLevels(fractions = []) {
    const levels = [0, 0, 0];
    fractions.forEach((value, index) => { levels[Math.min(2, Math.floor(index * 3 / fractions.length))] += Number(value) || 0; });
    return levels;
  }
  function createQueue(send, initial = {}) {
    let draft = { ...initial }, pending = {}, running = null, phase = "saved";
    let revision = 0;
    const listeners = new Set();
    const notify = () => listeners.forEach(fn => fn({ phase, settings: { ...draft } }));
    async function drain() {
      while (Object.keys(pending).length) {
        const batch = pending;
        pending = {};
        phase = "saving"; notify();
        try {
          const response = await send(batch);
          if (response.saved !== true) throw new Error("The server did not confirm this save.");
          draft = { ...draft, ...response.settings, ...pending };
        } catch (error) {
          pending = { ...batch, ...pending };
          phase = "error"; notify();
          throw error;
        }
      }
      phase = "saved"; notify();
      return { ...draft };
    }
    function flush() {
      if (!running) running = drain().finally(() => { running = null; });
      return running;
    }
    return {
      update(patch) {
        revision += 1;
        draft = { ...draft, ...patch }; pending = { ...pending, ...patch };
        phase = "saving"; notify();
        return flush();
      },
      flush,
      revision: () => revision,
      hydrate(settings, expectedRevision = revision) {
        if (expectedRevision === revision && !running && !Object.keys(pending).length) { draft = { ...settings }; revision += 1; notify(); }
      },
      snapshot: () => ({ ...draft }),
      subscribe(fn) { listeners.add(fn); fn({ phase, settings: { ...draft } }); return () => listeners.delete(fn); },
    };
  }

  const queues = new Map();
  function queueFor(deviceId, send, initial) {
    if (!queues.has(deviceId)) queues.set(deviceId, createQueue(send, initial));
    else queues.get(deviceId).hydrate(initial);
    return queues.get(deviceId);
  }

  function mount(container, queue, { onError, api, deviceId }) {
    const settings = queue.snapshot();
    const controls = [
      ["smoothing_ms", "Smoothing", 0, 500, 10, "ms", 0],
      ["brightness_min", "Brightness floor", 0, 100, 1, "%", 0],
      ["brightness_max", "Brightness ceiling", 0, 100, 1, "%", 100],
      ["noise_gate_floor", "Noise gate", 0, 0.03, 0.0005, "", 0.0015],
      ["agc_target_rms", "Auto-gain target", 0.01, 0.5, 0.01, "", 0.15],
      ["agc_attack_ms", "Gain attack", 5, 500, 5, "ms", 50],
      ["agc_release_ms", "Gain release", 50, 2000, 25, "ms", 400],
    ];
    const escape = value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const range = (key, label, min, max, step, unit, fallback) => {
      const value = Number(settings[key] ?? fallback);
      return `<label class="mix-range"><span>${label}<output for="mix-${key}">${value}${unit}</output></span>
        <input id="mix-${key}" data-mix="${key}" data-unit="${unit}" type="range" min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
    };
    container.innerHTML = `<div class="mixer-heading"><div><span class="eyebrow">SHAPE YOUR SOUND</span><h3>Lighting mixer</h3></div>
      <span class="mix-save" role="status" aria-live="polite">Saved</span><button class="btn small mix-retry" hidden>Retry save</button></div>
      <p class="panel-subtitle">Tune how sound becomes light. Every adjustment is saved for this bulb.</p>
      <div class="mix-bands">${["Bass", "Mid", "Treble"].map((name, i) => `<label class="mix-band"><span>${name}</span>
        <div class="mix-band-track"><div class="mix-band-level" data-band-meter="${i}"></div><input type="range" aria-label="${name} gain" data-gain="${i}" min="0" max="4" step="0.05" value="${Number(settings.band_gains?.[i] ?? 1)}"></div>
        <output>${Number(settings.band_gains?.[i] ?? 1).toFixed(2)}×</output></label>`).join("")}</div>
      <div class="mix-options">${[["noise_gate_enabled", "Noise gate", true], ["agc_enabled", "Automatic gain", false], ["dc_removal_enabled", "Remove DC offset", true], ["disable_flash_heavy", "Gentle modes only", false]].map(([key, label, fallback]) => `<label class="mix-toggle"><input type="checkbox" data-mix="${key}" ${settings[key] ?? fallback ? "checked" : ""}><span>${label}</span></label>`).join("")}</div>
      <div class="mix-control-grid">${controls.map(control => range(...control)).join("")}</div>
      <div class="mix-presets"><h4>Your saved mixes</h4><div class="row"><input class="mix-name" aria-label="Mix name" maxlength="100" placeholder="Name this mix"><button class="primary mix-create">Save mix</button></div><div class="mix-preset-list"></div></div>`;
    const unsubscribe = queue.subscribe(({ phase, settings: current }) => {
      container.querySelector(".mix-save").textContent = { saving: "Saving…", saved: "Saved", error: "Not saved" }[phase];
      container.querySelector(".mix-save").dataset.phase = phase;
      container.querySelector(".mix-retry").hidden = phase !== "error";
      if (phase === 'saved') {
        container.querySelectorAll('[data-mix]').forEach(input => {
          if (document.activeElement === input) return;
          const value = current[input.dataset.mix];
          if (value == null) return;
          if (input.type === 'checkbox') input.checked = value;
          else { input.value = value; input.closest('label').querySelector('output').textContent = `${Number(value)}${input.dataset.unit}`; }
        });
        container.querySelectorAll('[data-gain]').forEach(input => {
          if (document.activeElement === input) return;
          input.value = current.band_gains?.[Number(input.dataset.gain)] ?? 1;
          input.closest('label').querySelector('output').textContent = `${Number(input.value).toFixed(2)}×`;
        });
        const mode = document.querySelector('#audio-mode');
        if (mode && mode.value !== current.mode) mode.value = current.mode;
      }
    });
    const submit = change => queue.update(change).catch(onError);
    container.querySelector(".mix-retry").onclick = () => queue.flush().catch(onError);
    container.querySelectorAll("[data-mix]").forEach(input => {
      input.oninput = () => {
        const output = input.closest("label").querySelector("output");
        if (output) output.textContent = `${Number(input.value)}${input.dataset.unit}`;
      };
      input.onchange = () => submit({ [input.dataset.mix]: input.type === "checkbox" ? input.checked : Number(input.value) });
    });
    container.querySelectorAll("[data-gain]").forEach(input => {
      input.oninput = () => { input.closest("label").querySelector("output").textContent = `${Number(input.value).toFixed(2)}×`; };
      input.onchange = () => {
        const gains = [0, 1, 2].map(i => Number(container.querySelector(`[data-gain="${i}"]`).value));
        submit({ band_gains: gains });
      };
    });
    async function refreshPresets() {
      const presets = await api(`/api/audio/session-presets?device_id=${encodeURIComponent(deviceId)}`);
      if (!container.isConnected) return;
      const list = container.querySelector(".mix-preset-list");
      list.innerHTML = presets.length ? "" : '<p class="mix-empty">No saved mixes yet. Get it sounding right, then give it a name.</p>';
      for (const preset of presets) {
        const row = document.createElement("div"); row.className = "mix-preset-row";
        row.innerHTML = `<button class="mix-recall">${escape(preset.name)}</button><button class="btn small mix-overwrite" aria-label="Update ${escape(preset.name)}">Update</button><button class="btn small mix-rename" aria-label="Rename ${escape(preset.name)}">Rename</button><button class="btn small mix-delete" aria-label="Delete ${escape(preset.name)}">Delete</button>`;
        row.querySelector(".mix-recall").onclick = async () => {
          try {
            await queue.flush();
            // Load the mix without unexpectedly starting the light.
            await queue.update(preset.config);
            root.dispatchEvent(new CustomEvent("lighting-mix-recalled", { detail: { deviceId } }));
          } catch (error) { onError(error); }
        };
        row.querySelector(".mix-overwrite").onclick = async () => {
          try { await queue.flush(); await api(`/api/audio/session-presets/${preset.id}`, { method: "PATCH", body: JSON.stringify({ config: queue.snapshot() }) }); await refreshPresets(); } catch (error) { onError(error); }
        };
        row.querySelector(".mix-rename").onclick = () => {
          const field = document.createElement("input"); field.value = preset.name; field.maxLength = 100; field.setAttribute("aria-label", "Rename mix");
          row.querySelector(".mix-recall").replaceWith(field); field.focus(); field.select();
          const saveName = async () => {
            if (!field.value.trim()) return;
            try { await api(`/api/audio/session-presets/${preset.id}`, { method: "PATCH", body: JSON.stringify({ name: field.value.trim() }) }); await refreshPresets(); } catch (error) { onError(error); }
          };
          const rename = row.querySelector(".mix-rename");
          rename.textContent = "Save name"; rename.onclick = saveName;
          field.onkeydown = async event => {
            if (event.key === "Escape") await refreshPresets();
            if (event.key === "Enter") await saveName();
          };
        };
        row.querySelector(".mix-delete").onclick = async () => {
          try { await api(`/api/audio/session-presets/${preset.id}`, { method: "DELETE" }); await refreshPresets(); } catch (error) { onError(error); }
        };
        list.append(row);
      }
    }
    container.querySelector(".mix-create").onclick = async () => {
      const name = container.querySelector(".mix-name").value.trim();
      if (!name) { container.querySelector(".mix-name").focus(); return; }
      try {
        await queue.flush();
        await api(`/api/devices/${encodeURIComponent(deviceId)}/audio-reactive/session-presets`, { method: "POST", body: JSON.stringify({ ...queue.snapshot(), name }) });
        container.querySelector(".mix-name").value = ""; await refreshPresets();
      } catch (error) { onError(error); }
    };
    refreshPresets().catch(onError);
    return { dispose: unsubscribe, meter(fractions = []) {
      const levels = threeBandLevels(fractions);
      container.querySelectorAll("[data-band-meter]").forEach((bar, index) => {
        bar.style.transform = `scaleY(${Math.max(0, Math.min(1, levels[index]))})`;
      });
    } };
  }
  root.LightingMixer = { createQueue, queueFor, mount, threeBandLevels };
  if (typeof module !== "undefined") module.exports = root.LightingMixer;
})(globalThis);
