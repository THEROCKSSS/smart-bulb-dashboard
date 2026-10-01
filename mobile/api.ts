import AsyncStorage from "@react-native-async-storage/async-storage";
export const DEFAULT_SERVER =
  process.env.EXPO_PUBLIC_API_URL ||
  "https://owens-pc-vpn.tailff2683.ts.net:8502";
export type Settings = Record<string, any>;
export type Device = { id: string; name: string };
export type Mix = { id: string; name: string; config: Settings };
export function serverURL(value: string) {
  const url = new URL(value.trim());
  if (
    !["https:", "http:"].includes(url.protocol) ||
    url.username ||
    url.password
  )
    throw new Error("Enter an http or https dashboard address.");
  return url.origin;
}
export const loadServer = async () =>
  (await AsyncStorage.getItem("studio-server")) || DEFAULT_SERVER;
export const saveServer = (value: string) =>
  AsyncStorage.setItem("studio-server", serverURL(value));
export function client(server: string, unauthorized: () => void) {
  return async function api(
    path: string,
    body?: unknown,
    method = body === undefined ? "GET" : "POST",
  ): Promise<any> {
    const controller = new AbortController(),
      timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(server + path, {
        method,
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const data = await response.json();
      if (response.status === 401) unauthorized();
      if (!response.ok)
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : `Request failed (${response.status})`,
        );
      if (data.result && (data.result.Error || data.result.error))
        throw new Error(
          "The light did not confirm the command. Check its connection.",
        );
      return data;
    } catch (error) {
      if (error instanceof Error && error.name === "AbortError")
        throw new Error("Dashboard timed out. Check the connection and retry.");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  };
}
