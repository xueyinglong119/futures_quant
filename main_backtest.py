"""回测入口"""

import sys
from pathlib import Path

# 确保项目根目录在 Python 路径中
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.backtest import BacktestEngine


def main() -> None:
    engine = BacktestEngine()
    engine.run()


if __name__ == "__main__":
    main()
