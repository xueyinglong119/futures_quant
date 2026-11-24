"""头肩告警的形态 K 线图片。

蜡烛图 + 左肩/头/右肩标记 + 颈线虚线。图内文本全 ASCII，
避免容器镜像缺中文字体时豆腐块。
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from core.kline_store import DURATION_LABELS, load_bars

UP = "#D64B4B"
DOWN = "#2FA46A"
MARK = "#E8B86D"
NECK = "#8FA6BC"
BG = "#0E1A24"


def _parse_detail_time(text: Any, ref: datetime) -> datetime | None:
    """解析 detail 时间：常规告警是 MM-DD HH:MM；冲突大周期图是 ISO。

    MM-DD 缺年时用 ref 的年份；比参考时间晚一天以上视为跨年。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    # large_chart_detail / right_time 走 ISO（含 "T" 或完整年月日）
    if "T" in raw or (raw.count("-") >= 2 and len(raw) >= 16):
        try:
            return datetime.fromisoformat(raw.replace("Z", ""))
        except ValueError:
            pass
    try:
        dt = datetime.strptime(raw, "%m-%d %H:%M").replace(year=ref.year)
    except ValueError:
        return None
    if dt > ref + timedelta(days=1):
        dt = dt.replace(year=ref.year - 1)
    return dt


def _index_of(times: list[datetime], target: datetime | None) -> int | None:
    """精确命中；否则取最近一根（冲突大图的头时间曾用中点近似，对不上整点）。"""
    if target is None or not times:
        return None
    best_i = 0
    best_d = abs(times[0] - target)
    for i, t in enumerate(times):
        if t == target:
            return i
        d = abs(t - target)
        if d < best_d:
            best_d = d
            best_i = i
    # 最近根也不能离谱：超过 1.5 根周期时长就当没找到
    return best_i


def _head_idx_by_price(
    bars: list[dict[str, Any]],
    left_i: int,
    right_i: int,
    head_price: float,
    *,
    bottom: bool,
) -> int | None:
    """旧告警无真实头时刻时：在 L/R 之间找收盘最接近头价（顶取高、底取低）的那根。"""
    if right_i <= left_i + 1:
        return None
    best_i: int | None = None
    best_score: float | None = None
    for i in range(left_i + 1, right_i):
        close = float(bars[i]["close"])
        # 优先贴近头价；同距时顶偏好更高收盘、底偏好更低
        dist = abs(close - head_price)
        tie = -close if not bottom else close
        score = (dist, tie)
        if best_score is None or score < best_score:
            best_score = score
            best_i = i
    return best_i


def render_hs_chart(
    symbol: str,
    duration_sec: int,
    bar_time: str,
    detail: dict[str, Any],
    conn: Any,
    future_bars: int = 6,
) -> bytes | None:
    try:
        ref = datetime.fromisoformat(bar_time)
    except ValueError:
        return None
    left_t = _parse_detail_time(detail.get("left_time"), ref)
    if left_t is None:
        return None
    start = left_t - 20 * timedelta(seconds=duration_sec)
    end = ref + future_bars * timedelta(seconds=duration_sec)
    bars = load_bars(conn, symbol, duration_sec, start=start, end=end)
    if not bars:
        return None
    times = [b["datetime_end"] for b in bars]
    n = len(times)

    fig, ax = plt.subplots(figsize=(7.2, 4.0), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    span = max(max(b["high"] for b in bars) - min(b["low"] for b in bars), 1e-9)
    for i, b in enumerate(bars):
        o, h, l, c = b["open"], b["high"], b["low"], b["close"]
        color = UP if c >= o else DOWN
        ax.vlines(i, l, h, color=color, linewidth=0.8)
        body = abs(c - o) or span * 0.01
        ax.add_patch(
            Rectangle(
                (i - 0.35, min(o, c)),
                0.7,
                body,
                facecolor=color,
                edgecolor=color,
                linewidth=0.6,
            )
        )

    side = str(detail.get("side") or "")
    bottom = side == "long"
    # 三重的三个点是近似等高的顶/底，标号跟着规格的命名走，别写成肩头肩
    triple = str(detail.get("pattern_kind") or "").startswith("三重")
    labels = ("L1", "L2", "L3") if triple and side != "short" else ("P1", "P2", "P3")
    # 先解析 L/R，头点可按价位兜底
    left_i = _index_of(times, _parse_detail_time(detail.get("left_time"), ref))
    right_i = _index_of(times, _parse_detail_time(detail.get("right_time"), ref))
    for key, label, above in (
        ("left", labels[0] if triple else "L", side != "short"),
        ("head", labels[1] if triple else "H", side != "short"),
        ("right", labels[2] if triple else "R", side != "short"),
    ):
        price = detail.get(key)
        if not isinstance(price, (int, float)):
            continue
        t = _parse_detail_time(detail.get(f"{key}_time"), ref)
        idx = _index_of(times, t)
        if key == "head" and left_i is not None and right_i is not None:
            # 中点近似常落在空档：用 L/R 之间最贴近头价的收盘根
            by_price = _head_idx_by_price(
                bars, left_i, right_i, float(price), bottom=bottom
            )
            if by_price is not None:
                if idx is None or abs(bars[by_price]["close"] - price) <= abs(
                    bars[idx]["close"] - price
                ):
                    idx = by_price
        if idx is None:
            continue
        ax.scatter([idx], [price], s=70, facecolors="none", edgecolors=MARK, linewidths=1.6)
        ax.annotate(
            label,
            (idx, price),
            textcoords="offset points",
            xytext=(0, 10 if above else -16),
            color=MARK,
            fontsize=10,
            fontweight="bold",
            ha="center",
        )

    nl = _index_of(times, _parse_detail_time(detail.get("neck_l_time"), ref))
    nr = _index_of(times, _parse_detail_time(detail.get("neck_r_time"), ref))
    if nl is not None and nr is not None and nr > nl:
        pick = "close"
        slope = (bars[nr][pick] - bars[nl][pick]) / (nr - nl)
        ax.plot(
            [nl, nr],
            [bars[nl][pick], bars[nl][pick] + slope * (nr - nl)],
            linestyle="--",
            color=NECK,
            linewidth=1.2,
        )

    ticks = sorted({round(i * (n - 1) / 5) for i in range(6)}) if n > 1 else [0]
    ax.set_xticks(ticks)
    ax.set_xticklabels([times[i].strftime("%m-%d %H:%M") for i in ticks], fontsize=8)
    ax.tick_params(axis="y", labelsize=8, colors="#9BB0C1")
    ax.tick_params(axis="x", colors="#9BB0C1")
    ax.grid(alpha=0.15)
    for edge in ax.spines.values():
        edge.set_color("#24384A")
    title = f"{symbol} {DURATION_LABELS.get(duration_sec, duration_sec)} "
    if triple:
        title += "TRIPLE TOP" if side == "short" else "TRIPLE BOTTOM"
    else:
        title += "H&S TOP" if side == "short" else "H&S BOTTOM"
    ax.set_title(title, fontsize=10, color="#D7E0E8")
    ax.set_xlim(-1, n)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()
