import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  AppState,
  FlatList,
  Pressable,
  SafeAreaView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";

type AlertItem = {
  id: string;
  symbol: string;
  kind: string;
  reason: string;
  bar_time: string;
  created_at: string;
  read: boolean;
};

const STORAGE_API = "fq_api_base";
const DEFAULT_API = "http://127.0.0.1:8000";

function App() {
  const [apiBase, setApiBase] = useState(DEFAULT_API);
  const [apiInput, setApiInput] = useState(DEFAULT_API);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const knownIds = useRef<Set<string>>(new Set());
  const appState = useRef(AppState.currentState);

  const base = useMemo(() => apiBase.replace(/\/$/, ""), [apiBase]);

  const loadSettings = useCallback(async () => {
    const saved = await AsyncStorage.getItem(STORAGE_API);
    if (saved) {
      setApiBase(saved);
      setApiInput(saved);
    }
  }, []);

  const saveApi = useCallback(async () => {
    const next = apiInput.trim().replace(/\/$/, "") || DEFAULT_API;
    await AsyncStorage.setItem(STORAGE_API, next);
    setApiBase(next);
  }, [apiInput]);

  const fetchAlerts = useCallback(async () => {
    try {
      const resp = await fetch(`${base}/api/alerts?limit=50`);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = await resp.json();
      const items: AlertItem[] = data.alerts ?? [];
      for (const item of items) {
        knownIds.current.add(item.id);
      }
      setAlerts(items);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    knownIds.current = new Set();
    setLoading(true);
    fetchAlerts();
    const timer = setInterval(fetchAlerts, 15000);
    const sub = AppState.addEventListener("change", (next) => {
      if (appState.current.match(/inactive|background/) && next === "active") {
        fetchAlerts();
      }
      appState.current = next;
    });
    return () => {
      clearInterval(timer);
      sub.remove();
    };
  }, [fetchAlerts]);

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar barStyle="light-content" />
      <View style={styles.header}>
        <Text style={styles.brand}>期货形态告警</Text>
        <Text style={styles.sub}>头肩顶 / 头肩底 · 实时通知</Text>
      </View>

      <View style={styles.card}>
        <Text style={styles.label}>服务器地址（电脑局域网 IP）</Text>
        <TextInput
          style={styles.input}
          value={apiInput}
          onChangeText={setApiInput}
          autoCapitalize="none"
          autoCorrect={false}
          placeholder="http://192.168.x.x:8000"
          placeholderTextColor="#7A8B9A"
        />
        <View style={styles.row}>
          <Pressable style={styles.btn} onPress={saveApi}>
            <Text style={styles.btnText}>保存并连接</Text>
          </Pressable>
          <Pressable style={[styles.btn, styles.btnGhost]} onPress={fetchAlerts}>
            <Text style={styles.btnText}>刷新</Text>
          </Pressable>
        </View>
        <Text style={styles.meta}>打开后每 15 秒刷新一次告警</Text>
        {error ? <Text style={styles.error}>连接失败：{error}</Text> : null}
      </View>

      {loading ? (
        <ActivityIndicator color="#E8B86D" style={{ marginTop: 24 }} />
      ) : (
        <FlatList
          data={alerts}
          keyExtractor={(item) => item.id}
          contentContainerStyle={styles.list}
          ListEmptyComponent={
            <Text style={styles.empty}>暂无告警。出现头肩形态后会推到这里。</Text>
          }
          renderItem={({ item }) => (
            <View style={[styles.alert, item.read ? null : styles.alertNew]}>
              <Text style={styles.alertTitle}>
                {item.symbol} · {item.kind}
              </Text>
              <Text style={styles.alertTime}>{item.bar_time}</Text>
              <Text style={styles.alertReason}>{item.reason}</Text>
            </View>
          )}
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: "#0B1F33" },
  header: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 8 },
  brand: {
    color: "#F4F0E8",
    fontSize: 28,
    fontWeight: "700",
    letterSpacing: 1,
  },
  sub: { color: "#9BB0C1", marginTop: 4, fontSize: 14 },
  card: {
    marginHorizontal: 16,
    marginBottom: 8,
    padding: 14,
    borderRadius: 12,
    backgroundColor: "#143049",
  },
  label: { color: "#C7D3DE", marginBottom: 8, fontSize: 13 },
  input: {
    backgroundColor: "#0B1F33",
    color: "#F4F0E8",
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderWidth: 1,
    borderColor: "#2A4A66",
  },
  row: { flexDirection: "row", gap: 10, marginTop: 10 },
  btn: {
    backgroundColor: "#C45C26",
    paddingHorizontal: 14,
    paddingVertical: 10,
    borderRadius: 8,
  },
  btnGhost: { backgroundColor: "#2A4A66" },
  btnText: { color: "#FFF", fontWeight: "600" },
  meta: { color: "#8FA3B5", marginTop: 10, fontSize: 12 },
  error: { color: "#FF8F8F", marginTop: 6, fontSize: 12 },
  list: { padding: 16, paddingBottom: 40 },
  empty: { color: "#8FA3B5", textAlign: "center", marginTop: 40 },
  alert: {
    backgroundColor: "#143049",
    borderRadius: 12,
    padding: 14,
    marginBottom: 10,
    borderLeftWidth: 3,
    borderLeftColor: "#2A4A66",
  },
  alertNew: { borderLeftColor: "#E8B86D" },
  alertTitle: { color: "#F4F0E8", fontSize: 16, fontWeight: "700" },
  alertTime: { color: "#9BB0C1", marginTop: 4, fontSize: 12 },
  alertReason: { color: "#D7E0E8", marginTop: 8, lineHeight: 20 },
});

class AppErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { message: string }
> {
  state = { message: "" };

  static getDerivedStateFromError(error: unknown) {
    return {
      message: error instanceof Error ? error.message : String(error),
    };
  }

  render() {
    if (this.state.message) {
      return (
        <SafeAreaView style={styles.root}>
          <Text style={styles.brand}>期货形态告警</Text>
          <Text style={styles.error}>{this.state.message}</Text>
        </SafeAreaView>
      );
    }
    return this.props.children;
  }
}

export default function Root() {
  return (
    <AppErrorBoundary>
      <App />
    </AppErrorBoundary>
  );
}
