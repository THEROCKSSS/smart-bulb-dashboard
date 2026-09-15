import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AccessibilityInfo,
  Animated,
  AppState,
  Pressable,
  ScrollView,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaProvider, SafeAreaView } from "react-native-safe-area-context";
import { StatusBar } from "expo-status-bar";
import {
  client,
  DEFAULT_SERVER,
  Device,
  loadServer,
  Mix,
  saveServer,
  serverURL,
  Settings,
} from "./api";
import { Button, C, s } from "./ui";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { hsvHex, Select } from "./controls";
import RoomControls from "./RoomControls";
import AudioScreen from "./AudioScreen";
import ConnectScreen from "./ConnectScreen";
import DashboardTools from "./DashboardTools";
// Shared with the web: ordered writes, latest-value coalescing, save/retry feedback.
const { createQueue } = require("../frontend/mixer.js");

function Studio() {
  const [server, setServer] = useState(DEFAULT_SERVER),
    [address, setAddress] = useState(DEFAULT_SERVER);
  const [tab, setTab] = useState("Room"),
    [devices, setDevices] = useState<Device[]>([]),
    [deviceId, setDeviceId] = useState("");
  const [light, setLight] = useState<any>({}),
    [session, setSession] = useState<any>({}),
    [bridge, setBridge] = useState<any>({});
  const [settings, setSettings] = useState<Settings>({}),
    [mixes, setMixes] = useState<Mix[]>([]),
    [modes, setModes] = useState<string[]>([]);
  const [inputs, setInputs] = useState<any[]>([]),
    [toolRoute, setToolRoute] = useState("");
  const [scenes, setScenes] = useState<any[]>([]),
    [loadedKey, setLoadedKey] = useState("");
  const [error, setError] = useState(""),
    [phase, setPhase] = useState("loading"),
    [busy, setBusy] = useState(false);
  const [needsPin, setNeedsPin] = useState(false),
    [pin, setPin] = useState("");
  const [ready, setReady] = useState(false),
    [reduced, setReduced] = useState(false),
    [foreground, setForeground] = useState(true);
  const fade = useRef(new Animated.Value(1)).current;
  const api = useMemo(() => client(server, () => setNeedsPin(true)), [server]);
  const queue = useMemo(
    () =>
      deviceId
        ? createQueue((patch: Settings) =>
            api(
              `/api/devices/${encodeURIComponent(deviceId)}/audio-reactive/settings`,
              patch,
            ),
          )
        : null,
    [api, deviceId],
  );
  const path = `/api/devices/${encodeURIComponent(deviceId)}`;
  const key = server + "/" + deviceId,
    currentKey = useRef(key),
    busyRef = useRef(false);
  currentKey.current = key;
  const deviceReady = loadedKey === key;
  const commandEpoch = useRef(0),
    reloadSequence = useRef(0),
    currentServer = useRef(server),
    connectSequence = useRef(0);
  currentServer.current = server;
  const report = (err: any) =>
    setError(err?.message || "Could not reach the dashboard.");
  const edit = (patch: Settings) => {
    setError("");
    return queue?.update(patch).catch(report);
  };
  const run = async (work: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    commandEpoch.current += 1;
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (err) {
      report(err);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };
  useEffect(() => {
    loadServer()
      .then((value) => {
        setServer(value);
        setAddress(value);
        setReady(true);
      })
      .catch((err) => {
        report(err);
        setReady(true);
      });
    AccessibilityInfo.isReduceMotionEnabled().then(setReduced);
    const a = AccessibilityInfo.addEventListener(
      "reduceMotionChanged",
      setReduced,
    );
    const b = AppState.addEventListener("change", (value) =>
      setForeground(value === "active"),
    );
    return () => {
      a.remove();
      b.remove();
    };
  }, []);
  useEffect(() => {
    if (reduced) return;
    fade.setValue(0.6);
    Animated.timing(fade, {
      toValue: 1,
      duration: 180,
      useNativeDriver: true,
    }).start();
  }, [tab, reduced]);
  const connect = useCallback(async () => {
    const sequence = ++connectSequence.current;
    const [list, info, looks, remembered] = await Promise.all([
      api("/api/devices"),
      api("/api/audio/devices"),
      api("/api/scenes"),
      AsyncStorage.getItem("studio-bulb:" + server),
    ]);
    if (
      currentServer.current !== server ||
      sequence !== connectSequence.current
    )
      return;
    setScenes(looks);
    setDevices(list);
    setModes(info.modes || []);
    setInputs(info.devices || []);
    setDeviceId((previous) =>
      list.some((d: Device) => d.id === previous)
        ? previous
        : list.find((d: Device) => d.id === remembered)?.id ||
          list[0]?.id ||
          "",
    );
    setNeedsPin(false);
    if (!list.length)
      setError("No bulbs configured. Add your bulb in the web dashboard.");
  }, [api]);
  useEffect(() => {
    if (ready) connect().catch(report);
  }, [ready, connect]);
  useEffect(() => {
    if (!queue) return;
    return queue.subscribe((state: { phase: string; settings: Settings }) => {
      setPhase(state.phase);
      setSettings(state.settings);
    });
  }, [queue]);
  const reload = useCallback(async () => {
    if (!deviceId) return;
    const revision = queue.revision(),
      sequence = ++reloadSequence.current,
      epoch = commandEpoch.current;
    const [saved, presets, status, audio, input] = await Promise.all([
      api(path + "/audio-reactive/settings"),
      api(
        "/api/audio/session-presets?device_id=" + encodeURIComponent(deviceId),
      ),
      api(path + "/status"),
      api(path + "/audio-reactive/status"),
      api("/api/audio/bridge"),
    ]);
    if (
      currentKey.current !== key ||
      sequence !== reloadSequence.current ||
      epoch !== commandEpoch.current
    )
      return;
    queue.hydrate(saved.settings, revision);
    setMixes(presets);
    setLight(status);
    setSession(audio);
    setBridge(input);
    setLoadedKey(key);
  }, [api, deviceId, queue]);
  useEffect(() => {
    setSettings({});
    setLight({});
    setSession({});
    setBridge({});
    if (deviceId) reload().catch(report);
  }, [reload]);
  useEffect(() => {
    if (!deviceId || !foreground || needsPin) return;
    let stopped = false,
      timer: ReturnType<typeof setTimeout>,
      lastFull = 0;
    const poll = async () => {
      try {
        if (busyRef.current) return;
        const epoch = commandEpoch.current;
        const audio = await api(path + "/audio-reactive/status");
        if (!stopped && epoch === commandEpoch.current) setSession(audio);
        if (!stopped && !busyRef.current && Date.now() - lastFull > 3000) {
          await reload();
          lastFull = Date.now();
        }
      } catch (err) {
        if (!stopped) report(err);
      } finally {
        if (!stopped) timer = setTimeout(poll, tab === "Audio" ? 350 : 2500);
      }
    };
    poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [api, deviceId, foreground, needsPin, tab, reload]);
  const start = () =>
    run(async () => {
      await queue.flush();
      const saved = queue.snapshot();
      await api(path + "/audio-reactive/start", {
        ...saved,
        device_index: saved.device_index ?? 0,
      });
      await reload();
    });
  const stop = () =>
    run(async () => {
      await api(path + "/audio-reactive/stop", {});
      await reload();
    });
  const selected = devices.find((d) => d.id === deviceId);
  const openTools = (route: string) => {
    setToolRoute(route);
    setTab("Tools");
  };
  const changeBulb = (value: string) =>
    run(async () => {
      await queue?.flush();
      await AsyncStorage.setItem("studio-bulb:" + server, value);
      setLoadedKey("");
      setDeviceId(value);
    });
  const saveConnection = () =>
    run(async () => {
      const value = serverURL(address);
      await queue?.flush();
      await saveServer(value);
      setLoadedKey("");
      setDeviceId("");
      setDevices([]);
      setServer(value);
      if (value === server) await connect();
    });
  return (
    <SafeAreaView style={s.root} edges={["top", "bottom"]}>
      <StatusBar style="light" />
      <View style={s.top}>
        <Text style={s.brand}>
          ◉ Lumen<Text style={{ color: C.amber }}> / studio</Text>
        </Text>
        <Text style={s.eyebrow}>LIVE DATA</Text>
      </View>
      {tab === "Tools" && toolRoute ? (
        <DashboardTools
          url={
            server +
            "/?device=" +
            encodeURIComponent(deviceId) +
            "#/" +
            toolRoute
          }
          onBack={() => {
            setToolRoute("");
            connect().catch(report);
          }}
        />
      ) : (
        <ScrollView
          contentContainerStyle={s.content}
          keyboardShouldPersistTaps="handled"
        >
          <Animated.View style={{ opacity: fade }}>
            <Text style={s.eyebrow}>SMART LIGHT BULB DASHBOARD</Text>
            <Text style={s.heading}>
              {tab === "Room"
                ? "A little atmosphere."
                : tab === "Audio"
                  ? "Make the room move."
                  : tab === "Tools"
                    ? "Your whole studio."
                    : "Connected, simply."}
            </Text>
            {!!devices.length && (
              <Select
                label="Light bulb"
                value={deviceId}
                disabled={busy}
                options={devices.map((d) => ({ value: d.id, label: d.name }))}
                onChange={changeBulb}
              />
            )}
            {!!error && (
              <View style={s.error} accessibilityRole="alert">
                <Text style={{ color: C.red }}>{error}</Text>
                <Button
                  label="Retry connection"
                  onPress={() =>
                    run(async () => {
                      await connect();
                      await reload();
                    })
                  }
                />
              </View>
            )}
            {needsPin && (
              <View style={s.card}>
                <Text style={s.cardTitle}>Unlock dashboard</Text>
                <TextInput
                  accessibilityLabel="Dashboard PIN"
                  placeholder="PIN"
                  placeholderTextColor={C.muted}
                  secureTextEntry
                  value={pin}
                  onChangeText={setPin}
                  style={s.input}
                />
                <Button
                  label="Unlock"
                  active
                  disabled={busy}
                  onPress={() =>
                    run(async () => {
                      await api("/api/auth/login", { pin });
                      setPin("");
                      await connect();
                      await reload();
                    })
                  }
                />
              </View>
            )}
            {!deviceId && !needsPin && (
              <View style={s.card}>
                <Text style={s.body}>
                  {ready
                    ? "Connect to your dashboard to find your lights."
                    : "Opening your studio…"}
                </Text>
                <Button
                  label="Connection settings"
                  onPress={() => setTab("Connect")}
                />
                <Button
                  label="Add a light bulb"
                  onPress={() => openTools("system/settings")}
                />
              </View>
            )}
            {tab === "Room" && !!deviceId && (
              <>
                <View style={[s.card, s.room]}>
                  <View
                    style={[
                      s.orbit,
                      light.power && {
                        backgroundColor:
                          light.mode === "colour"
                            ? hsvHex(
                                light.hue ?? 35,
                                light.saturation_pct ?? 75,
                                25,
                              )
                            : "#3d3527",
                        borderColor:
                          light.mode === "colour"
                            ? hsvHex(
                                light.hue ?? 35,
                                light.saturation_pct ?? 75,
                              )
                            : C.amber,
                      },
                    ]}
                  >
                    <Text
                      style={[
                        s.bulb,
                        light.power && {
                          color:
                            light.mode === "colour"
                              ? hsvHex(
                                  light.hue ?? 35,
                                  light.saturation_pct ?? 75,
                                )
                              : C.amber,
                        },
                      ]}
                    >
                      ◉
                    </Text>
                  </View>
                  <Text style={s.cardTitle}>{selected?.name}</Text>
                  <Text style={s.body}>
                    {!deviceReady
                      ? "Connecting to this bulb…"
                      : light.online
                        ? light.power
                          ? "The room is glowing"
                          : "A quiet moment"
                        : "Light is offline"}
                  </Text>
                  <Button
                    label={
                      busy
                        ? "Sending…"
                        : light.power
                          ? "Turn light off"
                          : "Turn light on"
                    }
                    active={!!light.power}
                    disabled={busy || !deviceReady || !light.online}
                    onPress={() =>
                      run(async () => {
                        await api(path + "/power", { on: !light.power });
                        await reload();
                      })
                    }
                  />
                </View>
                <RoomControls
                  key={key}
                  api={api}
                  path={path}
                  light={light}
                  ready={deviceReady}
                  busy={busy}
                  run={run}
                  refresh={reload}
                  scenes={scenes}
                  openTools={openTools}
                />
                <View style={s.card}>
                  <Text style={s.cardTitle}>Let your music set the mood.</Text>
                  <Text style={s.body}>
                    Ready-made mixes, your saved favorites and every mixer
                    adjustment together.
                  </Text>
                  <Button
                    label="Open audio studio"
                    active
                    onPress={() => setTab("Audio")}
                  />
                </View>
              </>
            )}
            {tab === "Audio" && !!deviceId && (
              <AudioScreen
                key={key}
                api={api}
                path={path}
                settings={settings}
                session={session}
                bridge={bridge}
                modes={modes}
                mixes={mixes}
                phase={phase}
                queue={queue}
                edit={edit}
                run={run}
                reload={reload}
                start={start}
                stop={stop}
                busy={busy}
                ready={deviceReady}
                reduced={reduced}
                openTools={openTools}
                openConnect={() => setTab("Connect")}
              />
            )}
            {tab === "Connect" && (
              <ConnectScreen
                address={address}
                setAddress={setAddress}
                saveConnection={saveConnection}
                bridge={bridge}
                settings={settings}
                inputs={inputs}
                busy={busy}
                ready={deviceReady}
                active={session.active}
                api={api}
                queue={queue}
                reload={reload}
                run={run}
                openTools={openTools}
              />
            )}
            {tab === "Tools" && (
              <View style={s.card}>
                <Text style={s.body}>
                  The complete dashboard tools, inside this app. They use the
                  same bulbs, settings and saved data.
                </Text>
                {[
                  ["automation", "Timers & schedules"],
                  ["rooms", "Rooms, groups & zones"],
                  ["light/looks", "Scenes & effects"],
                  ["light/presets", "All colors & favorites"],
                  ["audio/session", "Group audio & custom genre presets"],
                  ["audio/presets", "All saved sessions"],
                  ["system/history", "Activity history"],
                  ["system/health", "Server health"],
                  ["system/diagnostics", "Bulb diagnostics"],
                  ["system/security", "Security & access"],
                  ["system/backup", "Backup & restore"],
                  ["system/settings", "Bulbs & settings"],
                  ["system/docs", "Help & documentation"],
                ].map(([route, label]) => (
                  <Button
                    key={route}
                    label={label}
                    onPress={() => openTools(route)}
                  />
                ))}
              </View>
            )}
          </Animated.View>
        </ScrollView>
      )}
      <View style={s.tabs}>
        {[
          ["Room", "◉"],
          ["Audio", "≋"],
          ["Tools", "▤"],
          ["Connect", "⌁"],
        ].map(([name, icon]) => (
          <Pressable
            key={name}
            accessibilityRole="tab"
            accessibilityState={{ selected: tab === name }}
            accessibilityLabel={name}
            onPress={() => {
              if (toolRoute) connect().catch(report);
              setTab(name);
              setToolRoute("");
            }}
            style={({ pressed }) => [s.tab, pressed && s.pressed]}
          >
            <Text style={[s.tabIcon, tab === name && { color: C.amber }]}>
              {icon}
            </Text>
            <Text style={[s.tabText, tab === name && { color: C.amber }]}>
              {name}
            </Text>
          </Pressable>
        ))}
      </View>
    </SafeAreaView>
  );
}
export default function App() {
  return (
    <SafeAreaProvider>
      <Studio />
    </SafeAreaProvider>
  );
}
