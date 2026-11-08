import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  AppState,
  FlatList,
  Platform,
  Pressable,
  SafeAreaView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import AsyncStorage from "@react-native-async-storage/async-storage";
import Constants from "expo-constants";
import * as Device from "expo-device";
import * as Notifications from "expo-notifications";

type AlertItem = {
  id: string;
  symbol: string;
  kind: string;
  reason: string;
  bar_time: string;
  created_at: string;
  read: boolean;
  extra?: {
    duration?: string;
    name?: string;
    display?: string;
    code?: string;
    duration_sec?: number;
  };
};

const STORAGE_API = "fq_api_base";
const DEFAULT_API = "http://127.0.0.1:8000";

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

function productNameFromSymbol(symbol: string): string {
  const raw = symbol.includes("@") ? symbol.split("@")[1] : symbol;
  const code = (raw.includes(".") ? raw.split(".")[1] : raw).replace(/\d+$/, "");
  const map: Record<string, string> = {
    wr: "线材",
    ni: "沪镍",
    ec: "集运欧线",
    lc: "碳酸锂",
    fu: "燃料油",
    eg: "乙二醇",
    lu: "低硫燃油",
    rb: "螺纹钢",
    au: "黄金",
    ag: "白银",
    cu: "沪铜",
    al: "沪铝",
    zn: "沪锌",
    pb: "沪铅",
    sn: "沪锡",
    ru: "橡胶",
    bu: "沥青",
    sp: "纸浆",
    ss: "不锈钢",
    hc: "热卷",
    sc: "原油",
    nr: "20号胶",
    bc: "国际铜",
    i: "铁矿石",
    j: "焦炭",
    jm: "焦煤",
    m: "豆粕",
    y: "豆油",
    p: "棕榈油",
    l: "塑料",
    v: "PVC",
    pp: "聚丙烯",
    pg: "LPG",
    jd: "鸡蛋",
    lh: "生猪",
    si: "工业硅",
    ps: "多晶硅",
    MA: "甲醇",
    TA: "PTA",
    SA: "纯碱",
    FG: "玻璃",
    SR: "白糖",
    CF: "棉花",
    RM: "菜粕",
    OI: "菜油",
    AP: "苹果",
    CJ: "红枣",
  };
  const name = map[code] || map[code.toLowerCase()] || map[code.toUpperCase()] || code;
  return symbol.startsWith("KQ.m@") ? `${name}主连` : name;
}

async function registerForPush(apiBase: string): Promise<string> {
  if (!Device.isDevice) {
    return "模拟器无法注册推送";
  }
  const { status: existing } = await Notifications.getPermissionsAsync();
  let finalStatus = existing;
  if (existing !== "granted") {
    const { status } = await Notifications.requestPermissionsAsync();
    finalStatus = status;
  }
  if (finalStatus !== "granted") {
    return "未授予通知权限";
  }

  if (Platform.OS === "android") {
    await Notifications.setNotificationChannelAsync("alerts", {
      name: "形态告警",
      importance: Notifications.AndroidImportance.MAX,
      vibrationPattern: [0, 250, 250, 250],
      lightColor: "#E8B86D",
    });
  }

  const projectId =
    Constants.easConfig?.projectId ??
    Constants.expoConfig?.extra?.eas?.projectId;
  const tokenResp = await Notifications.getExpoPushTokenAsync(
    projectId ? { projectId } : undefined
  );
  const token = tokenResp.data;
  const resp = await fetch(`${apiBase.replace(/\/$/, "")}/api/devices/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, platform: Platform.OS }),
  });
  if (!resp.ok) {
    throw new Error(`注册推送失败 HTTP ${resp.status}`);
  }
  return `已注册推送 ${token.slice(0, 18)}…`;
}

function App() {
  const [apiBase, setApiBase] = useState(DEFAULT_API);
  const [apiInput, setApiInput] = useState(DEFAULT_API);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pushStatus, setPushStatus] = useState("推送未注册");
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

  const setupPush = useCallback(async () => {
    try {
      const msg = await registerForPush(base);
      setPushStatus(msg);
    } catch (e) {
      // FCM/Firebase 未配置时 Expo 会抛英文长错误；列表轮询仍可用，不展示干扰信息
      const raw = e instanceof Error ? e.message : String(e);
      const isFcm =
        /FirebaseApp|fcm-credentials|getExpoPushTokenAsync|Default FirebaseApp/i.test(
          raw
        );
      setPushStatus(isFcm ? "" : raw);
    }
  }, [base]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    knownIds.current = new Set();
    setLoading(true);
    fetchAlerts();
    setupPush();
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
  }, [fetchAlerts, setupPush]);

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
        <Text style={styles.meta}>打开后每 15 秒刷新列表。K线时间≠发现时间。</Text>
        {pushStatus ? <Text style={styles.meta}>{pushStatus}</Text> : null}
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
            <Text style={styles.empty}>暂无告警。新形态出现后会推到这里和通知栏。</Text>
          }
          renderItem={({ item }) => {
            const code = item.extra?.code || item.symbol;
            const name = item.extra?.name || productNameFromSymbol(code);
            const duration = item.extra?.duration ? ` · ${item.extra.duration}` : "";
            return (
              <View style={[styles.alert, item.read ? null : styles.alertNew]}>
                <Text style={styles.alertTitle}>
                  {name}
                  {duration} · {item.kind}
                </Text>
                <Text style={styles.alertCode}>{code}</Text>
                <Text style={styles.alertTime}>K线时间 {item.bar_time}</Text>
                <Text style={styles.alertTime}>发现时间 {item.created_at}</Text>
                <Text style={styles.alertReason}>{item.reason}</Text>
              </View>
            );
          }}
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
  alertCode: { color: "#7A8B9A", marginTop: 2, fontSize: 11 },
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
