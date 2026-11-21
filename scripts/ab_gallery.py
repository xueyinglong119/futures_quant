"""把全量画廊拆成「A/B 命中」与「被过滤」两段，输出一个自包含页面。

`--all` 是全量（改前/无闸）画廊，`--subset` 是 A/B 后的命中画廊；两张页面都由
`build_hits_gallery.build_gallery` 生成，按 figcaption 第一行（品种 · 时刻）配对。
图片是 base64 自包含的，输出页可以直接放进 `reports/backtest_*/gallery_single.html`
由现成路由打开。

用在生产（脚本不在镜像里，用 bind mount 挂进去，不必重建）：
  docker compose run --rm -T -v /opt/futures_quant/scripts:/app/scripts:ro \
    --entrypoint python futures-api /app/scripts/ab_gallery.py \
    --all /app/reports/triple_300d_2h1d_gallery.html \
    --subset /app/reports/triple_300d_2h1d_hits4.html \
    --out /app/reports/backtest_triple/gallery_single.html
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

FIG_RE = re.compile(r"<figure>.*?</figure>", re.S)
CAP_RE = re.compile(r"<figcaption>(.*?)<br>", re.S)


def load(path: Path) -> tuple[list[tuple[str, str]], str]:
    html = path.read_text(encoding="utf-8")
    style = re.search(r"<style>.*?</style>", html, re.S)
    keyed: list[tuple[str, str]] = []
    for fig in FIG_RE.findall(html):
        m = CAP_RE.search(fig)
        key = re.sub(r"\s+", " ", m.group(1)).strip() if m else fig[:120]
        keyed.append((key, fig))
    return keyed, style.group(0) if style else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", required=True)
    ap.add_argument("--subset", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="形态 A/B：命中与被过滤")
    args = ap.parse_args()

    all_cards, style = load(Path(args.all))
    sub_cards, _ = load(Path(args.subset))
    sub_keys = {k for k, _ in sub_cards}
    all_keys = {k for k, _ in all_cards}
    hits = [(k, f) for k, f in all_cards if k in sub_keys]
    filtered = [(k, f) for k, f in all_cards if k not in sub_keys]
    missing = sub_keys - all_keys

    style = style.replace(
        "</style>", "h2 { padding:8px 24px 0; font-size:16px; }\n</style>"
    )
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{args.title}</title>
{style}
</head><body>
<h1>{args.title}</h1>
<p class="sub">全量 {len(all_cards)} 张 · 命中 {len(hits)} 张 · 被过滤 {len(filtered)} 张</p>
<h2>命中（A/B 放行）</h2>
<div class="grid">{chr(10).join(f for _, f in hits)}</div>
<h2>被过滤（A/B 拒绝）</h2>
<div class="grid">{chr(10).join(f for _, f in filtered)}</div>
</body></html>"""
    Path(args.out).write_text(html, encoding="utf-8")
    print(
        f"all={len(all_cards)} hits={len(hits)} filtered={len(filtered)} "
        f"missing={len(missing)} -> {args.out}"
    )
    for k in sorted(missing)[:5]:
        print("  MISS:", k)


if __name__ == "__main__":
    main()