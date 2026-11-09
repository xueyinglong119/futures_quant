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

# 1 小时 K 线（有效交易约 60 分钟凑一根；末根 14:15-15:00 为 45 分钟）
DAY_1H_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (time(10, 0), [(time(9, 0), time(10, 0))]),
    # 跨小节休市：10:00-10:15 + 10:30-11:15
    (time(11, 15), [(time(10, 0), time(10, 15)), (time(10, 30), time(11, 15))]),
    # 跨午休：11:15-11:30 + 13:30-14:15
    (time(14, 15), [(time(11, 15), time(11, 30)), (time(13, 30), time(14, 15))]),
    (time(15, 0), [(time(14, 15), time(15, 0))]),
]

NIGHT_1H_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (time(22, 0), [(time(21, 0), time(22, 0))]),
    (time(23, 0), [(time(22, 0), time(23, 0))]),
]

# 2 小时：相邻两根会话 1h 合并（日盘约 120/105 分钟）
DAY_2H_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (
        time(11, 15),
        [
            (time(9, 0), time(10, 0)),
            (time(10, 0), time(10, 15)),
            (time(10, 30), time(11, 15)),
        ],
    ),
    (
        time(15, 0),
        [
            (time(11, 15), time(11, 30)),
            (time(13, 30), time(15, 0)),
        ],
    ),
]

NIGHT_2H_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (time(23, 0), [(time(21, 0), time(23, 0))]),
]

# 3 小时：前三根会话 1h + 日盘末根剩余
DAY_3H_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (
        time(14, 15),
        [
            (time(9, 0), time(10, 0)),
            (time(10, 0), time(10, 15)),
            (time(10, 30), time(11, 15)),
            (time(11, 15), time(11, 30)),
            (time(13, 30), time(14, 15)),
        ],
    ),
    (time(15, 0), [(time(14, 15), time(15, 0))]),
]

NIGHT_3H_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    # 夜盘仅 120 分钟，仍落一根标注 23:00，避免夜盘数据丢失
    (time(23, 0), [(time(21, 0), time(23, 0))]),
]

# 4 小时：整段日盘一根（有效约 225 分钟 < 240）
DAY_4H_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (
        time(15, 0),
        [
            (time(9, 0), time(10, 15)),
            (time(10, 30), time(11, 30)),
            (time(13, 30), time(15, 0)),
        ],
    ),
]

NIGHT_4H_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (time(23, 0), [(time(21, 0), time(23, 0))]),
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


def get_day_1h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in DAY_1H_BAR_DEFS
    ]


def get_night_1h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in NIGHT_1H_BAR_DEFS
    ]


def get_day_2h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in DAY_2H_BAR_DEFS
    ]


def get_night_2h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in NIGHT_2H_BAR_DEFS
    ]


def get_day_3h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in DAY_3H_BAR_DEFS
    ]


def get_night_3h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in NIGHT_3H_BAR_DEFS
    ]


def get_day_4h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in DAY_4H_BAR_DEFS
    ]


def get_night_4h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in NIGHT_4H_BAR_DEFS
    ]


def _schedule_text(
    title: str,
    day_defs: list[SessionBarDef],
    night_defs: list[SessionBarDef],
) -> str:
    lines = [
        title,
        "【日盘】",
    ]
    prev = time(9, 0)
    for bar in day_defs:
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
    for bar in night_defs:
        seg_text = " + ".join(
            f"{s.strftime('%H:%M')}-{e.strftime('%H:%M')}" for s, e in bar.segments
        )
        lines.append(
            f"  {prev.strftime('%H:%M')} → {bar.label.strftime('%H:%M')} "
            f"(成交 {seg_text})"
        )
        prev = bar.label
    return "\n".join(lines)


def get_day_30min_schedule_text() -> str:
    return _schedule_text(
        "国内商品期货 30 分钟 K 线：datetime_start 为上一根结束时刻，datetime_end 为本根结束时刻",
        get_day_30min_bar_defs(),
        get_night_30min_bar_defs(),
    )


def get_day_1h_schedule_text() -> str:
    return _schedule_text(
        "国内商品期货 1 小时 K 线：按有效交易时长对齐，datetime_end 为盘面标注时刻",
        get_day_1h_bar_defs(),
        get_night_1h_bar_defs(),
    )


def get_day_2h_schedule_text() -> str:
    return _schedule_text(
        "国内商品期货 2 小时 K 线：合并相邻会话 1h，datetime_end 为盘面标注时刻",
        get_day_2h_bar_defs(),
        get_night_2h_bar_defs(),
    )


def get_day_3h_schedule_text() -> str:
    return _schedule_text(
        "国内商品期货 3 小时 K 线：前三根会话 1h + 日盘末根剩余",
        get_day_3h_bar_defs(),
        get_night_3h_bar_defs(),
    )


def get_day_4h_schedule_text() -> str:
    return _schedule_text(
        "国内商品期货 4 小时 K 线：整段日盘一根（约 225 分钟）",
        get_day_4h_bar_defs(),
        get_night_4h_bar_defs(),
    )


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


def get_session_1h_trigger_minutes() -> frozenset[time]:
    """1 分钟走到这些时刻时，可能刚完成一根新的 1 小时 bar。"""
    return frozenset(
        {
            time(9, 59),
            time(10, 14),
            time(11, 14),
            time(14, 14),
            time(14, 59),
            time(21, 59),
            time(22, 59),
        }
    )


def get_session_2h_trigger_minutes() -> frozenset[time]:
    return frozenset({time(11, 14), time(14, 59), time(22, 59)})


def get_session_3h_trigger_minutes() -> frozenset[time]:
    return frozenset({time(14, 14), time(14, 59), time(22, 59)})


def get_session_4h_trigger_minutes() -> frozenset[time]:
    return frozenset({time(14, 59), time(22, 59)})


def get_all_session_trigger_minutes() -> frozenset[time]:
    """任一会话周期（30m/1h/2h/3h/4h）可能收盘的 1m 时刻。"""
    return (
        get_session_bar_trigger_minutes()
        | get_session_1h_trigger_minutes()
        | get_session_2h_trigger_minutes()
        | get_session_3h_trigger_minutes()
        | get_session_4h_trigger_minutes()
    )


def aggregate_session_tf_bars(
    minute_bars: list[dict],
    trade_date: date,
    day_defs: list[SessionBarDef],
    night_defs: list[SessionBarDef],
    *,
    time_label: str = "end",
    include_night: bool = True,
) -> list[dict]:
    """从 1 分钟按给定小节规则合成 K 线。

    minute_bars 每项需含: datetime(datetime), open, high, low, close, volume, open_oi
    其中 datetime 须为北京时间。
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
    for bar_def in day_defs:
        matched = _select_minutes(day_minutes, bar_def.segments)
        if not matched:
            continue
        result.append(
            _build_session_bar(trade_date, prev_label, bar_def, matched, time_label)
        )
        prev_label = bar_def.label

    if include_night and night_minutes:
        prev_label = day_defs[-1].label if day_defs else time(15, 0)
        for bar_def in night_defs:
            matched = _select_minutes(night_minutes, bar_def.segments)
            if not matched:
                continue
            result.append(
                _build_session_bar(trade_date, prev_label, bar_def, matched, time_label)
            )
            prev_label = bar_def.label

    return result


def aggregate_session_bars(
    minute_bars: list[dict],
    trade_date: date,
    *,
    time_label: str = "end",
    include_night: bool = True,
) -> list[dict]:
    """从 1 分钟按国内期货 30 分钟规则合成（日盘 8 根 + 夜盘 4 根）。"""
    return aggregate_session_tf_bars(
        minute_bars,
        trade_date,
        get_day_30min_bar_defs(),
        get_night_30min_bar_defs(),
        time_label=time_label,
        include_night=include_night,
    )


def aggregate_session_1h_bars(
    minute_bars: list[dict],
    trade_date: date,
    *,
    time_label: str = "end",
    include_night: bool = True,
) -> list[dict]:
    """从 1 分钟按国内期货 1 小时规则合成（日盘 4 根 + 夜盘 2 根）。"""
    return aggregate_session_tf_bars(
        minute_bars,
        trade_date,
        get_day_1h_bar_defs(),
        get_night_1h_bar_defs(),
        time_label=time_label,
        include_night=include_night,
    )


def aggregate_session_2h_bars(
    minute_bars: list[dict],
    trade_date: date,
    *,
    time_label: str = "end",
    include_night: bool = True,
) -> list[dict]:
    """从 1 分钟按国内期货 2 小时规则合成（日盘 2 根 + 夜盘 1 根）。"""
    return aggregate_session_tf_bars(
        minute_bars,
        trade_date,
        get_day_2h_bar_defs(),
        get_night_2h_bar_defs(),
        time_label=time_label,
        include_night=include_night,
    )


def aggregate_session_3h_bars(
    minute_bars: list[dict],
    trade_date: date,
    *,
    time_label: str = "end",
    include_night: bool = True,
) -> list[dict]:
    """从 1 分钟按国内期货 3 小时规则合成。"""
    return aggregate_session_tf_bars(
        minute_bars,
        trade_date,
        get_day_3h_bar_defs(),
        get_night_3h_bar_defs(),
        time_label=time_label,
        include_night=include_night,
    )


def aggregate_session_4h_bars(
    minute_bars: list[dict],
    trade_date: date,
    *,
    time_label: str = "end",
    include_night: bool = True,
) -> list[dict]:
    """从 1 分钟按国内期货 4 小时规则合成（日盘整段 1 根）。"""
    return aggregate_session_tf_bars(
        minute_bars,
        trade_date,
        get_day_4h_bar_defs(),
        get_night_4h_bar_defs(),
        time_label=time_label,
        include_night=include_night,
    )


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
