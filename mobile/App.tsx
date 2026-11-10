import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  AppState,
  FlatList,
  Image,
  Platform,
  Pressable,
  SafeAreaView,
  ScrollView,
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

type ExecutionTrade = {
  id: number;
  name: string;
  symbol: string;
  duration: string;
  side_text: string;
  kind: string;
  status: string;
  open_time_bj: string;
  open_price: number;
  lots: number;
  margin: number;
  fee_open: number;
  fee_close: number | null;
  tp_price: number;
  sl_price: number;
  last_price: number | null;
  close_time_bj?: string | null;
  close_price: number | null;
  close_reason: string | null;
  net_pnl: number | null;
  unrealized: number | null;
  note: string;
};

type ExecutionDetail = {
  opened: boolean;
  trade: ExecutionTrade | null;
  note: string;
};

type BoardTrade = {
  id: number;
  name: string;
  symbol: string;
  duration: string;
  side_text: string;
  mark: string;
  can_close?: boolean;
  open_time: string;
  close_time: string | null;
  open_price: number;
  close_price: number | null;
  last_price?: number | null;
  lots: number;
  fee_open: number;
  net_pnl: number | null;
  unrealized?: number | null;
  close_reason: string | null;
  tp_price: number;
  sl_price: number;
};

type BoardData = {
  today: string;
  date: string;
  days: string[];
  summary: {
    opened: number;
    closed: number;
    wins: number;
    losses: number;
    win_rate: number | null;
    net_pnl: number;
    fee: number;
    equity: number | null;
    unrealized: number | null;
    open_positions: number | null;
    is_today: boolean;
  };
  trades: BoardTrade[];
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

async function ensureNotifyPermission(): Promise<boolean> {
  if (Platform.OS === "android") {
    await Notifications.setNotificationChannelAsync("alerts", {
      name: "形态告警",
      importance: Notifications.AndroidImportance.MAX,
      vibrationPattern: [0, 250, 250, 250],
      lightColor: "#E8B86D",
    });
  }
  const { status: existing } = await Notifications.getPermissionsAsync();
  let finalStatus = existing;
  if (existing !== "granted") {
    const { status } = await Notifications.requestPermissionsAsync();
    finalStatus = status;
  }
  return finalStatus === "granted";
}

function alertTitle(item: AlertItem): string {
  const code = item.extra?.code || item.symbol;
  const name = item.extra?.name || productNameFromSymbol(code);
  const duration = item.extra?.duration ? ` · ${item.extra.duration}` : "";
  return `${name}${duration} · ${item.kind}`;
}

async function showLocalAlerts(items: AlertItem[]): Promise<void> {
  const shown = items.slice(0, 5);
  for (const item of shown) {
    await Notifications.scheduleNotificationAsync({
      content: {
        title: alertTitle(item),
        body: item.reason,
        sound: "default",
        data: { id: item.id },
      },
      trigger: null,
    });
  }
  const rest = items.length - shown.length;
  if (rest > 0) {
    await Notifications.scheduleNotificationAsync({
      content: {
        title: "期货形态告警",
        body: `另有 ${rest} 条新告警，打开 App 查看`,
        sound: "default",
      },
      trigger: null,
    });
  }
}

async function registerForPush(apiBase: string): Promise<string> {
  if (!Device.isDevice) {
    return "模拟器无法注册推送";
  }
  const granted = await ensureNotifyPermission();
  if (!granted) {
    return "未授予通知权限";
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

function moneyText(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}`;
}

function App() {
  const [tab, setTab] = useState<"alerts" | "board">("alerts");
  const [apiBase, setApiBase] = useState(DEFAULT_API);
  const [apiInput, setApiInput] = useState(DEFAULT_API);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pushStatus, setPushStatus] = useState("推送未注册");
  const [boardDay, setBoardDay] = useState("");
  const [board, setBoard] = useState<BoardData | null>(null);
  const [boardError, setBoardError] = useState("");
  const [boardLoading, setBoardLoading] = useState(false);
  const knownIds = useRef<Set<string>>(new Set());
  const seeded = useRef(false);
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
      const fresh = items.filter((item) => !knownIds.current.has(item.id));
      for (const item of items) {
        knownIds.current.add(item.id);
      }
      if (seeded.current && fresh.length > 0) {
        const allowed = await ensureNotifyPermission();
        if (allowed) {
          await showLocalAlerts(fresh);
        }
      } else {
        seeded.current = true;
      }
      setAlerts(items);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [base]);

  const fetchBoard = useCallback(async (day?: string) => {
    setBoardLoading(true);
    try {
      const query = day ? `?date=${encodeURIComponent(day)}` : "";
      const resp = await fetch(`${base}/api/paper/board${query}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: BoardData = await resp.json();
      setBoard(data);
      setBoardDay(data.date);
      setBoardError("");
    } catch (e) {
      setBoardError(e instanceof Error ? e.message : String(e));
    } finally {
      setBoardLoading(false);
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
      setPushStatus(isFcm ? "通知已开启，新告警会显示在通知栏" : raw);
    }
  }, [base]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  useEffect(() => {
    knownIds.current = new Set();
    seeded.current = false;
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
        <View style={styles.tabs}>
          <Pressable style={[styles.tab, tab === "alerts" ? styles.tabOn : null]} onPress={() => setTab("alerts")}>
            <Text style={styles.tabText}>告警</Text>
          </Pressable>
          <Pressable
            style={[styles.tab, tab === "board" ? styles.tabOn : null]}
            onPress={() => {
              setTab("board");
              fetchBoard(boardDay || undefined);
            }}
          >
            <Text style={styles.tabText}>看板</Text>
          </Pressable>
        </View>
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
          <Pressable
            style={[styles.btn, styles.btnGhost]}
            onPress={() => (tab === "board" ? fetchBoard(boardDay || undefined) : fetchAlerts())}
          >
            <Text style={styles.btnText}>刷新</Text>
          </Pressable>
        </View>
        <Text style={styles.meta}>打开后每 15 秒刷新列表。K线时间≠发现时间。</Text>
        {pushStatus ? <Text style={styles.meta}>{pushStatus}</Text> : null}
        {error ? <Text style={styles.error}>连接失败：{error}</Text> : null}
      </View>

      {tab === "board" ? (
        <BoardPane
          board={board}
          loading={boardLoading}
          error={boardError}
          base={base}
          onSelect={(day) => fetchBoard(day)}
          onClosed={() => fetchBoard(boardDay || undefined)}
        />
      ) : loading ? (
        <ActivityIndicator color="#E8B86D" style={{ marginTop: 24 }} />
      ) : (
        <FlatList
          data={alerts}
          keyExtractor={(item) => item.id}
          contentContainerStyle={styles.list}
          ListEmptyComponent={
            <Text style={styles.empty}>暂无告警。新形态出现后会推到这里和通知栏。</Text>
          }
          renderItem={({ item }) => <AlertCard item={item} base={base} />}
        />
      )}
    </SafeAreaView>
  );
}

function AlertCard({ item, base }: { item: AlertItem; base: string }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<ExecutionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (!next || detail || loading) return;
    setLoading(true);
    setError("");
    try {
      const resp = await fetch(`${base}/api/alerts/${encodeURIComponent(item.id)}/execution`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setDetail((await resp.json()) as ExecutionDetail);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const code = item.extra?.code || item.symbol;
  const name = item.extra?.name || productNameFromSymbol(code);
  const duration = item.extra?.duration ? ` · ${item.extra.duration}` : "";
  const trade = detail?.trade;
  return (
    <Pressable style={[styles.alert, item.read ? null : styles.alertNew]} onPress={toggle}>
      <Text style={styles.alertTitle}>
        {name}
        {duration} · {item.kind}
      </Text>
      <Text style={styles.alertCode}>{code}</Text>
      <Text style={styles.alertTime}>K线时间 {item.bar_time}</Text>
      <Text style={styles.alertTime}>发现时间 {item.created_at}</Text>
      <Text style={styles.alertReason}>{item.reason}</Text>
      <Text style={styles.detailHint}>{open ? "收起执行详情 ▲" : "查看执行详情 ▼"}</Text>
      {open ? (
        <Image
          source={{ uri: `${base}/api/alerts/${encodeURIComponent(item.id)}/chart.png` }}
          style={styles.chartImg}
          resizeMode="contain"
        />
      ) : null}
      {open ? (
        loading ? (
          <ActivityIndicator color="#E8B86D" style={{ marginTop: 8 }} />
        ) : error ? (
          <Text style={styles.error}>执行详情加载失败：{error}</Text>
        ) : detail ? (
          <View style={styles.detailBox}>
            <Text style={styles.detailNote}>{detail.note}</Text>
            {trade ? (
              <>
                <Text style={styles.detailLine}>
                  {trade.open_time_bj} {trade.side_text} {trade.open_price} × {trade.lots} 手　保证金 {Number(trade.margin).toFixed(0)}
                </Text>
                <Text style={styles.detailLine}>
                  止盈 {trade.tp_price}　止损 {trade.sl_price}　开仓手续费 {Number(trade.fee_open).toFixed(2)}
                </Text>
                {trade.status === "open" ? (
                  <>
                    <Text style={styles.detailLine}>现价 {trade.last_price ?? "—"}</Text>
                    <Text style={[styles.detailLine, { color: pnlColor(trade.unrealized) }]}>
                      浮动盈亏 {moneyText(trade.unrealized)}
                    </Text>
                  </>
                ) : (
                  <>
                    <Text style={styles.detailLine}>
                      平仓 {trade.close_time_bj || "—"}　{trade.close_price ?? "—"}　{trade.close_reason || ""}
                    </Text>
                    <Text style={[styles.detailLine, { color: pnlColor(trade.net_pnl) }]}>
                      净盈亏 {moneyText(trade.net_pnl)}
                    </Text>
                  </>
                )}
              </>
            ) : null}
          </View>
        ) : null
      ) : null}
    </Pressable>
  );
}

function pnlColor(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "#F4F0E8";
  return value > 0 ? "#FF6B6B" : "#3DDC97";
}

function BoardPane({
  board,
  loading,
  error,
  base,
  onSelect,
  onClosed,
}: {
  board: BoardData | null;
  loading: boolean;
  error: string;
  base: string;
  onSelect: (day: string) => void;
  onClosed: () => void;
}) {
  const summary = board?.summary;
  const closeTrade = (item: BoardTrade) => {
    Alert.alert("手动平仓", `确认按最新价平掉 ${item.name} · ${item.duration}？`, [
      { text: "取消", style: "cancel" },
      {
        text: "平仓",
        style: "destructive",
        onPress: async () => {
          try {
            const resp = await fetch(`${base}/api/paper/close?trade_id=${item.id}`, {
              method: "POST",
            });
            const body = await resp.json().catch(() => ({}));
            if (!resp.ok) {
              throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${resp.status}`);
            }
            onClosed();
          } catch (e) {
            Alert.alert("平仓失败", e instanceof Error ? e.message : String(e));
          }
        },
      },
    ]);
  };
  return (
    <ScrollView contentContainerStyle={styles.list}>
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.dayRow}>
        {(board?.days ?? []).map((day) => (
          <Pressable
            key={day}
            style={[styles.dayChip, day === board?.date ? styles.dayChipOn : null]}
            onPress={() => onSelect(day)}
          >
            <Text style={styles.dayText}>{day === board?.today ? `今天 ${day.slice(5)}` : day}</Text>
          </Pressable>
        ))}
      </ScrollView>
      {loading ? <ActivityIndicator color="#E8B86D" /> : null}
      {error ? <Text style={styles.error}>看板加载失败：{error}</Text> : null}
      {summary ? (
        <View style={styles.statGrid}>
          <Stat label="平仓净盈亏" value={moneyText(summary.net_pnl)} pnl={summary.net_pnl} />
          <Stat label={summary.is_today ? "当前本金" : "当日最后本金"} value={summary.equity === null ? "—" : summary.equity.toFixed(2)} />
          <Stat label="开仓 / 平仓" value={`${summary.opened} / ${summary.closed}`} />
          <Stat label="胜率" value={summary.win_rate === null ? "—" : `${Math.round(summary.win_rate * 100)}%`} />
          <Stat label="手续费" value={summary.fee.toFixed(2)} />
          <Stat label="持仓浮动" value={moneyText(summary.unrealized)} pnl={summary.unrealized} />
        </View>
      ) : null}
      {(board?.trades ?? []).length === 0 && !loading ? (
        <Text style={styles.empty}>这一天没有开仓、平仓或持仓。</Text>
      ) : null}
      {(board?.trades ?? []).map((item) => {
        const open = !!item.can_close;
        return (
          <View key={item.id} style={styles.alert}>
            <Text style={styles.alertTitle}>
              {item.name} · {item.duration} · {item.side_text} · {item.mark}
            </Text>
            <Text style={styles.alertCode}>{item.symbol}</Text>
            <Text style={styles.alertTime}>
              开仓 {item.open_time}　{item.open_price} × {item.lots} 手
            </Text>
            <Text style={styles.alertTime}>
              平仓 {item.close_time || "—"}　{item.close_reason || ""}
            </Text>
            {open && item.last_price != null ? (
              <Text style={styles.alertTime}>现价 {item.last_price}</Text>
            ) : null}
            <Text style={styles.alertReason}>
              手续费 {Number(item.fee_open || 0).toFixed(2)}　
              {open ? `浮动 ${moneyText(item.unrealized ?? null)}` : `净盈亏 ${moneyText(item.net_pnl)}`}
            </Text>
            {open ? (
              <Pressable style={styles.closeBtn} onPress={() => closeTrade(item)}>
                <Text style={styles.closeBtnText}>平仓</Text>
              </Pressable>
            ) : null}
          </View>
        );
      })}
    </ScrollView>
  );
}

function Stat({ label, value, pnl }: { label: string; value: string; pnl?: number | null }) {
  const color = pnl === null || pnl === undefined || pnl === 0 ? "#F4F0E8" : pnl > 0 ? "#FF6B6B" : "#3DDC97";
  return (
    <View style={styles.stat}>
      <Text style={styles.meta}>{label}</Text>
      <Text style={[styles.statValue, { color }]}>{value}</Text>
    </View>
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
  tabs: { flexDirection: "row", gap: 8, marginTop: 12 },
  tab: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 999, backgroundColor: "#143049" },
  tabOn: { backgroundColor: "#C45C26" },
  tabText: { color: "#F4F0E8", fontWeight: "700" },
  dayRow: { gap: 8, paddingBottom: 12 },
  dayChip: { backgroundColor: "#143049", borderRadius: 999, paddingHorizontal: 12, paddingVertical: 8 },
  dayChipOn: { backgroundColor: "#C45C26" },
  dayText: { color: "#F4F0E8", fontSize: 13 },
  statGrid: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginBottom: 12 },
  stat: { width: "48%", backgroundColor: "#143049", borderRadius: 12, padding: 12 },
  statValue: { marginTop: 6, fontSize: 18, fontWeight: "700" },
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
  detailHint: { color: "#E8B86D", marginTop: 8, fontSize: 12, fontWeight: "600" },
  chartImg: {
    width: "100%",
    height: 220,
    marginTop: 8,
    borderRadius: 8,
    backgroundColor: "#0E1A24",
  },
  detailBox: {
    marginTop: 8,
    paddingTop: 8,
    borderTopWidth: 1,
    borderTopColor: "#2A4A66",
  },
  detailNote: { color: "#9BB0C1", fontSize: 12, marginBottom: 4 },
  detailLine: { color: "#D7E0E8", marginTop: 4, fontSize: 13 },
  closeBtn: {
    alignSelf: "flex-start",
    marginTop: 10,
    backgroundColor: "#C45C26",
    borderRadius: 8,
    paddingHorizontal: 14,
    paddingVertical: 8,
  },
  closeBtnText: { color: "#FFF", fontWeight: "700" },
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
