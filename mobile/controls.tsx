import React, { useEffect, useRef, useState } from "react";
import {
  Modal,
  Pressable,
  ScrollView,
  Text,
  TextInput,
  View,
} from "react-native";
import Svg, {
  Circle,
  Defs,
  Path,
  RadialGradient,
  Stop,
} from "react-native-svg";
import { Button, C, Fader, s } from "./ui";

export type Option = {
  value: string;
  label: string;
  detail?: string;
  disabled?: boolean;
};
export function Select({
  label,
  value,
  options,
  onChange,
  disabled = false,
}: {
  label: string;
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false),
    [search, setSearch] = useState("");
  const current = options.find((item) => item.value === value);
  return (
    <View style={{ gap: 8 }}>
      <Text style={s.label}>{label}</Text>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={label}
        accessibilityState={{ expanded: open, disabled }}
        disabled={disabled}
        onPress={() => {
          setSearch("");
          setOpen(true);
        }}
        style={[s.input, s.between, disabled && s.disabled]}
      >
        <Text style={[s.label, { flex: 1 }]}>
          {current?.label || "Choose…"}
        </Text>
        <Text style={s.number}>⌄</Text>
      </Pressable>
      <Modal
        visible={open}
        transparent
        animationType="slide"
        onRequestClose={() => setOpen(false)}
      >
        <View
          style={{
            flex: 1,
            backgroundColor: "#101410cc",
            justifyContent: "flex-end",
          }}
        >
          <Pressable
            accessibilityLabel="Close choices"
            onPress={() => setOpen(false)}
            style={{ flex: 1 }}
          />
          <View
            style={{
              backgroundColor: C.card,
              borderTopLeftRadius: 24,
              borderTopRightRadius: 24,
              padding: 24,
              paddingBottom: 36,
              maxHeight: "80%",
              gap: 16,
            }}
          >
            <View style={s.between}>
              <Text style={s.cardTitle}>{label}</Text>
              <Button label="Done" onPress={() => setOpen(false)} />
            </View>
            {options.length > 6 && (
              <TextInput
                accessibilityLabel={"Search " + label}
                placeholder="Search by name…"
                placeholderTextColor={C.muted}
                value={search}
                onChangeText={setSearch}
                style={s.input}
                autoCorrect={false}
              />
            )}
            <ScrollView keyboardShouldPersistTaps="handled">
              {options
                .filter((item) =>
                  (item.label + " " + (item.detail || ""))
                    .toLowerCase()
                    .includes(search.toLowerCase()),
                )
                .map((item) => (
                  <Pressable
                    key={item.value}
                    accessibilityRole="button"
                    accessibilityLabel={item.label}
                    accessibilityState={{
                      selected: item.value === value,
                      disabled: item.disabled,
                    }}
                    disabled={item.disabled}
                    onPress={() => {
                      setOpen(false);
                      onChange(item.value);
                    }}
                    style={[
                      {
                        paddingVertical: 16,
                        borderBottomWidth: 1,
                        borderColor: C.line,
                        gap: 5,
                      },
                      item.disabled && s.disabled,
                    ]}
                  >
                    <View style={s.between}>
                      <Text style={[s.label, { flex: 1 }]}>{item.label}</Text>
                      {item.value === value && <Text style={s.number}>✓</Text>}
                    </View>
                    {!!item.detail && <Text style={s.body}>{item.detail}</Text>}
                  </Pressable>
                ))}
              {!options.filter((item) =>
                (item.label + " " + (item.detail || ""))
                  .toLowerCase()
                  .includes(search.toLowerCase()),
              ).length && <Text style={s.body}>No matching choices.</Text>}
            </ScrollView>
          </View>
        </View>
      </Modal>
    </View>
  );
}

export function Section({
  title,
  subtitle,
  children,
  initiallyOpen = false,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  initiallyOpen?: boolean;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <View style={s.card}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={title}
        accessibilityState={{ expanded: open }}
        onPress={() => setOpen(!open)}
        style={[s.between, { minHeight: 44 }]}
      >
        <View style={{ flex: 1, gap: 5 }}>
          <Text style={s.cardTitle}>{title}</Text>
          {subtitle && <Text style={s.body}>{subtitle}</Text>}
        </View>
        <Text style={s.number}>{open ? "−" : "+"}</Text>
      </Pressable>
      {open && children}
    </View>
  );
}

export function hsvRGB(h: number, s: number, v = 100): number[] {
  const sat = s / 100,
    level = v / 100;
  return [5, 3, 1].map((n) => {
    const k = (n + h / 60) % 6;
    return Math.round(
      255 * (level - level * sat * Math.max(0, Math.min(k, 4 - k, 1))),
    );
  });
}
export const rgbHex = (rgb: number[]) =>
  "#" + rgb.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
export const hsvHex = (h: number, sat: number, v = 100) =>
  rgbHex(hsvRGB(h, sat, v));

const WEDGES = Array.from({ length: 120 }, (_, i) => {
  const a = (i * Math.PI) / 60,
    b = ((i + 1.15) * Math.PI) / 60;
  return (
    <Path
      key={i}
      d={`M 140 140 L ${140 + 136 * Math.cos(a)} ${140 + 136 * Math.sin(a)} A 136 136 0 0 1 ${140 + 136 * Math.cos(b)} ${140 + 136 * Math.sin(b)} Z`}
      fill={hsvHex(i * 3, 100)}
    />
  );
});
export function ColorWheel({
  hue,
  saturation,
  onChange,
  disabled = false,
}: {
  hue: number;
  saturation: number;
  onChange: (h: number, sat: number) => void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState({ h: hue, s: saturation });
  const latest = useRef(draft),
    dragging = useRef(false),
    width = useRef(280);
  useEffect(() => {
    if (!dragging.current) {
      latest.current = { h: hue, s: saturation };
      setDraft(latest.current);
    }
  }, [hue, saturation]);
  const point = (event: any) => {
    const x = (event.nativeEvent.locationX * 280) / width.current - 140;
    const y = (event.nativeEvent.locationY * 280) / width.current - 140;
    const next = {
      h: ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360,
      s: Math.min(100, (Math.hypot(x, y) / 136) * 100),
    };
    latest.current = next;
    setDraft(next);
  };
  return (
    <View style={{ gap: 12 }}>
      <View
        accessibilityLabel="Color wheel"
        testID="color-wheel"
        onLayout={(e) => {
          width.current = e.nativeEvent.layout.width;
        }}
        onStartShouldSetResponder={() => !disabled}
        onMoveShouldSetResponder={() => !disabled}
        onResponderGrant={(e) => {
          dragging.current = true;
          point(e);
        }}
        onResponderMove={point}
        onResponderTerminationRequest={() => false}
        onResponderRelease={() => {
          dragging.current = false;
          onChange(latest.current.h, latest.current.s);
        }}
        onResponderTerminate={() => {
          dragging.current = false;
          setDraft({ h: hue, s: saturation });
        }}
        style={{
          width: "100%",
          maxWidth: 300,
          aspectRatio: 1,
          alignSelf: "center",
          opacity: disabled ? 0.5 : 1,
        }}
      >
        <View pointerEvents="none" style={{ flex: 1 }}>
          <Svg width="100%" height="100%" viewBox="0 0 280 280">
            <Defs>
              <RadialGradient id="saturation">
                <Stop offset="0" stopColor="#fff" stopOpacity="1" />
                <Stop offset="1" stopColor="#fff" stopOpacity="0" />
              </RadialGradient>
            </Defs>
            {WEDGES}
            <Circle cx="140" cy="140" r="136" fill="url(#saturation)" />
            <Circle
              cx={
                140 +
                ((136 * draft.s) / 100) * Math.cos((draft.h * Math.PI) / 180)
              }
              cy={
                140 +
                ((136 * draft.s) / 100) * Math.sin((draft.h * Math.PI) / 180)
              }
              r="9"
              fill={hsvHex(draft.h, draft.s)}
              stroke="#f1efe7"
              strokeWidth="3"
            />
          </Svg>
        </View>
      </View>
      <View style={s.between}>
        <View
          style={{
            width: 30,
            height: 30,
            borderRadius: 15,
            backgroundColor: hsvHex(draft.h, draft.s),
            borderWidth: 1,
            borderColor: C.line,
          }}
        />
        <Text style={s.number}>
          {hsvHex(draft.h, draft.s).toUpperCase()} · {Math.round(draft.h)}°
        </Text>
        <Text style={s.body}>Drag to pick</Text>
      </View>
      <Fader
        label="Hue"
        value={draft.h}
        min={0}
        max={359}
        unit="°"
        disabled={disabled}
        onChange={(h) => onChange(h, draft.s)}
      />
      <Fader
        label="Saturation"
        value={draft.s}
        min={0}
        max={100}
        unit="%"
        disabled={disabled}
        onChange={(sat) => onChange(draft.h, sat)}
      />
    </View>
  );
}

export function Swatch({
  label,
  color,
  onPress,
  disabled = false,
  selected = false,
}: {
  label: string;
  color: string;
  onPress: () => void;
  disabled?: boolean;
  selected?: boolean;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ selected, disabled }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        {
          width: "47%",
          borderRadius: 12,
          borderWidth: 1,
          borderColor: selected ? C.amber : C.line,
          padding: 10,
          gap: 8,
        },
        pressed && s.pressed,
        disabled && s.disabled,
      ]}
    >
      <View
        testID={"swatch-" + label}
        style={{ height: 42, borderRadius: 7, backgroundColor: color }}
      />
      <Text style={s.label}>
        {label}
        {selected ? " ✓" : ""}
      </Text>
      <Text style={s.eyebrow}>{color.toUpperCase()}</Text>
    </Pressable>
  );
}
