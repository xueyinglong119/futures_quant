"""根据配置创建策略实例"""

from __future__ import annotations

from config.settings import Settings
from strategies.head_shoulders import HeadShouldersStrategy
from strategies.ma_crossover import MACrossoverStrategy


def create_strategy(settings: Settings):
    name = settings.strategy.name
    cfg = settings.strategy

    if name == "head_shoulders":
        hs = cfg.head_shoulders
        return HeadShouldersStrategy(
            pivot_order=hs.pivot_order,
            shoulder_tolerance=hs.shoulder_tolerance,
            min_span_bars=hs.min_span_bars,
            max_span_bars=hs.max_span_bars,
            neck_break_ratio=hs.neck_break_ratio,
            max_right_above_left_ratio=hs.max_right_above_left_ratio,
            min_head_neck_ratio=hs.min_head_neck_ratio,
            stop_beyond_head_ratio=hs.stop_beyond_head_ratio,
            take_profit_at_neck=hs.take_profit_at_neck,
            max_volume=cfg.max_volume,
        )

    if name == "ma_crossover":
        return MACrossoverStrategy(
            short_period=cfg.short_period,
            long_period=cfg.long_period,
            max_volume=cfg.max_volume,
        )

    raise ValueError(f"未知策略: {name}，可选 ma_crossover / head_shoulders")
