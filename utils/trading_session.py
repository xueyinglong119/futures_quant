"""国内商品期货日盘交易时段与 K 线切分规则"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# 夜盘连续竞价分两段：当晚 21:00 起、次日凌晨 02:30 前止。
# 多数品种 23:00 收夜盘（燃料油/甲醇等），有色到 01:00，贵金属与原油到 02:30，
# 所以整段都定义出来，实际没交易的钟点选不到分钟线、自然不落根。
NIGHT_EVENING: tuple[time, time] = (time(21, 0), time(23, 59, 59))
NIGHT_TAIL: tuple[time, time] = (time(0, 0), time(2, 30))
NIGHT_SESSION_SEGMENTS: list[tuple[time, time]] = [NIGHT_EVENING, NIGHT_TAIL]

NIGHT_30MIN_BAR_DEFS: list[tuple[time, list[tuple[time, time]]]] = [
    (time(21, 30), [(time(21, 0), time(21, 30))]),
    (time(22, 0), [(time(21, 30), time(22, 0))]),
    (time(22, 30), [(time(22, 0), time(22, 30))]),
    (time(23, 0), [(time(22, 30), time(23, 0))]),
    (time(23, 30), [(time(23, 0), time(23, 30))]),
    # 23:30-00:00 这根正好落在零点前（23:30~23:59 共 30 分钟），不跨零点
    (time(0, 0), [(time(23, 30), NIGHT_EVENING[1])]),
    (time(0, 30), [(time(0, 0), time(0, 30))]),
    (time(1, 0), [(time(0, 30), time(1, 0))]),
    (time(1, 30), [(time(1, 0), time(1, 30))]),
    (time(2, 0), [(time(1, 30), time(2, 0))]),
    (time(2, 30), [(time(2, 0), time(2, 30))]),
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
    (time(0, 0), [(time(23, 0), NIGHT_EVENING[1])]),
    (time(1, 0), [(time(0, 0), time(1, 0))]),
    (time(2, 0), [(time(1, 0), time(2, 0))]),
    # 贵金属/原油夜盘到 02:30：末根只有 30 分钟，标注真实收线时刻
    (time(2, 30), [(time(2, 0), time(2, 30))]),
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
    # 23:00-01:00 跨零点：当晚 23:00~23:59 与次日 00:00~00:59 合成一根
    (
        time(1, 0),
        [(time(23, 0), NIGHT_EVENING[1]), (time(0, 0), time(1, 0))],
    ),
    # 贵金属/原油到 02:30：末根 90 分钟，标注真实收线时刻
    (time(2, 30), [(time(1, 0), time(2, 30))]),
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

# 3h/4h 的夜盘按实际分钟数顺序切段：每段最多这么多根 1 分钟线，最后一段不足也算一根，
# 收线时刻取该段最后一根分钟线的下一分钟。夜盘 120 分钟时结果与旧的「整夜一根标 23:00」
# 完全一致，夜盘拉长到 240/330 分钟才会多落根，不会把 23:00 之后的成交丢掉。
NIGHT_3H_CHUNK_MINUTES = 180
NIGHT_4H_CHUNK_MINUTES = 240


@dataclass(frozen=True)
class SessionBarDef:
    label: time | None
    segments: tuple[tuple[time, time], ...] = ()
    # >0 表示「按分钟数顺序切段」：整段夜盘每 chunk_minutes 根切一根，末段不足也算一根，
    # 收线时刻由实际分钟线推出来，不用固定钟点（3h/4h 的夜盘用这个）。
    chunk_minutes: int = 0


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
    """夜盘 3h 不排固定钟点：整夜按 180 分钟顺序切段，末段不足也算一根。"""
    return [SessionBarDef(label=None, chunk_minutes=NIGHT_3H_CHUNK_MINUTES)]


def get_day_4h_bar_defs() -> list[SessionBarDef]:
    return [
        SessionBarDef(label=label, segments=tuple(segs))
        for label, segs in DAY_4H_BAR_DEFS
    ]


def get_night_4h_bar_defs() -> list[SessionBarDef]:
    """夜盘 4h 同 3h：整夜按 240 分钟顺序切段，不足一整段也算一根。"""
    return [SessionBarDef(label=None, chunk_minutes=NIGHT_4H_CHUNK_MINUTES)]


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
    lines.append("【夜盘 21:00 起，短到 23:00、有色到 01:00、贵金属与原油到 02:30】")
    prev = time(15, 0)
    for bar in night_defs:
        if bar.label is None:
            lines.append(
                f"  {prev.strftime('%H:%M')} → 整夜按 {bar.chunk_minutes} 分钟顺序切段，"
                "末段不足也算一根，收线时刻用实际分钟推"
            )
            break
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
            # 有色到 01:00、贵金属与原油到 02:30，后半夜这些分钟收线时才算完成一根
            time(23, 29),
            time(23, 59),
            time(0, 29),
            time(0, 59),
            time(1, 29),
            time(1, 59),
            time(2, 29),
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
            time(23, 59),
            time(0, 59),
            time(1, 59),
            time(2, 29),
        }
    )


def get_session_2h_trigger_minutes() -> frozenset[time]:
    return frozenset(
        {time(11, 14), time(14, 59), time(22, 59), time(0, 59), time(2, 29)}
    )


def get_session_3h_trigger_minutes() -> frozenset[time]:
    return frozenset(
        {time(14, 14), time(14, 59), time(22, 59), time(23, 59), time(0, 59), time(2, 29)}
    )


def get_session_4h_trigger_minutes() -> frozenset[time]:
    return frozenset({time(14, 59), time(22, 59), time(0, 59), time(2, 29)})


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
        [b for b in minute_bars if _is_night_minute(b["datetime"], trade_date)]
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
            if bar_def.chunk_minutes:
                # 3h/4h：整夜顺序切段，收线时刻由实际分钟线推，不用固定钟点
                chunked, prev_label = _chunk_night_bars(
                    night_minutes, trade_date, prev_label, bar_def, time_label
                )
                result.extend(chunked)
                continue
            matched = _select_minutes(night_minutes, bar_def.segments)
            if not matched:
                continue
            result.append(
                _build_night_bar(trade_date, prev_label, bar_def, matched, time_label)
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


def _bar_payload(
    dt_start: datetime,
    dt_end: datetime,
    matched: list[dict],
    time_label: str,
) -> dict:
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


def _build_session_bar(
    trade_date: date,
    prev_label: time,
    bar_def: SessionBarDef,
    matched: list[dict],
    time_label: str,
) -> dict:
    dt_start = _combine(trade_date, prev_label)
    dt_end = _combine(trade_date, bar_def.label)
    return _bar_payload(dt_start, dt_end, matched, time_label)


def _night_date(trade_date: date, label: time) -> date:
    """夜盘标签落在凌晨 ⇒ 那是次日的钟点，日期进一位（23:00 那类仍是当晚）。"""
    return trade_date + timedelta(days=1) if label < time(12, 0) else trade_date


def _build_night_bar(
    trade_date: date,
    prev_label: time,
    bar_def: SessionBarDef,
    matched: list[dict],
    time_label: str,
) -> dict:
    dt_start = _combine(_night_date(trade_date, prev_label), prev_label)
    dt_end = _combine(_night_date(trade_date, bar_def.label), bar_def.label)
    return _bar_payload(dt_start, dt_end, matched, time_label)


def _chunk_night_bars(
    night_minutes: list[dict],
    trade_date: date,
    prev_label: time,
    bar_def: SessionBarDef,
    time_label: str,
) -> tuple[list[dict], time]:
    """3h/4h 的夜盘：整夜按顺序每 chunk_minutes 根切一根，末段不足也算一根。

    收线时刻用该段最后一根分钟线的下一分钟，所以 120 分钟的夜盘仍是一根 23:00，
    240/330 分钟的夜盘才会多落根——固定钟点要么给短夜盘贴错标签，要么把后半夜并进次日日盘。
    """
    mins = sorted(night_minutes, key=lambda b: b["datetime"])
    size = bar_def.chunk_minutes
    out: list[dict] = []
    dt_start = _combine(trade_date, prev_label)
    for i in range(0, len(mins), size):
        chunk = mins[i : i + size]
        if out:
            dt_start = datetime.fromisoformat(out[-1]["datetime_end"])
        dt_end = chunk[-1]["datetime"] + timedelta(minutes=1)
        out.append(_bar_payload(dt_start, dt_end, chunk, time_label))
    return out, (dt_end.time() if out else prev_label)


def _is_night_minute(dt: datetime, trade_date: date) -> bool:
    """这根分钟线属不属于 trade_date 当晚开的那一夜。

    一夜 = 当晚 21:00 起 + 次日凌晨 02:30 前的尾巴：有色 23:00 之后还要交易到 01:00、
    贵金属与原油到 02:30，按自然日分组会把跨零点那段留在「次日」那一组里，谁都不认，
    那几分钟就既不进当晚的会话 K，也不进次日日盘，直接消失。
    """
    if dt.date() == trade_date:
        return dt.time() >= NIGHT_EVENING[0] and _is_in_night_session(dt.time())
    return dt.date() == trade_date + timedelta(days=1) and _is_in_night_session(
        dt.time()
    )


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
