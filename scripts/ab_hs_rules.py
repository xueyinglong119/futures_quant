"""全品种头肩规则 A/B：改前（高低点拐点 + 0.4h 肩离颈 + 等 4 根）
vs 改后（收盘价点位 + 0.3h 肩离颈 + 右肩当根即告警）。

会话周期用库内 1m 现场重算，避开 11-06/09/10 的洞。在回测机上跑，2 核就分两片：
  docker run -d --rm -e HS_AB_SHARD=0/2 -e HS_AB_OUT=/app/reports/ab_hs_0.txt \
    -v $PWD/reports:/app/reports futures-quant-api python -u scripts/ab_hs_rules.py
环境变量：HS_AB_SHARD=k/n 分片；HS_AB_TFS=30m,1h 周期；HS_AB_SYMBOLS=a,b 指定品种；
HS_AB_DUMP=1 逐条列出告警。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config.watchlist import WATCH_MAIN_SYMBOLS
from core.hs_monitor import TF_CONFIGS, bars_to_strategy_df, make_strategy
from core.kline_store import DURATION_1M, connect, list_symbols, load_bars
from core.session_rebuild import (
    TF_DURATION,
    aggregate_symbol_session,
    detect_1m_clock,
    minutes_as_beijing,
)
from replay_winrate import simulate_outcomes
from strategies.head_shoulders import HeadShouldersStrategy

EXPLICIT = [s.strip() for s in os.environ.get("HS_AB_SYMBOLS", "").split(",") if s.strip()]
_k, _n = (os.environ.get("HS_AB_SHARD", "0/1").split("/") + ["1"])[:2]
SHARD, SHARDS = int(_k), int(_n)
SESSION_TFS = tuple(
    s.strip() for s in os.environ.get("HS_AB_TFS", "30m,1h").split(",") if s.strip()
)
DAYS = 30
LOAD_DAYS = 60
OUT = ROOT / os.environ.get("HS_AB_OUT", "reports/ab_hs_rules.txt")
DUMP = bool(os.environ.get("HS_AB_DUMP"))

# 改前的肩离颈闸值靠这里还原；改后直接用库里的默认值
MODE: dict[str, object] = {"gate": "lib"}

_ORIG_SHAPE = HeadShouldersStrategy._shape_ok
_ORIG_PIVOT_SERIES = HeadShouldersStrategy._pivot_series
_ORIG_TAIL = HeadShouldersStrategy._with_tail_pivot
_ORIG_CONFIRM = HeadShouldersStrategy._right_shoulder_confirmed


def _shape_ok(self, *a, **kw):
    if MODE["gate"] == "lib":
        return _ORIG_SHAPE(self, *a, **kw)
    price_l, price_h, price_r, idx_l, idx_h, idx_r, neck_l, neck_r = a[:8]
    bottom = kw.get("bottom", False)
    n1, p1 = neck_l
    n2, p2 = neck_r
    neck_head = self._line_at(n1, p1, n2, p2, idx_h)
    height = (neck_head - price_h) if bottom else (price_h - neck_head)
    if height <= 0 or price_h == 0:
        return None
    if height / abs(price_h) < self.min_head_neck_ratio:
        return None
    if abs(p1 - p2) > 0.5 * height:
        return None
    if bottom:
        if price_l - price_h < 0.25 * height or price_r - price_h < 0.25 * height:
            return None
    elif price_h - price_l < 0.25 * height or price_h - price_r < 0.25 * height:
        return None
    if abs(price_l - price_r) > 0.5 * height:
        return None
    neck_at_l = self._line_at(n1, p1, n2, p2, idx_l)
    neck_at_r = self._line_at(n1, p1, n2, p2, idx_r)
    r_ = float(MODE["gate"])
    if r_:
        if bottom:
            if neck_at_l - price_l < r_ * height or neck_at_r - price_r < r_ * height:
                return None
        elif price_l - neck_at_l < r_ * height or price_r - neck_at_r < r_ * height:
            return None
    if self.strict_shape:
        if abs(price_l - price_r) > self.max_shoulder_spread_ratio * abs(neck_head):
            return None
        if abs(p1 - p2) > self.max_neck_slope_ratio * abs(neck_head):
            return None
    return height, neck_head


HeadShouldersStrategy._shape_ok = _shape_ok

VARIANTS = ["OLD 改前", "NEW 改后"]


def set_variant(name: str) -> None:
    """OLD = 把三处改动逐个还原；NEW = 库里默认。"""
    if name == "NEW 改后":
        MODE["gate"] = "lib"
        HeadShouldersStrategy._pivot_series = _ORIG_PIVOT_SERIES
        HeadShouldersStrategy._with_tail_pivot = _ORIG_TAIL
        HeadShouldersStrategy._right_shoulder_confirmed = _ORIG_CONFIRM
        return
    MODE["gate"] = 0.4
    HeadShouldersStrategy._pivot_series = (
        lambda self, klines, *, is_high: klines.high if is_high else klines.low
    )
    HeadShouldersStrategy._with_tail_pivot = lambda self, pivots, series, *, is_high: pivots
    HeadShouldersStrategy._right_shoulder_confirmed = (
        lambda self, idx_r, last_idx: last_idx == idx_r + self.pivot_order
    )


def to_dicts(df):
    rows = []
    for i in range(len(df)):
        r = df.iloc[i]
        rows.append(
            {
                "datetime_end": r["datetime"],
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
                "open_oi": int(r["open_oi"]),
            }
        )
    return rows


def replay(symbol: str, tf: int, dicts: list[dict], cutoff: str) -> list[dict]:
    """按当前变体口径逐根回放；预过滤跟着「右肩是不是当前这根」的规则走。"""
    strat = make_strategy(tf)
    df_all = bars_to_strategy_df(dicts)
    window = TF_CONFIGS[tf].lookback_bars
    order = strat.pivot_order
    min_len = strat.max_span_bars + order * 2 + 5
    tail_rule = strat._with_tail_pivot.__func__ is _ORIG_TAIL
    pts = strat._pivot_series(df_all, is_high=True)
    out = []
    for i in range(min_len - 1, len(df_all)):
        if tail_rule:
            # 改后：当前这根就是拐点才可能告警
            v = float(pts.iloc[i])
            left = pts.iloc[i - order : i]
            if not (v > float(left.max()) or v < float(left.min())):
                continue
        else:
            # 改前：告警发生在右肩后第 4 根，右肩得是 ±4 双侧极值
            j = i - order
            ok = False
            for series in (df_all.low, df_all.high):
                if j - order >= 0 and j + order < len(series):
                    seg = series.iloc[j - order : j + order + 1]
                    if float(series.iloc[j]) in (float(seg.min()), float(seg.max())):
                        ok = True
                        break
            if not ok:
                continue
        sig = strat.on_bar(df_all.iloc[max(0, i - window) : i + 1], symbol=symbol)
        if sig is not None:
            when = str(df_all.iloc[i]["datetime"])
            if when >= cutoff:
                out.append(
                    {
                        "when": when,
                        "i": i,
                        "rgi": max(0, i - window) + int((sig.detail or {}).get("right_idx", -1)),
                        "side": (sig.detail or {}).get("side", ""),
                        "reason": sig.reason,
                        "detail": getattr(sig, "detail", None) or {},
                    }
                )
    return out


def stats(sigs, df, symbol, tf):
    w = l = t = 0
    for r in simulate_outcomes(symbol, tf, df, sigs):
        if r["result"] == "win":
            w += 1
        elif r["result"] == "loss":
            l += 1
        elif r["result"] == "timeout":
            t += 1
    return w, l, t


def line(s: str) -> None:
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(s + "\n")


OUT.parent.mkdir(parents=True, exist_ok=True)
if OUT.exists():
    OUT.unlink()

t0 = time.time()
conn = connect()
ALL = EXPLICIT or [s for s in sorted(list_symbols(conn, DURATION_1M)) if s.startswith("KQ.m@")]
SYMBOLS = ALL[SHARD::SHARDS]
cutoff = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d %H:%M:%S")
m1_start = datetime.now() - timedelta(days=LOAD_DAYS)
WATCH = set(WATCH_MAIN_SYMBOLS)

line(
    f"规则 A/B  分片 {SHARD}/{SHARDS}  品种 {len(SYMBOLS)}/{len(ALL)}  "
    f"周期 {','.join(SESSION_TFS)}  统计窗 {DAYS} 天  1m 加载 {LOAD_DAYS} 天"
)
line("OLD 改前 = 高低点拐点 + 肩离颈>=0.4h + 右肩后等 4 根")
line("NEW 改后 = 点位按收盘价 + 肩离颈>=0.3h + 右肩当根即告警")
line("")

tot: dict[str, dict[str, int]] = {}
tot_w: dict[str, dict[str, int]] = {}
CUR_WATCH = False


def bump(
    tf: int, name: str, n: int, w: int, l: int, t: int,
    added: int, lost: int, same: int, lead_sum: int, lead_n: int,
) -> None:
    for table in (tot, tot_w) if CUR_WATCH else (tot,):
        a = table.setdefault(
            f"{tf}|{name}",
            {"n": 0, "w": 0, "l": 0, "t": 0, "add": 0, "drop": 0, "same": 0, "lead": 0, "ln": 0},
        )
        a["n"] += n
        a["w"] += w
        a["l"] += l
        a["t"] += t
        a["add"] += added
        a["drop"] += lost
        a["same"] += same
        a["lead"] += lead_sum
        a["ln"] += lead_n


for symbol in SYMBOLS:
    st = time.time()
    CUR_WATCH = symbol in WATCH
    raw1 = load_bars(conn, symbol, DURATION_1M, start=m1_start)
    parts = []
    dlines: list[str] = []
    if raw1:
        mins = minutes_as_beijing(raw1, detect_1m_clock(raw1))
        built = aggregate_symbol_session(mins, SESSION_TFS)  # type: ignore[arg-type]
        for tf_label in SESSION_TFS:
            rows = built.get(tf_label, [])
            if len(rows) < 200:
                continue
            tf = TF_DURATION[tf_label]
            df = bars_to_strategy_df(rows)
            dicts = to_dicts(df)
            got: dict[str, list[dict]] = {}
            for name in VARIANTS:
                set_variant(name)
                got[name] = replay(symbol, tf, dicts, cutoff)
            set_variant("NEW 改后")

            def pkey(s):
                return (s["side"], s["rgi"])

            ko = {pkey(s) for s in got["OLD 改前"]}
            kn = {pkey(s) for s in got["NEW 改后"]}
            at_new = {pkey(s): s["i"] for s in got["NEW 改后"]}
            # 同一右肩：改后在这根告警、改后要再等 4 根，差值即提前根数
            lead = [s["i"] - at_new[pkey(s)] for s in got["OLD 改前"] if pkey(s) in kn]
            if DUMP:
                for name in VARIANTS:
                    for s in got[name]:
                        dlines.append(f"    {name} {tf // 60}m {s['when']}  {s['reason']}")
            for name in VARIANTS:
                w, l, t = stats(got[name], df, symbol, tf)
                bump(
                    tf,
                    name,
                    len(got[name]),
                    w,
                    l,
                    t,
                    len(kn - ko) if name == "NEW 改后" else 0,
                    len(ko - kn) if name == "NEW 改后" else 0,
                    len(ko & kn),
                    sum(lead),
                    len(lead),
                )
            parts.append(
                f"{tf // 60:>2}m OLD={len(got['OLD 改前'])} NEW={len(got['NEW 改后'])}"
                f" 同{len(ko & kn)} 新{len(kn - ko)} 旧{len(ko - kn)}"
                f" 早{(sum(lead) / len(lead)) if lead else 0:.1f}根"
                f" 胜/负 {tot[f'{tf}|OLD 改前']['w']}/{tot[f'{tf}|OLD 改前']['l']}"
                f"->{tot[f'{tf}|NEW 改后']['w']}/{tot[f'{tf}|NEW 改后']['l']}"
            )
    tag = "watch" if symbol in WATCH else "extra "
    line(f"{symbol:18s} {tag} 1m={len(raw1):5d} | " + " | ".join(parts) + f"  ({time.time() - st:.0f}s)")
    for d in dlines:
        line(d)

conn.close()
for label, table in (("全品种", tot), ("仅监控 41 品种", tot_w)):
    line("")
    line(f"== 合计（本分片，{label}）==")
    for tf in sorted({int(k.split("|")[0]) for k in table}):
        for name in VARIANTS:
            a = table.get(f"{tf}|{name}")
            if not a:
                continue
            den = max(a["w"] + a["l"], 1)
            extra = (
                f"  同形态 {a['same']}（平均早 {a['lead'] / max(a['ln'], 1):.1f} 根）"
                f" 新增 {a['add']} 消失 {a['drop']}"
                if name == "NEW 改后"
                else ""
            )
            line(
                f"  {tf // 60:>3}m  {name:10s} {a['n']:4d} 条  胜/负/超时 {a['w']}/{a['l']}/{a['t']}"
                f"  胜率 {a['w'] / den:.1%}{extra}"
            )
line("")
line(f"  耗时 {time.time() - t0:.0f}s")
