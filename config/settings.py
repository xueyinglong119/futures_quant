"""期货量化交易系统 - 配置模块"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


@dataclass
class MysqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


@dataclass
class AuthConfig:
    user: str
    password: str


@dataclass
class BacktestConfig:
    start: date = date(2026, 1, 1)
    end: date = date(2026, 6, 13)
    kline_seconds: int = 60 * 30  # 30 分钟 K 线
    session_aligned: bool = True  # 按国内小节休市规则合成 30 分钟 K 线


@dataclass
class HeadShouldersConfig:
    pivot_order: int = 4              # 拐点左右各 N 根 K 线（略增以减少假拐点）
    shoulder_tolerance: float = 0.03  # 左右肩对称容忍 3%
    min_span_bars: int = 10           # 形态最短 10 根 K 线（5 小时）
    max_span_bars: int = 60           # 形态最长 60 根 K 线
    max_right_above_left_ratio: float = 0.015  # 右肩最多高于左肩 1.5%（相对头部）
    min_head_neck_ratio: float = 0.008         # 头-颈高度至少 0.8% 才交易
    stop_beyond_head_ratio: float = 0.005      # 止损：突破头部再 0.5%
    take_profit_at_neck: bool = True           # 止盈：触及颈线平仓
    neck_break_ratio: float = 0.001   # 保留，未用于开仓


@dataclass
class StrategyConfig:
    name: str = "head_shoulders"      # ma_crossover | head_shoulders
    short_period: int = 5
    long_period: int = 20
    max_volume: int = 1
    head_shoulders: HeadShouldersConfig = field(default_factory=HeadShouldersConfig)


@dataclass
class RiskConfig:
    enable_force_close: bool = False  # 是否启用日盘/夜盘结束前强制平仓
    force_close_day_time: str = "14:55"
    force_close_night_time: str = "22:55"


@dataclass
class Settings:
    auth: AuthConfig
    mysql: MysqlConfig | None = None
    symbols: list[str] = field(default_factory=list)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    reports_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "reports")
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")


# 各品种默认手数（郑商所甲醇最小开仓 8 手）
SYMBOL_MAX_VOLUME: dict[str, int] = {
    "KQ.m@SHFE.fu": 1,
    "KQ.m@INE.lu": 1,
    "KQ.m@CZCE.MA": 8,
}


def get_max_volume(main_symbol: str, default: int = 1) -> int:
    return SYMBOL_MAX_VOLUME.get(main_symbol, default)


def load_mysql_config() -> MysqlConfig:
    host = os.getenv("MYSQL_HOST", "").strip()
    user = os.getenv("MYSQL_USER", "").strip()
    password = os.getenv("MYSQL_PASSWORD", "").strip()
    database = os.getenv("MYSQL_DATABASE", "future_ai").strip()
    port = int(os.getenv("MYSQL_PORT", "3306"))
    if not all([host, user, password, database]):
        raise ValueError(
            "请在 .env 中配置 MYSQL_HOST / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE"
        )
    return MysqlConfig(
        host=host, port=port, user=user, password=password, database=database
    )


def load_settings() -> Settings:
    user = os.getenv("TQ_USER", "").strip()
    password = os.getenv("TQ_PASS", "").strip()
    if not user or not password:
        raise ValueError(
            "请在 .env 中配置 TQ_USER 和 TQ_PASS，可参考 .env.example"
        )

    mysql: MysqlConfig | None = None
    try:
        mysql = load_mysql_config()
    except ValueError:
        pass

    return Settings(
        auth=AuthConfig(user=user, password=password),
        mysql=mysql,
        symbols=[
            "KQ.m@SHFE.fu",   # 燃料油主连
            "KQ.m@INE.lu",    # 低硫燃油主连
            "KQ.m@CZCE.MA",   # 甲醇主连
        ],
    )
