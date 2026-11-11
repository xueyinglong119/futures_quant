"""把 _hits/_hits2 的命中图拼成一个本地画廊页。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

meta = []
for d in ("_hits", "_hits2"):
    for p in sorted((ROOT / d).glob("meta_*.json")):
        for item in json.loads(p.read_text(encoding="utf-8")):
            item["dir"] = d
            meta.append(item)

cards = "\n".join(
    f'<figure><img src="{"" if m["dir"] == "_hits" else "../" + m["dir"] + "/"}{m["file"]}" loading="lazy">'
    f'<figcaption>{m["symbol"]} · {m["when"]}<br>{m["reason"]}</figcaption></figure>'
    for m in meta
)
html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>改后命中形态画廊（{len(meta)} 张）</title>
<style>
body {{ margin:0; background:#0B1620; color:#D7E0E8; font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
h1 {{ padding:16px 24px 4px; font-size:20px; }}
p.sub {{ padding:0 24px 12px; color:#9BB0C1; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(430px,1fr)); gap:14px; padding:0 24px 40px; }}
figure {{ margin:0; background:#12202C; border:1px solid #24384A; border-radius:10px; padding:10px; }}
img {{ width:100%; border-radius:6px; background:#0E1A24; display:block; }}
figcaption {{ color:#9BB0C1; margin-top:8px; white-space:pre-wrap; }}
</style></head><body>
<h1>改后命中形态画廊（{len(meta)} 张）</h1>
<p class="sub">10 品种 × 15 天 × 1m · 当前标定（肩价差1.5% + 颈线回撤1.5× + ≤15m 跨日间隙130min）· 逐张目视评信噪比</p>
<div class="grid">{cards}</div>
</body></html>"""

out = ROOT / "_hits" / "gallery.html"
out.write_text(html, encoding="utf-8")
print(out, len(meta))
