"""常用盯盘品种（来自用户自选列表）"""

from __future__ import annotations

# 常用主连：拉长历史 / 换月记录
WATCH_MAIN_SYMBOLS: list[str] = [
    "KQ.m@SHFE.ru",
    "KQ.m@INE.nr",
    "KQ.m@DCE.eb",
    "KQ.m@DCE.pp",
    "KQ.m@SHFE.br",
    "KQ.m@SHFE.sn",
    "KQ.m@SHFE.ni",
    "KQ.m@SHFE.zn",
    "KQ.m@INE.lu",
    "KQ.m@SHFE.au",
    "KQ.m@DCE.pg",
    "KQ.m@CZCE.TA",
    "KQ.m@CZCE.MA",
    "KQ.m@DCE.v",
    "KQ.m@DCE.a",
    "KQ.m@CZCE.RM",
    "KQ.m@CZCE.OI",
    "KQ.m@DCE.m",
    "KQ.m@DCE.y",
    "KQ.m@DCE.p",
    "KQ.m@CZCE.UR",
    "KQ.m@CZCE.AP",
    "KQ.m@CZCE.CF",
    "KQ.m@CZCE.SR",
    "KQ.m@DCE.c",
    "KQ.m@INE.ec",
    "KQ.m@SHFE.fu",
    "KQ.m@DCE.eg",
    "KQ.m@SHFE.hc",
    "KQ.m@SHFE.rb",
    "KQ.m@DCE.i",
    "KQ.m@DCE.j",
    "KQ.m@DCE.jm",
    "KQ.m@CZCE.FG",
    "KQ.m@GFEX.lc",
    "KQ.m@GFEX.si",
    "KQ.m@CZCE.SA",
    "KQ.m@DCE.l",
    "KQ.m@SHFE.cu",
    "KQ.m@SHFE.ag",
    "KQ.m@SHFE.pb",
]

# 当前近月实盘合约（截图中的具体月份）
WATCH_NEAR_CONTRACTS: list[str] = [
    "SHFE.ru2701",
    "INE.nr2701",
    "DCE.eb2612",
    "DCE.pp2701",
    "SHFE.br2612",
    "SHFE.sn2612",
    "SHFE.ni2612",
    "SHFE.zn2612",
    "INE.lu2701",
    "SHFE.au2612",
    "DCE.pg2612",
    "CZCE.TA701",
    "CZCE.MA701",
    "DCE.v2701",
    "DCE.a2701",
    "CZCE.RM701",
    "CZCE.OI701",
    "DCE.m2701",
    "DCE.y2701",
    "DCE.p2701",
    "CZCE.UR701",
    "CZCE.AP701",
    "CZCE.CF701",
    "CZCE.SR701",
    "DCE.c2701",
    "INE.ec2612",
    "SHFE.fu2701",
    "DCE.eg2701",
    "SHFE.hc2701",
    "SHFE.rb2701",
    "DCE.i2701",
    "DCE.j2701",
    "DCE.jm2701",
    "CZCE.FG701",
    "GFEX.lc2701",
    "GFEX.si2612",
    "CZCE.SA701",
    "DCE.l2701",
    "SHFE.cu2612",
    "SHFE.ag2702",
    "SHFE.pb2612",
]

# Tick 研究（仅燃油 + 乙二醇）
TICK_RESEARCH_CONTRACTS: list[str] = [
    "SHFE.fu2701",
    "DCE.eg2701",
]

# 主连加长历史年数
WATCH_LONG_YEARS = 5

# 近月合约 1m 回溯月数
NEAR_MONTHS = 6

# Tick 回溯自然日（体积大，默认 10 天够研究）
TICK_LOOKBACK_DAYS = 10
