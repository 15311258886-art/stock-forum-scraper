# -*- coding: utf-8 -*-
"""验证 desktop_app 加固后的启动流程——**不真的开窗**（把 create_window/start 换成桩）。

检查点：
  1. 复用已有 8765（不重复起后端）；
  2. 在 import webview 前已注入 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS；
  3. webview.start() 收到 private_mode=False + storage_path（固定用户数据目录）；
  4. 不弹 WebView2 崩溃框（由 1/2 保证）。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.argv = ["desktop_app.py", "--allow-multi"]      # 跳过单实例互斥，便于测试

import webview                                       # noqa: E402
import viewer.desktop_app as d                       # noqa: E402

REPORT = ROOT / "tools" / "_desktop_app_report.json"
captured = {}


def fake_create_window(*a, **k):
    captured["create_window_args"] = [x for x in a]
    captured["create_window_kwargs"] = k
    return object()


def fake_start(*a, **k):
    captured["start_kwargs"] = k
    captured["env_arg"] = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")
    captured["storage"] = d.WEBVIEW_STORAGE
    captured["storage_exists"] = os.path.isdir(d.WEBVIEW_STORAGE)
    captured["port_in_use"] = d._port_in_use()
    # main() 末尾会 os._exit(0)，所以在这里落盘（而非返回后打印）
    REPORT.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")


webview.create_window = fake_create_window
webview.start = fake_start
d.main()
