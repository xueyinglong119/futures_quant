"""策略基类与信号定义"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Signal:
    target_volume: int
    action: str
    reason: str
    detail: dict | None = None
