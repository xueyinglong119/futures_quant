"""持仓主力换月逻辑自检（不连天勤/库）。"""

from __future__ import annotations

from datetime import datetime

from core.cont_symbols import drop_illiquid, illiquid_product_token
from core.dominant import DominantRow, apply_roll, cont_to_product_key, product_key_from_symbol


def _row(symbol: str, challenger: str | None = None, lead: int = 0) -> DominantRow:
    return DominantRow(
        product_key="SHFE.ag",
        symbol=symbol,
        challenger=challenger,
        lead_days=lead,
        open_interest=100.0,
        volume=10.0,
        updated_at=datetime.now(),
    )


# 1) 键解析
assert cont_to_product_key("KQ.m@SHFE.ag") == "SHFE.ag"
assert product_key_from_symbol("SHFE.ag2702") == "SHFE.ag"
assert product_key_from_symbol("CZCE.MA605") == "CZCE.MA"
assert product_key_from_symbol("KQ.m@DCE.i") == "DCE.i"
print("[1] product_key 解析 OK")

# 2) 换月确认：首次建档直接切；同月保持；挑战需连续 2 次
sym, ch, lead, sw = apply_roll(None, "SHFE.ag2702")
assert (sym, ch, lead, sw) == ("SHFE.ag2702", None, 0, True)

sym, ch, lead, sw = apply_roll(_row("SHFE.ag2702"), "SHFE.ag2702")
assert (sym, ch, lead, sw) == ("SHFE.ag2702", None, 0, False)

sym, ch, lead, sw = apply_roll(_row("SHFE.ag2702"), "SHFE.ag2706")
assert (sym, ch, lead, sw) == ("SHFE.ag2702", "SHFE.ag2706", 1, False)

sym, ch, lead, sw = apply_roll(
    _row("SHFE.ag2702", challenger="SHFE.ag2706", lead=1), "SHFE.ag2706"
)
assert (sym, ch, lead, sw) == ("SHFE.ag2706", None, 0, True)

# 挑战者换人，计数归 1
sym, ch, lead, sw = apply_roll(
    _row("SHFE.ag2702", challenger="SHFE.ag2706", lead=1), "SHFE.ag2704"
)
assert (sym, ch, lead, sw) == ("SHFE.ag2702", "SHFE.ag2704", 1, False)
print("[2] apply_roll 换月确认 OK")

# 3) 月约也能剔低流动性
assert illiquid_product_token("SHFE.wr2510") == "wr"
assert illiquid_product_token("CZCE.RS605") == "RS"
assert drop_illiquid(["SHFE.ag2702", "SHFE.wr2510", "DCE.rr2601"]) == ["SHFE.ag2702"]
print("[3] 月约 drop_illiquid OK")

print("OK 全部通过")
