"""周期冲突本机自检：判定表 A–G、探针推进、发射路径与告警过滤。

不连库、不写真告警文件：探针用替身策略（头肩检测器自己有 `_hs_equiv.py` 那套覆盖），
发射路径把 `emit_alert` 换成记录器。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core.hs_monitor as hm
from core.alert_store import add_alert, clear_alerts, list_alerts
from core.tf_conflict import (
    BLOCK,
    LargePattern,
    judge_many,
    judge_nested_pattern,
    probe_large_patterns,
    small_signal_of,
)

FAILS: list[str] = []


def ok(name: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name}")
    if not cond:
        FAILS.append(name)


def _mk_df(n: int, closes: list[float]) -> pd.DataFrame:
    t0 = datetime(2026, 11, 2, 9, 0)
    return pd.DataFrame(
        {
            "datetime": [t0 + timedelta(hours=i) for i in range(n)],
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [100] * n,
            "open_oi": [1000] * n,
        }
    )


class _FakeSignal:
    def __init__(self, detail: dict) -> None:
        self.detail = detail
        self.reason = "假大形态"


class _FakeStrat:
    """只在指定的 end 上返回形态，其余返回 None：用来单测探针的外壳。"""

    max_span_bars = 110
    pivot_order = 4

    def __init__(self, fires_at: int, detail: dict) -> None:
        self.fires_at = fires_at
        self.detail = detail

    def on_bar(self, klines, max_volume=None, symbol=""):  # noqa: ANN001, ARG002
        if len(klines) - 1 != self.fires_at:
            return None
        return _FakeSignal(dict(self.detail))


def _bottom_detail(left_idx: int, right_idx: int) -> dict:
    return {
        "side": "long",
        "pattern_id": f"bottom:x:{right_idx}",
        "left": 100.0,
        "head": 80.0,
        "right": 101.0,
        "neck": 110.0,
        "swing": 25.0,
        "left_idx": left_idx,
        "head_idx": (left_idx + right_idx) // 2,
        "right_idx": right_idx,
    }


def test_spec_branches() -> None:
    t0 = datetime(2026, 11, 20, 9, 0)
    t230 = t0 + timedelta(hours=230)
    large = LargePattern(
        duration_sec=7200,
        pattern="head_shoulder_bottom",
        direction="long",
        left=9548,
        head=9040,
        right=9268,
        neck=9827,
        start_dt=t0,
        head_dt=t0 + timedelta(hours=100),
        right_dt=t230,
        end_dt=t230,
        breakout=False,
        breakout_dt=None,
    )
    small = {
        "pattern": "head_shoulder_top",
        "direction": "short",
        "ls_price": 9560,
        "head_price": 10050,
        "rs_price": 9555,
        "neck_price": 9268,
        "breakout_confirmed": False,
        "time_idx": t230,
    }
    v = judge_nested_pattern(small, large)
    ok("生猪示例走 D 分支 downgrade 0.4", v["action"] == "downgrade" and v["weight"] == 0.4)
    ok("生猪示例 conflict=True", v["conflict"] is True)

    same = dict(small, direction="long", pattern="head_shoulder_bottom")
    ok("A 方向一致 allow 1.0", judge_nested_pattern(same, large)["action"] == "allow")

    after = LargePattern(**{**large.__dict__, "start_dt": t230 + timedelta(hours=1)})
    ok("B 不在区间 allow 0.8", judge_nested_pattern(small, after)["weight"] == 0.8)

    broke = LargePattern(**{**large.__dict__, "breakout": True, "breakout_dt": t230})
    vb = judge_nested_pattern(small, broke)
    ok("C 大形态已确认 block 0.0", vb["action"] == BLOCK and vb["weight"] == 0.0)

    # E/F/G 要先越过 D（小形态已破颈）才可能到达：生产上右肩当根必然未破，
    # 这里手工造 breakout_confirmed=True 只证明分支本身写对了。
    popped = dict(small, breakout_confirmed=True)
    # E/F 要求大形态还没破颈（否则先被 C 拦下），小形态自己已破颈：生产上当根到不了，
    # 这里只证明这两条分支本身写对了、阈值算对了。
    near = 9400.0  # ≤ 大底 head 9040 × 1.05 = 9492
    ve = judge_nested_pattern(dict(popped, rs_price=near), large)
    ok("E 大底支撑附近 block", ve["action"] == BLOCK and "大底支撑" in ve["reason"])
    top = LargePattern(
        duration_sec=7200,
        pattern="head_shoulder_top",
        direction="short",
        left=10400,
        head=10900,
        right=10450,
        neck=11200,
        start_dt=t0,
        head_dt=t0 + timedelta(hours=100),
        right_dt=t230,
        end_dt=t230,
        breakout=False,
        breakout_dt=None,
    )
    long_small = dict(popped, direction="long", pattern="head_shoulder_bottom", rs_price=10400)
    vf = judge_nested_pattern(long_small, top)
    ok("F 大顶压力附近 block", vf["action"] == BLOCK and "大顶压力" in vf["reason"])
    far_small = dict(long_small, rs_price=9100)  # 离 10900×0.95=10355 还远
    vg = judge_nested_pattern(far_small, top)
    ok("G 其它冲突 downgrade 0.5", vg["action"] == "downgrade" and vg["weight"] == 0.5)


def test_worst_of_many() -> None:
    t0 = datetime(2026, 11, 20, 9, 0)
    small = {
        "pattern": "head_shoulder_top",
        "direction": "short",
        "ls_price": 1,
        "head_price": 2,
        "rs_price": 9555,
        "neck_price": 1,
        "breakout_confirmed": False,
        "time_idx": t0 + timedelta(hours=5),
    }
    mk = lambda **kw: LargePattern(  # noqa: E731
        duration_sec=kw.get("dur", 3600),
        pattern="head_shoulder_bottom",
        direction="long",
        left=1,
        head=1,
        right=1,
        neck=1,
        start_dt=t0,
        head_dt=t0 + timedelta(hours=3),
        right_dt=t0 + timedelta(hours=6),
        end_dt=t0 + timedelta(hours=6),
        breakout=kw.get("br", False),
        breakout_dt=None,
    )
    v = judge_many(small, [mk(), mk(dur=86400, br=True)])
    ok("多条大形态取最坏 block", v["action"] == BLOCK)
    ok("最坏结论权重取最小", v["weight"] == 0.0)
    ok("large 列表全带上", len(v["large"]) == 2)
    ok("reason 只列拦下来的那条", "1d" in v["reason"] and v["reason"].count("禁止") == 1)
    v2 = judge_many(small, [mk(), mk(dur=7200)])
    ok("同为降权取权重更小的一条", v2["weight"] == 0.4 and v2["action"] == "downgrade")


def test_probe_and_advance() -> None:
    n = 200
    closes = [100.0] * n  # 右肩之后仍在颈线 110 下方，先当候选
    detail = _bottom_detail(180 - 20, 180)  # 左肩 160、右肩 180
    df = _mk_df(n, closes)

    found = probe_large_patterns("X", 3600, df, probe_bars=40, strategy_factory=lambda d: _FakeStrat(180, detail))
    ok("探针找到 180 那根的大形态", len(found) == 1)
    ok("未破颈 → candidate", found and found[0].status == "candidate")
    ok("区间起点 = 左肩那根", found and found[0].start_dt == df.iloc[160]["datetime"])
    ok("区间终点 = 大周期当前根", found and found[0].end_dt == df.iloc[n - 1]["datetime"])

    up = list(closes)
    for i in range(190, n):
        up[i] = 111.0  # 右肩之后收越颈线 110
    found = probe_large_patterns("X", 3600, _mk_df(n, up), probe_bars=40, strategy_factory=lambda d: _FakeStrat(180, detail))
    ok("右肩后收越颈线 → confirmed", found and found[0].breakout and found[0].status == "confirmed")

    dead = list(closes)
    dead[195] = 79.0  # 收在头部 80 之下：结构破了
    found = probe_large_patterns("X", 3600, _mk_df(n, dead), probe_bars=40, strategy_factory=lambda d: _FakeStrat(180, detail))
    ok("收盘越过头部 → 形态作废不再参与", found == [])

    back = list(closes)
    for i in range(190, n):
        back[i] = 111.0
    back[197] = 109.0  # 破位后又收回颈线下方 → 假突破
    found = probe_large_patterns("X", 3600, _mk_df(n, back), probe_bars=40, strategy_factory=lambda d: _FakeStrat(180, detail))
    ok("破颈线后收回 → 形态作废", found == [])

    far = probe_large_patterns("X", 3600, _mk_df(n, closes), probe_bars=10, strategy_factory=lambda d: _FakeStrat(180, detail))
    ok("超出探针窗的老形态不参与（宁可不拦）", far == [])

    # 顶形态：头在颈线上方、收盘还在颈线上方（还没跌破）才叫候选
    top_detail = _bottom_detail(160, 180) | {
        "side": "short",
        "left": 119.0,
        "head": 130.0,
        "right": 121.0,
        "neck": 110.0,
    }
    got = probe_large_patterns(
        "X", 3600, _mk_df(n, [120.0] * n), probe_bars=40, strategy_factory=lambda d: _FakeStrat(180, top_detail)
    )
    ok("顶形态 direction=short 且未跌破颈线仍是候选", got and got[0].direction == "short" and got[0].status == "candidate")

    triple = _bottom_detail(160, 180) | {"pattern_kind": "三重底"}
    got = probe_large_patterns("X", 3600, df, probe_bars=40, strategy_factory=lambda d: _FakeStrat(180, triple))
    ok("三重顶底不参与冲突判断", got == [])


def test_small_signal_of() -> None:
    detail = _bottom_detail(160, 180)
    s = small_signal_of(detail, datetime(2026, 11, 23, 10, 0))
    ok("小信号右肩收盘未越颈 → 未突破", s["breakout_confirmed"] is False)
    ok("底 → direction long / pattern bottom", s["direction"] == "long" and s["pattern"] == "head_shoulder_bottom")
    popped = dict(detail, right=111.0)
    ok("小信号收盘越颈 → 已突破", small_signal_of(popped, datetime(2026, 11, 23))["breakout_confirmed"] is True)


def test_deliver_path() -> None:
    sent: list[dict] = []
    real_emit = hm.emit_alert

    def fake_emit(**kw):
        sent.append(kw)
        return f"id{len(sent)}"

    hm.emit_alert = fake_emit  # type: ignore[assignment]
    opened: list[str] = []
    try:
        det = hm.Detection(
            symbol="KQ.m@CZCE.PF",
            duration_sec=1800,
            last_dt=datetime(2026, 11, 23, 14, 0),
            kind="头肩顶",
            signal=_FakeSignal(_bottom_detail(10, 30) | {"side": "short"}),
            df=pd.DataFrame(),
            strict=True,
        )
        verdict = {"action": BLOCK, "weight": 0.0, "conflict": True, "reason": "1h 头肩底 大周期形态已确认", "large": []}

        eng = hm.HsMonitorEngine()
        eng.on_signal = lambda *a: opened.append("trade")
        eng.conflict_judge = lambda symbol, tf, detail, bar_dt: dict(verdict)
        n = eng.deliver(det)
        ok("block 计入两条告警", n == 2)
        ok("block 不开模拟盘", opened == [])
        ok("原告警带冲突前缀", sent[0]["reason"].startswith("[周期冲突·禁止开仓]"))
        ok("原告警带 verdict", sent[0]["conflict"]["action"] == BLOCK)
        ok("超严重单独一条", sent[1]["kind"] == hm.CONFLICT_KIND and sent[1]["conflict_severe"] is True)
        ok("超严重那条带小形态 detail（图/手动开仓要用）", sent[1]["detail"] is det.signal.detail)
        ok("日志标签分开", hm.alert_tag(det) == "严格告警·冲突禁止")

        sent.clear()
        opened.clear()
        eng2 = hm.HsMonitorEngine()
        eng2.on_signal = lambda *a: opened.append("trade")
        eng2.conflict_judge = lambda symbol, tf, detail, bar_dt: dict(verdict)
        eng2.deliver(det, provisional=True)
        ok("抢跑只标记，不再发超严重", len(sent) == 1)

        sent.clear()
        calls = {"n": 0}

        def counting_judge(symbol, tf, detail, bar_dt):
            calls["n"] += 1
            return {"action": "downgrade", "weight": 0.4, "conflict": True, "reason": "1h 头肩底 候选", "large": []}

        eng3 = hm.HsMonitorEngine()
        eng3.on_signal = lambda *a: opened.append("trade")
        eng3.conflict_judge = counting_judge
        eng3.deliver(det)
        # 同一个形态在另一档上再出一次（reason 不同 → 两条告警），探针只该打一次
        sup = hm.Detection(
            symbol="KQ.m@CZCE.PF",
            duration_sec=1800,
            last_dt=det.last_dt,
            kind="头肩顶",
            signal=_FakeSignal(det.signal.detail),
            df=pd.DataFrame(),
            super_strict=True,
        )
        sup.signal.reason = "超严格档同一个形态"
        eng3.deliver(sup)
        ok("同形态只探一次库", calls["n"] == 1)
        ok("降权仍照常开仓", opened == ["trade", "trade"])
        ok("降权不发超严重", len(sent) == 2)

        sent.clear()
        opened.clear()
        eng4 = hm.HsMonitorEngine()
        eng4.on_signal = lambda *a: opened.append("trade")
        eng4.conflict_judge = lambda symbol, tf, detail, bar_dt: {
            "action": "allow",
            "weight": 0.8,
            "conflict": False,
            "reason": "2h 小形态不在大形态区间内",
            "large": [],
        }
        eng4.deliver(det)
        ok("allow 不打冲突标记", sent[0]["conflict"] is None)
        ok("allow 不写前缀", not sent[0]["reason"].startswith("["))
        ok("allow 不拦开仓", opened == ["trade"])
        ok("allow 标签回到原档", hm.alert_tag(det) == "严格告警")

        sent.clear()
        eng5 = hm.HsMonitorEngine()  # 没挂探针：一切照旧
        eng5.deliver(det)
        ok("未挂探针时零变化", len(sent) == 1 and sent[0]["conflict"] is None)
    finally:
        hm.emit_alert = real_emit  # type: ignore[assignment]


def test_store_filter() -> None:
    """全程走临时文件：真实 `data/alerts.json` 一个字节都不碰。"""
    import core.alert_store as store

    tmp = Path(tempfile.mkdtemp(prefix="tf_conflict_")) / "alerts.json"
    real = store._STORE_PATH
    store._STORE_PATH = tmp
    try:
        clear_alerts()
        add_alert(
            symbol="A",
            kind="头肩顶·严格",
            reason="[周期冲突·禁止开仓] x",
            bar_time="2026-11-23T14:00:00",
            extra={"duration": "30分钟", "strict": True, "conflict": True, "tf_conflict": {"action": BLOCK}},
        )
        add_alert(
            symbol="B",
            kind="周期冲突·超严重",
            reason="逆大周期",
            bar_time="2026-11-23T14:00:00",
            extra={"duration": "30分钟", "conflict": True, "tf_conflict": {"action": BLOCK}},
        )
        add_alert(symbol="C", kind="头肩底", reason="无冲突", bar_time="2026-11-23T14:05:00", extra={"duration": "5分钟"})
        ids = [a["symbol"] for a in list_alerts(conflict=True)]
        ok("conflict=1 只出冲突两条", sorted(ids) == ["A", "B"])
        ids = [a["symbol"] for a in list_alerts(conflict=False)]
        ok("conflict=0 只出无冲突", ids == ["C"])
        ok("严格页不受影响（只认 extra.strict）", [a["symbol"] for a in list_alerts(strict=True)] == ["A"])

        # 超严重那条带的是被拦形态的 detail：不能再挂 extra.pattern，
        # 否则同形态的下一条严格档会把它当「让位的常规档」顶掉、冲突卡凭空消失
        detail = _bottom_detail(10, 30)
        hm.emit_alert(
            symbol="K", duration_sec=1800, kind="头肩顶", when=datetime(2026, 11, 23, 14, 0),
            reason="常规档右肩", detail=detail, push=False, toast=False,
        )
        hm.emit_alert(
            symbol="K", duration_sec=1800, kind=hm.CONFLICT_KIND, when=datetime(2026, 11, 23, 14, 0),
            reason="逆大周期", detail=detail, conflict={"action": BLOCK}, conflict_severe=True,
            push=False, toast=False,
        )
        hm.emit_alert(
            symbol="K", duration_sec=1800, kind="头肩顶", when=datetime(2026, 11, 23, 14, 0),
            reason="同形态的严格档", detail=detail, strict=True, push=False, toast=False,
        )
        items = list_alerts(limit=50, include_superseded=True)
        by_kind = {a["kind"]: a for a in items if a["symbol"] == "K"}
        ok("同形态常规档被顶掉", by_kind["头肩顶"].get("superseded") is True)
        ok("超严重卡不被顶掉", not by_kind[hm.CONFLICT_KIND].get("superseded"))
        ok("超严重卡不带 extra.pattern", "pattern" not in (by_kind[hm.CONFLICT_KIND]["extra"] or {}))
        ok("超严重卡仍进冲突页", "K" in [a["symbol"] for a in list_alerts(conflict=True)])
    finally:
        store._STORE_PATH = real
        shutil.rmtree(tmp.parent, ignore_errors=True)


if __name__ == "__main__":
    test_spec_branches()
    test_worst_of_many()
    test_probe_and_advance()
    test_small_signal_of()
    test_deliver_path()
    test_store_filter()
    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项: {FAILS}"))
    sys.exit(1 if FAILS else 0)
