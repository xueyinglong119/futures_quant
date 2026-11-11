"""把命中图目录拼成画廊页（--embed 生成 base64 单文件，可拷走离线打开）。"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def build_gallery(dirs: list[Path], embed: bool, out: Path, subtitle: str = "") -> int:
    meta = []
    for d in dirs:
        for p in sorted(d.glob("meta_*.json")):
            for item in json.loads(p.read_text(encoding="utf-8")):
                item["dir"] = str(d)
                meta.append(item)

    def _src(m: dict) -> str:
        path = Path(m["dir"]) / m["file"]
        if embed:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        if Path(m["dir"]) == dirs[0]:
            return m["file"]
        return f'../{Path(m["dir"]).name}/{m["file"]}'

    cards = "\n".join(
        f'<figure><img src="{_src(m)}" loading="lazy">'
        f'<figcaption>{m["symbol"]} · {m["when"]}<br>{m["reason"]}</figcaption></figure>'
        for m in meta
    )
    sub = f'<p class="sub">{subtitle}</p>' if subtitle else ""
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>形态画廊（{len(meta)} 张）</title>
<style>
body {{ margin:0; background:#0B1620; color:#D7E0E8; font:13px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
h1 {{ padding:16px 24px 4px; font-size:20px; }}
p.sub {{ padding:0 24px 12px; color:#9BB0C1; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(430px,1fr)); gap:14px; padding:0 24px 40px; }}
figure {{ margin:0; background:#12202C; border:1px solid #24384A; border-radius:10px; padding:10px; }}
img {{ width:100%; border-radius:6px; background:#0E1A24; display:block; }}
figcaption {{ color:#9BB0C1; margin-top:8px; white-space:pre-wrap; }}
</style></head><body>
<h1>形态画廊（{len(meta)} 张）· {" + ".join(d.name for d in dirs)}</h1>
{sub}
<div class="grid">{cards}</div>
</body></html>"""
    out.write_text(html, encoding="utf-8")
    return len(meta)


def main() -> None:
    embed = "--embed" in sys.argv
    dirs = [ROOT / a for a in sys.argv[1:] if not a.startswith("--")] or [
        ROOT / "_hits",
        ROOT / "_hits2",
    ]
    out_name = "gallery_single.html" if embed else "gallery.html"
    n = build_gallery(dirs, embed, dirs[0] / out_name)
    print(dirs[0] / out_name, n)


if __name__ == "__main__":
    main()