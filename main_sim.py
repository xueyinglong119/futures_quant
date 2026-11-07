"""模拟交易入口"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.simulation import run_simulation


def main() -> None:
    run_simulation()


if __name__ == "__main__":
    main()
