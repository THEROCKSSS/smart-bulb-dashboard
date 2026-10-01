import React, { useEffect, useRef, useState } from "react";
import { Switch, Text, TextInput, View } from "react-native";
import { Button, C, Fader, Meter, s } from "./ui";
import { hsvHex, rgbHex, Section, Select } from "./controls";
import { Settings } from "./api";
const { threeBandLevels } = require("../frontend/mixer.js");
const modeInfo = require("./audio-modes.json");
const pretty = (value: string) =>
  value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

function NumberSetting({
  label,
  value,
  onChange,
  nullable,
  disabled,
  min,
  max,
  integer = false,
}: any) {
  const [draft, setDraft] = useState(value == null ? "" : String(value));
  const [error, setError] = useState("");
  const focused = useRef(false);
  useEffect(() => {
    if (focused.current) return;
    setDraft(value == null ? "" : String(value));
    setError("");
  }, [value]);
  const save = () => {
    const next = draft.trim() === "" && nullable ? null : Number(draft);
    if (
      next !== null &&
      (!draft.trim() ||
        !Number.isFinite(next) ||
        next < min ||
        next > max ||
        (integer && !Number.isInteger(next)))
    ) {
      setError(
        `Enter ${integer ? "a whole number" : "a number"} from ${min} to ${max}.`,
      );
      return;
    }
    setError("");
    if (next !== value) onChange(next);
  };
  return (
    <View style={{ gap: 6 }}>
      <Text style={s.label}>{label}</Text>
      <TextInput
        accessibilityLabel={label}
        editable={!disabled}
        style={[s.input, disabled && s.disabled]}
        keyboardType="decimal-pad"
        value={draft}
        onChangeText={setDraft}
        onFocus={() => {
          focused.current = true;
        }}
        onBlur={() => {
          focused.current = false;
          save();
        }}
        onSubmitEditing={save}
        placeholder={nullable ? "Default / no limit" : String(min)}
        placeholderTextColor={C.muted}
      />
      {!!error && <Text style={{ color: C.red }}>{error}</Text>}
    </View>
  );
}

export default function AudioScreen({
  api,
  path,
  settings,
  session,
  bridge,
  modes,
  mixes,
  phase,
  queue,
  edit,
  run,
  reload,
  start,
  stop,
  busy,
  ready,
  reduced,
  openTools,
  openConnect,
}: any) {
  const [genres, setGenres] = useState<any[]>([]),
    [colors, setColors] = useState<any[]>([]),
    [name, setName] = useState(""),
    [renameId, setRenameId] = useState(""),
    [genreError, setGenreError] = useState("");
  const [starter, setStarter] = useState(""),
    [section, setSection] = useState("mix");
  useEffect(() => {
    let current = true;
    Promise.all([api("/api/audio/presets"), api("/api/presets")])
      .then(([list, palette]) => {
        if (current) {
          setGenres(list.presets);
          setColors(palette);
          setGenreError("");
        }
      })
      .catch((err: Error) => {
        if (current) setGenreError(err.message);
      });
    return () => {
      current = false;
    };
  }, [api]);
  const disabled = busy || !ready;
  const saveMix = () =>
    run(async () => {
      if (!name.trim()) throw new Error("Give your mix a name.");
      await queue.flush();
      if (renameId)
        await api(
          "/api/audio/session-presets/" + renameId,
          { name: name.trim() },
          "PATCH",
        );
      else
        await api(path + "/audio-reactive/session-presets", {
          ...queue.snapshot(),
          name: name.trim(),
        });
      setName("");
      setRenameId("");
      await reload();
    });
  const selected = genres.find((p) => p.id === starter);
  return (
    <>
      <View style={s.card}>
        <View style={s.between}>
          <Text style={s.cardTitle}>Lighting mixer</Text>
          <Text
            accessibilityLiveRegion="polite"
            style={[s.number, phase === "error" && { color: C.red }]}
          >
            {phase === "saving"
              ? "Saving…"
              : phase === "error"
                ? "Not saved"
                : phase === "loading"
                  ? "Loading…"
                  : "Saved"}
          </Text>
        </View>
        {phase === "error" && (
          <Button
            label="Retry save"
            onPress={() =>
              run(async () => {
                await queue.flush();
              })
            }
          />
        )}
        <Text style={s.body}>
          {bridge.connected
            ? bridge.devices?.find((d: any) => d.index === bridge.device_index)
                ?.name || "Windows audio connected"
            : "Waiting for Windows audio"}
        </Text>
        <View style={s.meters}>
          {["BASS", "MID", "TREBLE"].map((label, i) => (
            <Meter
              key={label}
              label={label}
              value={threeBandLevels(session.bands?.fractions)[i]}
              reduced={reduced}
            />
          ))}
        </View>
        <View style={s.wrap}>
          <Button
            label={session.active ? "Session running" : "Start session"}
            active
            disabled={
              disabled ||
              session.active ||
              (settings.source === "bridge" && !bridge.connected)
            }
            onPress={start}
          />
          <Button
            label="Stop"
            disabled={busy || !session.active}
            onPress={stop}
          />
          <Button label="Choose audio input" onPress={openConnect} />
        </View>
        <Text style={s.body}>
          {session.active
            ? "LIVE DATA · Listening now"
            : "Ready when you are. Choose a starting mix or tune your own."}
        </Text>
        {!!session.tempo?.bpm && (
          <Text style={s.number}>{Math.round(session.tempo.bpm)} BPM</Text>
        )}
        {session.active && (
          <Button
            label="Tap tempo"
            onPress={() =>
              run(async () => {
                await api(path + "/audio-reactive/tap-tempo", {});
              })
            }
          />
        )}
      </View>
      <View style={[s.wrap, { marginTop: 18 }]}>
        {[
          ["mix", "Mix & presets"],
          ["tune", "Fine tune"],
        ].map(([value, label]) => (
          <Button
            key={value}
            label={label}
            active={section === value}
            onPress={() => setSection(value)}
          />
        ))}
      </View>
      {section === "mix" && (
        <>
          <View style={s.card}>
            <Text style={s.cardTitle}>Start with a mood</Text>
            <Text style={s.eyebrow}>NON-LIVE DATA · READY-MADE MIXES</Text>
            {!!genreError && <Text style={{ color: C.red }}>{genreError}</Text>}
            <Select
              label="Starting mix"
              value={starter}
              options={genres.map((p) => ({
                value: p.id,
                label: p.name,
                detail: p.description,
              }))}
              onChange={setStarter}
            />
            {selected && (
              <>
                <Text style={s.body}>{selected.description}</Text>
                <Text style={s.number}>
                  {selected.tempo_range || pretty(selected.mode)}
                </Text>
                <View style={s.wrap}>
                  {(selected.palette || []).map((id: string) => {
                    const color = colors.find((p) => p.id === id);
                    return color ? (
                      <View key={id} style={{ flex: 1, minWidth: 32, gap: 5 }}>
                        <View
                          style={{
                            height: 32,
                            borderRadius: 8,
                            backgroundColor: rgbHex(color.rgb),
                          }}
                        />
                        <Text style={s.eyebrow}>{color.name}</Text>
                      </View>
                    ) : null;
                  })}
                </View>
                <Text style={s.body}>
                  Palette preview · modes use their built-in color mappings.
                </Text>
                <Button
                  label={"Use " + selected.name}
                  active
                  disabled={disabled}
                  onPress={() =>
                    run(async () => {
                      const patch: Settings = {};
                      [
                        "mode",
                        "sensitivity",
                        "monochrome_hue",
                        "n_bands",
                        "min_dwell_ms",
                        "beat_sensitivity",
                      ].forEach((key) => {
                        patch[key] = selected[key];
                      });
                      await queue.update(patch);
                      await reload();
                    })
                  }
                />
              </>
            )}
          </View>
          <View style={s.card}>
            <Text style={s.cardTitle}>Your saved mixes</Text>
            <Text style={s.body}>
              Save the complete setup for this bulb. Load a mix, adjust it, and
              save it again here.
            </Text>
            <TextInput
              accessibilityLabel="Mix name"
              value={name}
              onChangeText={setName}
              placeholder="Evening listening"
              placeholderTextColor={C.muted}
              maxLength={100}
              style={s.input}
            />
            <Button
              label={renameId ? "Save name" : "Save mix"}
              active
              disabled={disabled || !name.trim()}
              onPress={saveMix}
            />
            {!!renameId && (
              <Button
                label="Cancel rename"
                onPress={() => {
                  setRenameId("");
                  setName("");
                }}
              />
            )}
            {!mixes.length && (
              <Text style={s.body}>
                No saved mixes yet. Try a starting mood above, then give your
                mix a name.
              </Text>
            )}
            {mixes.map((mix: any) => (
              <View
                key={mix.id}
                style={{
                  paddingVertical: 16,
                  borderTopWidth: 1,
                  borderColor: C.line,
                  gap: 10,
                }}
              >
                <Text style={s.cardTitle}>{mix.name}</Text>
                <Text style={s.body}>
                  {modeInfo[mix.config.mode]?.name || pretty(mix.config.mode)} ·{" "}
                  {mix.config.sensitivity}× · {mix.config.n_bands} bands ·{" "}
                  {mix.config.min_dwell_ms} ms
                </Text>
                <View style={s.wrap}>
                  <Button
                    label={"Recall " + mix.name}
                    active
                    disabled={disabled || session.active}
                    onPress={() =>
                      run(async () => {
                        await queue.flush();
                        await queue.update(mix.config);
                        await reload();
                      })
                    }
                  />
                  <Button
                    label={"Update " + mix.name}
                    disabled={disabled}
                    onPress={() =>
                      run(async () => {
                        await queue.flush();
                        await api(
                          "/api/audio/session-presets/" + mix.id,
                          { config: queue.snapshot() },
                          "PATCH",
                        );
                        await reload();
                      })
                    }
                  />
                  <Button
                    label={"Rename " + mix.name}
                    disabled={busy}
                    onPress={() => {
                      setRenameId(mix.id);
                      setName(mix.name);
                    }}
                  />
                  <Button
                    label={"Delete " + mix.name}
                    disabled={busy}
                    onPress={() =>
                      run(async () => {
                        await api(
                          "/api/audio/session-presets/" + mix.id,
                          undefined,
                          "DELETE",
                        );
                        await reload();
                      })
                    }
                  />
                </View>
              </View>
            ))}
            {session.active && (
              <Text style={s.body}>
                Stop the current session to recall a saved capture and timing
                setup.
              </Text>
            )}
          </View>
        </>
      )}
      <View style={s.card}>
        <Select
          label="Lighting mode"
          value={settings.mode || ""}
          options={modes.map((mode: string) => ({
            value: mode,
            label: modeInfo[mode]?.name || pretty(mode),
            detail: modeInfo[mode]?.desc,
          }))}
          disabled={disabled}
          onChange={(mode) => edit({ mode })}
        />
        <Text style={s.body}>{modeInfo[settings.mode]?.desc}</Text>
        {(settings.band_gains || [1, 1, 1]).map((gain: number, i: number) => (
          <Fader
            key={i}
            label={
              (i < 3 ? ["Bass", "Mid", "Treble"][i] : "Band " + (i + 1)) +
              " gain"
            }
            value={gain}
            min={0}
            max={4}
            step={0.05}
            unit="×"
            disabled={disabled}
            onChange={(value) => {
              const gains = [...settings.band_gains];
              gains[i] = value;
              edit({ band_gains: gains });
            }}
          />
        ))}
        <Fader
          label="Sensitivity"
          value={settings.sensitivity ?? 1}
          min={0.1}
          max={5}
          step={0.1}
          unit="×"
          disabled={disabled}
          onChange={(sensitivity) => edit({ sensitivity })}
        />
        <Select
          label="Beat response"
          value={settings.beat_sensitivity || "normal"}
          options={["subtle", "normal", "aggressive"].map((value) => ({
            value,
            label: pretty(value),
          }))}
          disabled={disabled}
          onChange={(beat_sensitivity) => edit({ beat_sensitivity })}
        />
      </View>
      {section === "tune" && (
        <>
          <View style={s.card}>
            <Text style={s.cardTitle}>Color & movement</Text>
            <View
              style={{
                height: 28,
                borderRadius: 8,
                backgroundColor: hsvHex(settings.monochrome_hue ?? 280, 75),
              }}
            />
            <Fader
              label="Audio color hue"
              value={settings.monochrome_hue ?? 280}
              min={0}
              max={359}
              unit="°"
              disabled={disabled}
              onChange={(monochrome_hue) => edit({ monochrome_hue })}
            />
            <Fader
              label="Frequency bands"
              value={settings.n_bands ?? 3}
              min={3}
              max={16}
              disabled={disabled}
              onChange={(n_bands) => edit({ n_bands })}
            />
            {[
              ["min_dwell_ms", "Color dwell", 40, 5000, 5, " ms"],
              ["smoothing_ms", "Smoothing", 0, 2000, 10, " ms"],
              [
                "brightness_min",
                "Brightness floor",
                0,
                settings.brightness_max ?? 100,
                1,
                "%",
              ],
              [
                "brightness_max",
                "Brightness ceiling",
                settings.brightness_min ?? 0,
                100,
                1,
                "%",
              ],
            ].map(([key, label, min, max, step, unit]) => (
              <Fader
                key={key}
                label={String(label)}
                value={settings[String(key)] ?? Number(min)}
                min={Number(min)}
                max={Number(max)}
                step={Number(step)}
                unit={String(unit)}
                disabled={disabled}
                onChange={(value) => edit({ [String(key)]: value })}
              />
            ))}
          </View>
          <Section
            title="Signal processing"
            subtitle="Noise gate, automatic gain and calibration"
            initiallyOpen
          >
            {[
              ["noise_gate_enabled", "Noise gate"],
              ["agc_enabled", "Automatic gain"],
              ["dc_removal_enabled", "Remove DC offset"],
              ["use_saved_calibration", "Use saved calibration"],
            ].map(([key, label]) => (
              <View key={key} style={s.toggle}>
                <Text style={[s.label, { flex: 1 }]}>{label}</Text>
                <Switch
                  accessibilityLabel={label}
                  value={!!settings[key]}
                  disabled={
                    disabled ||
                    (key === "use_saved_calibration" && session.active)
                  }
                  onValueChange={(value) => edit({ [key]: value })}
                  trackColor={{ false: C.line, true: "#8b704c" }}
                  thumbColor={C.amber}
                />
              </View>
            ))}
            {[
              ["noise_gate_floor", "Noise gate threshold", 0, 1],
              ["agc_target_rms", "Auto-gain target", 0.001, 1],
              ["agc_attack_ms", "Gain attack (ms)", 1, 5000],
              ["agc_release_ms", "Gain release (ms)", 1, 10000],
            ].map(([key, label, min, max]) => (
              <NumberSetting
                key={key}
                label={label}
                value={settings[String(key)]}
                min={min}
                max={max}
                disabled={disabled}
                onChange={(value: number) => edit({ [String(key)]: value })}
              />
            ))}
          </Section>
          <Section
            title="Session & gentle lighting"
            subtitle="Timing, brightness changes and flash limits"
          >
            {session.active && (
              <Text style={s.body}>
                Stop the session to change capture timing and restart options.
              </Text>
            )}
            {[
              ["disable_flash_heavy", "Gentle modes only"],
              ["silence_auto_off", "Turn off during silence"],
            ].map(([key, label]) => (
              <View key={key} style={s.toggle}>
                <Text style={s.label}>{label}</Text>
                <Switch
                  accessibilityLabel={label}
                  value={!!settings[key]}
                  disabled={
                    disabled || (key === "silence_auto_off" && session.active)
                  }
                  onValueChange={(value) => edit({ [key]: value })}
                  trackColor={{ false: C.line, true: "#8b704c" }}
                  thumbColor={C.amber}
                />
              </View>
            ))}
            {[
              [
                "max_duration_s",
                "Session duration (seconds)",
                0.1,
                86400,
                true,
                true,
              ],
              ["warmup_s", "Warm-up (seconds)", 0, 120, false, true],
              [
                "auto_resume_grace_s",
                "Resume grace (seconds)",
                0,
                3600,
                false,
                true,
              ],
              [
                "max_flash_rate_hz",
                "Maximum flashes per second",
                0.1,
                3,
                true,
                false,
              ],
              [
                "max_brightness_swing",
                "Maximum brightness change (%)",
                0,
                100,
                true,
                false,
              ],
            ].map(([key, label, min, max, nullable, restart]) => (
              <NumberSetting
                key={String(key)}
                label={label}
                value={settings[String(key)]}
                min={min}
                max={max}
                nullable={nullable}
                disabled={disabled || (restart && session.active)}
                onChange={(value: number | null) =>
                  edit({ [String(key)]: value })
                }
              />
            ))}
            <Select
              label="Analysis timing"
              value={
                settings.hop_size == null && settings.window_size == null
                  ? "default"
                  : `${settings.hop_size}:${settings.window_size}`
              }
              options={[
                { value: "default", label: "Automatic (recommended)" },
                { value: "256:1024", label: "Fast · 256 / 1024 samples" },
                { value: "512:2048", label: "Balanced · 512 / 2048 samples" },
                { value: "1024:4096", label: "Detailed · 1024 / 4096 samples" },
                ...((settings.hop_size || settings.window_size) &&
                !["256:1024", "512:2048", "1024:4096"].includes(
                  `${settings.hop_size}:${settings.window_size}`,
                )
                  ? [
                      {
                        value: `${settings.hop_size}:${settings.window_size}`,
                        label: `Current · ${settings.hop_size ?? "default"} / ${settings.window_size ?? "default"} samples`,
                      },
                    ]
                  : []),
              ]}
              disabled={disabled || session.active}
              onChange={(value) => {
                const [hop_size, window_size] =
                  value === "default"
                    ? [null, null]
                    : value.split(":").map(Number);
                edit({ hop_size, window_size });
              }}
            />
          </Section>
          <Section
            title="Live response"
            subtitle="Tempo, latency and input health"
          >
            {!session.active && (
              <Text style={s.body}>
                Start a session to see live response measurements.
              </Text>
            )}
            {session.latency?.budget && (
              <Text style={s.body}>
                Software: {session.latency.budget.software_p50_ms} ms typical ·
                Bulb round-trip: {session.latency.budget.hardware_p50_ms} ms
                typical
              </Text>
            )}
            {session.latency?.frames && (
              <Text style={s.body}>
                {session.latency.frames.processed} frames ·{" "}
                {session.latency.frames.dropped} dropped ·{" "}
                {session.latency.frames.late_pct}% late
              </Text>
            )}
            {!!session.sender?.error && (
              <Text style={{ color: C.red }}>{session.sender.error}</Text>
            )}
            <Button
              label="Group audio & custom genre presets"
              onPress={() => openTools("audio/session")}
            />
          </Section>
        </>
      )}
    </>
  );
}
