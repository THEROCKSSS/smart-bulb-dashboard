import React, { useState } from "react";
import { Text, TextInput, View } from "react-native";
import { Button, C, s } from "./ui";
import { Section, Select } from "./controls";

export default function ConnectScreen({
  address,
  setAddress,
  saveConnection,
  bridge,
  settings,
  inputs,
  busy,
  ready,
  active,
  api,
  queue,
  reload,
  run,
  openTools,
}: any) {
  const [choosing, setChoosing] = useState(false);
  const devices = bridge.devices || [];
  const current = devices.find((d: any) => d.index === bridge.device_index);
  return (
    <>
      <View style={s.card}>
        <Text style={s.cardTitle}>Windows audio</Text>
        <Text style={s.body}>
          {bridge.connected
            ? "Connected · your selection is remembered on the PC."
            : "Waiting for the PC audio connection."}
        </Text>
        <Select
          label="Windows audio input"
          value={String(bridge.device_index ?? "")}
          disabled={busy || !ready || choosing || !bridge.connected || active}
          options={[...devices]
            .sort(
              (a, b) =>
                Number(/CABLE Output/i.test(b.name)) -
                Number(/CABLE Output/i.test(a.name)),
            )
            .map((device: any) => ({
              value: String(device.index),
              label:
                device.name + (device.hostapi ? ` · ${device.hostapi}` : ""),
              detail: /CABLE Output/i.test(device.name)
                ? "Recommended · listens to your Windows music mix"
                : "Capture audio from this PC input",
            }))}
          onChange={(value) =>
            run(async () => {
              setChoosing(true);
              try {
                const selected = devices.find(
                  (d: any) => d.index === Number(value),
                );
                await queue.flush();
                await api("/api/audio/bridge/device", {
                  device_index: Number(value),
                });
                // The bridge streams slot zero to the container. Its Windows index
                // is global host state, not the container input index.
                await queue.update({
                  source: "bridge",
                  device_index: 0,
                  source_device_name: selected.name,
                  source_hostapi: selected.hostapi || null,
                });
                await reload();
              } finally {
                setChoosing(false);
              }
            })
          }
        />
        {active && (
          <Text style={s.body}>
            Stop the audio session before switching capture devices.
          </Text>
        )}
        <Text style={s.body}>
          Play music into CABLE Input. This app listens to CABLE Output. Your
          Voicemeeter desktop and AUX sends are connected to that cable.
        </Text>
        {current && <Text style={s.number}>Selected: {current.name}</Text>}
        <Button
          label="Refresh connection"
          disabled={busy}
          onPress={() => run(reload)}
        />
      </View>
      <Section
        title="Dashboard connection"
        subtitle="Your saved server address"
      >
        <Text style={s.body}>
          Keep Tailscale connected when away from home. The address is already
          filled in for your PC.
        </Text>
        <TextInput
          accessibilityLabel="Dashboard address"
          value={address}
          onChangeText={setAddress}
          autoCapitalize="none"
          autoCorrect={false}
          keyboardType="url"
          style={s.input}
        />
        <Button
          label="Save connection"
          active
          disabled={busy}
          onPress={saveConnection}
        />
      </Section>
      <Section
        title="Other capture options"
        subtitle="Server inputs and fallback devices"
      >
        <Select
          label="Audio source"
          value={settings.source || "bridge"}
          disabled={busy || !ready || active}
          options={[
            { value: "bridge", label: "Windows audio bridge (recommended)" },
            {
              value: "device",
              label: "Server audio device",
              disabled: !inputs.length,
            },
          ]}
          onChange={(value) =>
            run(async () => {
              await queue.update({
                source: value,
                device_index:
                  value === "bridge" ? 0 : (inputs[0]?.index ?? null),
              });
            })
          }
        />
        {settings.source === "device" && (
          <Select
            label="Server audio input"
            value={String(settings.device_index ?? "")}
            disabled={busy || !ready || active}
            options={inputs.map((d: any) => ({
              value: String(d.index),
              label: d.name,
            }))}
            onChange={(value) =>
              run(async () => {
                await queue.update({ device_index: Number(value) });
              })
            }
          />
        )}
        <Select
          label="Fallback input"
          value={String(settings.fallback_device_index ?? "")}
          disabled={busy || !ready || active || settings.source === "bridge"}
          options={[
            { value: "", label: "No fallback" },
            ...inputs.map((d: any) => ({
              value: String(d.index),
              label: d.name,
            })),
          ]}
          onChange={(value) =>
            run(async () => {
              await queue.update({
                fallback_device_index: value === "" ? null : Number(value),
              });
            })
          }
        />
        <Text style={s.body}>
          {inputs.length
            ? "Server inputs are available on the host running the API."
            : "The container has no direct audio inputs. Use the Windows bridge."}
        </Text>
      </Section>
      <Section
        title="Bulbs & setup"
        subtitle="Add lights or troubleshoot a connection"
      >
        <Button
          label="Manage light bulbs"
          onPress={() => openTools("system/settings")}
        />
        <Button
          label="Connection diagnostics"
          onPress={() => openTools("system/diagnostics")}
        />
      </Section>
    </>
  );
}
