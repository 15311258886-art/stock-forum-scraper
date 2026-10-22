# -*- coding: utf-8 -*-
"""pytest 根配置：把仓库根加入 sys.path，使 `import viewer.server` 在测试中可用。

仅做路径注入，不含任何 fixture 副作用（避免影响 import 副作用测试 P0-05）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
