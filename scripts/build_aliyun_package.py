"""打包阿里云部署 zip（排除 .env / 缓存 / 本地报告）"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    ".idea",
    ".vscode",
    "mobile",
    "node_modules",
    "android",
    "ios",
}
SKIP_FILE_NAMES = {".env", "credentials.json"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".apk", ".keystore"}


def should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = set(rel.parts)
    if parts & SKIP_DIR_NAMES:
        return True
    if path.name in SKIP_FILE_NAMES:
        return True
    if path.suffix in SKIP_SUFFIXES:
        return True
    # 本地回测报告不打进包，保留目录占位
    if len(rel.parts) >= 2 and rel.parts[0] == "reports" and path.is_file():
        if path.name != ".gitkeep":
            return True
    if len(rel.parts) >= 2 and rel.parts[0] == "data" and path.is_file():
        if path.name != ".gitkeep":
            return True
    return False


def main() -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = DIST / f"futures_quant_aliyun_{stamp}.zip"

    files: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if should_skip(p):
            continue
        files.append(p)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(files):
            zf.write(p, arcname=str(p.relative_to(ROOT)).replace("\\", "/"))

    print(f"ZIP={zip_path}")
    print(f"SIZE_MB={zip_path.stat().st_size / 1024 / 1024:.2f}")
    print(f"FILES={len(files)}")


if __name__ == "__main__":
    main()
