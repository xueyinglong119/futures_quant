"""周期冲突影响回放：小周期信号有多少正压在大周期的反向形态上，拦掉的那批盈亏如何。

生产侧 `HS_TF_CONFLICT=1` 会把判到 block 的信号**拦住模拟盘开仓**（动 11-22 定稿的
「严格/超严格才开仓」口径），所以开之前先在这里量清楚：

- 每个小周期档位有多少条信号被判 allow / downgrade / block；
- block 分别来自哪个大周期（判断「所有更大周期」这条要不要收窄）；
- 被拦掉的这批按现口径（止盈 0.8×swing、止损 0.2×swing、平衡胜率 20%）本会是什么结果
  —— 拦对了是省亏损，拦错了是少赚，两个数都要看见才谈得上裁决。

判定用的就是生产那份 `probe_large_patterns` / `judge_many`，把大周期历史截到信号那根
再喂进去，所以不存在「回测含未来、实盘看不到」的口径差。
"""

from __future__ import annotations

import argparse
import sys
from bisect import bisect_right
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_killed_samples import replay_detail  # noqa: E402
from core.hs_monitor import (  # noqa: E402
    HS_MONITOR_DURATIONS,
    LARGER_MAP,
    TF_CONFIGS,
    bars_to_strategy_df,
)
from core.kline_store import (  # noqa: E402
    DURATION_LABELS,
    TABLE_NAME,
    connect,
    load_bars,
)
from core.tf_conflict import (  # noqa: E402
    BLOCK,
    PROBE_BARS,
    judge_many,
    probe_large_patterns,
    small_signal_of,
)
from replay_winrate import simulate_outcomes  # noqa: E402

TIERS = ("常规档", "严格档")
BALANCE = 0.2 / (0.8 + 0.2)


def _min_bars(tf: int) -> int:
    cfg = TF_CONFIGS[tf]
    return cfg.max_span_bars + cfg.pivot_order * 2 + 5


def _bar_times(df: pd.DataFrame) -> list[datetime]:
    out = []
    for val in df["datetime"]:
        dt = pd.Timestamp(val).to_pydatetime()
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        out.append(dt.replace(microsecond=0))
    return out


def _asof_index(times: list[datetime], when: datetime) -> int:
    """大周期上「最后一根时刻 ≤ 小信号时刻」的下标。

    对齐用库内标签（raw 周期是起始时刻、会话周期是结束时刻），与生产探针同一口径。
    """
    return bisect_right(times, when) - 1


class AsOfJudge:
    """同一次回放里按 (大周期, 截断下标) 缓存探针结果：小信号挤在同几根大 K 上时省得多。"""

    def __init__(self, symbol: str, dfs: dict[int, pd.DataFrame], *, probe_bars: int) -> None:
        self.symbol = symbol
        self.dfs = dfs
        self.times = {tf: _bar_times(df) for tf, df in dfs.items()}
        self.probe_bars = probe_bars
        self._memo: dict[tuple[int, int], list] = {}
        self.probes = 0

    def larges(self, tf: int, when: datetime) -> list:
        out: list = []
        for big in LARGER_MAP.get(tf, ()):
            dfb = self.dfs.get(big)
            if dfb is None:
                continue
            j = _asof_index(self.times[big], when)
            if j < _min_bars(big) - 1:
                continue
            key = (big, j)
            if key not in self._memo:
                self.probes += 1
                self._memo[key] = probe_large_patterns(
                    self.symbol, big, dfb.iloc[: j + 1], probe_bars=self.probe_bars
                )
            out.extend(self._memo[key])
        return out

    def __call__(self, tf: int, detail: dict, when: datetime) -> dict | None:
        larges = self.larges(tf, when)
        if not larges:
            return None
        return judge_many(small_signal_of(detail, when), larges)


def _outcome_sum(rows: list[dict]) -> str:
    decided = [r for r in rows if r["result"] in ("win", "loss")]
    if not decided:
        return "已决 0"
    w = sum(1 for r in decided if r["result"] == "win")
    exp = (w * 0.8 - (len(decided) - w) * 0.2) / len(decided)
    return f"已决 {len(decided)} 胜率 {w / len(decided):.1%} 期望 {exp:+.3f}×swing"


def _job(job: tuple[str, int, int, list[int]]) -> tuple[str, list[dict], list[str]]:
    symbol, days, probe_bars, tfs = job
    # 大周期不论问没问都要取：30m 的信号要靠 1h/2h/1d 压不压着来判
    needed = set(tfs)
    for small in tfs:
        needed.update(LARGER_MAP.get(small, ()))
    conn = connect()
    bars: dict[int, list[dict]] = {}
    try:
        for tf in sorted(needed):
            rows = load_bars(conn, symbol, tf, start=datetime.now() - timedelta(days=days))
            if len(rows) >= _min_bars(tf):
                bars[tf] = rows
    finally:
        conn.close()
    dfs = {tf: bars_to_strategy_df(rows) for tf, rows in bars.items()}
    judge = AsOfJudge(symbol, dfs, probe_bars=probe_bars)
    out: list[dict] = []
    for tf in tfs:
        rows = bars.get(tf)
        df = dfs.get(tf)
        if rows is None or df is None:
            continue
        for tier, strict_tier in zip(TIERS, (False, True)):
            for s in replay_detail(symbol, tf, rows, True, strict_tier=strict_tier):
                when = datetime.fromisoformat(s["when"])
                verdict = judge(tf, s["detail"], when)
                res = simulate_outcomes(symbol, tf, df, [s])
                out.append(
                    {
                        "tf": tf,
                        "tier": tier,
                        "when": s["when"],
                        "action": verdict["action"] if verdict else "none",
                        "weight": verdict["weight"] if verdict else 1.0,
                        "confirmed_larges": (
                            sorted({int(lg["duration_sec"]) for lg in verdict["large"] if lg["status"] == "confirmed"})
                            if verdict
                            else []
                        ),
                        "reason": verdict["reason"] if verdict else "",
                        "result": res[0]["result"] if res else "none",
                    }
                )
    missing = [DURATION_LABELS[tf] for tf in tfs if tf not in bars]
    return symbol, out, missing


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=str, default="", help="逗号分隔品种，缺省全主力")
    ap.add_argument("--tfs", type=str, default="", help="逗号分隔周期秒（小周期），缺省全部监控周期")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--probe-bars", type=int, default=PROBE_BARS)
    args = ap.parse_args()

    tfs = (
        [int(x) for x in args.tfs.split(",") if x.strip()]
        if args.tfs
        else list(HS_MONITOR_DURATIONS)
    )
    cutoff = datetime.now() - timedelta(days=args.days)
    conn = connect()
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT symbol FROM {TABLE_NAME} "
                "WHERE duration_sec=60 AND datetime_end>=%s AND symbol LIKE 'KQ.m@%%'",
                (cutoff,),
            )
            symbols = [r[0] for r in cur.fetchall()]
    conn.close()
    print(
        f"品种 {len(symbols)}，回溯 {args.days} 天，小周期 {tfs}，"
        f"大周期取每个信号之上的全部更大周期（平衡胜率 {BALANCE:.1%}）",
        flush=True,
    )

    jobs = [(s, args.days, args.probe_bars, tfs) for s in symbols]
    rows: list[dict] = []
    short: list[tuple[str, list[str]]] = []
    done = 0
    t_start = datetime.now()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for symbol, chunk, missing in ex.map(_job, jobs, chunksize=1):
            done += 1
            if done % 10 == 0:
                el = (datetime.now() - t_start).total_seconds()
                rate = el / max(done, 1)
                left = rate * (len(jobs) - done) / max(args.workers, 1)
                print(
                    f"进度 {done}/{len(jobs)} 已用 {el:.0f}s 均 {rate:.1f}s/品种"
                    f" 预估还剩 {left / 60:.1f} 分钟",
                    flush=True,
                )
            for r in chunk:
                r["symbol"] = symbol
            rows.extend(chunk)
            if missing:
                short.append((symbol, missing))

    print("\n== 逐周期 × 档位 ==", flush=True)
    for tf in tfs:
        label = DURATION_LABELS.get(tf, str(tf))
        for tier in TIERS:
            rs = [r for r in rows if r["tf"] == tf and r["tier"] == tier]
            if not rs:
                print(f"  {label} {tier}: 无信号", flush=True)
                continue
            none = sum(1 for r in rs if r["action"] == "none")
            allow = sum(1 for r in rs if r["action"] == "allow")
            down = sum(1 for r in rs if r["action"] == "downgrade")
            blocked = [r for r in rs if r["action"] == BLOCK]
            src: dict[int, int] = {}
            for r in blocked:
                for lg in r["confirmed_larges"]:
                    src[lg] = src.get(lg, 0) + 1
            from_txt = " / ".join(
                f"{DURATION_LABELS.get(k, k)} {v}" for k, v in sorted(src.items(), reverse=True)
            )
            print(
                f"  {label} {tier}: 信号 {len(rs)} 无大形态 {none} 放行 {allow} 降权 {down}"
                f" 禁止 {len(blocked)}"
                + (f"（按已确认的大周期计：{from_txt}）" if from_txt else ""),
                flush=True,
            )
            if blocked:
                print(f"      拦掉那批: {_outcome_sum(blocked)}", flush=True)
                print(
                    f"      放行那批: {_outcome_sum([r for r in rs if r['action'] != BLOCK])}",
                    flush=True,
                )

    print("\n== 合计 ==", flush=True)
    for tier in TIERS:
        rs = [r for r in rows if r["tier"] == tier]
        if not rs:
            print(f"  {tier}: 无信号", flush=True)
            continue
        blocked = [r for r in rs if r["action"] == BLOCK]
        down = [r for r in rs if r["action"] == "downgrade"]
        print(
            f"  {tier}: 信号 {len(rs)} 降权 {len(down)}（{len(down) / len(rs):.1%}）"
            f" 禁止 {len(blocked)}（{len(blocked) / len(rs):.1%}）",
            flush=True,
        )
        if blocked:
            print(
                f"      拦掉: {_outcome_sum(blocked)} ｜ 放行: {_outcome_sum([r for r in rs if r['action'] != BLOCK])}",
                flush=True,
            )

    hits = [r for r in rows if r["action"] == BLOCK]
    print(
        f"\n样例（最近 15 条禁止，共 {len(hits)} 条）：",
        flush=True,
    )
    for r in hits[-15:]:
        print(
            f"  {r['symbol']} {DURATION_LABELS.get(r['tf'], r['tf'])} {r['tier']} {r['when']}"
            f" → {r['reason']}",
            flush=True,
        )

    if short:
        print(
            f"\n根数不足被整片跳过的周期：{len(short)} 个品种，如 {short[0][0]} 缺"
            f" {','.join(short[0][1])}（长周期要先把加载窗拉长）",
            flush=True,
        )


if __name__ == "__main__":
    main()
