# 让 pytest 能找到 core 包
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))