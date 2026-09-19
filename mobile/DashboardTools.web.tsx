import React from "react";
import { Text, View } from "react-native";
import { Button, s } from "./ui";

// The native app embeds these views. Browser builds navigate in the same tab:
// the dashboard deliberately forbids cross-origin framing.
export default function DashboardTools({
  url,
  onBack,
}: {
  url: string;
  onBack: () => void;
}) {
  return (
    <View style={s.card}>
      <Text style={s.cardTitle}>Dashboard tools</Text>
      <Text style={s.body}>
        Expo Go opens these tools inside the app. In a browser, continue to the
        full dashboard in this tab.
      </Text>
      <Button
        label="Continue to dashboard tools"
        active
        onPress={() => {
          window.location.href = url;
        }}
      />
      <Button label="Back to studio" onPress={onBack} />
    </View>
  );
}
