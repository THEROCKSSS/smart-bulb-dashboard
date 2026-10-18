import React, { useState } from "react";
import { ActivityIndicator, Text, View } from "react-native";
import { WebView } from "react-native-webview";
import { Button, C, s } from "./ui";

export default function DashboardTools({
  url,
  onBack,
}: {
  url: string;
  onBack: () => void;
}) {
  const [failed, setFailed] = useState(false),
    [attempt, setAttempt] = useState(0);
  const origin = new URL(url).origin;
  const allowed = (target: string) => {
    if (target === "about:blank") return true;
    try {
      return new URL(target).origin === origin;
    } catch {
      return false;
    }
  };
  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <View style={{ padding: 12 }}>
        <Button label="Back to studio" onPress={onBack} />
      </View>
      {failed ? (
        <View style={s.card}>
          <Text style={s.cardTitle}>Dashboard unavailable</Text>
          <Text style={s.body}>Check your connection, then try again.</Text>
          <Button
            label="Retry dashboard"
            onPress={() => {
              setFailed(false);
              setAttempt(attempt + 1);
            }}
          />
        </View>
      ) : (
        <WebView
          key={attempt}
          source={{ uri: url }}
          style={{ flex: 1, backgroundColor: C.bg }}
          sharedCookiesEnabled
          thirdPartyCookiesEnabled={false}
          setSupportMultipleWindows={false}
          originWhitelist={["*"]}
          onShouldStartLoadWithRequest={(request) => allowed(request.url)}
          startInLoadingState
          renderLoading={() => (
            <ActivityIndicator size="large" color={C.amber} />
          )}
          onError={() => setFailed(true)}
          onHttpError={(event) => {
            if (event.nativeEvent.statusCode >= 500) setFailed(true);
          }}
        />
      )}
    </View>
  );
}
