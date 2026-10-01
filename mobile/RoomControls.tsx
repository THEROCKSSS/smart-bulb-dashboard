import React, { useEffect, useState } from "react";
import { Text, TextInput, View } from "react-native";
import { Button, C, Fader, s } from "./ui";
import {
  ColorWheel,
  hsvHex,
  hsvRGB,
  rgbHex,
  Section,
  Swatch,
} from "./controls";

export default function RoomControls({
  api,
  path,
  light,
  ready,
  busy,
  run,
  refresh,
  scenes,
  openTools,
}: any) {
  const [presets, setPresets] = useState<any[]>([]),
    [favorites, setFavorites] = useState<any[]>([]),
    [name, setName] = useState("");
  const [loadError, setLoadError] = useState("");
  const brightness =
    (light.mode === "colour" ? light.value_pct : light.brightness_pct) ?? 50;
  const disabled = busy || !ready || !light.online;
  const hue = light.hue ?? 35,
    sat = light.saturation_pct ?? 75;
  useEffect(() => {
    let current = true;
    setFavorites([]);
    setName("");
    Promise.all([api("/api/presets"), api(path + "/favorites")])
      .then(([colors, saved]) => {
        if (current) {
          setPresets(colors);
          setFavorites(saved);
          setLoadError("");
        }
      })
      .catch((err: Error) => {
        if (current) setLoadError(err.message);
      });
    return () => {
      current = false;
    };
  }, [api, path]);
  const command = (route: string, data: any) =>
    run(async () => {
      await api(path + route, data);
      await refresh();
    });
  return (
    <>
      <View style={s.card}>
        <Text style={s.cardTitle}>Your color, your room</Text>
        <ColorWheel
          hue={hue}
          saturation={sat}
          disabled={disabled}
          onChange={(h, s) => command("/color/hsv", { h, s, v: brightness })}
        />
        <Fader
          label="Brightness"
          value={brightness}
          min={1}
          max={100}
          unit="%"
          disabled={disabled}
          onChange={(value) => command("/brightness", { value })}
        />
        <View style={s.wrap}>
          {(
            [
              ["Amber", 35],
              ["Rose", 335],
              ["Ocean", 205],
              ["Lavender", 275],
            ] as [string, number][]
          ).map(([label, h]) => (
            <Swatch
              key={label}
              label={label}
              color={hsvHex(h, 75)}
              disabled={disabled}
              selected={
                light.mode === "colour" &&
                Math.abs(hue - h) < 2 &&
                Math.abs(sat - 75) < 2
              }
              onPress={() => command("/color/hsv", { h, s: 75, v: brightness })}
            />
          ))}
        </View>
        <Button
          label="Surprise me"
          disabled={disabled}
          onPress={() => command("/color/random", {})}
        />
      </View>
      <Section
        title="White light"
        subtitle="From a warm evening glow to cool daylight"
      >
        <Fader
          label="White temperature"
          value={light.color_temp_pct ?? 25}
          min={0}
          max={100}
          unit="% cool"
          disabled={disabled}
          onChange={(value) =>
            command("/white", { brightness, color_temp: value })
          }
        />
        <View style={s.wrap}>
          {[
            ["Warm white", 0, "#ffd49a"],
            ["Soft white", 30, "#ffe6c4"],
            ["Daylight", 65, "#eff4ff"],
            ["Cool white", 100, "#d2e3ff"],
          ].map(([label, temp, color]) => (
            <Swatch
              key={label}
              label={String(label)}
              color={String(color)}
              disabled={disabled}
              onPress={() =>
                command("/white", { brightness, color_temp: temp })
              }
            />
          ))}
        </View>
      </Section>
      <Section
        title="Colors & favorites"
        subtitle="The full color collection, plus your saved colors"
      >
        {!!loadError && <Text style={{ color: C.red }}>{loadError}</Text>}
        <Text style={s.eyebrow}>NON-LIVE DATA · COLOR LIBRARY</Text>
        <View style={s.wrap}>
          {presets.map((preset) => (
            <Swatch
              key={preset.id}
              label={preset.name}
              color={rgbHex(preset.rgb)}
              disabled={disabled}
              onPress={() =>
                command("/presets/apply", { preset_id: preset.id })
              }
            />
          ))}
        </View>
        <Text style={s.cardTitle}>Your favorites</Text>
        <TextInput
          accessibilityLabel="Favorite color name"
          value={name}
          onChangeText={setName}
          maxLength={100}
          placeholder="Name this color"
          placeholderTextColor={C.muted}
          style={s.input}
        />
        <Button
          label="Save current color"
          disabled={disabled || !name.trim()}
          onPress={() =>
            run(async () => {
              const [r, g, b] = hsvRGB(hue, sat, brightness);
              await api(path + "/favorites", { name: name.trim(), r, g, b });
              setFavorites(await api(path + "/favorites"));
              setName("");
            })
          }
        />
        {!favorites.length && (
          <Text style={s.body}>
            No favorite colors yet. Pick a color above and save it here.
          </Text>
        )}
        {favorites.map((favorite) => (
          <View key={favorite.id} style={{ gap: 8 }}>
            <Swatch
              label={favorite.name}
              color={rgbHex(favorite.rgb)}
              disabled={disabled}
              onPress={() => {
                const [r, g, b] = favorite.rgb;
                command("/color", { r, g, b });
              }}
            />
            <Button
              label={"Delete color " + favorite.name}
              disabled={busy}
              onPress={() =>
                run(async () => {
                  await api(
                    path + "/favorites/" + favorite.id,
                    undefined,
                    "DELETE",
                  );
                  setFavorites(await api(path + "/favorites"));
                })
              }
            />
          </View>
        ))}
      </Section>
      <Section
        title="Scenes & effects"
        subtitle="Ready-made moods and moving light"
      >
        <Text style={s.eyebrow}>NON-LIVE DATA · SAVED LOOKS</Text>
        <View style={s.wrap}>
          {scenes.map((scene: any) => (
            <Button
              key={scene.id}
              label={scene.name}
              disabled={disabled}
              onPress={() => command("/scenes/apply", { scene_id: scene.id })}
            />
          ))}
        </View>
        {!scenes.length && (
          <Text style={s.body}>No saved scenes are available.</Text>
        )}
        <Button
          label="Open effects controls"
          onPress={() => openTools("light/looks")}
        />
      </Section>
    </>
  );
}
