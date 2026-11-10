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
    """detail 里的 MM-DD HH:MM 补上年份；比参考时间晚一天以上视为跨年。"""
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.strptime(text, "%m-%d %H:%M").replace(year=ref.year)
    except ValueError:
        return None
    if dt > ref + timedelta(days=1):
        dt = dt.replace(year=ref.year - 1)
    return dt


def _index_of(times: list[datetime], target: datetime | None) -> int | None:
    if target is None:
        return None
    for i, t in enumerate(times):
        if t == target:
            return i
    return None


def render_hs_chart(
    symbol: str,
    duration_sec: int,
    bar_time: str,
    detail: dict[str, Any],
    conn: Any,
) -> bytes | None:
    try:
        ref = datetime.fromisoformat(bar_time)
    except ValueError:
        return None
    left_t = _parse_detail_time(detail.get("left_time"), ref)
    if left_t is None:
        return None
    start = left_t - 20 * timedelta(seconds=duration_sec)
    end = ref + 6 * timedelta(seconds=duration_sec)
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
    for key, label, above in (
        ("left", "L", side != "short"),
        ("head", "H", side != "short"),
        ("right", "R", side != "short"),
    ):
        t = _parse_detail_time(detail.get(f"{key}_time"), ref)
        price = detail.get(key)
        idx = _index_of(times, t)
        if idx is None or not isinstance(price, (int, float)):
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
        pick = "low" if side == "short" else "high"
        ax.plot(
            [nl, nr],
            [bars[nl][pick], bars[nr][pick]],
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
    title += "H&S TOP" if side == "short" else "H&S BOTTOM"
    ax.set_title(title, fontsize=10, color="#D7E0E8")
    ax.set_xlim(-1, n)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()
