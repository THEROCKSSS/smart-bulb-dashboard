import React, { useEffect, useRef, useState } from "react";
import { Animated, Pressable, StyleSheet, Text, View } from "react-native";
import Slider from "@react-native-community/slider";
export const C = {
  bg: "#171917",
  card: "#232620",
  line: "#3a3e34",
  ink: "#f1efe7",
  muted: "#b3b6a9",
  amber: "#e9bd7e",
  red: "#f2978c",
};
export function Button({
  label,
  onPress,
  active = false,
  disabled = false,
}: {
  label: string;
  onPress: () => void;
  active?: boolean;
  disabled?: boolean;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ disabled, selected: active }}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        s.button,
        active && s.buttonActive,
        pressed && s.pressed,
        disabled && s.disabled,
      ]}
    >
      <Text style={[s.buttonText, active && { color: C.bg }]}>{label}</Text>
    </Pressable>
  );
}
export function Fader({
  label,
  value,
  min,
  max,
  step = 1,
  unit = "",
  onChange,
  disabled = false,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  unit?: string;
  onChange: (value: number) => void;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  const dragging = useRef(false);
  useEffect(() => {
    if (!dragging.current) setDraft(value);
  }, [value]);
  return (
    <View style={s.fader}>
      <View style={s.between}>
        <Text style={s.label}>{label}</Text>
        <Text style={s.number}>
          {Number(draft.toFixed(3))}
          {unit}
        </Text>
      </View>
      <Slider
        disabled={disabled}
        accessibilityLabel={label}
        value={draft}
        minimumValue={min}
        maximumValue={max}
        step={step}
        onSlidingStart={() => {
          dragging.current = true;
        }}
        onValueChange={setDraft}
        onSlidingComplete={(next) => {
          dragging.current = false;
          onChange(next);
        }}
        minimumTrackTintColor={C.amber}
        maximumTrackTintColor={C.line}
        thumbTintColor={C.amber}
        style={{ height: 42 }}
      />
    </View>
  );
}
export function Meter({
  value,
  label,
  reduced,
}: {
  value: number;
  label: string;
  reduced: boolean;
}) {
  const level = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    Animated.timing(level, {
      toValue: Math.max(0, Math.min(1, value || 0)),
      duration: reduced ? 0 : 200,
      useNativeDriver: false,
    }).start();
  }, [value, reduced]);
  return (
    <View style={s.meterColumn}>
      <View style={s.meterTrack}>
        <Animated.View
          style={[
            s.meterFill,
            {
              height: level.interpolate({
                inputRange: [0, 1],
                outputRange: ["0%", "100%"],
              }),
            },
          ]}
        />
      </View>
      <Text style={s.eyebrow}>{label}</Text>
      <Text style={s.number}>{Math.round((value || 0) * 100)}%</Text>
    </View>
  );
}
export const s = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },
  top: {
    paddingHorizontal: 24,
    paddingVertical: 18,
    borderBottomWidth: 1,
    borderColor: C.line,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  brand: { color: C.ink, fontSize: 19, fontWeight: "600" },
  content: {
    padding: 24,
    paddingBottom: 40,
    maxWidth: 760,
    width: "100%",
    alignSelf: "center",
  },
  heading: {
    color: C.ink,
    fontSize: 38,
    letterSpacing: -1.5,
    fontWeight: "600",
    marginTop: 12,
    marginBottom: 24,
  },
  eyebrow: {
    color: C.muted,
    fontSize: 9,
    letterSpacing: 1.4,
    fontWeight: "600",
  },
  card: {
    padding: 22,
    backgroundColor: C.card,
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 20,
    marginTop: 18,
    gap: 14,
  },
  cardTitle: {
    color: C.ink,
    fontSize: 21,
    fontWeight: "500",
    letterSpacing: -0.3,
  },
  body: { color: C.muted, lineHeight: 23, fontSize: 13 },
  label: { color: C.ink, fontSize: 13 },
  number: { color: C.amber, fontSize: 12, fontVariant: ["tabular-nums"] },
  button: {
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 10,
    paddingVertical: 14,
    paddingHorizontal: 16,
    minHeight: 48,
    justifyContent: "center",
  },
  buttonActive: { backgroundColor: C.amber, borderColor: C.amber },
  buttonText: {
    color: C.ink,
    fontSize: 13,
    textAlign: "center",
    fontWeight: "600",
  },
  pressed: { opacity: 0.65, transform: [{ scale: 0.98 }] },
  disabled: { opacity: 0.4 },
  between: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
  },
  wrap: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  fader: { marginTop: 8, gap: 8 },
  toggle: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    minHeight: 48,
  },
  input: {
    borderWidth: 1,
    borderColor: C.line,
    borderRadius: 10,
    padding: 16,
    color: C.ink,
    fontSize: 14,
    backgroundColor: C.bg,
  },
  error: {
    backgroundColor: "#392723",
    borderWidth: 1,
    borderColor: "#70483c",
    borderRadius: 12,
    padding: 18,
    gap: 14,
    marginTop: 16,
  },
  room: { alignItems: "center", paddingVertical: 34 },
  orbit: {
    width: 156,
    height: 156,
    borderRadius: 78,
    borderWidth: 1,
    borderColor: C.line,
    justifyContent: "center",
    alignItems: "center",
    marginBottom: 8,
  },
  orbitOn: { backgroundColor: "#3d3527", borderColor: "#9b7c50" },
  bulb: { fontSize: 84, color: "#59614e" },
  meters: {
    flexDirection: "row",
    gap: 24,
    justifyContent: "space-around",
    marginVertical: 16,
  },
  meterColumn: { alignItems: "center", gap: 10, flex: 1 },
  meterTrack: {
    width: "100%",
    height: 100,
    backgroundColor: C.bg,
    justifyContent: "flex-end",
    overflow: "hidden",
    borderRadius: 5,
  },
  meterFill: { backgroundColor: "#b9935e", width: "100%" },
  tabs: {
    flexDirection: "row",
    justifyContent: "space-around",
    backgroundColor: "#1c1f1b",
    borderTopWidth: 1,
    borderColor: C.line,
    paddingVertical: 9,
  },
  tab: {
    minWidth: 68,
    minHeight: 52,
    alignItems: "center",
    justifyContent: "center",
    gap: 4,
  },
  tabIcon: { color: C.muted, fontSize: 24 },
  tabText: { color: C.muted, fontSize: 11 },
});
