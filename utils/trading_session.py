"""国内商品期货日盘交易时段与 K 线切分规则"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

# 夜盘连续竞价（燃料油/低硫燃油/甲醇等：21:00-23:00）
NIGHT_SESSION_SEGMENTS: list[tuple[time, time]] = [
    (time(21, 0), time(23, 0)),
]

NIGHT_30MIN_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (time(21, 30), [(time(21, 0), time(21, 30))]),
    (time(22, 0), [(time(21, 30), time(22, 0))]),
    (time(22, 30), [(time(22, 0), time(22, 30))]),
    (time(23, 0), [(time(22, 30), time(23, 0))]),
]

# 商品期货日盘连续竞价（含小节休市 10:15-10:30）
DAY_SESSION_SEGMENTS: list[tuple[time, time]] = [
    (time(9, 0), time(10, 15)),
    (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)),
]

# 30 分钟 K 线：每根 bar 由若干实际交易片段组成；label 为国内软件常用标注时刻
DAY_30MIN_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (time(9, 30), [(time(9, 0), time(9, 30))]),
    (time(10, 0), [(time(9, 30), time(10, 0))]),
    # 跨 10:15-10:30 小节休市：10:00-10:15 + 10:30-10:45
    (time(10, 45), [(time(10, 0), time(10, 15)), (time(10, 30), time(10, 45))]),
    (time(11, 15), [(time(10, 45), time(11, 15))]),
    # 跨午休：11:15-11:30(15分) + 13:30-13:45(15分) = 30 分钟有效成交
    (time(13, 45), [(time(11, 15), time(11, 30)), (time(13, 30), time(13, 45))]),
    (time(14, 15), [(time(13, 45), time(14, 15))]),
    (time(14, 45), [(time(14, 15), time(14, 45))]),
    # 日盘最后一根：14:45-15:00（15 分钟有效成交，结束标注 15:00）
    (time(15, 0), [(time(14, 45), time(15, 0))]),
]


@dataclass(frozen=True)
class SessionBarDef:
    label: time
    segments: tuple[tuple[time, time], ...]


def get_night_30min_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in NIGHT_30MIN_BAR_DEFS
    ]


def get_day_30min_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in DAY_30MIN_BAR_DEFS
    ]


def get_day_30min_schedule_text() -> str:
    lines = [
        "国内商品期货 30 分钟 K 线：datetime_start 为上一根结束时刻，datetime_end 为本根结束时刻",
        "【日盘】",
    ]
    prev = time(9, 0)
    for bar in get_day_30min_bar_defs():
        seg_text = " + ".join(
            f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in bar.segments
        )
        lines.append(
            f"  {prev.strftime('%H:%M')} → {bar.label.strftime('%H:%M')} "
            f"(成交 {seg_text})"
        )
        prev = bar.label
    lines.append("【夜盘 21:00-23:00】")
    prev = time(15, 0)
    for bar in get_night_30min_bar_defs():
        seg_text = " + ".join(
            f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in bar.segments
        )
        lines.append(
            f"  {prev.strftime('%H:%M')} → {bar.label.strftime('%H:%M')} "
            f"(成交 {seg_text})"
        )
        prev = bar.label
    return "\n".join(lines)


def _combine(dt: date, t: time) -> datetime:
    return datetime.combine(dt, t)


def _in_segment(t: time, start: time, end: time) -> bool:
    return start <= t < end


def get_session_bar_trigger_minutes() -> frozenset[time]:
    """1 分钟 K 线走到这些时刻时，可能刚完成一根新的 30 分钟 bar。"""
    return frozenset(
        {
            time(9, 29),
            time(9, 59),
            time(10, 14),
            time(10, 44),
            time(11, 14),
            time(11, 29),
            time(13, 44),
            time(14, 14),
            time(14, 44),
            time(14, 59),
            time(21, 29),
            time(21, 59),
            time(22, 29),
            time(22, 59),
        }
    )


def aggregate_session_bars(
    minute_bars: list[dict],
    trade_date: date,
    *,
    time_label: str = "end",
    include_night: bool = True,
) -> list[dict]:
    """
    从 1 分钟 K 线按国内期货 30 分钟规则合成 K 线（日盘 8 根 + 夜盘 4 根）。

    minute_bars 每项需含: datetime(datetime), open, high, low, close, volume, open_oi
    """
    day_minutes = [
        b
        for b in minute_bars
        if b["datetime"].date() == trade_date
        and _is_in_day_session(b["datetime"].time())
    ]
    night_minutes = (
        [
            b
            for b in minute_bars
            if b["datetime"].date() == trade_date
            and _is_in_night_session(b["datetime"].time())
        ]
        if include_night
        else []
    )

    result: list[dict] = []
    prev_label = time(9, 0)
    for bar_def in get_day_30min_bar_defs():
        matched = _select_minutes(day_minutes, bar_def.segments)
        if not matched:
            continue
        result.append(
            _build_session_bar(trade_date, prev_label, bar_def, matched, time_label)
        )
        prev_label = bar_def.label

    if include_night and night_minutes:
        prev_label = get_day_30min_bar_defs()[-1].label  # 15:00
        for bar_def in get_night_30min_bar_defs():
            matched = _select_minutes(night_minutes, bar_def.segments)
            if not matched:
                continue
            result.append(
                _build_session_bar(trade_date, prev_label, bar_def, matched, time_label)
            )
            prev_label = bar_def.label

    return result


def _build_session_bar(
    trade_date: date,
    prev_label: time,
    bar_def: SessionBarDef,
    matched: list[dict],
    time_label: str,
) -> dict:
    dt_start = _combine(trade_date, prev_label)
    dt_end = _combine(trade_date, bar_def.label)
    label = dt_end if time_label == "end" else dt_start
    return {
        "datetime": label.isoformat(timespec="seconds"),
        "datetime_start": dt_start.isoformat(timespec="seconds"),
        "datetime_end": dt_end.isoformat(timespec="seconds"),
        "open": float(matched[0]["open"]),
        "high": max(float(m["high"]) for m in matched),
        "low": min(float(m["low"]) for m in matched),
        "close": float(matched[-1]["close"]),
        "volume": sum(int(m["volume"]) for m in matched),
        "open_oi": int(matched[-1]["open_oi"]),
    }


def _is_in_day_session(t: time) -> bool:
    for start, end in DAY_SESSION_SEGMENTS:
        if _in_segment(t, start, end):
            return True
    return False


def _is_in_night_session(t: time) -> bool:
    for start, end in NIGHT_SESSION_SEGMENTS:
        if _in_segment(t, start, end):
            return True
    return False


def _select_minutes(
    day_minutes: list[dict],
    segments: tuple[tuple[time, time], ...],
) -> list[dict]:
    matched: list[dict] = []
    for bar in day_minutes:
        t = bar["datetime"].time()
        for start, end in segments:
            if _in_segment(t, start, end):
                matched.append(bar)
                break
    matched.sort(key=lambda x: x["datetime"])
    return matched
