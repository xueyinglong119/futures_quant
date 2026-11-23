"""大小周期嵌套形态冲突判定（用户 2026-11-23 规格）。

小周期头肩信号出告警的那根，回查同品种**所有更大周期**上是否正压着一个反向头肩形态，
按规格的 A–G 判定表出 allow / downgrade / block，取最坏结论。

两处规格歧义按「让用户给的生猪示例成立」取，理由写在 HS_RULES.md §1 周期冲突：

1. 跨周期不能比下标（两边下标是各自周期的根数），区间一律用 K 线时刻比。
   `end_dt` 取大周期**当前根**而不是右肩那根 —— 否则 C 分支（大形态已突破）永远不可达，
   因为颈线突破必然发生在右肩之后。
2. 大形态 `status` 只由「右肩之后有没有收越颈线」决定：越过即 confirmed + breakout，
   没越就是 candidate（规格里这两个字段在判定表里同时为真才 block，等价于一条）。

判定表顺序按原话实现，**不擅自调整**：D（小形态未破颈 → downgrade 0.4）排在 E/F 之前，
而我们所有告警都是右肩收盘当根发出、那一刻右肩收盘必然还在颈线内侧
（`_shape_ok` 的肩离颈余量 ≥0.1×头高，`strategies/head_shoulders.py:520`），
所以 E/F（离大底支撑 / 大顶压力 ≤5%）在现口径下是走不到的分支。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import pandas as pd

# 整条通道默认关：block 会拦住严格档的模拟盘开仓（动 11-22 定稿口径），
# 先用 scripts/audit_tf_conflict.py 出影响面再决定开不开。
TF_CONFLICT = os.environ.get("HS_TF_CONFLICT", "0") == "1"
# 大周期形态的回溯探针根数（按那个周期自己的根算）
PROBE_BARS = int(os.environ.get("HS_TF_CONFLICT_PROBE_BARS", "60"))
# 每个大周期最多留几条还活着的形态（同刻常规档/严格档可能各出一条，取最近两条）
MAX_PER_TF = 2
# 规格里「靠近大底支撑 / 大顶压力」的容差
NEAR_TOLERANCE_RATIO = float(os.environ.get("HS_TF_CONFLICT_NEAR_TOLERANCE", "0.05"))

PATTERN_TOP = "head_shoulder_top"
PATTERN_BOTTOM = "head_shoulder_bottom"

ALLOW = "allow"
DOWNGRADE = "downgrade"
BLOCK = "block"

_RANK = {ALLOW: 0, DOWNGRADE: 1, BLOCK: 2}
# 与 `_scan` 的 min_len 对齐：max_span_bars + 2*pivot_order + 5
_MIN_LEN_PAD = 5


@dataclass
class LargePattern:
    """大周期上当前还活着的那个头肩形态，点位沿用检测器的收盘价口径。"""

    duration_sec: int
    pattern: str
    direction: str
    left: float
    head: float
    right: float
    neck: float
    start_dt: datetime
    right_dt: datetime
    end_dt: datetime
    breakout: bool
    breakout_dt: datetime | None

    @property
    def status(self) -> str:
        return "confirmed" if self.breakout else "candidate"

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_sec": self.duration_sec,
            "pattern": self.pattern,
            "direction": self.direction,
            "left": round(self.left, 4),
            "head": round(self.head, 4),
            "right": round(self.right, 4),
            "neck": round(self.neck, 4),
            "start": _iso(self.start_dt),
            "right_shoulder": _iso(self.right_dt),
            "as_of": _iso(self.end_dt),
            "status": self.status,
            "breakout": self.breakout,
            "breakout_at": _iso(self.breakout_dt) if self.breakout_dt else None,
        }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _label(duration_sec: int) -> str:
    from core.kline_store import DURATION_LABELS  # 延迟导入，避开与 hs_monitor 的环

    return DURATION_LABELS.get(duration_sec, str(duration_sec))


def _crossed(bottom: bool, neck: float, close: float) -> bool:
    """收盘是否站到颈线外侧：底要看涨破，顶要看跌破。"""
    return close > neck if bottom else close < neck


def _recovered(bottom: bool, neck: float, close: float) -> bool:
    """破位之后又收回颈线内侧：底涨破后重新收在颈线下方、顶跌破后重新收在颈线上方。"""
    return close < neck if bottom else close > neck


def _beyond_head(bottom: bool, head: float, close: float) -> bool:
    """收盘越过头部极值，形态结构已经破了，不再当作大形态压着。"""
    return close < head if bottom else close > head


def is_inside_large_pattern(small_dt: datetime, large: LargePattern) -> bool:
    """小形态落在大形态区间内才算嵌套（规格 §三.2，下标换成时刻）。"""
    return large.start_dt <= small_dt <= large.end_dt


def near_large_support(price: float, large: LargePattern, tolerance_ratio: float = NEAR_TOLERANCE_RATIO) -> bool:
    """小顶做空时离大底支撑（头部）是否太近（规格 §三.3）。"""
    return price <= large.head * (1.0 + tolerance_ratio)


def near_large_resistance(price: float, large: LargePattern, tolerance_ratio: float = NEAR_TOLERANCE_RATIO) -> bool:
    """小底做多时离大顶压力（头部）是否太近（规格 §三.3 的顶版）。"""
    return price >= large.head * (1.0 - tolerance_ratio)


def small_signal_of(detail: dict[str, Any], bar_dt: datetime) -> dict[str, Any]:
    """把头肩信号的 detail 换成规格里那个 small_signal 形状。

    `breakout_confirmed` 按当根收盘价实算（右肩收盘就是当根收盘），不写死 False：
    现口径下它恒为假，哪天肩部余量闸改了这里会自己跟上。
    """
    bottom = str(detail.get("side")) == "long"
    right = float(detail.get("right") or 0.0)
    neck = float(detail.get("neck") or 0.0)
    return {
        "pattern": PATTERN_BOTTOM if bottom else PATTERN_TOP,
        "direction": "long" if bottom else "short",
        "ls_price": float(detail.get("left") or 0.0),
        "head_price": float(detail.get("head") or 0.0),
        "rs_price": right,
        "neck_price": neck,
        "breakout_confirmed": bool(neck) and _crossed(bottom, neck, right),
        "time_idx": bar_dt,
    }


def judge_nested_pattern(small: dict[str, Any], large: LargePattern) -> dict[str, Any]:
    """规格 §四 的判定表，A–G 顺序与阈值原样保留。"""
    conflict_direction = small["direction"] != large.direction
    inside = is_inside_large_pattern(small["time_idx"], large)
    large_confirmed = large.status == "confirmed"
    large_breakout = large.breakout
    small_breakout = bool(small["breakout_confirmed"])
    current_price = float(small["rs_price"])
    tf = f"{_label(large.duration_sec)} {_pattern_label(large.pattern)}"

    if not conflict_direction:
        return _verdict(ALLOW, "大小周期方向一致", 1.0, False, tf)
    if not inside:
        return _verdict(ALLOW, "小形态不在大形态区间内", 0.8, False, tf)
    if large_confirmed and large_breakout:
        return _verdict(BLOCK, "大周期形态已确认，禁止逆大周期开仓", 0.0, True, tf)
    if not small_breakout:
        return _verdict(
            DOWNGRADE, "大周期候选形态内部的反向小顶/小底，小形态未确认突破", 0.4, True, tf
        )
    if large.pattern == PATTERN_BOTTOM and near_large_support(current_price, large):
        return _verdict(BLOCK, "大底支撑附近，小顶做空盈亏比不足", 0.2, True, tf)
    if large.pattern == PATTERN_TOP and near_large_resistance(current_price, large):
        return _verdict(BLOCK, "大顶压力附近，小底做多盈亏比不足", 0.2, True, tf)
    return _verdict(DOWNGRADE, "大小周期方向冲突，需二次确认", 0.5, True, tf)


def _pattern_label(pattern: str) -> str:
    return "头肩底" if pattern == PATTERN_BOTTOM else "头肩顶"


def _verdict(action: str, reason: str, weight: float, conflict: bool, tf: str) -> dict[str, Any]:
    return {
        "action": action,
        "reason": f"{tf} {reason}",
        "weight": weight,
        "conflict": conflict,
        "tf": tf,
    }


def judge_many(small: dict[str, Any], larges: list[LargePattern]) -> dict[str, Any] | None:
    """对多条大形态逐个判，取最坏结论（block > downgrade > allow）。"""
    if not larges:
        return None
    verdicts = [judge_nested_pattern(small, lg) for lg in larges]
    worst = max(verdicts, key=lambda v: (_RANK[str(v["action"])], -float(v["weight"])))
    blocked = [v for v in verdicts if v["action"] == BLOCK]
    conflicted = [v for v in verdicts if v["conflict"]]
    # 理由只列最坏那一档的，且封顶三条：五条大周期全压上来时 reason 不能写成一段散文
    listed = (blocked or conflicted or [worst])[:3]
    return {
        "action": worst["action"],
        "weight": float(worst["weight"]),
        "conflict": bool(conflicted),
        "reason": "；".join(str(v["reason"]) for v in listed),
        "large": [lg.to_dict() for lg in larges],
    }


# ---------------------------------------------------------------- 大周期形态探针


def _state_of(
    symbol: str,
    duration_sec: int,
    sig: Any,
    win: pd.DataFrame,
) -> LargePattern | None:
    detail = getattr(sig, "detail", None) or {}
    if detail.get("pattern_kind"):
        # 三重顶/底不参与冲突判断（用户 2026-11-23 裁决：只头肩顶/底）
        return None
    for key in ("left", "head", "right", "neck", "left_idx", "right_idx"):
        if detail.get(key) is None:
            return None
    bottom = str(detail.get("side")) == "long"
    try:
        left_dt = _ts(win.iloc[int(detail["left_idx"])]["datetime"])
        right_dt = _ts(win.iloc[int(detail["right_idx"])]["datetime"])
    except (IndexError, KeyError, ValueError):
        return None
    last_dt = _ts(win.iloc[-1]["datetime"])
    return LargePattern(
        duration_sec=duration_sec,
        pattern=PATTERN_BOTTOM if bottom else PATTERN_TOP,
        direction="long" if bottom else "short",
        left=float(detail["left"]),
        head=float(detail["head"]),
        right=float(detail["right"]),
        neck=float(detail["neck"]),
        start_dt=left_dt,
        right_dt=right_dt,
        end_dt=last_dt,
        breakout=False,
        breakout_dt=None,
    )


def _ts(val: Any) -> datetime:
    dt = pd.Timestamp(val).to_pydatetime()
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _advance(
    state: LargePattern,
    df_all: pd.DataFrame,
    right_idx_in_all: int,
) -> tuple[LargePattern, bool]:
    """右肩之后的 K 线上推进：破颈线 → confirmed；越过头部或重回颈线 → 形态作废。"""
    bottom = state.direction == "long"
    for j in range(right_idx_in_all + 1, len(df_all)):
        row = df_all.iloc[j]
        close = float(row["close"])
        if not state.breakout:
            if _crossed(bottom, state.neck, close):
                state.breakout = True
                state.breakout_dt = _ts(row["datetime"])
        elif _recovered(bottom, state.neck, close):
            return state, False
        if _beyond_head(bottom, state.head, close):
            return state, False
    state.end_dt = _ts(df_all.iloc[-1]["datetime"])
    return state, True


def probe_large_patterns(
    symbol: str,
    duration_sec: int,
    df_all: pd.DataFrame,
    *,
    probe_bars: int = PROBE_BARS,
    strategy_factory: Callable[[int], Any] | None = None,
) -> list[LargePattern]:
    """往回最多探 `probe_bars` 根，找出这个周期上当前还活着的大形态。

    形态只在「右肩 = 当根」时成立，所以探针按 `end` 逐根截断喂 `on_bar`，
    命中的那个 `end` 就是右肩；再从那根往后走收盘价，补出突破/失效状态。
    """
    if df_all is None or len(df_all) < 2:
        return []
    from core.hs_monitor import TF_CONFIGS, make_strategy  # 延迟导入：hs_monitor 反过来用本模块

    strat = (strategy_factory or make_strategy)(duration_sec)
    cfg = TF_CONFIGS.get(duration_sec)
    lookback = cfg.lookback_bars if cfg else len(df_all)
    min_len = strat.max_span_bars + strat.pivot_order * 2 + _MIN_LEN_PAD
    n = len(df_all)
    if n < min_len:
        return []
    lowest_end = max(min_len - 1, n - 1 - max(0, int(probe_bars)))
    out: list[LargePattern] = []
    for end in range(n - 1, lowest_end - 1, -1):
        win = df_all.iloc[max(0, end + 1 - lookback) : end + 1]
        sig = strat.on_bar(win, max_volume=1, symbol=symbol)
        if sig is None:
            continue
        state = _state_of(symbol, duration_sec, sig, win)
        if state is None:
            continue
        state, alive = _advance(state, df_all, end)
        if alive:
            out.append(state)
            if len(out) >= MAX_PER_TF:
                break
    return out


class ConflictJudge:
    """信号路径上的周期冲突判定器：按大周期最新根缓存探针结果。

    只在有信号要发的时候才被调，一次最多几条大周期形态；水位没前进就吃缓存，
    所以每个大周期每根最多重算一次。启动即有状态（探针纯按库里历史算，不靠进程里等）。
    """

    def __init__(
        self,
        larger_map: dict[int, list[int]],
        load_df: Callable[[str, int], pd.DataFrame],
        *,
        probe_bars: int = PROBE_BARS,
        strategy_factory: Callable[[int], Any] | None = None,
    ) -> None:
        self.larger_map = larger_map
        self.load_df = load_df
        self.probe_bars = probe_bars
        self.strategy_factory = strategy_factory
        self._cache: dict[tuple[str, int], tuple[datetime, list[LargePattern]]] = {}
        self.loads = 0
        self.probes = 0

    def states(self, symbol: str, duration_sec: int) -> list[LargePattern]:
        df = self.load_df(symbol, duration_sec)
        if df is None or df.empty:
            return []
        last_dt = _ts(df.iloc[-1]["datetime"])
        hit = self._cache.get((symbol, duration_sec))
        if hit is not None and hit[0] == last_dt:
            return hit[1]
        found = probe_large_patterns(
            symbol,
            duration_sec,
            df,
            probe_bars=self.probe_bars,
            strategy_factory=self.strategy_factory,
        )
        self.loads += 1
        self._cache[(symbol, duration_sec)] = (last_dt, found)
        return found

    def __call__(self, symbol: str, duration_sec: int, detail: dict[str, Any], bar_dt: datetime) -> dict[str, Any] | None:
        larges: list[LargePattern] = []
        for big in self.larger_map.get(duration_sec, ()):
            larges.extend(self.states(symbol, big))
        if not larges:
            return None
        self.probes += 1
        return judge_many(small_signal_of(detail, bar_dt), larges)
