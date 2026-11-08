from __future__ import annotations

from pathlib import Path

from PIL import Image

SRC = Path(
    r"C:\Users\Administrator\.cursor\projects\d-IdeaProjects-futures-quant\assets"
    r"\___AI______-78752876-d591-418a-a45a-25b04f1797e9.jpg"
)
ASSETS = Path(__file__).resolve().parent.parent / "assets"
ANDROID_RES = Path(__file__).resolve().parent.parent / "android" / "app" / "src" / "main" / "res"


def is_bg(px, x: int, y: int) -> bool:
    r, g, b, a = px[x, y]
    if a < 10:
        return True
    # white / near-white canvas
    if r > 245 and g > 245 and b > 245:
        return True
    # faint gray watermark text
    if abs(r - g) < 8 and abs(g - b) < 8 and r > 160:
        return True
    return False


def content_bbox(im: Image.Image) -> tuple[int, int, int, int]:
    px = im.load()
    w, h = im.size
    minx, miny, maxx, maxy = w, h, 0, 0
    found = False
    for y in range(h):
        for x in range(w):
            if not is_bg(px, x, y):
                found = True
                minx = min(minx, x)
                miny = min(miny, y)
                maxx = max(maxx, x)
                maxy = max(maxy, y)
    if not found:
        raise SystemExit("no content found in source image")
    return minx, miny, maxx + 1, maxy + 1


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    im = Image.open(SRC).convert("RGBA")
    print("orig", im.size)
    box = content_bbox(im)
    print("bbox", box)
    crop = im.crop(box)
    cw, ch = crop.size
    side = max(cw, ch)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(crop, ((side - cw) // 2, (side - ch) // 2))

    # Final store icons
    icon = sq.resize((1024, 1024), Image.Resampling.LANCZOS)
    # Flatten onto red so adaptive icon never shows white corners
    flat = Image.new("RGB", (1024, 1024), (180, 28, 34))
    flat.paste(icon.convert("RGB"), (0, 0), icon)

    flat.save(ASSETS / "icon.png", "PNG", optimize=True)
    flat.save(ASSETS / "adaptive-icon.png", "PNG", optimize=True)

    splash = Image.new("RGB", (1284, 2778), (11, 31, 51))
    icon_s = flat.resize((720, 720), Image.Resampling.LANCZOS)
    splash.paste(icon_s, ((1284 - 720) // 2, (2778 - 720) // 2))
    splash.save(ASSETS / "splash.png", "PNG", optimize=True)

    # Update Android mipmaps used by local Gradle builds
    sizes = {
        "mipmap-mdpi": 48,
        "mipmap-hdpi": 72,
        "mipmap-xhdpi": 96,
        "mipmap-xxhdpi": 144,
        "mipmap-xxxhdpi": 192,
    }
    for folder, size in sizes.items():
        out_dir = ANDROID_RES / folder
        if not out_dir.exists():
            continue
        resized = flat.resize((size, size), Image.Resampling.LANCZOS)
        for stem in ("ic_launcher", "ic_launcher_round", "ic_launcher_foreground"):
            webp = out_dir / f"{stem}.webp"
            png = out_dir / f"{stem}.png"
            # Prefer webp (existing Expo output); avoid png+webp duplicate resource names
            if png.exists():
                png.unlink()
            resized.save(webp, "WEBP", quality=92)

    print("saved icons to", ASSETS)
    print("icon size", (ASSETS / "icon.png").stat().st_size)


if __name__ == "__main__":
    main()
