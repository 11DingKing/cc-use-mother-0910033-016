"""启动月度活动统计对账后端服务。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from monthly_recon.api import main

if __name__ == "__main__":
    main()
